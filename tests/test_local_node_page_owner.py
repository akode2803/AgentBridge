"""Authenticated lifecycle composition for the inactive page runtime."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from agentbridge.gui.context import GuiApp
from agentbridge.mesh import local_input_runtime as local_input_runtime_module
from agentbridge.mesh.node_page_owner import LocalNodePageOwner
from agentbridge.mesh.node_page_runtime import LocalNodePageRuntime
from agentbridge.mesh.service import Mesh
from agentbridge.node.server import LocalNodeServer
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_selectors import Selector
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.local_mutations import (
    LocalMutationTransport, root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient


EPOCH = "12345678-1234-5678-9234-567812345678"
ACCOUNT = "12345678-1234-5678-9234-567812345679"
OTHER_ACCOUNT = "12345678-1234-5678-9234-567812345680"


def _world(tmp_path):
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_source_ledger_version": 2,
        "_scoped_source_version": 1,
        "_recovery_account": ACCOUNT,
        "ab_change_epochs": [{
            "root": "team", "epoch": EPOCH, "minimum_cursor": 0,
            "source_schema_version": 2,
        }],
        "ab_change_events": [],
        "ab_docs": [],
        "ab_logs": [],
    })
    provider = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://Example.TEST:443/",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    coordinator = MutationCoordinator(
        tmp_path / "coordinator", root_identity(provider),
    )
    transport = LocalMutationTransport(provider, coordinator)
    store = Store(tmp_path / "store.sqlite")
    return client, transport, store


def _mesh_world(tmp_path):
    client, transport, store = _world(tmp_path)
    path = store.path
    store.close()
    mesh = Mesh(
        transport, 'aryan', 'desktop', home=tmp_path / 'mesh-home',
        store_path=path,
    )
    return client, mesh


def _wait_for(condition, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('condition not met in time')


def test_mesh_activates_exact_node_owner_and_closes_it_after_worker(tmp_path):
    _client, mesh = _mesh_world(tmp_path)
    owner = None
    try:
        assert mesh.activate_local_node()
        owner = mesh._local_node_owner
        assert owner is not None and owner.runtime is not None
        assert mesh.local_node_active
        assert mesh.local_inputs._node_source is owner.runtime
        assert mesh.sync._retired

        mesh.start(heartbeat=False)
        assert mesh.local_inputs._thread is not None
        assert mesh.local_inputs._thread.is_alive()
    finally:
        mesh.close()

    assert owner is not None and owner.server is None and owner.runtime is None
    assert not mesh.local_node_active


def test_mesh_keeps_legacy_writer_when_authenticated_node_open_is_unavailable(tmp_path):
    client, mesh = _mesh_world(tmp_path)
    client.db['_recovery_denied'] = True
    try:
        assert not mesh.activate_local_node()
        assert mesh._local_node_owner is None
        assert mesh.local_inputs._node_source is None
        assert not mesh.sync._retired
    finally:
        mesh.close()


def test_mesh_keeps_legacy_writer_when_node_has_pending_mutation_fence(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    coordinator = mesh.tx._coordinator
    intent = coordinator.begin((Selector('doc_exact', 'probe.json'),))
    monkeypatch.setattr(
        local_input_runtime_module, 'LOCAL_NODE_ACTIVATION_PENDING_WAIT_S', 0,
    )
    try:
        assert not mesh.activate_local_node()
        assert mesh._local_node_owner is None
        assert mesh.local_inputs._node_source is None
        assert not mesh.sync._retired
        assert not list((mesh.home / 'nodes').glob('*/state.json'))
        assert not list((mesh.home / 'nodes').glob('*/token'))
        with coordinator._transaction() as conn:
            assert conn.execute(
                'SELECT token FROM mutation_intents'
            ).fetchall() == [(intent.token,)]
    finally:
        mesh.close()


def test_node_handoff_rechecks_mutation_cut_after_event_owner_binding(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    coordinator = mesh.tx._coordinator
    original = LocalNodePageRuntime.bind_event_owner
    intents = []

    def bind_then_mutate(runtime, *args):
        result = original(runtime, *args)
        intents.append(coordinator.begin((Selector('doc_exact', 'race.json'),)))
        return result

    monkeypatch.setattr(LocalNodePageRuntime, 'bind_event_owner', bind_then_mutate)
    monkeypatch.setattr(
        local_input_runtime_module, 'LOCAL_NODE_ACTIVATION_PENDING_WAIT_S', 0,
    )
    try:
        assert not mesh.activate_local_node()
        assert len(intents) == 1
        assert not mesh.sync._retired
        assert mesh.local_inputs._node_source is None
        assert mesh._local_node_owner is None
    finally:
        mesh.close()


def test_node_handoff_retries_an_exact_transient_mutation_fence(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    coordinator = mesh.tx._coordinator
    intent = coordinator.begin((Selector('doc_exact', 'transient.json'),))
    original_gate = coordinator.node_activation_gate
    attempts = []

    @contextmanager
    def settle_after_first_gate(node):
        with original_gate(node) as ready:
            attempts.append(ready)
            yield ready
        if not ready:
            coordinator.complete(intent)

    monkeypatch.setattr(coordinator, 'node_activation_gate', settle_after_first_gate)
    try:
        assert mesh.activate_local_node()
        assert attempts == [False, True]
        assert mesh.local_node_active
        assert mesh.sync._retired
    finally:
        mesh.close()


def test_node_handoff_drains_root_dependent_sync_work_before_root_cut(tmp_path):
    _client, mesh = _mesh_world(tmp_path)
    coordinator = mesh.tx._coordinator
    writer_entered = threading.Event()
    writer_release = threading.Event()
    writer_done = threading.Event()
    activation = []
    errors = []

    def root_dependent_sync_callback():
        try:
            with mesh.sync._write_cycle():
                writer_entered.set()
                assert writer_release.wait(5.0)
                intent = coordinator.begin((
                    Selector('doc_exact', 'delivery-receipt.json'),
                ))
                coordinator.complete(intent)
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_done.set()

    writer = threading.Thread(target=root_dependent_sync_callback, daemon=True)
    writer.start()
    assert writer_entered.wait(1.0)
    activator = threading.Thread(
        target=lambda: activation.append(mesh.activate_local_node()), daemon=True,
    )
    activator.start()
    deadline = time.monotonic() + 2.0
    while not mesh.sync._quiescing and time.monotonic() < deadline:
        time.sleep(.005)
    assert mesh.sync._quiescing and not mesh.sync._retired

    writer_release.set()
    assert writer_done.wait(2.0)
    writer.join(2.0)
    activator.join(5.0)
    try:
        assert errors == []
        assert activation == [True]
        assert mesh.local_node_active and mesh.sync._retired
    finally:
        mesh.close()


def test_pending_handoff_retains_owner_when_withdrawal_fails(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    coordinator = mesh.tx._coordinator
    coordinator.begin((Selector('doc_exact', 'pending.json'),))
    monkeypatch.setattr(
        local_input_runtime_module, 'LOCAL_NODE_ACTIVATION_PENDING_WAIT_S', 0,
    )
    original_close = LocalNodePageOwner.close
    calls = []

    def fail_once(owner):
        calls.append(owner)
        if len(calls) == 1:
            raise RuntimeError('controlled withdrawal failure')
        return original_close(owner)

    monkeypatch.setattr(LocalNodePageOwner, 'close', fail_once)
    with pytest.raises(RuntimeError, match='controlled withdrawal failure'):
        mesh.activate_local_node()
    owner = mesh._local_node_owner
    assert owner is not None
    assert not mesh.sync._retired
    assert owner.server is not None and owner.server.state_path.exists()

    mesh.close()

    assert calls == [owner, owner]
    assert owner.server is None and owner.runtime is None


def test_mesh_keeps_legacy_writer_when_event_owner_binding_is_unavailable(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    closed = []
    original_close = LocalNodePageOwner.close

    def record_close(owner):
        original_close(owner)
        closed.append(owner)

    monkeypatch.setattr(LocalNodePageOwner, 'close', record_close)
    monkeypatch.setattr(
        LocalNodePageRuntime, 'bind_event_owner',
        lambda *_args: (_ for _ in ()).throw(RuntimeError('journal busy')),
    )
    try:
        assert not mesh.activate_local_node()
        assert mesh._local_node_owner is None
        assert mesh.local_inputs._node_source is None
        assert not mesh.sync._retired
        assert len(closed) == 1
        assert closed[0].server is None and closed[0].runtime is None
    finally:
        mesh.close()


def test_mesh_does_not_restore_legacy_writer_after_failed_retired_handoff(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    monkeypatch.setattr(
        mesh.local_inputs, 'install_node_source',
        lambda *_args: (_ for _ in ()).throw(RuntimeError('handoff failed')),
    )
    try:
        with pytest.raises(RuntimeError, match='handoff failed'):
            mesh.activate_local_node()
        assert mesh._local_node_owner is None
        assert mesh.local_inputs._node_source is None
        assert mesh.sync._retired
    finally:
        mesh.close()


def test_closed_mesh_rejects_activation_before_node_open(tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    mesh.close()
    opened = []
    monkeypatch.setattr(
        LocalNodePageOwner, 'open',
        classmethod(lambda cls, *_args, **_kwargs: opened.append(True)),
    )

    with pytest.raises(RuntimeError, match='mesh is closed'):
        mesh.activate_local_node()

    assert not opened


def test_failed_activation_cleanup_owner_is_retained_for_close_retry(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    original_close = LocalNodePageOwner.close
    close_calls = []

    def fail_once(owner):
        close_calls.append(owner)
        if len(close_calls) == 1:
            raise RuntimeError('cleanup interrupted')
        return original_close(owner)

    monkeypatch.setattr(LocalNodePageOwner, 'close', fail_once)
    monkeypatch.setattr(
        mesh.local_inputs, 'install_node_source',
        lambda *_args: (_ for _ in ()).throw(RuntimeError('handoff failed')),
    )

    with pytest.raises(RuntimeError, match='cleanup interrupted'):
        mesh.activate_local_node()
    owner = mesh._local_node_owner
    assert owner is not None
    assert mesh.sync._retired

    mesh.close()

    assert close_calls == [owner, owner]
    assert mesh._local_node_owner is None
    assert owner.server is None and owner.runtime is None


def test_mesh_close_waits_for_active_legacy_write_before_store_close(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    store_closed = threading.Event()
    close_errors = []

    def leased_write():
        with mesh.sync._write_cycle():
            entered.set()
            assert release.wait(5.0)

    original_store_close = mesh.store.close

    def record_store_close():
        assert mesh.sync._active_cycles == 0
        store_closed.set()
        original_store_close()

    monkeypatch.setattr(mesh.store, 'close', record_store_close)
    writer = threading.Thread(target=leased_write)
    writer.start()
    assert entered.wait(2.0)

    def close_mesh():
        try:
            mesh.close()
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_mesh)
    closer.start()
    assert not store_closed.wait(.1)

    release.set()
    writer.join(2.0)
    closer.join(2.0)

    assert not writer.is_alive() and not closer.is_alive()
    assert not close_errors
    assert store_closed.is_set()


def test_mesh_close_signals_local_input_before_outbox_drain_timeout(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    assert mesh.activate_local_node()
    owner = mesh._local_node_owner
    assert owner is not None
    mesh.start()

    original_outbox_stop = mesh.outbox.stop
    original_store_close = mesh.store.close
    store_closes = []

    def fail_outbox_stop(*_args, **_kwargs):
        assert mesh.sync._stop.is_set()
        assert mesh.outbox._stop_requested.is_set()
        assert mesh.local_inputs._closed
        assert mesh.local_inputs._stop.is_set()
        assert mesh.local_inputs.unread._closed
        assert mesh.local_inputs.unread._stop.is_set()
        raise TimeoutError('outbox dispatch did not stop')

    def record_store_close():
        store_closes.append(True)
        original_store_close()

    monkeypatch.setattr(mesh.outbox, 'stop', fail_outbox_stop)
    monkeypatch.setattr(mesh.store, 'close', record_store_close)

    with pytest.raises(TimeoutError, match='outbox dispatch did not stop'):
        mesh.close()

    assert mesh._local_node_owner is owner
    assert owner.server is not None and owner.runtime is not None
    assert store_closes == []

    monkeypatch.setattr(mesh.outbox, 'stop', original_outbox_stop)
    mesh.close()

    assert mesh._local_node_owner is None
    assert owner.server is None and owner.runtime is None
    assert store_closes == [True]


def test_mesh_close_revokes_background_owners_before_local_input_timeout(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    assert mesh.activate_local_node()
    owner = mesh._local_node_owner
    assert owner is not None

    # Start only the independent producers. A synthetic busy worker lock makes
    # the real local-input stop boundary fail without a five-second test delay.
    mesh.outbox.start()
    mesh.notifier.start()
    mesh.presence.start(interval=60)
    real_worker_lock = mesh.local_inputs._worker_lock
    original_store_close = mesh.store.close
    store_closes = []

    class BusyLock:
        @staticmethod
        def acquire(*, timeout):
            assert timeout == 5
            assert mesh.sync._stop.is_set()
            assert mesh.notifier._stop.is_set()
            assert mesh.presence._stop.is_set()
            assert mesh.outbox._stop.is_set()
            assert not mesh.notifier._thread.is_alive()
            assert not mesh.presence._thread.is_alive()
            assert not mesh.outbox._thread.is_alive()
            return False

        @staticmethod
        def release():
            raise AssertionError('a busy lock must not be released')

    def record_store_close():
        store_closes.append(True)
        original_store_close()

    mesh.local_inputs._worker_lock = BusyLock()
    monkeypatch.setattr(mesh.store, 'close', record_store_close)

    with pytest.raises(RuntimeError, match='Store must remain open'):
        mesh.close()

    assert mesh._local_node_owner is owner
    assert owner.server is not None and owner.runtime is not None
    assert store_closes == []

    mesh.local_inputs._worker_lock = real_worker_lock
    mesh.close()

    assert mesh._local_node_owner is None
    assert owner.server is None and owner.runtime is None
    assert store_closes == [True]


def test_mesh_close_revokes_background_owners_before_node_withdrawal_timeout(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    assert mesh.activate_local_node()
    owner = mesh._local_node_owner
    assert owner is not None
    mesh.start()

    original_owner_close = owner.close
    original_store_close = mesh.store.close
    store_closes = []
    withdrawal_calls = []

    def fail_withdrawal():
        withdrawal_calls.append(True)
        assert mesh.sync._stop.is_set()
        assert mesh.notifier._stop.is_set()
        assert mesh.presence._stop.is_set()
        assert mesh.outbox._stop.is_set()
        assert not mesh.notifier._thread.is_alive()
        assert not mesh.presence._thread.is_alive()
        assert not mesh.outbox._thread.is_alive()
        raise TimeoutError('local node recovery did not stop')

    def record_store_close():
        store_closes.append(True)
        original_store_close()

    monkeypatch.setattr(owner, 'close', fail_withdrawal)
    monkeypatch.setattr(mesh.store, 'close', record_store_close)

    with pytest.raises(TimeoutError, match='local node recovery did not stop'):
        mesh.close()

    assert withdrawal_calls == [True]
    assert mesh._local_node_owner is owner
    assert owner.server is not None and owner.runtime is not None
    assert store_closes == []

    monkeypatch.setattr(owner, 'close', original_owner_close)
    mesh.close()

    assert mesh._local_node_owner is None
    assert owner.server is None and owner.runtime is None
    assert store_closes == [True]


def test_mesh_close_revokes_background_owners_before_retirement_timeout(
        tmp_path, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    outbox_entered = threading.Event()
    outbox_release = threading.Event()
    sync_entered = threading.Event()
    sync_release = threading.Event()
    sent = []

    def handler(_target, payload):
        sent.append(payload['id'])
        if payload['id'] == 'first':
            outbox_entered.set()
            assert outbox_release.wait(10)

    mesh.outbox.handlers = {'probe': handler}
    mesh.store.outbox_add('probe', 'first-target', {'id': 'first'})
    mesh.store.outbox_add('probe', 'second-target', {'id': 'second'})
    mesh.start()
    mesh.outbox.notify()
    assert outbox_entered.wait(5)

    def leased_write():
        with mesh.sync._write_cycle():
            sync_entered.set()
            assert sync_release.wait(10)

    writer = threading.Thread(target=leased_write)
    writer.start()
    assert sync_entered.wait(5)

    original_retire = mesh.sync.retire
    retire_calls = []
    store_closes = []
    original_store_close = mesh.store.close
    close_errors = []

    def short_retire(*, timeout_s):
        retire_calls.append(timeout_s)
        assert mesh.sync._stop.is_set()
        assert mesh.notifier._stop.is_set()
        assert mesh.presence._stop.is_set()
        assert mesh.outbox._stop.is_set()
        return original_retire(timeout_s=.01 if len(retire_calls) == 1 else timeout_s)

    def record_store_close():
        store_closes.append(True)
        original_store_close()

    monkeypatch.setattr(mesh.sync, 'retire', short_retire)
    monkeypatch.setattr(mesh.store, 'close', record_store_close)

    def close_mesh():
        try:
            mesh.close()
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_mesh)
    closer.start()
    try:
        assert mesh.outbox._stop_requested.wait(5)
        assert not mesh.outbox._stop.is_set()
    finally:
        outbox_release.set()
    closer.join(5)

    assert not closer.is_alive()
    assert len(close_errors) == 1
    assert isinstance(close_errors[0], TimeoutError)
    assert str(close_errors[0]) == 'legacy sync owner has active writes'
    assert retire_calls == [10.0]
    assert store_closes == []
    assert not mesh.notifier._thread.is_alive()
    assert not mesh.presence._thread.is_alive()
    assert not mesh.outbox._thread.is_alive()
    assert sent == ['first']
    assert mesh.store.outbox_counts() == {'pending': 1}

    sync_release.set()
    writer.join(5)
    assert not writer.is_alive()
    try:
        mesh.close()
    finally:
        sync_release.set()
    assert retire_calls == [10.0, 10.0]
    assert store_closes == [True]


def test_gui_adopts_node_scheduler_then_retires_legacy_sync_thread(
        tmp_path, clouds):
    _client, mesh = _mesh_world(tmp_path)
    app = GuiApp(
        clouds.root(tmp_path / 'gui-root'), home=tmp_path / 'gui-home',
        machine='gui', encrypt=False,
    )
    try:
        app._adopt(mesh)
        assert app.mesh is mesh
        assert app._session_read_ready
        _wait_for(lambda: mesh.local_node_active)
        _wait_for(lambda: app._sync_thread is None)
        assert app._sync_thread is None
        assert mesh.local_inputs._thread is not None
        assert mesh.local_inputs._thread.is_alive()
    finally:
        app.close()

    assert not mesh.local_node_active


def test_gui_uses_legacy_sync_when_pre_retirement_node_open_is_unavailable(
        tmp_path, clouds):
    client, mesh = _mesh_world(tmp_path)
    client.db['_recovery_denied'] = True
    app = GuiApp(
        clouds.root(tmp_path / 'fallback-root'), home=tmp_path / 'fallback-home',
        machine='gui', encrypt=False, poll_s=.05,
    )
    try:
        app._adopt(mesh)
        _wait_for(lambda: app._node_activation_thread is None)
        assert app.mesh is mesh and not mesh.local_node_active
        assert not mesh.sync._retired
        assert app._sync_thread is not None and app._sync_thread.is_alive()
    finally:
        app.close()


def test_gui_close_during_delayed_activation_cannot_reopen_node_or_session(
        tmp_path, clouds, monkeypatch):
    _client, mesh = _mesh_world(tmp_path)
    app = GuiApp(
        clouds.root(tmp_path / 'closing-root'), home=tmp_path / 'closing-home',
        machine='gui', encrypt=False, poll_s=.05,
    )
    entered = threading.Event()
    release = threading.Event()

    def delayed_activation():
        entered.set()
        assert release.wait(5.0)
        return Mesh.activate_local_node(mesh)

    monkeypatch.setattr(mesh, 'activate_local_node', delayed_activation)
    app._adopt(mesh)
    assert entered.wait(2.0)
    assert app.mesh is mesh and app._session_read_ready

    app.close()
    release.set()
    _wait_for(lambda: app._node_activation_thread is None)

    assert app.mesh is None
    assert mesh._closed
    assert not mesh.local_node_active
    assert not getattr(app, '_restore_retrying', False)


def test_owner_pins_authenticated_provider_principal_before_node_open(tmp_path):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    try:
        assert owner.server is not None and owner.runtime is not None
        assert owner.runtime.server is owner.server
        assert owner.server.identity.provider_endpoint == "https://example.test"
        assert owner.server.identity.root == "supabase://team"
        assert owner.server.identity.principal == ACCOUNT
        assert owner.server.identity.machine == "desktop"
        assert owner.server.port > 0
    finally:
        owner.close()
        store.close()

    assert owner.server is None and owner.runtime is None
    owner.close()


def test_failed_authenticated_fence_leaves_no_node_owner(tmp_path):
    client, transport, store = _world(tmp_path)
    client.db["_recovery_denied"] = True

    try:
        with pytest.raises((NotImplementedError, ValueError)):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
        assert not (tmp_path / "home" / "nodes").exists()
    finally:
        store.close()


def test_startup_rechecks_location_after_authenticated_fence(tmp_path,
                                                             monkeypatch):
    _client, transport, store = _world(tmp_path)
    original = transport.recovery_fence

    def drift_after_fence():
        cut = original()
        transport._transport.root = "other"
        transport._transport.cache_key = "supabase:https://Example.TEST:443/:other"
        return cut

    monkeypatch.setattr(transport, "recovery_fence", drift_after_fence)
    try:
        with pytest.raises(ValueError, match="identity changed"):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
        assert not (tmp_path / "home" / "nodes").exists()
    finally:
        store.close()


def test_recovery_rechecks_pinned_location_after_owner_open(tmp_path):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    try:
        transport._transport.root = "other"
        transport._transport.cache_key = "supabase:https://Example.TEST:443/:other"
        with pytest.raises(ValueError, match="identity changed"):
            owner.server.recover_once(attempted_ns=1)
        assert owner.server.store.claimed_recovery_id() is None
        assert owner.server.store.status(
            node_epoch="", started_ns=0,
        ).admitted_generation == 0
    finally:
        owner.close()
        store.close()


def test_incremental_reads_recheck_pinned_authenticated_principal(tmp_path):
    client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    try:
        client.db["_recovery_account"] = OTHER_ACCOUNT
        with pytest.raises(ValueError, match="principal changed"):
            transport.source_ledger_fence()
    finally:
        client.db["_recovery_account"] = ACCOUNT
        owner.close()
        store.close()


def test_incremental_read_rejects_principal_change_during_call(
        tmp_path, monkeypatch):
    client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    provider = transport._transport
    original = provider.source_ledger_fence

    def changed_principal():
        result = original()
        client.db["_recovery_account"] = OTHER_ACCOUNT
        return result

    monkeypatch.setattr(provider, "source_ledger_fence", changed_principal)
    try:
        with pytest.raises(ValueError, match="principal changed"):
            transport.source_ledger_fence()
    finally:
        client.db["_recovery_account"] = ACCOUNT
        owner.close()
        store.close()


def test_incremental_read_rejects_endpoint_root_aba(tmp_path, monkeypatch):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    provider = transport._transport
    original = provider.source_ledger_fence

    def aba_read():
        provider.root = "other"
        provider.cache_key = "supabase:https://Example.TEST:443/:other"
        provider.root = "team"
        provider.cache_key = "supabase:https://Example.TEST:443/:team"
        return original()

    monkeypatch.setattr(provider, "source_ledger_fence", aba_read)
    try:
        with pytest.raises(ValueError, match="identity changed"):
            transport.source_ledger_fence()
    finally:
        owner.close()
        store.close()


def test_node_source_reads_bypass_mutable_cache_wrapper(tmp_path, monkeypatch):
    client, direct, store = _world(tmp_path)
    provider = direct._transport
    cached = CachingTransport(provider, auto_refresh=False)
    coordinator = MutationCoordinator(
        tmp_path / "cached-coordinator", root_identity(cached),
    )
    transport = LocalMutationTransport(cached, coordinator)
    owner = LocalNodePageOwner.open(
        tmp_path / "cached-home", transport, store, "desktop",
    )
    calls = []

    def wrapper_bypass():
        calls.append(True)
        client.db["_recovery_account"] = OTHER_ACCOUNT
        return cached.inner.source_ledger_fence()

    monkeypatch.setattr(cached, "source_ledger_fence", wrapper_bypass)
    try:
        assert transport.source_ledger_fence().epoch == EPOCH
        assert calls == []
        replacement = SupabaseTransport(
            "team", env={"SUPABASE_URL": "https://Example.TEST:443/",
                         "SUPABASE_SECRET_KEY": "sb_secret_x"},
            client=client,
        )
        cached.inner = replacement
        with pytest.raises(ValueError, match="identity changed"):
            transport.source_ledger_fence()
        cached.inner = provider
    finally:
        cached.inner = provider
        client.db["_recovery_account"] = ACCOUNT
        owner.close()
        cached.close()
        store.close()


def test_node_source_io_does_not_hold_identity_lock(tmp_path, monkeypatch):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    provider = transport._transport
    original = provider.source_ledger_fence
    entered = threading.Event()
    release = threading.Event()
    changed = threading.Event()
    errors = []

    def blocked_read():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("source test did not release provider")
        return original()

    def read():
        try:
            transport.source_ledger_fence()
        except BaseException as exc:
            errors.append(exc)

    def retarget():
        provider.root = "other"
        provider.cache_key = "supabase:https://Example.TEST:443/:other"
        provider.root = "team"
        provider.cache_key = "supabase:https://Example.TEST:443/:team"
        changed.set()

    monkeypatch.setattr(provider, "source_ledger_fence", blocked_read)
    reader = threading.Thread(target=read)
    mutator = threading.Thread(target=retarget)
    reader.start()
    try:
        assert entered.wait(5)
        mutator.start()
        # A source call must not hold the identity mutex while blocked in
        # provider I/O. The monotonic revision rejects its result afterward.
        assert changed.wait(5)
    finally:
        release.set()
    reader.join(5)
    mutator.join(5)
    try:
        assert not reader.is_alive() and not mutator.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert "identity changed" in str(errors[0])
    finally:
        owner.close()
        store.close()


def test_interrupt_after_server_start_releases_owner_resources(tmp_path,
                                                               monkeypatch):
    _client, transport, store = _world(tmp_path)
    opened = []
    original = LocalNodeServer.start

    def interrupted(server):
        original(server)
        opened.append(server)
        raise KeyboardInterrupt

    monkeypatch.setattr(LocalNodeServer, "start", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
        assert len(opened) == 1
        assert opened[0].port == 0
        assert opened[0]._owner is None
        assert not opened[0].state_path.exists()
    finally:
        store.close()


def test_concurrent_close_is_idempotent(tmp_path, monkeypatch):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    server = owner.server
    original = server.stop
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(3)
    errors = []

    def delayed_stop():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("close test did not release stop")
        original()

    def close():
        start.wait()
        try:
            owner.close()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(server, "stop", delayed_stop)
    threads = [threading.Thread(target=close) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    try:
        assert entered.wait(5)
    finally:
        release.set()
    for thread in threads:
        thread.join(5)
    store.close()

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert owner.server is None and owner.runtime is None


def test_owner_rejects_rebound_mutation_coordinator_before_provider_io(
        tmp_path, monkeypatch):
    _client, transport, store = _world(tmp_path)
    transport._coordinator.identity = "changed"
    monkeypatch.setattr(
        transport, "recovery_fence",
        lambda: pytest.fail("provider read crossed changed identity"),
    )

    try:
        with pytest.raises(ValueError, match="identity changed"):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
    finally:
        store.close()
