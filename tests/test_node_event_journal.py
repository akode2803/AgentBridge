"""Atomic bounded work evidence for node-admitted records."""
from __future__ import annotations

import json

import pytest

from agentbridge.store import coupled_payload_stage, node_event_journal
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
