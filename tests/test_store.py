"""Store: cache idempotency, offsets/cursors, and outbox durability."""

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
