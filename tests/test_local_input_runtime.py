"""Composition and lifecycle coverage for opt-in local input ingestion."""
from __future__ import annotations

import hashlib
import threading

import pytest

from agentbridge.mesh import local_input_runtime
from agentbridge.store import overlay_index
from agentbridge.mesh.sealer import PlainSealer
from agentbridge.mesh.service import Mesh
from agentbridge.store import local_source
from agentbridge.store.db import Store
from agentbridge.transport.local_mutations import (
    LocalMutationTransport,
    owned_transport,
)
from agentbridge.transport.raw_documents import RawCollectionUnavailable


CHAT = "room"
META = f"chats/{CHAT}/meta.json"
STATE = f"chats/{CHAT}/overlays/state/alice.json"


def _documents(value=1):
    return {
        META: {"id": CHAT, "members": ["alice"], "value": value},
        "users/alice.json": {"name": "alice", "value": value},
        "lifecycle/alice/0001.json": {
            "id": "life-1", "subject": "alice", "value": value,
        },
        STATE: {"ns": value, "hidden": [], "starred": []},
    }


@pytest.fixture
def rig(clouds, tmp_path):
    provider = clouds.cached(tmp_path / "provider")
    for path, value in _documents().items():
        provider.put_doc(path, value)
    mesh = Mesh(
        provider, "alice", "machine", home=tmp_path / "home",
        store_path=tmp_path / "store.sqlite", local_inputs=True,
    )
    try:
        yield mesh, provider
    finally:
        mesh.close()


def test_opt_in_initializes_schema_preserves_namespace_and_owns_all_services(clouds, tmp_path):
    root = clouds.root(tmp_path / "provider")
    home = tmp_path / "home"
    legacy_tag = hashlib.sha1(clouds.cached(root).cache_key.encode()).hexdigest()[:12]
    legacy_path = home / "cache" / f"alice@machine-{legacy_tag}.sqlite"
    seeded = Store(legacy_path)
    seeded.cache_doc("sentinel.json", {"legacy": True})
    seeded.close()

    plain = Mesh(clouds.cached(root), "plain", "machine", home=home)
    assert plain.local_inputs is None
    plain.close()

    provider = clouds.cached(root)
    mesh = Mesh(provider, "alice", "machine", home=home, local_inputs=True)
    try:
        assert mesh.store.path == legacy_path
        assert mesh.store.cached_doc("sentinel.json") == {"legacy": True}
        assert type(mesh.tx) is LocalMutationTransport
        assert mesh.local_inputs.transport is mesh.tx
        tables = {row[0] for row in mesh.store._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {
            "local_source_schema", "local_sources", "local_source_writes",
            "local_source_definitions", "local_source_selectors",
        } <= tables
        services = (
            mesh.directory, mesh.keys, mesh.privacy, mesh.attachments,
            mesh.messaging, mesh.membership, mesh.accounts, mesh.presence,
            mesh.sync, mesh.applink.registry, mesh.applink.control,
        )
        assert all(service.tx is mesh.tx for service in services)
    finally:
        mesh.close()


def test_borrowed_owner_is_reused_and_explicit_sealer_is_rejected(clouds, tmp_path):
    provider = clouds.cached(tmp_path / "provider")
    owner = owned_transport(provider, tmp_path / "owner-home")
    mesh = Mesh(
        owner, "alice", "machine", home=tmp_path / "mesh-home",
        store_path=tmp_path / "borrowed.sqlite",
    )
    try:
        assert mesh.tx is owner
        assert mesh.local_inputs.transport is owner
    finally:
        mesh.close()

    with pytest.raises(ValueError, match="Mesh-owned sealer"):
        Mesh(
            clouds.cached(tmp_path / "other-provider"), "alice", "machine",
            home=tmp_path / "other-home", local_inputs=True,
            sealer=PlainSealer(),
        )


def test_independent_cloud_caches_match_admission_and_stable_poll(clouds, tmp_path):
    root = tmp_path / "provider"
    provider = clouds.cached(root)
    for path, value in _documents().items():
        provider.put_doc(path, value)
    direct = Mesh(
        clouds.cached(root), "alice", "direct", home=tmp_path / "direct-home",
        store_path=tmp_path / "direct.sqlite", local_inputs=True,
    )
    cached_transport = clouds.cached(root)
    cached_transport.refresh()
    cached = Mesh(
        cached_transport, "alice", "cached", home=tmp_path / "cached-home",
        store_path=tmp_path / "cached.sqlite", local_inputs=True,
    )
    try:
        assert direct.local_inputs.ingest(CHAT) is True
        direct_reader, direct_receipt, _direct_index = direct.local_inputs.inputs(CHAT)
        direct_records = direct_reader.capture_authority(
            direct_receipt, ("alice",),
        ).documents.records

        assert cached.tx._transport is cached_transport
        assert cached.local_inputs.ingest(CHAT) is True
        cached_reader, cached_receipt, first_index = cached.local_inputs.inputs(CHAT)
        cached_records = cached_reader.capture_authority(
            cached_receipt, ("alice",),
        ).documents.records
        assert cached_records == direct_records

        assert cached.local_inputs.ingest(CHAT) is False
        _reader, stable_receipt, stable_index = cached.local_inputs.inputs(CHAT)
        assert stable_receipt.source.raw == cached_receipt.source.raw
        assert stable_index.build == first_index.build
    finally:
        cached.close()
        direct.close()


def test_ingest_inputs_health_unchanged_and_restart_reuse_persisted_index(clouds, rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT) is True
    reader, receipt, index = runtime.inputs(CHAT)
    first_health = runtime.health(CHAT)
    assert first_health["ready"] and first_health["failures"] == 0
    assert runtime.ingest(CHAT) is False
    _reader2, receipt2, index2 = runtime.inputs(CHAT)
    assert receipt2.source.raw == receipt.source.raw
    assert index2.build == index.build

    def forbidden(*_args, **_kwargs):
        raise AssertionError("foreground inputs consulted the provider")

    for name in ("get_doc", "get_docs", "list_docs", "snapshot_docs"):
        monkeypatch.setattr(provider, name, forbidden)
    _reader3, receipt3, index3 = runtime.inputs(CHAT)
    assert receipt3.source.raw == receipt.source.raw
    assert index3 == index2
    assert reader.chat == CHAT
    persisted_health = runtime.health(CHAT)

    store_path, home, root = mesh.store.path, mesh.home, provider.root
    mesh.close()
    reopened = Mesh(
        clouds.cached(root), "alice", "machine", home=home,
        store_path=store_path, local_inputs=True,
    )
    try:
        _reader4, receipt4, index4 = reopened.local_inputs.inputs(CHAT)
        assert receipt4 == receipt3
        assert index4 == index3
        assert reopened.local_inputs.health(CHAT) == persisted_health
    finally:
        reopened.close()
    # The fixture's final close remains safe and idempotent.


def test_owned_write_invalidates_before_provider_call_and_failed_write_stays_pending(
        rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    original = provider.put_doc
    observed = []

    def inspect_then_write(path, value):
        observed.append(runtime.health(CHAT))
        return original(path, value)

    monkeypatch.setattr(provider, "put_doc", inspect_then_write)
    mesh.tx.put_doc(META, _documents(2)[META])
    assert len(observed) == 1 and not observed[0]["ready"]
    assert not runtime.health(CHAT)["ready"]
    assert runtime.health(CHAT)["writes_pending"] == 0

    def fail_write(_path, _value):
        raise OSError("ambiguous provider failure")

    monkeypatch.setattr(provider, "put_doc", fail_write)
    with pytest.raises(OSError, match="ambiguous provider failure"):
        mesh.tx.put_doc(META, _documents(3)[META])
    failed = runtime.health(CHAT)
    assert not failed["ready"]
    with pytest.raises(local_source.SourceChanged, match="source_mutation_pending"):
        runtime.ingest(CHAT)
    root = runtime.coordinator._transaction()
    with root as conn:
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (1,)


def test_unsafe_collection_retires_readiness_and_persists_bounded_health(rig):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    before = runtime.health(CHAT)
    with provider._lock:
        provider._authority_unsafe.add(META)

    with pytest.raises(RawCollectionUnavailable, match="unsafe_cached_value"):
        runtime.ingest(CHAT)
    failed = runtime.health(CHAT)
    assert failed == {
        "ready": False,
        "writes_pending": 0,
        "last_success_ns": before["last_success_ns"],
        "failures": 1,
        "error": "unavailable",
    }


def test_stale_failure_cannot_retire_newer_publication(rig):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    reader = runtime.reader(CHAT)
    stale = reader.capture().source
    mesh.tx.put_doc(META, _documents(2)[META])
    assert runtime.ingest(CHAT)
    winner = runtime.inputs(CHAT)[1].source

    with runtime.coordinator.publication_gate(mesh.store, reader.definition):
        recorded = local_source.record_failure(
            mesh.store, reader.definition.source,
            reason="unavailable", expected=stale,
        )
    assert recorded is False
    assert local_source.capture(mesh.store, reader.definition.source) == winner
    assert runtime.health(CHAT)["ready"]


def test_request_run_due_failure_finishes_scheduler_lease(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    clock = iter((10.0, 11.0, 12.0))
    monkeypatch.setattr(local_input_runtime.time, "monotonic", lambda: next(clock, 12.0))
    assert runtime.request(CHAT, selected=True, activity=True)

    def fail(_chat):
        raise RuntimeError("ingestion failed")

    monkeypatch.setattr(runtime, "ingest", fail)
    assert runtime.run_due() is True
    state = runtime.schedule._states[CHAT]
    assert runtime.schedule._running is None
    assert state.failures == 1


def test_inputs_checks_index_inside_atomic_capture(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    original = overlay_index._ready

    def index_rebuilt(conn, database_path, expected):
        assert conn.in_transaction
        conn.execute('UPDATE overlay_index_ready SET generation=generation+1 WHERE source=?',
                     (expected.source.source_id,))
        return original(conn, database_path, expected)

    monkeypatch.setattr(overlay_index, "_ready", index_rebuilt)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="changed"):
        runtime.inputs(CHAT)


def test_mesh_start_stop_and_watcher_fallback_are_bounded(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    started = threading.Event()

    def unavailable_watch():
        started.set()
        raise OSError("watch unavailable")

    monkeypatch.setattr(mesh.tx, "watch", unavailable_watch)
    runtime.start()
    assert started.wait(1)
    runtime.start()
    runtime.stop()
    assert runtime._thread is not None and not runtime._thread.is_alive()


def test_watcher_is_closed_during_shutdown(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs

    class Watcher:
        def __init__(self):
            self.closed = threading.Event()

        def wait(self, timeout):
            runtime._stop.wait(timeout)
            return False

        def close(self):
            self.closed.set()

    watcher = Watcher()
    monkeypatch.setattr(mesh.tx, "watch", lambda: watcher)
    runtime.start()
    runtime.stop()
    assert watcher.closed.wait(1)


def test_stop_quiesces_manual_ingest_and_permanently_closes_runtime(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    failures = []
    original = local_input_runtime.collect_document_batches

    def blocked_collect(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(local_input_runtime, "collect_document_batches", blocked_collect)
    def interrupted_ingest():
        try:
            runtime.ingest(CHAT)
        except RuntimeError as exc:
            failures.append(exc)

    ingest = threading.Thread(target=interrupted_ingest)
    ingest.start()
    assert entered.wait(1)
    shutdown = threading.Thread(target=lambda: (runtime.stop(), stopped.set()))
    shutdown.start()
    assert not stopped.wait(0.05)
    release.set()
    ingest.join(2)
    shutdown.join(2)
    assert stopped.is_set()
    assert len(failures) == 1 and str(failures[0]) == 'local input ingestion stopped'
    assert runtime.health(CHAT)['ready'] is False
    assert runtime.request(CHAT, selected=True, activity=True) is False
    with pytest.raises(RuntimeError, match="closed"):
        runtime.start()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.ingest(CHAT)


def test_mesh_close_timeout_leaves_store_open(rig):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    real_lock = runtime._worker_lock

    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == 5
            return False

        def release(self):
            raise AssertionError("unacquired lock released")

    runtime._worker_lock = BusyLock()
    with pytest.raises(RuntimeError, match="Store must remain open"):
        mesh.close()
    assert mesh.store._conn().execute("SELECT 1").fetchone() == (1,)
    runtime._worker_lock = real_lock


@pytest.mark.parametrize('outcomes, expected_calls, expected_result', [
    ([True] * 10, 4, True), ([False], 1, False), ([True, False], 2, True),
])
def test_preparation_burst_caps_work_and_keeps_partial_progress(
        rig, monkeypatch, outcomes, expected_calls, expected_result):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    calls = []
    values = iter(outcomes)
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: 10.0)
    def prepare():
        calls.append(True)
        return next(values)
    monkeypatch.setattr(runtime, 'prepare_one', prepare)
    assert runtime._prepare_burst() is expected_result
    assert len(calls) == expected_calls


@pytest.mark.parametrize('elapsed', [0.020, 0.100])
def test_preparation_burst_yields_after_time_budget_even_with_more_jobs(rig, monkeypatch, elapsed):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    now, calls = [10.0], []
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: now[0])
    def slow_quantum():
        calls.append(True)
        now[0] += elapsed
        return True
    monkeypatch.setattr(runtime, 'prepare_one', slow_quantum)
    assert runtime._prepare_burst() is True
    assert calls == [True]  # One quantum may exceed the cooperative budget.


def test_preparation_burst_stops_between_quanta(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    calls = []
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: 10.0)
    def stop_after_one():
        calls.append(True)
        runtime._stop.set()
        return True
    monkeypatch.setattr(runtime, 'prepare_one', stop_after_one)
    assert runtime._prepare_burst() is True
    assert calls == [True]
    assert runtime._prepare_burst() is False
    assert calls == [True]


def test_worker_preparation_backlog_progress_keeps_every_collection_turn(rig, monkeypatch):
    from types import SimpleNamespace
    mesh, _provider = rig
    runtime = mesh.local_inputs
    queue = iter(range(12))
    events, rounds = [], []
    monkeypatch.setattr(runtime.transport, 'watch', lambda: None)
    monkeypatch.setattr(runtime, 'discover', lambda: None)
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: 10.0)
    def prepare():
        events.append(('prepare', next(queue)))
        return True
    def collect():
        events.append(('ingest', len(rounds)))
        rounds.append(True)
        if len(rounds) == 2:
            runtime._stop.set()
        return True
    monkeypatch.setattr(runtime, 'prepare_one', prepare)
    monkeypatch.setattr(runtime, 'run_due', collect)
    monkeypatch.setattr(runtime, 'presence', SimpleNamespace(run_due=lambda: events.append(('presence',)) or False))
    monkeypatch.setattr(runtime, 'auxiliary', SimpleNamespace(run_due=lambda: events.append(('aux',)) or False))
    runtime._run()
    assert events == ([('prepare', i) for i in range(4)] + [('ingest', 0), ('presence',), ('aux',)]
                      + [('prepare', i) for i in range(4, 8)] + [('ingest', 1), ('presence',), ('aux',)])
    # Restore real owners before fixture shutdown.
    monkeypatch.undo()


@pytest.mark.parametrize('error,blocked', [
    (local_source.SourceChanged('source_mutation_pending'), True),
    (local_source.SourceChanged('source_mutation_pending', 'extra'), False),
    (local_source.SourceChanged('source_changed'), False),
    (local_source.SourceChanged('source_mutation_pending_extra'), False),
    (RuntimeError('source_mutation_pending'), False),
    (OSError('provider unavailable'), False),
    (RawCollectionUnavailable('mirror_pending'), False),
])
def test_run_due_only_exact_pending_intent_uses_short_retry(rig, monkeypatch, error, blocked):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    now = [10.0]
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: now[0])
    runtime.request(CHAT, selected=True, activity=True)
    now[0] = 11.0
    def fail(_chat):
        raise error
    monkeypatch.setattr(runtime, 'ingest', fail)
    assert runtime.run_due() is True
    state = runtime.schedule._states[CHAT]
    assert state.failures == (0 if blocked else 1)
    assert state.due - now[0] == pytest.approx(0.35 if blocked else 4.0)


def test_persistent_mutation_pending_never_collects_or_publishes(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    def fail_write(*_args):
        raise OSError('ambiguous write')
    monkeypatch.setattr(provider, 'put_doc', fail_write)
    with pytest.raises(OSError):
        mesh.tx.put_doc(META, _documents(2)[META])
    monkeypatch.setattr(local_input_runtime, 'collect_document_batches',
                        lambda *_a, **_kw: pytest.fail('pending intent read provider'))
    now = [10.0]
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: now[0])
    runtime.request(CHAT, selected=True, activity=True)
    attempts = 0
    for tick in range(1, 201):
        now[0] = 10.0 + tick / 100
        runtime.request(CHAT, selected=True, activity=True)
        attempts += runtime.run_due()
        assert not runtime.health(CHAT)['ready']
    assert 5 <= attempts <= 6
    assert runtime.schedule._states[CHAT].failures == 0
    with runtime.coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (1,)


def test_route_selection_controls_preparation_priority_without_queuing_or_io(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    prep = runtime._page_preparation
    assert runtime.request('room', selected=True)
    assert prep._selected == 'room'
    assert not prep._terminals and not prep._proofs
    assert runtime.request('background')
    assert prep._selected == 'room'
    assert runtime.request('next-room', selected=True)
    assert prep._selected == 'next-room'
    monkeypatch.setattr(runtime.schedule, 'request', lambda *_a, **_kw: False)
    assert not runtime.request('denied', selected=True)
    assert prep._selected == 'next-room'
    runtime.clear_selection()
    assert prep._selected is None
    assert not prep._terminals and not prep._proofs
