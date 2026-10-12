"""Authenticated lifecycle composition for the inactive page runtime."""
from __future__ import annotations

import threading
import time

import pytest

from agentbridge.gui.context import GuiApp
from agentbridge.mesh.node_page_owner import LocalNodePageOwner
from agentbridge.mesh.service import Mesh
from agentbridge.node.server import LocalNodeServer
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
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
