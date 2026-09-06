"""R165 reset-aware, store-scoped log-position regressions."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from agentbridge.store.db import LogIngestionConflict, Store
from agentbridge.store.log_position import LogPosition


def _record(message_id: str, ns: int = 1) -> dict:
    return {
        "id": message_id,
        "ns": ns,
        "from": "ann",
        "kind": "message",
        "body": message_id,
    }


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "cache.sqlite")
    yield opened
    opened.close()


def test_capture_is_frozen_resolved_and_reads_all_fields_once(store):
    statements: list[str] = []
    store._conn().set_trace_callback(statements.append)

    position = store.capture_log_position("chat", "ann@box")

    assert position == LogPosition(
        database_path=str(store.path.resolve()),
        incarnation=position.incarnation,
        chat_id="chat",
        log_name="ann@box",
        reset_generation=0,
        revision=0,
        offset=0,
    )
    assert position.incarnation
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert all(name in selects[0] for name in (
        "ingestion_identity", "ingestion_chat_resets",
        "ingestion_log_revisions", "log_offsets",
    ))
    with pytest.raises(FrozenInstanceError):
        position.offset = 1


def test_offset_insert_update_delete_and_aba_invalidate_old_token(store):
    original = store.capture_log_position("chat", "ann@box")

    store.set_offset("chat", "ann@box", 0)
    inserted = store.capture_log_position("chat", "ann@box")
    store.set_offset("chat", "ann@box", 0)
    same_value = store.capture_log_position("chat", "ann@box")
    store.set_offset("chat", "ann@box", 9)
    store.set_offset("chat", "ann@box", 0)
    aba = store.capture_log_position("chat", "ann@box")
    with store._conn() as conn:
        conn.execute(
            "DELETE FROM log_offsets WHERE chat_id=? AND log_name=?",
            ("chat", "ann@box"),
        )
    deleted = store.capture_log_position("chat", "ann@box")

    assert (inserted.revision, same_value.revision, aba.revision,
            deleted.revision) == (1, 2, 4, 5)
    assert original.offset == aba.offset == deleted.offset == 0
    with pytest.raises(LogIngestionConflict):
        store.ingest_log("chat", "ann@box", original, 1, [_record("stale")])
    assert store.message_count("chat") == 0


def test_raw_sql_offset_key_move_invalidates_old_and_new_scopes(store):
    store.set_offset("chat", "old@box", 0)
    old_scope = store.capture_log_position("chat", "old@box")
    new_scope = store.capture_log_position("chat", "new@box")

    with store._conn() as conn:
        conn.execute(
            "UPDATE log_offsets SET log_name=? WHERE chat_id=? AND log_name=?",
            ("new@box", "chat", "old@box"),
        )

    moved_from = store.capture_log_position("chat", "old@box")
    moved_to = store.capture_log_position("chat", "new@box")
    assert moved_from.offset == old_scope.offset == 0
    assert moved_from.revision == old_scope.revision + 1
    assert moved_to.revision == new_scope.revision + 1
    for log_name, stale in (("old@box", old_scope), ("new@box", new_scope)):
        with pytest.raises(LogIngestionConflict):
            store.ingest_log("chat", log_name, stale, 1, [])


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "nonempty"])
def test_forget_chat_invalidates_old_scan_and_fresh_scan_recovers(store, populated):
    if populated:
        position = store.capture_log_position("chat", "ann@box")
        store.ingest_log("chat", "ann@box", position, 7, [_record("old")])
    stale = store.capture_log_position("chat", "ann@box")
    store.cache_doc("trust/peer.json", {"trusted": True})
    outbox_seq = store.outbox_add("append_log", "chat|ann@box", {"id": "kept"})

    store.forget_chat("chat")

    reset = store.capture_log_position("chat", "ann@box")
    assert reset.reset_generation == stale.reset_generation + 1
    assert reset.offset == 0
    assert store.message_count("chat") == 0
    assert store.cached_doc("trust/peer.json") == {"trusted": True}
    assert [item.seq for item in store.outbox_claim_due()] == [outbox_seq]
    with pytest.raises(LogIngestionConflict):
        store.ingest_log("chat", "ann@box", stale, 4, [_record("stale")])

    fresh = store.capture_log_position("chat", "ann@box")
    assert store.ingest_log(
        "chat", "ann@box", fresh, 4, [_record("fresh", 2)]) == [
            _record("fresh", 2)
        ]


def test_position_changes_are_visible_after_reopen_and_across_connections(tmp_path):
    path = tmp_path / "cache.sqlite"
    first = Store(path)
    second = Store(path)
    try:
        before = second.capture_log_position("chat", "ann@box")
        first.set_offset("chat", "ann@box", 12)
        across_connection = second.capture_log_position("chat", "ann@box")
        assert across_connection.offset == 12
        assert across_connection.revision == before.revision + 1
        assert across_connection.incarnation == before.incarnation
    finally:
        second.close()
        first.close()

    reopened = Store(path)
    try:
        after_reopen = reopened.capture_log_position("chat", "ann@box")
        assert after_reopen == across_connection
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("chat_id", "log_name"),
    [("other-chat", "ann@box"), ("chat", "other@box")],
)
def test_position_rejects_different_chat_or_log(store, chat_id, log_name):
    position = store.capture_log_position("chat", "ann@box")
    with pytest.raises(LogIngestionConflict):
        store.ingest_log(chat_id, log_name, position, 1, [_record("wrong-scope")])
    assert store.message_count(chat_id) == 0


def test_unrelated_log_revision_does_not_invalidate_position(store):
    position = store.capture_log_position("chat", "ann@box")
    store.set_offset("chat", "other@box", 8)

    assert store.ingest_log(
        "chat", "ann@box", position, 3, [_record("accepted")]
    ) == [_record("accepted")]


def test_copied_database_at_different_path_rejects_source_position(tmp_path):
    source_path = tmp_path / "source.sqlite"
    copied_path = tmp_path / "copied.sqlite"
    source = Store(source_path)
    position = source.capture_log_position("chat", "ann@box")
    source.close()
    shutil.copy2(source_path, copied_path)

    copied = Store(copied_path)
    try:
        copied_position = copied.capture_log_position("chat", "ann@box")
        assert copied_position.incarnation == position.incarnation
        assert copied_position.database_path != position.database_path
        with pytest.raises(LogIngestionConflict):
            copied.ingest_log("chat", "ann@box", position, 1, [_record("wrong-db")])
    finally:
        copied.close()


def test_recreated_database_at_same_path_changes_incarnation(tmp_path):
    path = tmp_path / "cache.sqlite"
    original = Store(path)
    position = original.capture_log_position("chat", "ann@box")
    original.close()
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)

    recreated = Store(path)
    try:
        replacement = recreated.capture_log_position("chat", "ann@box")
        assert replacement.database_path == position.database_path
        assert replacement.incarnation != position.incarnation
        with pytest.raises(LogIngestionConflict):
            recreated.ingest_log(
                "chat", "ann@box", position, 1, [_record("old-incarnation")])
    finally:
        recreated.close()


def test_forget_chat_failure_rolls_back_reset_and_deletions(store):
    position = store.capture_log_position("chat", "ann@box")
    store.ingest_log("chat", "ann@box", position, 6, [_record("retained")])
    before = store.capture_log_position("chat", "ann@box")
    with store._conn() as conn:
        conn.execute(
            "CREATE TRIGGER reject_forget BEFORE DELETE ON messages "
            "BEGIN SELECT RAISE(ABORT, 'forget failed'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forget failed"):
        store.forget_chat("chat")

    assert store.capture_log_position("chat", "ann@box") == before
    assert [row["id"] for row in store.messages("chat")] == ["retained"]


def test_counter_overflow_fails_closed_and_rolls_back_work(store):
    position = store.capture_log_position("log-chat", "ann@box")
    store.ingest_log("log-chat", "ann@box", position, 1, [])
    with store._conn() as conn:
        conn.execute(
            "UPDATE ingestion_log_revisions SET revision=? "
            "WHERE chat_id=? AND log_name=?",
            (2**63 - 1, "log-chat", "ann@box"),
        )
    max_revision = store.capture_log_position("log-chat", "ann@box")

    with pytest.raises(sqlite3.IntegrityError):
        store.ingest_log(
            "log-chat", "ann@box", max_revision, 2, [_record("not-inserted")])
    assert store.capture_log_position("log-chat", "ann@box") == max_revision
    assert store.message_count("log-chat") == 0

    store.upsert_messages("reset-chat", [_record("not-deleted")])
    store.forget_chat("reset-chat")
    store.upsert_messages("reset-chat", [_record("not-deleted")])
    with store._conn() as conn:
        conn.execute(
            "UPDATE ingestion_chat_resets SET generation=? WHERE chat_id=?",
            (2**63 - 1, "reset-chat"),
        )
    max_reset = store.capture_log_position("reset-chat", "ann@box")

    with pytest.raises(sqlite3.IntegrityError):
        store.forget_chat("reset-chat")
    assert store.capture_log_position("reset-chat", "ann@box") == max_reset
    assert [row["id"] for row in store.messages("reset-chat")] == ["not-deleted"]


def test_additive_migration_preserves_old_data_outbox_doc_and_log_head(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages(
          chat_id TEXT NOT NULL, id TEXT NOT NULL, ns INTEGER NOT NULL,
          sender TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'message',
          payload TEXT NOT NULL, PRIMARY KEY(chat_id,id));
        CREATE TABLE log_offsets(
          chat_id TEXT NOT NULL, log_name TEXT NOT NULL, offset INTEGER NOT NULL,
          PRIMARY KEY(chat_id,log_name));
        CREATE TABLE docs(
          path TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_ns INTEGER NOT NULL);
        CREATE TABLE outbox(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          target TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL,
          created_ns INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_ns INTEGER NOT NULL DEFAULT 0, lease_ns INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'pending', last_error TEXT NOT NULL DEFAULT '');
        """
    )
    record = _record("old-message")
    conn.execute(
        "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
        ("chat", record["id"], record["ns"], "ann", "message", json.dumps(record)),
    )
    conn.execute("INSERT INTO log_offsets VALUES(?,?,?)", ("chat", "ann@box", 17))
    conn.execute("INSERT INTO docs VALUES(?,?,?)", ("trust/peer.json", '{"ok":true}', 1))
    conn.execute(
        "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
        ("append_log", "chat|ann@box", '{"id":"pending"}', 2),
    )
    conn.commit()
    conn.close()

    migrated = Store(path)
    try:
        position = migrated.capture_log_position("chat", "ann@box")
        assert position.offset == 17
        assert position.revision == 0
        assert migrated.messages("chat") == [record]
        assert migrated.cached_doc("trust/peer.json") == {"ok": True}
        assert [item.payload for item in migrated.outbox_claim_due()] == [
            {"id": "pending"}
        ]
    finally:
        migrated.close()


def test_integer_expected_position_is_rejected_without_fallback(store):
    with pytest.raises(TypeError, match="LogPosition"):
        store.ingest_log("chat", "ann@box", 0, 1, [_record("must-not-land")])
    assert store.message_count("chat") == 0
