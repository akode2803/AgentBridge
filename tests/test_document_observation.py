"""R167 isolated local document-observation regressions."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from agentbridge.store import document_observation
from agentbridge.store.db import (
    DocumentObservationConflict,
    DocumentPosition,
    Store,
)


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _full(store: Store, source: str = "folder:one", **documents):
    return store.publish_document_batch(
        store.capture_document_position(source), documents, cursor=1, full=True
    )


def test_absent_capture_is_frozen_resolved_and_does_not_create_source(store):
    position = store.capture_document_position("folder:one")
    assert position == DocumentPosition(
        str(store.path), position.incarnation, "folder:one", 0, 0, False
    )
    assert position.incarnation
    assert store._conn().execute(
        "SELECT count(*) FROM document_observation_sources"
    ).fetchone()[0] == 0
    with pytest.raises(FrozenInstanceError):
        position.cursor = 1


def test_full_delta_empty_revival_null_and_tombstone_semantics(store):
    first = store.publish_document_batch(
        store.capture_document_position("source"),
        {"b.json": None, "a.json": {"value": [1]}},
        cursor=4,
        full=True,
    )
    assert (first.generation, first.cursor, first.initialized) == (1, 4, True)
    snapshot = store.capture_document_observation("source")
    assert [row.path for row in snapshot.records] == ["a.json", "b.json"]
    assert snapshot.documents() == {"a.json": {"value": [1]}, "b.json": None}
    snapshot.documents()["a.json"]["value"].append(2)
    assert snapshot.documents()["a.json"] == {"value": [1]}

    second = store.publish_document_batch(
        first, {"a.json": {"value": [2]}}, cursor=4,
        deleted_paths=("never-seen.json", "b.json"),
    )
    assert second.generation == 2  # Same cursor still invalidates older tokens.
    rows = store.capture_document_observation("source")
    assert rows.documents() == {"a.json": {"value": [2]}}
    assert rows.document("b.json", "gone") == "gone"
    assert [(r.path, r.payload_json, r.deleted) for r in rows.records] == [
        ("a.json", '{"value":[2]}', False),
        ("b.json", None, True),
        ("never-seen.json", None, True),
    ]

    empty = store.publish_document_batch(second, {}, cursor=5)
    revived = store.publish_document_batch(
        empty, {"b.json": None}, cursor=6
    )
    assert revived.generation == 4
    assert store.capture_document_observation("source").document(
        "b.json", "missing"
    ) is None


def test_full_snapshot_tombstones_absent_rows_and_revives_supplied_rows(store):
    first = _full(store, **{"keep": 1, "remove": 2})
    second = store.publish_document_batch(
        first, {"keep": 3}, cursor=2, full=True
    )
    assert second.generation == 2
    assert store.capture_document_observation("folder:one").decoded_records() == [
        ("keep", 3, False), ("remove", None, True)
    ]


def test_reset_is_source_local_advances_empty_and_requires_next_full(store):
    one = _full(store, "one", doc={"one": True})
    _full(store, "two", doc={"two": True})
    reset = store.reset_document_observation(one)
    assert (reset.generation, reset.cursor, reset.initialized) == (2, 0, False)
    assert store.capture_document_observation("one").records == ()
    assert store.capture_document_observation("two").documents() == {
        "doc": {"two": True}
    }
    with pytest.raises(ValueError, match="initialized"):
        store.publish_document_batch(reset, {"doc": 2}, cursor=1)
    restarted = store.publish_document_batch(reset, {}, cursor=0, full=True)
    assert (restarted.generation, restarted.initialized) == (3, True)

    absent = store.capture_document_position("empty")
    empty_reset = store.reset_document_observation(absent)
    assert empty_reset.generation == 1
    assert store.reset_document_observation(empty_reset).generation == 2


def test_two_writers_with_same_position_have_one_winner(store):
    other = Store(store.path)
    try:
        expected = store.capture_document_position("source")
        winner = store.publish_document_batch(
            expected, {"doc": "winner"}, cursor=1, full=True
        )
        with pytest.raises(DocumentObservationConflict):
            other.publish_document_batch(
                expected, {"doc": "loser"}, cursor=2, full=True
            )
        assert other.capture_document_position("source") == winner
        assert other.capture_document_observation("source").documents() == {
            "doc": "winner"
        }
    finally:
        other.close()


@pytest.mark.parametrize("reset", [False, True])
def test_reader_cannot_mix_position_with_same_count_write_or_reset(
    store, monkeypatch, reset
):
    current = _full(store, a={"old": 1})
    writer = Store(store.path)
    connect = document_observation.sqlite3.connect
    fired = []

    def open_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)

        def race(sql):
            if not fired and sql.startswith("SELECT count(*)"):
                fired.append(True)
                if reset:
                    writer.reset_document_observation(current)
                else:
                    writer.publish_document_batch(
                        current, {"b": {"new": 2}}, cursor=2, full=True
                    )

        conn.set_trace_callback(race)
        return conn

    monkeypatch.setattr(document_observation.sqlite3, "connect", open_reader)
    try:
        captured = store.capture_document_observation("folder:one")
        assert fired
        assert captured.position == current
        assert captured.documents() == {"a": {"old": 1}}
        fresh = store.capture_document_observation("folder:one")
        assert fresh.position.generation == 2
        assert fresh.documents() == ({} if reset else {"b": {"new": 2}})
    finally:
        writer.close()


def test_cursor_regression_and_failures_leave_position_and_rows_unchanged(store):
    current = store.publish_document_batch(
        store.capture_document_position("source"), {"old": 1},
        cursor=5, full=True,
    )
    cases = [
        lambda: store.publish_document_batch(current, {"new": 2}, cursor=4),
        lambda: store.publish_document_batch(
            current, {"new": float("nan")}, cursor=6
        ),
        lambda: store.publish_document_batch(
            current, {"new": "large"}, cursor=6, max_bytes=1
        ),
        lambda: store.publish_document_batch(
            current, {"new": 2}, cursor=6, max_documents=1
        ),
    ]
    for operation in cases:
        with pytest.raises((ValueError, OverflowError)):
            operation()
        assert store.capture_document_position("source") == current
        assert store.capture_document_observation("source").documents() == {"old": 1}


def test_batch_count_budget_rejects_before_payload_serialization_or_begin(store):
    position = store.capture_document_position("source")

    class MustNotSerialize:
        pass

    statements = []
    store._conn().set_trace_callback(statements.append)
    with pytest.raises(OverflowError, match="batch exceeds row budget"):
        store.publish_document_batch(
            position, {"one": MustNotSerialize(), "two": 2},
            cursor=1, full=True, max_documents=1,
        )
    assert not any(sql == "BEGIN IMMEDIATE" for sql in statements)
    assert store.capture_document_position("source") == position


def test_injected_error_and_generation_overflow_roll_back_atomically(store):
    current = _full(store, old=1)
    with store._conn() as conn:
        conn.execute(
            "CREATE TRIGGER reject_observation BEFORE INSERT "
            "ON document_observation_records WHEN NEW.path='reject' "
            "BEGIN SELECT RAISE(ABORT, 'observation failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="observation failed"):
        store.publish_document_batch(current, {"reject": 2}, cursor=2)
    assert store.capture_document_position("folder:one") == current
    assert store.capture_document_observation("folder:one").documents() == {"old": 1}


def test_base_exception_after_row_write_rolls_back_owned_transaction(store):
    current = _full(store, old=1)
    real = store._conn()

    class InterruptingConnection:
        def __init__(self, conn):
            self.conn = conn

        @property
        def in_transaction(self):
            return self.conn.in_transaction

        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO document_observation_sources"):
                raise KeyboardInterrupt("injected interruption")
            return self.conn.execute(sql, parameters)

        def commit(self):
            return self.conn.commit()

        def rollback(self):
            return self.conn.rollback()

    store._local.conn = InterruptingConnection(real)
    try:
        with pytest.raises(KeyboardInterrupt, match="injected interruption"):
            store.publish_document_batch(current, {"partial": 2}, cursor=2)
        assert not real.in_transaction
    finally:
        store._local.conn = real
    assert store.capture_document_position("folder:one") == current
    assert store.capture_document_observation("folder:one").documents() == {"old": 1}

    with store._conn() as conn:
        conn.execute(
            "UPDATE document_observation_sources SET generation=? WHERE source_id=?",
            (2**63 - 1, "folder:one"),
        )
    exhausted = store.capture_document_position("folder:one")
    with pytest.raises(OverflowError, match="exhausted"):
        store.publish_document_batch(exhausted, {"never": 3}, cursor=3)
    assert store.capture_document_position("folder:one") == exhausted
    assert store.capture_document_observation("folder:one").documents() == {"old": 1}


def test_caller_transaction_is_preserved_and_excluded_from_private_reads(store):
    current = _full(store, visible=1)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE document_observation_records SET payload='2' "
        "WHERE source_id='folder:one' AND path='visible'"
    )
    try:
        assert store.capture_document_observation("folder:one").documents() == {
            "visible": 1
        }
        with pytest.raises(sqlite3.OperationalError, match="caller transaction"):
            store.publish_document_batch(current, {"other": 2}, cursor=2)
        assert conn.in_transaction
        assert conn.execute(
            "SELECT payload FROM document_observation_records WHERE path='visible'"
        ).fetchone()[0] == "2"
        with pytest.raises(sqlite3.OperationalError, match="caller transaction"):
            store.reset_document_observation(current)
        assert conn.in_transaction
    finally:
        conn.rollback()


def test_invalid_types_paths_overlap_duplicates_and_json_keys_are_prewrite(store):
    position = store.capture_document_position("source")
    invalid = [
        lambda: store.publish_document_batch(position, [], cursor=0, full=True),
        lambda: store.publish_document_batch(
            position, {}, cursor=0, deleted_paths=[], full=True
        ),
        lambda: store.publish_document_batch(position, {}, cursor=True, full=True),
        lambda: store.publish_document_batch(position, {}, cursor=0, full=1),
        lambda: store.publish_document_batch(
            position, {}, cursor=0, full=True, max_documents=True
        ),
        lambda: store.publish_document_batch(
            position, {"doc": 1}, cursor=0, deleted_paths=("doc",)
        ),
        lambda: store.publish_document_batch(
            position, {}, cursor=0, deleted_paths=("doc", "doc")
        ),
        lambda: store.publish_document_batch(
            position, {"doc": {1: "bad", "1": "collision"}},
            cursor=0, full=True,
        ),
    ]
    for operation in invalid:
        with pytest.raises((TypeError, ValueError)):
            operation()
        assert store.capture_document_position("source") == position

    for bad_path in (
        "", "/absolute", "C:/drive", "with:colon", "a\\b", "a//b",
        "a/./b", "a/../b", "nul\x00path",
    ):
        with pytest.raises(ValueError):
            store.publish_document_batch(
                position, {bad_path: 1}, cursor=0, full=True
            )


def test_exact_utf8_limits_cover_batch_and_retained_capture(store):
    position = store.capture_document_position("source")
    payload = json.dumps("é", ensure_ascii=False, separators=(",", ":"))
    exact = len("unicode".encode()) + len(payload.encode())
    with pytest.raises(OverflowError):
        store.publish_document_batch(
            position, {"unicode": "é"}, cursor=1, full=True,
            max_bytes=exact - 1,
        )
    published = store.publish_document_batch(
        position, {"unicode": "é"}, cursor=1, full=True, max_bytes=exact
    )
    assert published.generation == 1
    with pytest.raises(OverflowError):
        store.capture_document_observation("source", max_bytes=exact - 1)
    assert store.capture_document_observation(
        "source", max_bytes=exact
    ).documents() == {"unicode": "é"}


def test_capture_preflights_before_payload_materialization_and_closes(store, monkeypatch):
    _full(store, payload="large")
    connect = document_observation.sqlite3.connect
    statements = []
    readers = []

    def open_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        readers.append(conn)
        return conn

    monkeypatch.setattr(document_observation.sqlite3, "connect", open_reader)
    with pytest.raises(OverflowError):
        store.capture_document_observation("folder:one", max_bytes=1)
    assert not any(sql.startswith("SELECT path,payload") for sql in statements)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")

    statements.clear()
    captured = store.capture_document_observation("folder:one")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[1].execute("SELECT 1")
    assert captured.documents() == {"payload": "large"}


def test_private_reader_setup_failure_closes_connection(store, monkeypatch):
    real_connect = document_observation.sqlite3.connect
    readers = []

    class SetupFailureConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            raise KeyboardInterrupt("reader setup interrupted")

        def close(self):
            return self.conn.close()

    def open_reader(*args, **kwargs):
        wrapped = SetupFailureConnection(real_connect(*args, **kwargs))
        readers.append(wrapped)
        return wrapped

    monkeypatch.setattr(document_observation.sqlite3, "connect", open_reader)
    with pytest.raises(KeyboardInterrupt, match="reader setup interrupted"):
        store.capture_document_position("source")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].conn.execute("SELECT 1")


def test_private_position_reader_closes_and_ignores_pending_source_state(
    store, monkeypatch
):
    original = store.capture_document_position("source")
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO document_observation_sources VALUES('source',1,9,1)"
    )
    connect = document_observation.sqlite3.connect
    readers = []

    def open_reader(*args, **kwargs):
        reader = connect(*args, **kwargs)
        readers.append(reader)
        return reader

    monkeypatch.setattr(document_observation.sqlite3, "connect", open_reader)
    try:
        assert store.capture_document_position("source") == original
        assert conn.in_transaction
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            readers[0].execute("SELECT 1")
    finally:
        conn.rollback()


def test_copied_database_scope_and_forged_position_fields_are_rejected(store, tmp_path):
    expected = store.capture_document_position("source")
    store.close()
    copied_path = tmp_path / "copy.sqlite"
    shutil.copy2(store.path, copied_path)
    copied = Store(copied_path)
    try:
        assert copied.capture_document_position("source").incarnation == expected.incarnation
        with pytest.raises(DocumentObservationConflict, match="another store"):
            copied.publish_document_batch(expected, {}, cursor=0, full=True)
    finally:
        copied.close()

    for field, value in (
        ("generation", True), ("cursor", False), ("initialized", 1)
    ):
        forged = object.__new__(DocumentPosition)
        for name in DocumentPosition.__dataclass_fields__:
            object.__setattr__(forged, name, getattr(expected, name))
        object.__setattr__(forged, field, value)
        with pytest.raises(ValueError):
            Store(store.path).publish_document_batch(
                forged, {}, cursor=0, full=True
            )


def test_additive_migration_and_reset_preserve_legacy_durable_tables(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy = Store(path)
    legacy.upsert_messages("chat", [{"id": "m", "ns": 1}])
    legacy.set_offset("chat", "writer", 7)
    legacy.cache_doc("lifecycle/head.json", {"epoch": 4})
    legacy.claim_once("effect", "claim", 8)
    outbox = legacy.outbox_add("append_log", "chat|writer", {"id": "pending"})
    source = _full(legacy, "source", observed={"value": 1})
    before = {
        table: tuple(legacy._conn().execute(f"SELECT * FROM {table}"))
        for table in ("messages", "log_offsets", "docs", "claims", "outbox")
    }
    legacy.close()

    reopened = Store(path)
    try:
        after = {
            table: tuple(reopened._conn().execute(f"SELECT * FROM {table}"))
            for table in ("messages", "log_offsets", "docs", "claims", "outbox")
        }
        assert after == before
        reopened.reset_document_observation(source)
        assert {
            table: tuple(reopened._conn().execute(f"SELECT * FROM {table}"))
            for table in ("messages", "log_offsets", "docs", "claims", "outbox")
        } == before
        assert reopened.cached_doc("lifecycle/head.json") == {"epoch": 4}
        assert [item.seq for item in reopened.outbox_claim_due()] == [outbox]
    finally:
        reopened.close()
