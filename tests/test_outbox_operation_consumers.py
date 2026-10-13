"""Root consumer ownership survives retries, closure and Store cleanup crashes."""
import json
import sqlite3

import pytest
import test_operation_journal as cases
from test_outbox_mutation_owner import setup
from test_receipt_retirement import _epoch, NEXT

from agentbridge.store import local_source, operation_consumers

world = cases.world


def _pending(world, payload=None):
    store, journal, rpc, owner, _ = setup(world)
    payload = {'id': 'message', 'ns': 1} if payload is None else payload
    store.outbox_add('append_log', 'room|alice.jsonl', payload)
    return store, journal, rpc, owner, store.outbox_claim_due()[0]


def _args(item):
    return {'p_chat': 'room', 'p_log': 'alice.jsonl', 'p_record': item.payload}


def _allocate(journal, store, item, args=None):
    return journal.allocate_outbox_ticket(store, item.seq, 'append', item.owner_digest,
                                         'log_append', _args(item) if args is None else args)


def test_atomic_slot_replays_exact_ticket_after_reopen_and_rejects_changed_body(world):
    store, journal, _, _, item = _pending(world)
    ticket = _allocate(journal, store, item)
    restored = cases._reload(world)
    assert restored.get_outbox_leg(store, item.seq, 'append', item.owner_digest) == ticket
    assert _allocate(restored, store, item) == ticket
    with pytest.raises(local_source.SourceChanged, match='request_changed'):
        _allocate(restored, store, item, {**_args(item), 'p_record': {'id': 'different'}})
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (8,)
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (1,)


def test_release_requires_sent_and_consumed_even_after_provider_closure(world):
    store, journal, _, owner, item = _pending(world)
    owner.dispatch(item)
    ticket = journal.get_outbox_leg(store, item.seq, 'append', item.owner_digest)
    with pytest.raises(local_source.SourceChanged, match='unsettled'):
        journal.release_ticket(ticket)
    with pytest.raises(local_source.SourceChanged, match='outbox_changed'):
        journal.consume_outbox_leg(store, item.seq, 'append', item.owner_digest)
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 0
    with pytest.raises(local_source.SourceChanged, match='unsettled'):
        journal.release_ticket(ticket)
    assert store.outbox_mark_sent(item)
    assert journal.consume_outbox_leg(store, item.seq, 'append', item.owner_digest) == ticket
    with pytest.raises(local_source.SourceChanged, match='missing'):
        journal.get_ticket(ticket)
    journal.finish_outbox(store, item.seq, item.owner_digest)
    # Root completion may precede Store cleanup or a process crash.
    assert journal.consume_outbox_leg(store, item.seq, 'append', item.owner_digest) == ticket
    journal.finish_outbox(store, item.seq, item.owner_digest)


def test_sent_without_retained_append_is_not_completion_proof(world):
    store, journal, _, _, item = _pending(world)
    # Install capability using another valid owner, then forge only Store state.
    _allocate(journal, store, item)
    store.outbox_add('append_log', 'other|alice.jsonl', {'id': 'other'})
    forged = store.outbox_claim_due()[0]
    assert store.outbox_mark_sent(forged)
    for action in (lambda: journal.consume_outbox_leg(store, forged.seq, 'append', forged.owner_digest),
                   lambda: journal.finish_outbox(store, forged.seq, forged.owner_digest)):
        with pytest.raises(local_source.SourceChanged, match='missing_leg'):
            action()


def test_sent_or_deleted_rows_never_allocate_fresh_tickets(world):
    store, journal, _, _, item = _pending(world)
    assert store.outbox_mark_sent(item)
    with pytest.raises(local_source.SourceChanged, match='outbox_changed'):
        _allocate(journal, store, item)
    assert store.outbox_finalize_sent(store.outbox_sent_items()[0])
    with pytest.raises(local_source.SourceChanged, match='outbox_changed'):
        _allocate(journal, store, item)


def test_consumed_owner_reaped_only_after_store_deletion(world, monkeypatch):
    store, journal, _, owner, item = _pending(world)
    monkeypatch.setattr(operation_consumers, 'MAX_CONSUMERS', 1)
    owner.dispatch(item)
    assert store.outbox_mark_sent(item)
    sent = store.outbox_sent_items()[0]
    owner.release(sent)
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT consumed FROM typed_operation_consumers').fetchall() == [(1,)]
    assert store.outbox_finalize_sent(sent)
    store.outbox_add('append_log', 'room|alice.jsonl', {'id': 'next'})
    newer = store.outbox_claim_due()[0]
    _allocate(journal, store, newer)
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT owner_seq,consumed FROM typed_operation_consumers').fetchall() == [(newer.seq, 0)]


def test_store_race_after_node_retirement_rolls_back_request_and_slot(world, monkeypatch):
    store, journal, _, _, item = _pending(world)
    root = journal.coordinator
    retire = root._retire_nodes

    def change_after_retirement(conn, token):
        retire(conn, token)
        assert store.outbox_mark_sent(item)

    monkeypatch.setattr(root, '_retire_nodes', change_after_retirement)
    with pytest.raises(local_source.SourceChanged, match='outbox_changed'):
        _allocate(journal, store, item)
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (6,)


def test_finish_requires_all_manifest_legs_from_exact_payload(world):
    manifest = {'v': 2, 'blob_id': 'f2-abc', 'record': {}, 'sealed_bytes': 1, 'sealed_sha256': 'a' * 64}
    payload = {'v': 1, 'envelope': {'id': 'message'}, 'attachments': [manifest]}
    store, journal, _, owner, item = _pending(world, payload)
    with pytest.raises(local_source.SourceChanged, match='manifest_unacknowledged'):
        _allocate(journal, store, item, {**_args(item), 'p_record': payload['envelope']})


def test_append_slot_binds_exact_serialized_store_digest(world):
    store, journal, _, _, item = _pending(world)
    _allocate(journal, store, item)
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE outbox SET payload=? WHERE seq=?', (json.dumps(item.payload, indent=2), item.seq))
    with pytest.raises(local_source.SourceChanged, match='outbox_changed'):
        journal.get_outbox_leg(store, item.seq, 'append', item.owner_digest)


@pytest.mark.parametrize('original,changed', [(True, 1), (1, 1.0)])
def test_append_canonical_payload_distinguishes_json_scalar_types(world, original, changed):
    store, journal, _, _, item = _pending(world, {'id': 'message', 'value': original})
    with pytest.raises(local_source.SourceChanged, match='request_changed'):
        _allocate(journal, store, item, {**_args(item), 'p_record': {'id': 'message', 'value': changed}})


def test_final_store_cut_is_held_until_root_commit(world, monkeypatch):
    from contextlib import contextmanager

    store, journal, _, _, item = _pending(world)
    root = journal.coordinator
    transaction = root._transaction
    observed = []

    @contextmanager
    def check_commit(**kwargs):
        with transaction(**kwargs) as conn:
            yield conn
            if conn.execute('SELECT format FROM mutation_root').fetchone() == (8,):
                with sqlite3.connect(store.path, timeout=0) as contender:
                    with pytest.raises(sqlite3.OperationalError, match='locked'):
                        contender.execute('BEGIN IMMEDIATE')
                observed.append(True)

    monkeypatch.setattr(root, '_transaction', check_commit)
    _allocate(journal, store, item)
    assert observed == [True]


def _attachment_pending(world):
    from agentbridge.transport.staged_attachments import _digest
    manifest = {'v': 2, 'blob_id': 'f2-reserved', 'record': {},
                'sealed_bytes': 1, 'sealed_sha256': 'a' * 64}
    payload = {'v': 1, 'envelope': {'id': 'reserved-message'}, 'attachments': [manifest]}
    store, journal, rpc, owner, item = _pending(world, payload)
    args = {'p_chat': 'room', 'p_blob': manifest['blob_id'], 'p_sealed_bytes': 1,
            'p_sealed_sha256': 'a' * 64, 'p_manifest_digest': _digest('room', manifest),
            'p_object_id': '00000000-0000-4000-8000-000000000001'}
    return store, journal, item, args


def _manifest(journal, store, item, args):
    return journal.allocate_outbox_ticket(store, item.seq, 'manifest:f2-reserved',
                                         item.owner_digest, 'blob_manifest', args)


def _terminal(journal, ticket):
    from agentbridge.transport.mutation_receipts import decode_mutation_receipt
    request = journal.get_ticket(ticket).request
    e = request.expectation
    raw = {name: getattr(e, name) for name in (
        'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
    result = dict(e.targets)
    if e.kind == 'log_append':
        result['row_id'] = 1
    raw.update(schema_version=2, provider_digest='c' * 64, result=result)
    journal.mark_attempted(request)
    journal.accept(request, decode_mutation_receipt(raw, e))
    journal.complete(request)
    journal.mark_acknowledged(request)


def test_complete_owner_slot_budget_rejects_before_first_leg(world, monkeypatch):
    store, journal, item, args = _attachment_pending(world)
    monkeypatch.setattr(operation_consumers, 'MAX_CONSUMERS', 1)
    with pytest.raises(local_source.SourceChanged, match='reservation_budget'):
        _manifest(journal, store, item, args)
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (6,)


def test_outstanding_owner_reservation_protects_final_append_record(world, monkeypatch):
    from agentbridge.store import operation_journal
    store, journal, item, args = _attachment_pending(world)
    monkeypatch.setattr(operation_journal, 'MAX_RECORDS', 3)
    manifest = _manifest(journal, store, item, args)
    journal.allocate_ticket('doc_put', {'p_path': 'presence/other.json', 'p_data': {}})
    with pytest.raises(local_source.SourceChanged, match='record_budget'):
        journal.allocate_ticket('doc_put', {'p_path': 'presence/next.json', 'p_data': {}})
    _terminal(journal, manifest)
    ticket = _allocate(journal, store, item, {**_args(item), 'p_record': item.payload['envelope']})
    assert journal.get_ticket(ticket).request.expectation.kind == 'log_append'
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert operation_consumers.reservation_capacity(conn) == (0, 0)
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (3,)


def test_owner_reserves_append_bytes_and_receipts_against_other_writers(world, monkeypatch):
    from agentbridge.store import operation_journal
    store, journal, item, args = _attachment_pending(world)
    _manifest(journal, store, item, args)
    with sqlite3.connect(journal.coordinator.path) as conn:
        count, reserved_bytes = operation_consumers.reservation_capacity(conn)
        pending = conn.execute('SELECT count(*) FROM typed_operations WHERE receipt IS NULL').fetchone()[0]
        capacity = operation_journal._bytes_used(conn) + reserved_bytes + (pending + count) * operation_journal.MAX_RECEIPT_BYTES
    monkeypatch.setattr(operation_journal, 'MAX_BYTES', capacity)
    with pytest.raises(local_source.SourceChanged, match='byte_budget'):
        journal.allocate_ticket('doc_put', {'p_path': 'presence/next.json', 'p_data': {}})
    assert journal.get_outbox_leg(store, item.seq, 'manifest:f2-reserved', item.owner_digest)


def test_cross_store_allocation_reaps_deleted_completed_owner(world, monkeypatch, tmp_path):
    from agentbridge.store.db import Store
    from agentbridge.store import source_selectors
    store, journal, _, owner, item = _pending(world)
    monkeypatch.setattr(operation_consumers, 'MAX_CONSUMERS', 1)
    owner.dispatch(item)
    assert store.outbox_mark_sent(item)
    sent = store.outbox_sent_items()[0]
    owner.release(sent)
    assert store.outbox_finalize_sent(sent)
    other = Store(tmp_path / 'other.sqlite')
    try:
        local_source.initialize(other)
        source_selectors.initialize(other)
        journal.coordinator.register_store(other)
        other.outbox_add('append_log', 'room|alice.jsonl', {'id': 'new-store'})
        fresh = other.outbox_claim_due()[0]
        _allocate(journal, other, fresh)
        with sqlite3.connect(journal.coordinator.path) as conn:
            assert conn.execute('SELECT DISTINCT owner_path FROM typed_outbox_reservations').fetchall() == [(str(other.path),)]
    finally:
        other.close()


def test_explicit_post_delete_reap_retains_live_sent_evidence(world):
    store, journal, _, owner, item = _pending(world)
    owner.dispatch(item)
    assert store.outbox_mark_sent(item)
    sent = store.outbox_sent_items()[0]
    owner.release(sent)
    assert journal.reap_outbox_consumers() == 0
    assert store.outbox_finalize_sent(sent)
    assert journal.reap_outbox_consumers() == 1
    assert journal.reap_outbox_consumers() == 0
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_outbox_reservations').fetchone() == (0,)


def test_outbox_target_partitions_only_first_pipe(world):
    store, journal, _, _, _ = _pending(world)
    store.outbox_add('append_log', 'other|alice|machine.jsonl', {'id': 'pipe'})
    item = store.outbox_claim_due()[0]
    ticket = _allocate(journal, store, item, {'p_chat': 'other', 'p_log': 'alice|machine.jsonl', 'p_record': item.payload})
    assert dict(journal.get_ticket(ticket).request.expectation.targets)['log_name'] == 'alice|machine.jsonl'


def test_format7_owner_upgrade_reserves_missing_legs_before_any_new_owner(world):
    from agentbridge.store.mutation_coordinator import _V7_ROOT_SCHEMA
    store, journal, item, args = _attachment_pending(world)
    ticket = _manifest(journal, store, item, args)
    with sqlite3.connect(journal.coordinator.path) as conn:
        conn.execute('DROP TABLE typed_outbox_reservations')
        conn.execute('ALTER TABLE mutation_root RENAME TO old_root')
        conn.execute(_V7_ROOT_SCHEMA)
        conn.execute('INSERT INTO mutation_root SELECT singleton,identity,epoch,7,revision FROM old_root')
        conn.execute('DROP TABLE old_root')
    restored = cases._reload(world)
    assert _manifest(restored, store, item, args) == ticket
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (8,)
        assert operation_consumers.reservation_capacity(conn)[0] == 1


@pytest.mark.parametrize('kind', ['append', 'manifest'])
def test_first_owner_reservation_rejects_prior_stable_operation(world, kind):
    from uuid import uuid5
    from agentbridge.transport.application_mutations import stable_append_id
    from agentbridge.transport.staged_attachments import _MANIFEST_NAMESPACE, _digest
    from agentbridge.transport.typed_mutations import prepare_mutation
    journal = world[-1]
    manifest = {'v': 2, 'blob_id': 'f2-reserved', 'record': {},
                'sealed_bytes': 1, 'sealed_sha256': 'a' * 64}
    args = {'p_chat': 'room', 'p_blob': 'f2-reserved', 'p_sealed_bytes': 1,
            'p_sealed_sha256': 'a' * 64, 'p_manifest_digest': _digest('room', manifest),
            'p_object_id': '00000000-0000-4000-8000-000000000001'}
    if kind == 'append':
        old_id = stable_append_id('room', 'alice.jsonl', 'reserved-message')
        old_kind, old_args = 'log_append', {'p_chat': 'room', 'p_log': 'alice.jsonl', 'p_record': {'id': 'reserved-message'}}
    else:
        old_id = str(uuid5(_MANIFEST_NAMESPACE, json.dumps(['mesh', 'room', 'f2-reserved'], separators=(',', ':'), ensure_ascii=False)))
        old_kind, old_args = 'blob_manifest', args
    journal.prepare(prepare_mutation('mesh', cases.EPOCH, old_id, cases.ACTOR, old_kind, old_args))
    store, journal, item, args = _attachment_pending(world)
    with pytest.raises(local_source.SourceChanged, match='legacy_' + kind + '_requires_drain'):
        _manifest(journal, store, item, args)
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (1,)
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (6,)


def test_generic_writer_cannot_steal_unadopted_format7_capacity(world, monkeypatch):
    from agentbridge.store import operation_journal
    from agentbridge.store.mutation_coordinator import _V7_ROOT_SCHEMA
    store, journal, item, args = _attachment_pending(world)
    _manifest(journal, store, item, args)
    with sqlite3.connect(journal.coordinator.path) as conn:
        conn.execute('DROP TABLE typed_outbox_reservations')
        conn.execute('ALTER TABLE mutation_root RENAME TO old_root')
        conn.execute(_V7_ROOT_SCHEMA)
        conn.execute('INSERT INTO mutation_root SELECT singleton,identity,epoch,7,revision FROM old_root')
        conn.execute('DROP TABLE old_root')
    monkeypatch.setattr(operation_journal, 'MAX_RECORDS', 2)
    with pytest.raises(local_source.SourceChanged, match='record_budget'):
        journal.allocate_ticket('doc_put', {'p_path': 'presence/steal.json', 'p_data': {}})
    with sqlite3.connect(journal.coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (1,)
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (7,)
