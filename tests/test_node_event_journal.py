"""Atomic bounded work evidence for node-admitted records."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentbridge.store import coupled_payload_stage, node_event_journal, page_inputs
from agentbridge.store.db import Store


CHAT = "room"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    coupled_payload_stage.initialize(value)
    node_event_journal.initialize(value)
    try:
        yield value
    finally:
        value.close()


def _record(index: int) -> dict:
    return {
        "id": f"m{index:04}", "ns": index, "from": "alice",
        "kind": "message", "text": str(index),
    }


def _stage(conn, stage: str, records: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO coupled_payload_messages VALUES(?,?,?,?,?,?,?)",
        ((stage, rec["id"], rec["ns"], rec["from"], rec["kind"],
          json.dumps(rec, separators=(",", ":")), len(json.dumps(rec)))
         for rec in records),
    )
    conn.commit()


def _publish_and_admit(store, stage: str, records: list[dict]) -> None:
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    node_event_journal.publish_in_transaction(
        conn, store, stage=stage, chat_id=CHAT,
    )
    store._insert_messages(conn, CHAT, records, observed_ns=1)
    conn.commit()


def test_publication_caps_pending_identities_and_preserves_delivery_highwater(
        store):
    count = node_event_journal.MAX_PENDING_EVENTS + 1
    records = [_record(index) for index in range(count)]
    conn = store._conn()
    _stage(conn, "stage", records)
    _publish_and_admit(store, "stage", records)

    claimed = []
    saw_gap = False
    while True:
        page = node_event_journal.claim_events(store)
        if page is None:
            break
        saw_gap = saw_gap or page.gap
        claimed.extend(item.record["id"] for item in page.records)

    assert saw_gap
    assert len(claimed) == node_event_journal.MAX_PENDING_EVENTS
    assert claimed[0] == "m0001" and claimed[-1] == f"m{count - 1:04}"
    assert node_event_journal.pending_deliveries(store) == (
        node_event_journal.NodeDelivery(CHAT, count - 1),
    )


def test_missing_or_moved_admitted_identity_becomes_one_at_most_once_gap(store):
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items(chat,message_id,ns,sender,kind) "
        "VALUES(?,?,?,?,?)",
        (CHAT, "missing", 1, "alice", "message"),
    )
    conn.commit()

    claim = node_event_journal.claim_events(store)

    assert claim == node_event_journal.NodeEventClaim((), True)
    assert node_event_journal.claim_events(store) is None


def test_claim_preflights_aggregate_payload_bytes_before_loading(store):
    size = node_event_journal.MAX_CLAIM_BYTES // 2 + 1024
    records = [
        {**_record(1), "text": "a" * size},
        {**_record(2), "text": "b" * size},
    ]
    for chat, record in (("a", records[0]), ("b", records[1])):
        store.upsert_messages(chat, (record,), observed_ns=1)
        store._conn().execute(
            "INSERT INTO node_event_journal_items"
            "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
            (chat, record["id"], record["ns"], record["from"], record["kind"]),
        )
    store._conn().commit()

    first = node_event_journal.claim_events(store, limit=2)
    second = node_event_journal.claim_events(store, limit=2)

    assert first is not None and len(first.records) == 1 and not first.gap
    assert second is not None and len(second.records) == 1 and not second.gap
    assert node_event_journal.claim_events(store) is None


def test_oversize_current_payload_is_consumed_as_gap_without_decode(store):
    record = _record(1)
    store.upsert_messages(CHAT, (record,), observed_ns=1)
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, record["id"], record["ns"], record["from"], record["kind"]),
    )
    conn.execute(
        "UPDATE messages SET payload=? WHERE chat_id=? AND id=?",
        ("x" * (node_event_journal.MAX_CLAIM_BYTES + 1), CHAT, record["id"]),
    )
    conn.commit()

    assert node_event_journal.claim_events(store) == (
        node_event_journal.NodeEventClaim((), True)
    )
    assert node_event_journal.claim_events(store) is None


def test_remove_reintroduce_aba_keeps_one_identity_and_sets_gap(store):
    record = _record(1)
    conn = store._conn()
    _stage(conn, "stage-a", [record])
    _publish_and_admit(store, "stage-a", [record])
    conn.execute("DELETE FROM messages WHERE chat_id=? AND id=?", (CHAT, record["id"]))
    conn.commit()
    _stage(conn, "stage-b", [record])
    _publish_and_admit(store, "stage-b", [record])

    claim = node_event_journal.claim_events(store)

    assert claim is not None and claim.gap
    assert [item.record["id"] for item in claim.records] == [record["id"]]
    assert node_event_journal.claim_events(store) is None


def test_projection_peek_is_payload_free_single_chat_and_byte_bounded(store):
    large = page_inputs.MAX_BYTES // 2 + 1024
    records = [
        {**_record(1), 'text': 'a' * large},
        {**_record(2), 'text': 'b' * large},
        _record(3),
    ]
    store.upsert_messages(CHAT, records, observed_ns=1)
    other = _record(4)
    store.upsert_messages('other', [other], observed_ns=1)
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ((CHAT, row['id'], row['ns'], row['from'], row['kind']) for row in records),
    )
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ('other', other['id'], other['ns'], other['from'], other['kind']),
    )
    conn.commit()

    first = node_event_journal.peek_event_identities(store)

    assert [item.message_id for item in first] == ['m0001']
    assert all(item.chat_id == CHAT for item in first)
    assert not hasattr(first[0], 'record')


def test_projection_peek_returns_missing_or_oversize_head_for_gap_retirement(store):
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ((CHAT, 'missing', 1, 'alice', 'message'),
         (CHAT, 'later', 2, 'alice', 'message')),
    )
    conn.commit()

    page = node_event_journal.peek_event_identities(store)

    assert [item.message_id for item in page] == ['missing', 'later']


def test_exact_projection_consumption_is_transactional_and_cas_safe(store):
    records = [_record(1), _record(2)]
    store.upsert_messages(CHAT, records, observed_ns=1)
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ((CHAT, row['id'], row['ns'], row['from'], row['kind']) for row in records),
    )
    conn.commit()
    expected = node_event_journal.peek_event_identities(store)

    conn.execute('BEGIN IMMEDIATE')
    consumed = node_event_journal.consume_event_identities_in_transaction(
        conn, store, expected,
    )
    assert consumed == node_event_journal.NodeEventConsume(True, False)
    conn.rollback()
    assert node_event_journal.peek_event_identities(store) == expected

    conn.execute(
        "UPDATE node_event_journal_items SET ns=99 WHERE sequence=?",
        (expected[0].sequence,),
    )
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    stale = node_event_journal.consume_event_identities_in_transaction(
        conn, store, expected,
    )
    conn.commit()
    assert stale == node_event_journal.NodeEventConsume(False, False)
    assert len(node_event_journal.peek_event_identities(store)) == 2


@pytest.mark.parametrize('mutation', ['missing', 'moved', 'oversize'])
def test_projection_consumes_unprojectable_identity_as_durable_gap(store, mutation):
    record = _record(1)
    store.upsert_messages(CHAT, [record], observed_ns=1)
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, record['id'], record['ns'], record['from'], record['kind']),
    )
    conn.commit()
    expected = node_event_journal.peek_event_identities(store)
    if mutation == 'missing':
        conn.execute('DELETE FROM messages WHERE chat_id=? AND id=?', (CHAT, record['id']))
    elif mutation == 'moved':
        conn.execute(
            'UPDATE messages SET ns=ns+1 WHERE chat_id=? AND id=?',
            (CHAT, record['id']),
        )
    else:
        conn.execute(
            'UPDATE messages SET payload=? WHERE chat_id=? AND id=?',
            ('x' * (page_inputs.MAX_BYTES + 1), CHAT, record['id']),
        )
    conn.commit()

    conn.execute('BEGIN IMMEDIATE')
    result = node_event_journal.consume_event_identities_in_transaction(
        conn, store, expected,
    )
    conn.commit()

    assert result == node_event_journal.NodeEventConsume(True, True)
    assert node_event_journal.peek_event_identities(store) == ()
    assert node_event_journal.claim_gap(store)
    assert not node_event_journal.claim_gap(store)


def test_gap_claim_is_independent_of_pending_projection_rows(store):
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, 'missing', 1, 'alice', 'message'),
    )
    conn.execute(
        'UPDATE node_event_journal_state SET gap=1 WHERE singleton=1',
    )
    conn.commit()

    assert node_event_journal.claim_gap(store)
    assert [item.message_id for item in node_event_journal.peek_event_identities(store)] == [
        'missing',
    ]


def test_projection_peek_caps_one_chat_at_thirty_two_identities(store):
    records = [_record(index) for index in range(1, 41)]
    store.upsert_messages(CHAT, records, observed_ns=1)
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ((CHAT, row['id'], row['ns'], row['from'], row['kind']) for row in records),
    )
    conn.commit()

    page = node_event_journal.peek_event_identities(store)

    assert len(page) == node_event_journal.MAX_PROJECT_EVENTS
    assert [item.message_id for item in page[:2]] == ['m0001', 'm0002']


def test_projection_consume_requires_owned_active_transaction(store, tmp_path):
    record = _record(1)
    store.upsert_messages(CHAT, [record], observed_ns=1)
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, record['id'], record['ns'], record['from'], record['kind']),
    )
    conn.commit()
    expected = node_event_journal.peek_event_identities(store)

    with pytest.raises(sqlite3.OperationalError, match='requires a Store transaction'):
        node_event_journal.consume_event_identities_in_transaction(
            conn, store, expected,
        )

    other = Store(tmp_path / 'other.sqlite')
    coupled_payload_stage.initialize(other)
    node_event_journal.initialize(other)
    try:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='another Store'):
            node_event_journal.consume_event_identities_in_transaction(
                conn, other, expected,
            )
        conn.rollback()
    finally:
        other.close()


def test_terminal_gap_retirement_is_exact_cas_and_does_not_touch_newer_aba(store):
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, 'blocked', 1, 'alice', 'message'),
    )
    conn.commit()
    expected = node_event_journal.peek_event_identities(store)
    conn.execute(
        'UPDATE node_event_journal_items SET ns=2 WHERE sequence=?',
        (expected[0].sequence,),
    )
    conn.commit()

    assert not node_event_journal.retire_event_identities_as_gap(store, expected)
    current = node_event_journal.peek_event_identities(store)
    assert len(current) == 1 and current[0].ns == 2
    assert not node_event_journal.claim_gap(store)

    assert node_event_journal.retire_event_identities_as_gap(store, current)
    assert node_event_journal.peek_event_identities(store) == ()
    assert node_event_journal.claim_gap(store)


def test_terminal_gap_retirement_rejects_multiple_identities(store):
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ((CHAT, 'blocked-1', 1, 'alice', 'message'),
         (CHAT, 'blocked-2', 2, 'alice', 'message')),
    )
    conn.commit()
    expected = node_event_journal.peek_event_identities(store)

    with pytest.raises(
        ValueError, match="terminal node event retirement requires one identity"
    ):
        node_event_journal.retire_event_identities_as_gap(store, expected)

    assert node_event_journal.peek_event_identities(store) == expected
    assert not node_event_journal.claim_gap(store)


def test_delivery_completion_cannot_erase_a_concurrently_newer_highwater(store):
    conn = store._conn()
    conn.execute("INSERT INTO node_event_delivery VALUES(?,?)", (CHAT, 5))
    conn.commit()
    stale = node_event_journal.pending_deliveries(store)[0]
    conn.execute("UPDATE node_event_delivery SET max_ns=6 WHERE chat=?", (CHAT,))
    conn.commit()

    assert not node_event_journal.complete_delivery(store, stale)
    current = node_event_journal.pending_deliveries(store)
    assert current == (node_event_journal.NodeDelivery(CHAT, 6),)
    assert node_event_journal.complete_delivery(store, current[0])
    assert node_event_journal.pending_deliveries(store) == ()


def test_delivery_page_rotates_in_bounded_chat_order(store):
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_delivery VALUES(?,?)",
        ((f"chat-{index:02}", index + 1) for index in range(35)),
    )
    conn.commit()

    first = node_event_journal.delivery_page(store)
    second = node_event_journal.delivery_page(
        store, after_chat=first.deliveries[-1].chat_id,
    )

    assert len(first.deliveries) == node_event_journal.MAX_DELIVERIES
    assert first.has_more
    assert [item.chat_id for item in second.deliveries[:3]] == [
        "chat-32", "chat-33", "chat-34",
    ]
    assert second.deliveries[3].chat_id == "chat-00"
    assert second.has_more


def test_publication_failure_rolls_back_event_and_delivery_work(store):
    rec = _record(1)
    conn = store._conn()
    conn.execute(
        "INSERT INTO coupled_payload_messages VALUES(?,?,?,?,?,?,?)",
        ("stage", rec["id"], rec["ns"], rec["from"], rec["kind"],
         json.dumps(rec, separators=(",", ":")), len(json.dumps(rec))),
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    node_event_journal.publish_in_transaction(
        conn, store, stage="stage", chat_id=CHAT,
    )
    conn.rollback()

    assert node_event_journal.claim_events(store) is None
    assert node_event_journal.pending_deliveries(store) == ()


def test_unowned_trigger_is_rejected_before_it_can_discard_event_work(store):
    record = _record(1)
    conn = store._conn()
    _stage(conn, "stage", [record])
    conn.execute(
        "CREATE TRIGGER discard_node_event AFTER INSERT ON "
        "node_event_journal_items BEGIN DELETE FROM node_event_journal_items; END",
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(
            node_event_journal.NodeEventJournalChanged,
            match="objects changed"):
        node_event_journal.publish_in_transaction(
            conn, store, stage="stage", chat_id=CHAT,
        )
    conn.rollback()
    assert conn.execute(
        "SELECT count(*) FROM node_event_journal_items",
    ).fetchone() == (0,)


def test_partial_or_mutated_journal_namespace_fails_closed(store):
    conn = store._conn()
    conn.execute("DROP TABLE node_event_delivery")
    conn.commit()

    with pytest.raises(
            node_event_journal.NodeEventJournalChanged,
            match="schema changed"):
        node_event_journal.pending_deliveries(store)


def test_overfull_exact_schema_is_rejected_before_publication_work(store):
    conn = store._conn()
    count = node_event_journal.MAX_PENDING_EVENTS + 1
    conn.executemany(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        ((CHAT, f"pending-{index}", index, "alice", "message")
         for index in range(count)),
    )
    conn.commit()
    record = _record(count + 1)
    _stage(conn, "stage", [record])
    conn.execute("BEGIN IMMEDIATE")

    with pytest.raises(
            node_event_journal.NodeEventJournalChanged,
            match="capacity changed"):
        node_event_journal.publish_in_transaction(
            conn, store, stage="stage", chat_id=CHAT,
        )
    conn.rollback()

    assert conn.execute(
        "SELECT count(*) FROM node_event_journal_items",
    ).fetchone() == (count,)
    assert node_event_journal.pending_deliveries(store) == ()
