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
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (7,)
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
            if conn.execute('SELECT format FROM mutation_root').fetchone() == (7,):
                with sqlite3.connect(store.path, timeout=0) as contender:
                    with pytest.raises(sqlite3.OperationalError, match='locked'):
                        contender.execute('BEGIN IMMEDIATE')
                observed.append(True)

    monkeypatch.setattr(root, '_transaction', check_commit)
    _allocate(journal, store, item)
    assert observed == [True]
