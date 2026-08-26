"""Store: cache idempotency, offsets/cursors, and outbox durability."""

import sqlite3
import threading

import pytest

from agentbridge.store.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "cache.sqlite")
    yield s
    s.close()


def test_upsert_idempotent_and_ordered(store):
    recs = [
        {"id": "m2", "ns": 20, "from": "b", "kind": "message"},
        {"id": "m1", "ns": 10, "from": "a", "kind": "message"},
    ]
    assert len(store.upsert_messages("c1", recs)) == 2
    # replay (shrunk-file re-read / at-least-once send) adds nothing —
    # and the returned list is what the event pump publishes, so empty here
    assert store.upsert_messages("c1", recs) == []
    got = store.messages("c1")
    assert [m["id"] for m in got] == ["m1", "m2"]  # ns order, not insert order
    assert store.messages("c1", after_ns=10) == [recs[0]]
    assert store.message_count("c1") == 2


def test_message_observation_is_first_ingestion_evidence(store):
    rec = {"id": "m1", "ns": 10, "from": "a", "kind": "message"}
    assert store.upsert_messages(
        "c1", [rec], observed_ns=123, observed_mono=456,
        observed_clock="clock-a") == [rec]
    assert store.message_observation("c1", "m1") == (123, 456, "clock-a")
    assert store.upsert_messages(
        "c1", [rec], observed_ns=999, observed_clock="clock-b") == []
    assert store.message_observation("c1", "m1") == (123, 456, "clock-a")


def test_state_events_after_ignores_messages_and_reaction_breadcrumbs(store):
    store.upsert_messages("c1", [
        {"id": "i1", "ns": 10, "kind": "info", "event": {"type": "created"}},
        {"id": "m1", "ns": 30, "kind": "message", "body": "newer"},
        {"id": "i2", "ns": 20, "kind": "info", "event": {"type": "renamed"}},
        {"id": "i3", "ns": 40, "kind": "info", "event": {"type": "reaction"}},
    ])
    assert [row["id"] for row in store.state_events_after("c1", 10)] == ["i2"]
    assert store.state_events_after("missing", 0) == []


def test_malformed_records_skipped(store):
    ins = store.upsert_messages("c1", [{"id": "ok", "ns": 1}, {"ns": 2}, {"id": "x"}])
    assert [r["id"] for r in ins] == ["ok"] and store.message_count("c1") == 1


def test_offsets_and_cursors(store):
    assert store.get_offset("c1", "a@m") == 0
    store.set_offset("c1", "a@m", 512)
    store.set_offset("c1", "a@m", 1024)
    assert store.get_offset("c1", "a@m") == 1024

    assert store.get_cursor("read", "c1") == 0
    store.set_cursor("read", "c1", 999)
    assert store.get_cursor("read", "c1") == 999


def test_doc_cache_roundtrip(store):
    assert store.cached_doc("users/aryan.json") is None
    store.cache_doc("users/aryan.json", {"name": "aryan"})
    assert store.cached_doc("users/aryan.json")["name"] == "aryan"


def test_forget_chat(store):
    store.upsert_messages("c1", [{"id": "m1", "ns": 1}])
    store.set_offset("c1", "a@m", 10)
    store.forget_chat("c1")
    assert store.message_count("c1") == 0 and store.get_offset("c1", "a@m") == 0


def test_outbox_claim_lease_done(store):
    seq = store.outbox_add("post", "c1", {"id": "m1", "body": "hi"})
    items = store.outbox_claim_due()
    assert [i.seq for i in items] == [seq]
    # leased: a second claim while the lease is live returns nothing
    assert store.outbox_claim_due() == []
    store.outbox_done(seq)
    assert store.outbox_counts() == {}


def test_message_cache_and_send_intent_are_one_transaction(store):
    store._conn().execute(
        "CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox "
        "BEGIN SELECT RAISE(ABORT, 'intent failed'); END")
    record = {"id": "m-atomic", "ns": 10, "from": "aryan",
              "kind": "message"}
    with pytest.raises(sqlite3.IntegrityError):
        store.cache_and_outbox_add(
            "c1", record, "append_log", "c1|aryan@box", record)
    assert store.message_count("c1") == 0
    assert store.outbox_counts() == {}


def test_outbox_survives_restart(store, tmp_path):
    """The 'no message ever lost' core: enqueue, crash before flush, reopen."""
    store.outbox_add("post", "c1", {"id": "m1"})
    store.close()  # simulated crash/restart boundary
    s2 = Store(tmp_path / "cache.sqlite")
    items = s2.outbox_claim_due()
    assert len(items) == 1 and items[0].payload["id"] == "m1"
    s2.close()


def test_outbox_expired_lease_reclaimable(store):
    store.outbox_add("post", "c1", {"id": "m1"})
    assert len(store.outbox_claim_due(lease_s=0.0)) == 1  # lease expires instantly
    again = store.outbox_claim_due()  # crashed sender's item comes back
    assert len(again) == 1 and again[0].attempts == 0


def test_outbox_retry_schedules_future(store):
    seq = store.outbox_add("post", "c1", {"id": "m1"})
    store.outbox_claim_due()
    store.outbox_retry(seq, "boom", delay_s=60.0)
    assert store.outbox_claim_due() == []  # not due yet
    counts = store.outbox_counts()
    assert counts.get("pending") == 1


def test_outbox_dead(store):
    seq = store.outbox_add("???", "c1", {})
    store.outbox_dead(seq, "no handler")
    assert store.outbox_claim_due() == []
    assert store.outbox_counts() == {"dead": 1}


def test_dead_outbox_rows_are_bounded_without_touching_pending(store):
    pending = store.outbox_add("post", "c1", {"id": "pending"})
    for i in range(4):
        seq = store.outbox_add("bad", "c1", {"id": f"dead-{i}"})
        store.outbox_dead(seq, "bad")
    removed = store.outbox_prune_dead(max_age_s=10**9, max_rows=2)
    assert len(removed) == 2
    assert store.outbox_counts() == {"dead": 2, "pending": 1}
    assert any(i.seq == pending for i in store.outbox_claim_due())


def test_store_multithreaded_writes(store):
    def burst(tag):
        for i in range(50):
            store.upsert_messages("mt", [{"id": f"{tag}-{i}", "ns": i + 1}])

    threads = [threading.Thread(target=burst, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.message_count("mt") == 200


def test_message_column_migration_is_concurrent_start_safe(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE messages(chat_id TEXT NOT NULL,id TEXT NOT NULL,"
            "ns INTEGER NOT NULL,sender TEXT NOT NULL DEFAULT '',"
            "kind TEXT NOT NULL DEFAULT 'message',payload TEXT NOT NULL,"
            "PRIMARY KEY(chat_id,id))")
    barrier = threading.Barrier(8)
    errors = []

    def open_store():
        try:
            barrier.wait()
            opened = Store(path)
            opened.close()
        except Exception as exc:  # pragma: no cover - assertion reports detail
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert {"observed_ns", "observed_mono", "observed_clock"} <= columns
