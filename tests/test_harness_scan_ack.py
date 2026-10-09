"""Durable harness scan acknowledgement contracts."""

from __future__ import annotations

import sqlite3

import pytest

from agentbridge.store import Store
from agentbridge.store import db, harness_scan_ack, log_position


def _record(message_id: str, *, ns: int = 1):
    return {
        "id": message_id,
        "ns": ns,
        "from": "writer",
        "kind": "message",
        "body": message_id,
    }


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    yield value
    value.close()


def test_exact_acknowledgement_retires_pending_generation(store):
    store.upsert_messages("room", [_record("one")])
    expected = store.capture_membership_input_position("room")
    assert store.pending_harness_scans("helper") == harness_scan_ack.PendingHarnessScans(
        (expected,), False,
    )

    assert store.acknowledge_harness_scan("helper", expected) is True
    assert store.pending_harness_scans("helper") == harness_scan_ack.PendingHarnessScans(
        (), False,
    )


def test_new_message_after_ack_is_pending_and_stale_ack_cannot_hide_it(store):
    store.upsert_messages("room", [_record("one")])
    stale = store.capture_membership_input_position("room")
    assert store.acknowledge_harness_scan("helper", stale) is True

    store.upsert_messages("room", [_record("two", ns=2)])
    current = store.capture_membership_input_position("room")
    assert current.generation > stale.generation
    assert store.acknowledge_harness_scan("helper", stale) is False
    assert store.pending_harness_scans("helper").positions == (current,)


def test_acknowledgement_is_isolated_per_agent(store):
    store.upsert_messages("room", [_record("one")])
    expected = store.capture_membership_input_position("room")

    assert store.acknowledge_harness_scan("helper", expected) is True
    assert store.pending_harness_scans("helper").positions == ()
    assert store.pending_harness_scans("reviewer").positions == (expected,)

    assert store.acknowledge_harness_scan("reviewer", expected) is True
    assert store.pending_harness_scans("reviewer").positions == ()


def test_existing_message_database_is_seeded_pending_on_upgrade(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(db._SCHEMA)
        log_position.initialize(conn)
        with conn:
            conn.execute(
                "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) "
                "VALUES('room','old',1,'writer','message','{}')"
            )
    finally:
        conn.close()

    migrated = Store(path)
    try:
        pending = migrated.pending_harness_scans("helper").positions
        assert len(pending) == 1
        assert pending[0].chat_id == "room"
        assert pending[0].generation == 1
    finally:
        migrated.close()


def test_database_recreation_rejects_old_ack_position(tmp_path):
    path = tmp_path / "store.sqlite"
    original = Store(path)
    original.upsert_messages("room", [_record("old")])
    old = original.capture_membership_input_position("room")
    assert original.acknowledge_harness_scan("helper", old) is True
    original.close()

    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"store.sqlite{suffix}").unlink(missing_ok=True)

    recreated = Store(path)
    try:
        recreated.upsert_messages("room", [_record("new")])
        current = recreated.capture_membership_input_position("room")
        assert current.incarnation != old.incarnation
        assert recreated.acknowledge_harness_scan("helper", old) is False
        assert recreated.pending_harness_scans("helper").positions == (current,)
    finally:
        recreated.close()


def test_pending_is_sorted_and_bounded(store):
    for chat_id in ("room-c", "room-a", "room-b"):
        store.upsert_messages(chat_id, [_record(chat_id)])

    page = store.pending_harness_scans("helper", limit=2)
    assert [p.chat_id for p in page.positions] == [
        "room-a", "room-b",
    ]
    assert page.has_more is True
    assert store.pending_harness_scans("helper", limit=3).has_more is False
    with pytest.raises(ValueError):
        store.pending_harness_scans("helper", limit=0)
    with pytest.raises(ValueError):
        store.pending_harness_scans(
            "helper", limit=harness_scan_ack.MAX_PENDING + 1,
        )


def test_incompatible_ack_table_fails_store_initialization(tmp_path):
    path = tmp_path / "broken.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE harness_scan_ack(chat_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        harness_scan_ack.HarnessScanAckUnavailable,
        match="incompatible harness scan acknowledgement table",
    ):
        Store(path)


def test_acknowledgement_rejects_foreign_database_position(tmp_path):
    first = Store(tmp_path / "first.sqlite")
    second = Store(tmp_path / "second.sqlite")
    try:
        first.upsert_messages("room", [_record("one")])
        position = first.capture_membership_input_position("room")
        with pytest.raises(ValueError, match="another database"):
            second.acknowledge_harness_scan("helper", position)
    finally:
        first.close()
        second.close()
