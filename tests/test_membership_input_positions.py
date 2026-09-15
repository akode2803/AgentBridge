"""Regression contracts for durable Store membership-input positions."""

from __future__ import annotations

import sqlite3

import pytest

from agentbridge.store import (
    MembershipInputPosition,
    MembershipInputUnavailable,
    Store,
)
from agentbridge.store import membership_input_position as positions
from agentbridge.store import db, log_position


CHAT = "membership-chat"
OTHER = "other-chat"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    yield value
    value.close()


def _record(message_id: str, *, ns: int = 1, kind: str = "info", body: str = "x"):
    return {
        "id": message_id,
        "ns": ns,
        "from": "writer",
        "kind": kind,
        "event": {"type": "membership"},
        "body": body,
    }


def _message_row(chat_id: str, message_id: str, *, ns: int = 1,
                 sender: str = "writer", kind: str = "info",
                 payload: str = '{"event":{"type":"membership"}}'):
    return (chat_id, message_id, ns, sender, kind, payload)


def _generation(store: Store, chat_id: str) -> int:
    return store.capture_membership_input_position(chat_id).generation


def _external(path, statement: str, params=()):
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(statement, params)
    finally:
        conn.close()


def test_legacy_message_and_reset_migration_seed_positions_and_reopen_preserves_token(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(db._SCHEMA)
        log_position.initialize(conn)
        with conn:
            conn.execute(
                "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                _message_row(CHAT, "legacy"),
            )
            conn.execute(
                "INSERT INTO ingestion_chat_resets(chat_id,generation) VALUES(?,7)",
                (OTHER,),
            )
    finally:
        conn.close()

    migrated = Store(path)
    try:
        message_position = migrated.capture_membership_input_position(CHAT)
        reset_position = migrated.capture_membership_input_position(OTHER)
        assert message_position.generation == reset_position.generation == 1
    finally:
        migrated.close()

    reopened = Store(path)
    try:
        assert reopened.capture_membership_input_position(CHAT) == message_position
        assert reopened.capture_membership_input_position(OTHER) == reset_position
        assert reopened.membership_input_position_matches(message_position)
        assert reopened.membership_input_position_matches(reset_position)
    finally:
        reopened.close()


def test_private_read_transaction_keeps_position_and_rows_on_one_wal_cut(store):
    store.upsert_messages(CHAT, [_record("old")])
    writer = Store(store.path)
    reader = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    try:
        reader.execute("BEGIN")
        old = positions._capture(reader, store.path, CHAT)
        old_rows = reader.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY ns,sender,id", (CHAT,),
        ).fetchall()
        writer.upsert_messages(CHAT, [_record("new", ns=2)])
        assert old_rows == [("old",)]
        assert old.generation < writer.capture_membership_input_position(CHAT).generation
        assert reader.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY ns,sender,id", (CHAT,),
        ).fetchall() == old_rows
    finally:
        reader.close()
        writer.close()

    fresh = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    try:
        fresh.execute("BEGIN")
        current = positions._capture(fresh, store.path, CHAT)
        rows = fresh.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY ns,sender,id", (CHAT,),
        ).fetchall()
        assert current.generation > old.generation
        assert rows == [("old",), ("new",)]
    finally:
        fresh.close()


def test_unseen_insert_duplicate_log_ingestion_and_cache_outbox_are_fenced(store):
    unseen = store.capture_membership_input_position(CHAT)
    assert unseen.generation == 0
    assert store.membership_input_position_matches(unseen)

    store.upsert_messages(CHAT, [_record("one")])
    inserted = store.capture_membership_input_position(CHAT)
    assert inserted.generation > unseen.generation
    assert not store.membership_input_position_matches(unseen)

    store.upsert_messages(CHAT, [_record("one")])
    assert store.capture_membership_input_position(CHAT) == inserted

    before_log = store.capture_log_position(CHAT, "writer@box")
    store.ingest_log(CHAT, "writer@box", before_log, 8, [_record("two", ns=2)])
    after_log = store.capture_membership_input_position(CHAT)
    assert after_log.generation > inserted.generation

    store.cache_and_outbox_add(
        CHAT, _record("three", ns=3), "append_log", "writer@box", {"id": "three"},
    )
    assert _generation(store, CHAT) > after_log.generation


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        ("UPDATE messages SET payload=? WHERE chat_id=? AND id=?",
         ('{"event":{"type":"changed"}}', CHAT, "one")),
        ("UPDATE messages SET kind='message' WHERE chat_id=? AND id=?", (CHAT, "one")),
        ("UPDATE messages SET ns=9 WHERE chat_id=? AND id=?", (CHAT, "one")),
        ("UPDATE messages SET sender='later' WHERE chat_id=? AND id=?", (CHAT, "one")),
        ("UPDATE messages SET id='renamed' WHERE chat_id=? AND id=?", (CHAT, "one")),
    ),
)
def test_every_membership_selection_or_order_update_invalidates(store, statement, params):
    store.upsert_messages(CHAT, [_record("one")])
    before = store.capture_membership_input_position(CHAT)
    with store._conn() as conn:
        conn.execute(statement, params)
    after = store.capture_membership_input_position(CHAT)
    assert after.generation > before.generation
    assert not store.membership_input_position_matches(before)


def test_chat_move_invalidates_both_positions_and_unrelated_chat_remains_current(store):
    store.upsert_messages(CHAT, [_record("move")])
    before_source = store.capture_membership_input_position(CHAT)
    before_dest = store.capture_membership_input_position(OTHER)
    untouched = store.capture_membership_input_position("untouched")
    with store._conn() as conn:
        conn.execute("UPDATE messages SET chat_id=? WHERE chat_id=? AND id=?",
                     (OTHER, CHAT, "move"))
    assert _generation(store, CHAT) > before_source.generation
    assert _generation(store, OTHER) > before_dest.generation
    assert store.membership_input_position_matches(untouched)


@pytest.mark.parametrize("recursive", (0, 1))
def test_replace_info_to_ordinary_and_update_replace_conflict_are_fenced(store, recursive):
    store.upsert_messages(CHAT, [_record("same")])
    stale = store.capture_membership_input_position(CHAT)
    conn = store._conn()
    conn.execute(f"PRAGMA recursive_triggers={recursive}")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages(chat_id,id,ns,sender,kind,payload) "
            "VALUES(?,?,?,?,?,?)",
            _message_row(CHAT, "same", kind="message"),
        )
    assert not store.membership_input_position_matches(stale)

    store.upsert_messages(CHAT, [_record("source", ns=2)])
    store.upsert_messages(OTHER, [_record("target", ns=3)])
    stale_source = store.capture_membership_input_position(CHAT)
    stale_dest = store.capture_membership_input_position(OTHER)
    with conn:
        conn.execute(
            "UPDATE OR REPLACE messages SET chat_id=?,id=? WHERE chat_id=? AND id=?",
            (OTHER, "target", CHAT, "source"),
        )
    assert not store.membership_input_position_matches(stale_source)
    assert not store.membership_input_position_matches(stale_dest)


def test_delete_reinsert_aba_and_empty_repeated_reset_do_not_preserve_tokens(store):
    store.upsert_messages(CHAT, [_record("stable")])
    stale = store.capture_membership_input_position(CHAT)
    with store._conn() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id=? AND id=?", (CHAT, "stable"))
        conn.execute(
            "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
            _message_row(CHAT, "stable"),
        )
    fresh = store.capture_membership_input_position(CHAT)
    assert fresh.generation > stale.generation
    assert not store.membership_input_position_matches(stale)

    empty = store.capture_membership_input_position("empty")
    store.forget_chat("empty")
    after_one = store.capture_membership_input_position("empty")
    store.forget_chat("empty")
    after_two = store.capture_membership_input_position("empty")
    assert after_one.generation > empty.generation
    assert after_two.generation > after_one.generation


def test_positions_are_aba_fenced_across_separate_writers_and_rollback(store):
    writer = Store(store.path)
    try:
        old = store.capture_membership_input_position(CHAT)
        writer.upsert_messages(CHAT, [_record("writer")])
        new = store.capture_membership_input_position(CHAT)
        assert not store.membership_input_position_matches(old)
        assert writer.membership_input_position_matches(new)

        conn = sqlite3.connect(store.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                _message_row(CHAT, "rolled-back", ns=2),
            )
            conn.rollback()
        finally:
            conn.close()
        assert store.membership_input_position_matches(new)
    finally:
        writer.close()


def test_generation_exhaustion_aborts_message_write_without_partial_row(store):
    _external(
        store.path,
        "INSERT INTO membership_input_versions(chat_id,generation) VALUES(?,?)",
        (CHAT, positions.MAX_SQLITE_INTEGER),
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_messages(CHAT, [_record("overflow")])
    assert store.message_count(CHAT) == 0
    assert _generation(store, CHAT) == positions.MAX_SQLITE_INTEGER

    with pytest.raises(sqlite3.IntegrityError):
        with store._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages(chat_id,id,ns,sender,kind,payload) "
                "VALUES(?,?,?,?,?,?)",
                _message_row(CHAT, "replace-overflow"),
            )
    assert store.message_count(CHAT) == 0
    assert _generation(store, CHAT) == positions.MAX_SQLITE_INTEGER


def test_invalid_stored_generation_is_never_normalized_to_unseen(store):
    store.upsert_messages(CHAT, [_record("present")])
    with store._conn() as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE membership_input_versions SET generation=0 WHERE chat_id=?",
            (CHAT,),
        )
    with pytest.raises(MembershipInputUnavailable, match="invalid membership input generation"):
        store.capture_membership_input_position(CHAT)


@pytest.mark.parametrize(
    "corrupt",
    (
        "not-an-integer",
        sqlite3.Binary(b"x" * (1024 * 1024)),
    ),
)
def test_text_or_blob_generation_is_rejected_without_normalizing_or_decoding(store, corrupt):
    store.upsert_messages(CHAT, [_record("present")])
    with store._conn() as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE membership_input_versions SET generation=? WHERE chat_id=?",
            (corrupt, CHAT),
        )
    with pytest.raises(MembershipInputUnavailable, match="invalid membership input generation"):
        store.capture_membership_input_position(CHAT)


def test_internal_capture_rejects_reader_from_a_different_database(store, tmp_path):
    other = Store(tmp_path / "other.sqlite")
    reader = sqlite3.connect(other.path.as_uri() + "?mode=ro", uri=True)
    try:
        reader.execute("BEGIN")
        with pytest.raises(ValueError, match="connection belongs to another database"):
            positions._capture(reader, store.path, CHAT)
    finally:
        reader.close()
        other.close()


def test_move_into_exhausted_destination_aborts_without_partially_advancing_source(store):
    store.upsert_messages(CHAT, [_record("move")])
    source = store.capture_membership_input_position(CHAT)
    _external(
        store.path,
        "INSERT INTO membership_input_versions(chat_id,generation) VALUES(?,?)",
        (OTHER, positions.MAX_SQLITE_INTEGER),
    )
    with pytest.raises(sqlite3.IntegrityError):
        with store._conn() as conn:
            conn.execute(
                "UPDATE OR IGNORE messages SET chat_id=? WHERE chat_id=? AND id=?",
                (OTHER, CHAT, "move"),
            )
    assert store.capture_membership_input_position(CHAT) == source
    assert store.capture_membership_input_position(OTHER).generation == positions.MAX_SQLITE_INTEGER
    assert store._conn().execute(
        "SELECT chat_id FROM messages WHERE id='move'"
    ).fetchone() == (CHAT,)


def test_forged_wrong_scope_boolean_and_oversized_tokens_are_rejected(store):
    current = store.capture_membership_input_position(CHAT)
    wrong_path = MembershipInputPosition(
        "elsewhere.sqlite", current.incarnation, current.namespace_epoch, CHAT,
        current.generation,
    )
    with pytest.raises(ValueError, match="belongs to another database"):
        store.membership_input_position_matches(wrong_path)

    with pytest.raises(ValueError):
        MembershipInputPosition(current.database_path, current.incarnation,
                                current.namespace_epoch, CHAT, True)
    with pytest.raises(OverflowError):
        MembershipInputPosition(current.database_path, current.incarnation,
                                current.namespace_epoch, "x" * 4097, 0)
    with pytest.raises((TypeError, ValueError)):
        store.membership_input_position_matches(object())


def test_missing_or_altered_owned_schema_fails_closed_even_while_store_is_open(store):
    token = store.capture_membership_input_position(CHAT)
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            conn.execute("DROP TRIGGER membership_input_messages_insert")
    finally:
        conn.close()
    with pytest.raises(MembershipInputUnavailable):
        store.capture_membership_input_position(CHAT)
    with pytest.raises(MembershipInputUnavailable):
        store.membership_input_position_matches(token)


def test_partial_namespace_with_missing_table_rejects_on_reopen(tmp_path):
    path = tmp_path / "store.sqlite"
    value = Store(path)
    value.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("DROP TABLE membership_input_versions")
    finally:
        conn.close()
    with pytest.raises(MembershipInputUnavailable, match="partial membership input namespace"):
        Store(path)


def test_private_capture_does_not_scan_unrelated_message_history(store):
    store.upsert_messages(CHAT, [_record("target")])
    rows = [
        _message_row("unrelated", f"bulk-{index}", ns=index + 1)
        for index in range(2_000)
    ]
    with store._conn() as conn:
        conn.executemany(
            "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
            rows,
        )

    reader = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    steps = 0

    def instruction_budget():
        nonlocal steps
        steps += 1
        return int(steps > 6_000)

    try:
        reader.execute("BEGIN")
        reader.set_progress_handler(instruction_budget, 1)
        position = positions._capture(reader, store.path, CHAT)
        assert position.chat_id == CHAT
    finally:
        reader.set_progress_handler(None, 0)
        reader.close()


@pytest.mark.parametrize(
    ("statement", "params"),
    (
        ("UPDATE membership_input_namespace SET epoch='not-a-hex-epoch'", ()),
        ("INSERT INTO membership_input_namespace(singleton,epoch) VALUES(2,?)",
         ("a" * 64,)),
        ("UPDATE membership_input_namespace SET epoch=?", ("x" * 4097,)),
    ),
)
def test_reopen_rejects_malformed_or_unbounded_namespace_identity(tmp_path, statement, params):
    path = tmp_path / "store.sqlite"
    initialized = Store(path)
    initialized.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(statement, params)
    finally:
        conn.close()
    with pytest.raises(MembershipInputUnavailable):
        Store(path)


def test_reopen_rejects_present_chat_without_its_generation(tmp_path):
    path = tmp_path / "store.sqlite"
    initialized = Store(path)
    initialized.upsert_messages(CHAT, [_record("present")])
    initialized.close()
    _external(path, "DELETE FROM membership_input_versions WHERE chat_id=?", (CHAT,))
    with pytest.raises(MembershipInputUnavailable, match="generation is missing"):
        Store(path)


def test_full_owned_namespace_recreation_changes_epoch(tmp_path):
    path = tmp_path / "store.sqlite"
    original = Store(path)
    try:
        old = original.capture_membership_input_position(CHAT)
    finally:
        original.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            for name in positions._TRIGGERS:
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("DROP TABLE membership_input_versions")
            conn.execute("DROP TABLE membership_input_namespace")
    finally:
        conn.close()
    recreated = Store(path)
    try:
        current = recreated.capture_membership_input_position(CHAT)
        assert current.namespace_epoch != old.namespace_epoch
        assert not recreated.membership_input_position_matches(old)
    finally:
        recreated.close()


def test_standalone_observe_closes_private_reader_after_capture_failure(store, monkeypatch):
    closed = []
    original = positions._open_reader

    class WrappedConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            closed.append(True)
            self._conn.close()

    monkeypatch.setattr(positions, "_open_reader",
                        lambda *args, **kwargs: WrappedConnection(original(*args, **kwargs)))
    with pytest.raises(ValueError):
        positions.observe(store.path, "\ud800")
    assert not closed  # invalid input must fail before opening a reader

    monkeypatch.setattr(positions, "_capture", lambda *_args: (_ for _ in ()).throw(
        sqlite3.DatabaseError("injected")))
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        positions.observe(store.path, CHAT)
    assert closed == [True]
