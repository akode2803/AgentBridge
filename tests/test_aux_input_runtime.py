"""Reusable status/room-runtime raw companions stay bounded and source fenced."""
from __future__ import annotations

import sqlite3

import pytest

from agentbridge.mesh import aux_input_runtime as module
from agentbridge.mesh.local_aux_source import LocalAuxSource
from agentbridge.store import (aux_inputs, document_observation as docs,
                               local_source, raw_publication, staged_source)
from agentbridge.store.db import Store
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import owned_transport
from agentbridge.transport.raw_documents import RawCollectionUnavailable


CHAT = 'room'
STATUS = 'status/typing_alice.json'
PAUSE = f'chats/{CHAT}/runtime/member-control/pause/one.json'


@pytest.fixture
def rig(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    folder.put_doc(STATUS, {'user': 'alice', 'chat_id': CHAT, 'updated': '2026-01-01T00:00:00Z'})
    folder.put_doc(PAUSE, {'record': {'paused': True}, 'sig': 'candidate-only'})
    owned = owned_transport(folder, tmp_path / 'owner')
    store = Store(tmp_path / 'store.sqlite')
    clock = [10.0]
    runtime = module.AuxInputRuntime(owned, store, clock=lambda: clock[0])
    try:
        yield runtime, owned, folder, store, clock
    finally:
        runtime.stop()
        store.close()
        owned.close()


def test_raw_sources_are_independent_and_do_not_claim_authority(rig):
    runtime, owned, _folder, _store, _clock = rig
    assert runtime.ingest('status').ready
    assert runtime.ingest('runtime', CHAT).ready
    status_reader, status_receipt, status = runtime.inputs('status')
    room_reader, room_receipt, room = runtime.inputs('runtime', CHAT)
    assert isinstance(status_reader, LocalAuxSource)
    assert status_reader.definition.selectors[0].value == 'status'
    assert room_reader.definition.selectors[0].value == f'chats/{CHAT}/runtime'
    assert status.documents.document(STATUS)['user'] == 'alice'
    assert status.documents.document(PAUSE) is None
    assert room.documents.document(PAUSE)['record']['paused'] is True
    assert room.documents.document(STATUS) is None
    assert status.position != room.position

    owned.put_doc(STATUS, {'user': 'alice', 'chat_id': CHAT, 'updated': 'later'})
    with pytest.raises(local_source.SourceChanged):
        status_reader.capture_documents(status_receipt)
    assert room_reader.capture_documents(room_receipt).documents.document(PAUSE)


def test_identical_repoll_keeps_physical_generation_but_binds_fresh_receipt(rig):
    runtime, _owned, _folder, _store, _clock = rig
    first = runtime.ingest('status')
    assert first.ready
    second = runtime.ingest('status')
    assert second.ready and second.raw == first.raw and second.revision > first.revision
    assert runtime.inputs('status')[1].source == second


def test_coalesced_requested_work_is_not_a_foreground_provider_scan(rig, monkeypatch):
    runtime, _owned, folder, _store, clock = rig
    original = folder.list_docs
    monkeypatch.setattr(folder, 'list_docs',
                        lambda *_args, **_kwargs: pytest.fail('request walked provider'))
    assert runtime.request('status') and runtime.request('status')
    assert runtime.request('runtime', CHAT)
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs('status')
    monkeypatch.setattr(folder, 'list_docs', original)
    assert not runtime.run_due()
    clock[0] += 0.1
    assert runtime.run_due()
    assert runtime.run_due()
    assert runtime.inputs('status')[1].source.ready
    assert runtime.inputs('runtime', CHAT)[1].source.ready
    assert not runtime.run_due()  # coalesced, backoff until next due


def test_request_arriving_during_ingestion_schedules_one_bounded_rerun(rig, monkeypatch):
    runtime, _owned, _folder, _store, clock = rig
    original = runtime._ingest
    calls = []

    def work(reader):
        calls.append(reader.scope)
        if len(calls) == 1:
            assert runtime.request('status')
        return original(reader)

    monkeypatch.setattr(runtime, '_ingest', work)
    assert runtime.request('status')
    clock[0] += 0.1
    assert runtime.run_due() and calls == ['status']
    assert not runtime.run_due()
    clock[0] += 0.35
    assert runtime.run_due() and calls == ['status', 'status']
    assert not runtime.run_due()


def test_three_docs_over_request_limit_fail_before_payload_copy(rig, monkeypatch):
    runtime, _owned, folder, _store, _clock = rig
    folder.put_doc('status/typing_bob.json', {'user': 'bob'})
    folder.put_doc('status/typing_carl.json', {'user': 'carl'})
    runtime.ingest('status')
    original = aux_inputs.docs._capture_selected
    monkeypatch.setattr(aux_inputs.docs, '_capture_selected',
                        lambda *_args, **_kwargs: pytest.fail('copied overflowing payload'))
    with pytest.raises(aux_inputs.AuxInputsUnavailable, match='aux_document_budget'):
        runtime.inputs('status', max_documents=2)
    monkeypatch.setattr(aux_inputs.docs, '_capture_selected', original)


def test_default_ten_thousand_document_ceiling_rejects_10001st_without_copy(rig, monkeypatch):
    runtime, _owned, _folder, store, _clock = rig
    reader = runtime.reader('status')
    captured = SourcePublisher(runtime.coordinator, store, reader.definition).capture()
    with runtime.coordinator.publication_gate(store, reader.definition):
        claimed = local_source.claim_collection(store, captured.source)
    stage = staged_source.begin_raw(store, reader.definition.source, expected=claimed)
    for start in range(0, 10_001, 128):
        staged_source.append_raw(store, stage, {
            f'status/entry-{i:05d}.json': {'chat_id': CHAT}
            for i in range(start, min(start + 128, 10_001))
        })
    staged_source.finish_raw(store, stage)
    with runtime.coordinator.publication_gate(store, reader.definition):
        raw_publication.admit(store, claimed, stage, observed_ns=1)
    monkeypatch.setattr(aux_inputs.docs, '_capture_selected',
                        lambda *_args, **_kwargs: pytest.fail('copied overflowing manifest'))
    with pytest.raises(aux_inputs.AuxInputsUnavailable, match='aux_document_budget'):
        runtime.inputs('status')


def test_prefix_capture_is_generic_over_an_admitted_chat_source(rig):
    runtime, _owned, _folder, store, _clock = rig
    from agentbridge.mesh.local_page_source import LocalPageSource
    from agentbridge.store import staged_publication

    room = LocalPageSource(runtime.coordinator, store, CHAT)
    captured = SourcePublisher(runtime.coordinator, store, room.definition).capture()
    with runtime.coordinator.publication_gate(store, room.definition):
        claimed = local_source.claim_collection(store, captured.source)
    stage = staged_source.begin(store, room.definition.source, CHAT, expected=claimed)
    staged_source.append(store, stage, {
        f'chats/{CHAT}/meta.json': {'id': CHAT},
        f'chats/{CHAT}/keys/1.json': {'epoch': 1},
    })
    staged_source.finish(store, stage)
    with runtime.coordinator.publication_gate(store, room.definition):
        admitted, _index = staged_publication.admit(store, claimed, stage, observed_ns=1)
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        selected = aux_inputs.capture_prefix(conn, store.path, admitted.raw,
                                             f'chats/{CHAT}/keys', max_documents=64)
    finally:
        conn.close()
    assert selected.documents.document(f'chats/{CHAT}/keys/1.json') == {'epoch': 1}
    assert selected.position == admitted.raw


def test_oversize_raw_prefix_fails_before_payload_copy(rig, monkeypatch):
    runtime, _owned, folder, _store, _clock = rig
    for i in range(3):
        folder.put_doc(f'status/large-{i}.json', {'payload': 'x' * (3 * 1024 * 1024)})
    runtime.ingest('status')
    monkeypatch.setattr(aux_inputs.docs, '_capture_selected',
                        lambda *_args, **_kwargs: pytest.fail('copied over-budget payload'))
    with pytest.raises(aux_inputs.AuxInputsUnavailable, match='aux_byte_budget'):
        runtime.inputs('status')


def test_provisional_collection_keeps_old_ready_but_handled_failure_retires(rig, monkeypatch):
    runtime, _owned, _folder, _store, _clock = rig
    runtime.ingest('status')
    old = runtime.inputs('status')[1].source.raw
    original = module.collect_document_batches

    def fail_after_batch(transport, definition, *, consume, **kwargs):
        def sink(batch):
            consume(batch)
            assert runtime.inputs('status')[1].source.raw == old
            raise RawCollectionUnavailable('partial')
        return original(transport, definition, consume=sink, batch_documents=1, **kwargs)

    monkeypatch.setattr(module, 'collect_document_batches', fail_after_batch)
    with pytest.raises(RawCollectionUnavailable, match='partial'):
        runtime.ingest('status')
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs('status')


def test_local_mutation_during_build_rejects_old_candidate(rig, monkeypatch):
    runtime, owned, _folder, _store, _clock = rig
    runtime.ingest('runtime', CHAT)
    original = module.collect_document_batches

    def mutate_after_batch(transport, definition, *, consume, **kwargs):
        def sink(batch):
            consume(batch)
            owned.put_doc(PAUSE, {'record': {'paused': False}, 'sig': 'new'})
        return original(transport, definition, consume=sink, batch_documents=1, **kwargs)

    monkeypatch.setattr(module, 'collect_document_batches', mutate_after_batch)
    with pytest.raises(local_source.SourceChanged):
        runtime.ingest('runtime', CHAT)
    with pytest.raises(local_source.SourceChanged):
        runtime.inputs('runtime', CHAT)


def test_post_admit_cleanup_fault_cannot_retire_new_winner(rig, monkeypatch):
    runtime, _owned, folder, _store, _clock = rig
    first = runtime.ingest('status')
    folder.put_doc(STATUS, {'user': 'alice', 'chat_id': CHAT, 'updated': 'new'})
    monkeypatch.setattr(staged_source, 'retire_generation',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('cleanup failed')))
    with pytest.raises(OSError, match='cleanup failed'):
        runtime.ingest('status')
    _reader, receipt, captured = runtime.inputs('status')
    assert receipt.source.ready and receipt.source.raw != first.raw
    assert captured.documents.document(STATUS)['updated'] == 'new'


def test_atomic_pointer_rollback_retains_old_ready_if_failure_record_cannot_commit(rig, monkeypatch):
    runtime, _owned, folder, _store, _clock = rig
    first = runtime.ingest('status')
    folder.put_doc(STATUS, {'user': 'alice', 'chat_id': CHAT, 'updated': 'new'})
    capture = raw_publication.owner.capture_in_transaction
    witnessed = []

    def fail_after_pointer(conn, store, source):
        result = capture(conn, store, source)
        if result.ready and result.raw != first.raw:
            witnessed.append(result.raw)
            raise sqlite3.OperationalError('post-pointer fault')
        return result

    monkeypatch.setattr(raw_publication.owner, 'capture_in_transaction', fail_after_pointer)
    monkeypatch.setattr(module.local_source, 'record_failure',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('failure record unavailable')))
    with pytest.raises(OSError, match='failure record unavailable'):
        runtime.ingest('status')
    assert witnessed
    assert runtime.inputs('status')[1].source.raw == first.raw


def test_stale_candidate_or_failure_cannot_retire_newer_winner(rig):
    runtime, _owned, folder, store, _clock = rig
    first = runtime.ingest('status')
    reader = runtime.reader('status')
    publisher = SourcePublisher(runtime.coordinator, store, reader.definition)
    captured = publisher.capture()
    with runtime.coordinator.publication_gate(store, reader.definition):
        stale = local_source.claim_collection(store, captured.source)
    stage = staged_source.begin_raw(store, reader.definition.source, expected=stale)
    staged_source.finish_raw(store, stage)
    folder.put_doc(STATUS, {'user': 'alice', 'chat_id': CHAT, 'updated': 'later'})
    winner = runtime.ingest('status')
    assert winner.raw != first.raw
    with runtime.coordinator.publication_gate(store, reader.definition):
        with pytest.raises(local_source.SourceChanged):
            raw_publication.admit(store, stale, stage, observed_ns=1)
        assert not local_source.record_failure(store, reader.definition.source,
                                               reason='unavailable', expected=stale)
    assert runtime.inputs('status')[1].source == winner


def test_queue_covers_room_inventory_and_rotates_old_room_hints(rig):
    runtime, _owned, _folder, _store, _clock = rig
    for scope in ('status', 'users', 'peer', 'identities'):
        assert runtime.request(scope)
    for number in range(128):
        assert runtime.request('runtime', f'room-{number}')
    assert len(runtime._jobs) == module.MAX_JOBS
    runtime._running = ('runtime', 'room-0')
    assert runtime.request('runtime', 'newly-selected')
    assert ('runtime', 'room-0') in runtime._jobs
    assert ('runtime', 'room-1') not in runtime._jobs
    assert ('runtime', 'newly-selected') in runtime._jobs
    assert all((scope, '') in runtime._jobs for scope in ('status', 'users', 'peer', 'identities'))
    assert len(runtime._jobs) == module.MAX_JOBS
