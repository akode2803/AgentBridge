"""Opt-in raw presence scans are background work, never receipt authority."""
from __future__ import annotations

import time

import pytest

from agentbridge.mesh import presence_input_runtime as module
from agentbridge.mesh.local_presence_source import LocalPresenceSource
from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import owned_transport
from agentbridge.transport.raw_documents import RawCollectionUnavailable


@pytest.fixture
def rig(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    folder.put_doc('presence/alice-device.json',
                   {'user': 'bob', 'last_seen_ns': 17, 'online': False})
    folder.put_doc('presence/bob-device.json',
                   {'user': 'bob', 'last_seen_ns': 31, 'online': True})
    owned = owned_transport(folder, tmp_path / 'owner')
    store = Store(tmp_path / 'cache.sqlite')
    ticks = [10.0]
    runtime = module.PresenceInputRuntime(owned, store, clock=lambda: ticks[0])
    yield runtime, owned, folder, store, ticks
    runtime.stop()
    store.close()
    owned.close()


def test_request_is_bounded_hint_and_raw_floors_are_exact(rig, monkeypatch):
    runtime, _owned, folder, store, _ticks = rig
    assert runtime.reader.definition.selectors == (
        source_selectors.Selector('doc_prefix', 'presence'),)
    original_list = folder.list_docs

    def no_foreground_walk(*_args, **_kwargs):
        raise AssertionError('request attempted provider traversal')

    monkeypatch.setattr(folder, 'list_docs', no_foreground_walk)
    assert runtime.request()
    assert runtime.request()
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs(('bob',))
    monkeypatch.setattr(folder, 'list_docs', original_list)
    assert runtime.run_due()
    reader, receipt, inputs = runtime.inputs(('bob', 'alice'))
    assert isinstance(reader, LocalPresenceSource)
    assert receipt.source.ready and receipt.source.logical_source == runtime.reader.definition.source
    assert inputs.floors == (('bob', 31), ('alice', 0))
    assert 0 < inputs.observed_ns <= time.time_ns()
    assert reader.capture_members(receipt, ('bob',)).floors == (('bob', 31),)
    conn = store._conn()
    conn.execute('BEGIN')
    try:
        assert reader.matches_in_transaction(conn, receipt)
    finally:
        conn.rollback()
    assert store._conn().execute('SELECT count(*) FROM overlay_index_ready WHERE source=?',
                                 (receipt.source.raw.source_id,)).fetchone() == (0,)


def test_exact_floors_and_observation_time_share_one_store_cut(rig):
    runtime, _owned, _folder, store, _ticks = rig
    runtime.request()
    assert runtime.run_due()
    reader, receipt, first = runtime.inputs(('bob',))
    assert first.observed_ns > 0
    stale = time.time_ns() - 31 * 1_000_000_000
    with store._conn():
        store._conn().execute('UPDATE local_sources SET last_success_ns=? WHERE source=?',
                              (stale, reader.definition.source))
    second = reader.capture_members(receipt, ('bob',))
    assert second.floors == first.floors
    assert second.observed_ns == stale and second != first
    conn = store._conn()
    conn.execute('BEGIN')
    try:
        assert reader.matches_in_transaction(conn, receipt)
        assert reader.capture_in_transaction(conn, receipt, ('bob',)) == second
    finally:
        conn.rollback()
    # A ready raw/index generation alone cannot establish fresh observation.


def test_local_heartbeat_retires_only_presence_before_external_write(rig, monkeypatch):
    runtime, owned, folder, _store, ticks = rig
    runtime.request()
    assert runtime.run_due()
    original = folder.put_doc

    def check_retired(path, payload):
        assert path == 'presence/new.json'
        assert not runtime.health()['ready']
        return original(path, payload)

    monkeypatch.setattr(folder, 'put_doc', check_retired)
    owned.put_doc('presence/new.json', {'user': 'alice', 'last_seen_ns': 55})
    assert not runtime.health()['ready']
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs(('alice',))
    runtime.request()
    assert not runtime.run_due()  # four-second success cadence
    ticks[0] += 4.0
    assert runtime.run_due()
    assert runtime.inputs(('alice',))[2].floors == (('alice', 55),)


def test_failed_partial_scan_remains_unready_and_retries_with_backoff(rig, monkeypatch):
    runtime, _owned, folder, _store, ticks = rig
    runtime.request()
    assert runtime.run_due()
    prior = runtime.reader.capture().source.raw
    folder.put_doc('presence/third.json', {'user': 'alice', 'last_seen_ns': 99})
    original = module.collect_document_batches

    def partial(transport, definition, *, consume, **kwargs):
        def sink(batch):
            consume(batch)
            raise RawCollectionUnavailable('incomplete_scan')
        return original(transport, definition, consume=sink, batch_documents=1, **kwargs)

    monkeypatch.setattr(module, 'collect_document_batches', partial)
    runtime.request()
    ticks[0] += 4.0
    assert runtime.run_due()
    assert not runtime.health()['ready']
    assert local_source.capture(_store, runtime.reader.definition.source).raw == prior
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs(('alice',))
    assert not runtime.run_due()  # failed attempt waits four more seconds
    monkeypatch.setattr(module, 'collect_document_batches', original)
    ticks[0] += 4.0
    assert runtime.run_due()
    assert runtime.inputs(('alice',))[2].floors == (('alice', 99),)


def test_new_local_mutation_cannot_cross_staged_admission(rig, monkeypatch):
    runtime, owned, _folder, _store, _ticks = rig
    original = module.presence_index.build

    def mutate_before_admit(*args, **kwargs):
        result = original(*args, **kwargs)
        owned.put_doc('presence/race.json', {'user': 'bob', 'last_seen_ns': 101})
        return result

    monkeypatch.setattr(module.presence_index, 'build', mutate_before_admit)
    with pytest.raises(local_source.SourceChanged):
        runtime.ingest()
    assert not runtime.health()['ready']
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs(('bob',))


def test_stop_is_a_flag_and_foreground_never_restarts_owner(rig):
    runtime, _owned, _folder, _store, _ticks = rig
    runtime.stop()
    assert not runtime.request()
    assert not runtime.run_due()
    with pytest.raises(RuntimeError, match='closed'):
        runtime.ingest()
