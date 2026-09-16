"""Focused readiness and exact-selection contracts for document publication."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from agentbridge.store import document_observation
from agentbridge.store.db import DocumentObservationConflict, Store


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _full(store: Store, source: str, documents: dict, cursor: int = 1):
    return store.publish_document_batch(
        store.capture_document_position(source), documents, cursor=cursor, full=True,
    )


def test_direct_row_writes_invalidate_sources_and_keep_raw_rows(store):
    a = _full(store, "a", {"change": 1, "move": 2})
    b = _full(store, "b", {"other": 3})

    with store._conn() as conn:
        conn.execute(
            "UPDATE document_observation_records SET payload='4' "
            "WHERE source_id='a' AND path='change'"
        )
    dirty_a = store.capture_document_position("a")
    assert (dirty_a.generation, dirty_a.cursor, dirty_a.initialized) == (
        a.generation + 1, a.cursor, False,
    )
    assert store.capture_document_observation("a").document("change") == 4
    assert store.capture_document_position("b") == b

    # REPLACE must invalidate through its inserted row. An ignored insert does
    # no row mutation and therefore must not advance again.
    with store._conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO document_observation_records "
            "VALUES('a','change','5',0)"
        )
    replaced = store.capture_document_position("a")
    assert (replaced.generation, replaced.initialized) == (
        dirty_a.generation + 1, False,
    )
    with store._conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO document_observation_records "
            "VALUES('a','change','999',0)"
        )
    assert store.capture_document_position("a") == replaced
    assert store.capture_document_observation("a").document("change") == 5

    # Moving a raw row dirties both the old and new source. The row itself is
    # retained under its new owner for a future full rebuild.
    before_b = store.capture_document_position("b")
    with store._conn() as conn:
        conn.execute(
            "UPDATE document_observation_records SET source_id='b' "
            "WHERE source_id='a' AND path='move'"
        )
    moved_a = store.capture_document_position("a")
    moved_b = store.capture_document_position("b")
    assert moved_a.generation == replaced.generation + 1
    assert moved_b.generation == before_b.generation + 1
    assert not moved_a.initialized and not moved_b.initialized
    assert store.capture_document_observation("a").document("move", "gone") == "gone"
    assert store.capture_document_observation("b").document("move") == 2


def test_invalidate_is_source_local_retains_rows_and_rejects_stale_publish(store):
    one = _full(store, "one", {"kept": {"v": 1}}, cursor=7)
    two = _full(store, "two", {"other": True}, cursor=9)

    invalid = store.invalidate_document_observation(one)
    assert (invalid.generation, invalid.cursor, invalid.initialized) == (
        one.generation + 1, 7, False,
    )
    assert store.capture_document_observation("one").documents() == {
        "kept": {"v": 1}
    }
    assert store.capture_document_position("two") == two
    with pytest.raises(DocumentObservationConflict):
        store.publish_document_batch(one, {"stale": True}, cursor=8, full=True)
    with pytest.raises(ValueError, match="initialized"):
        store.publish_document_batch(invalid, {"delta": True}, cursor=8)

    rebuilt = store.publish_document_batch(
        invalid, {"fresh": True}, cursor=8, full=True,
    )
    assert (rebuilt.generation, rebuilt.initialized) == (
        invalid.generation + 1, True,
    )
    store.close()
    reopened = Store(store.path)
    try:
        assert reopened.capture_document_position("one") == rebuilt
        assert reopened.capture_document_position("two") == two
        assert reopened.capture_document_observation("one").decoded_records() == [
            ("fresh", True, False), ("kept", None, True),
        ]
    finally:
        reopened.close()


def test_raw_insert_conflict_cannot_bypass_generation_exhaustion(store):
    _full(store, "source", {"doc": 1})
    with store._conn() as conn:
        conn.execute(
            "UPDATE document_observation_sources SET generation=? "
            "WHERE source_id='source'",
            (2**63 - 1,),
        )
    exhausted = store.capture_document_position("source")

    # A genuinely ignored existing row is not a mutation and stays a no-op.
    with store._conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO document_observation_records "
            "VALUES('source','doc','2',0)"
        )
    assert store.capture_document_position("source") == exhausted
    assert store.capture_document_observation("source").document("doc") == 1

    # OR IGNORE must not suppress exhaustion when its insert would add a row.
    for statement in (
        "INSERT OR IGNORE INTO document_observation_records "
        "VALUES('source','new','2',0)",
        "INSERT OR REPLACE INTO document_observation_records "
        "VALUES('source','doc','2',0)",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="document generation exhausted"):
            with store._conn() as conn:
                conn.execute(statement)

    assert store.capture_document_position("source") == exhausted
    assert store.capture_document_observation("source").document("doc") == 1
    assert store.capture_document_observation("source").document("new", "missing") == "missing"


def test_selected_capture_is_exact_ordered_and_ignores_unrelated_large_rows(store):
    expected = _full(store, "source", {
        "wanted/a": {"v": 1},
        "wanted/deleted": {"old": True},
        "unrelated/huge": "x" * 200_000,
    })
    expected = store.publish_document_batch(
        expected, {}, cursor=2, deleted_paths=("wanted/deleted",),
    )
    statements = []
    real_connect = document_observation.sqlite3.connect

    def traced_reader(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    original = document_observation.sqlite3.connect
    document_observation.sqlite3.connect = traced_reader
    try:
        selected = store.capture_selected_documents(
            expected, ("wanted/a", "missing", "wanted/deleted"), max_bytes=200,
        )
    finally:
        document_observation.sqlite3.connect = original
    assert selected.position == expected
    assert selected.decoded_records() == [
        ("wanted/a", {"v": 1}, False),
        ("missing", None, True),
        ("wanted/deleted", None, True),
    ]
    point_reads = [
        sql for sql in statements
        if "FROM document_observation_records" in sql
    ]
    assert point_reads
    assert all("source_id=" in sql and "path=" in sql for sql in point_reads)
    assert not any("unrelated/huge" in sql for sql in point_reads)


def test_selected_capture_preflights_total_bytes_before_payload_reads(store, monkeypatch):
    expected = _full(store, "source", {"a": "a" * 40, "b": "b" * 40})
    real_connect = document_observation.sqlite3.connect
    statements = []

    def traced_reader(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(document_observation.sqlite3, "connect", traced_reader)
    with pytest.raises(OverflowError, match="byte budget"):
        store.capture_selected_documents(expected, ("a", "b"), max_bytes=50)
    assert not any(
        sql.startswith("SELECT payload,deleted") for sql in statements
    ), "byte cap must be decided before any selected payload is materialized"


def test_selected_capture_position_and_payload_share_one_transaction(
    store, monkeypatch,
):
    expected = _full(store, "source", {"doc": {"version": 1}})
    writer = Store(store.path)
    real_connect = document_observation.sqlite3.connect
    start = threading.Event()
    finished = threading.Event()
    errors = []

    def mutate():
        try:
            assert start.wait(10)
            with writer._conn() as conn:
                conn.execute(
                    "UPDATE document_observation_records SET payload=? "
                    "WHERE source_id=? AND path=?",
                    ('{"version":2}', "source", "doc"),
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=mutate)
    thread.start()

    def raced_reader(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        fired = False

        def trace(sql):
            nonlocal fired
            if not fired and sql.startswith("SELECT coalesce(length"):
                fired = True
                start.set()
                assert finished.wait(10), "writer did not finish during selected capture"

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(document_observation.sqlite3, "connect", raced_reader)
    try:
        captured = store.capture_selected_documents(expected, ("doc",))
    finally:
        start.set()
        thread.join(10)
        writer.close()
    assert not thread.is_alive()
    assert not errors
    assert captured.position == expected
    assert captured.documents() == {"doc": {"version": 1}}
    fresh = store.capture_document_position("source")
    assert fresh.generation == expected.generation + 1 and not fresh.initialized
    with pytest.raises(DocumentObservationConflict):
        store.capture_selected_documents(expected, ("doc",))


@pytest.mark.parametrize("damage", ["missing", "modified", "extra"])
def test_reopen_rejects_missing_modified_or_extra_readiness_trigger(tmp_path, damage):
    path = tmp_path / f"{damage}.sqlite"
    opened = Store(path)
    opened.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            if damage == "missing":
                conn.execute("DROP TRIGGER document_source_dirty_insert")
            elif damage == "modified":
                conn.execute("DROP TRIGGER document_source_dirty_insert")
                conn.execute(
                    "CREATE TRIGGER document_source_dirty_insert AFTER INSERT "
                    "ON document_observation_records BEGIN SELECT 1; END"
                )
            else:
                conn.execute(
                    "CREATE TRIGGER document_source_dirty_extra AFTER INSERT "
                    "ON document_observation_records BEGIN SELECT 1; END"
                )
    finally:
        conn.close()
    with pytest.raises(sqlite3.OperationalError, match="invalid document readiness triggers"):
        Store(path)


def test_reopen_rejects_missing_initialized_readiness_table(tmp_path):
    path = tmp_path / "missing-table.sqlite"
    opened = Store(path)
    opened.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("DROP TABLE document_observation_records")
    finally:
        conn.close()
    with pytest.raises(
        sqlite3.OperationalError,
        match="invalid document readiness table: document_observation_records",
    ):
        Store(path)


def test_selected_capture_rejects_blob_payload_in_live_row(store):
    _full(store, "source", {"doc": {"valid": True}})
    with store._conn() as conn:
        conn.execute(
            "UPDATE document_observation_records SET payload=? "
            "WHERE source_id='source' AND path='doc'",
            (sqlite3.Binary(b'{"forged":true}'),),
        )
        # Simulate a corrupt external writer falsely declaring the row ready.
        conn.execute(
            "UPDATE document_observation_sources SET initialized=1 "
            "WHERE source_id='source'"
        )
    corrupt = store.capture_document_position("source")
    with pytest.raises(sqlite3.OperationalError, match="malformed observed document row"):
        store.capture_selected_documents(corrupt, ("doc",))


@pytest.mark.parametrize("generation", [-1, "bad", 1.5])
def test_raw_mutation_rejects_corrupt_generation_types(store, generation):
    _full(store, "source", {"doc": 1})
    conn = store._conn()
    conn.execute("PRAGMA ignore_check_constraints=ON")
    try:
        with conn:
            conn.execute(
                "UPDATE document_observation_sources SET generation=? "
                "WHERE source_id='source'",
                (generation,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="document generation exhausted"):
            with conn:
                conn.execute(
                    "INSERT INTO document_observation_records "
                    "VALUES('source','must-not-land','2',0)"
                )
        assert conn.execute(
            "SELECT count(*) FROM document_observation_records "
            "WHERE source_id='source' AND path='must-not-land'"
        ).fetchone() == (0,)
    finally:
        conn.execute("PRAGMA ignore_check_constraints=OFF")
