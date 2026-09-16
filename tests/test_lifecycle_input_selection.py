"""Bounded exact-head and lifecycle-subject Store selections."""
from __future__ import annotations

import sqlite3

import pytest

from agentbridge.store import document_observation, lifecycle_heads, lifecycle_inputs
from agentbridge.store.db import Store


def _in_tx(store, call):
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        return call(conn)
    finally:
        conn.rollback()


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def test_selected_heads_preserve_absent_tombstone_and_present_states(store):
    store.cache_doc(lifecycle_heads.PREFIX + "present", {"v": 1})
    store.cache_doc(lifecycle_heads.PREFIX + "tombstone", {"v": 2})
    with store._conn():
        store._conn().execute(
            "DELETE FROM docs WHERE path=?", (lifecycle_heads.PREFIX + "tombstone",),
        )
    lifecycle_inputs.prepare(store._conn())

    selected = _in_tx(store, lambda conn: lifecycle_inputs.capture_heads(
        conn, store.path, ("absent", "tombstone", "present"),
    ))
    assert [(entry.subject, entry.generation, entry.payload_json)
            for entry in selected.entries] == [
        ("absent", 0, None),
        ("tombstone", 2, None),
        ("present", 1, '{"v": 1}'),
    ]
    assert selected.serialized_bytes > 0
    assert _in_tx(store, lambda conn: lifecycle_inputs.matches_heads(
        conn, store.path, selected,
    )) is True


def test_selected_orphan_and_malformed_head_fail_but_unrelated_corruption_isolated(store):
    store.cache_doc(lifecycle_heads.PREFIX + "good", {"ok": True})
    store.cache_doc(lifecycle_heads.PREFIX + "orphan", {"bad": True})
    with store._conn():
        store._conn().execute(
            "DELETE FROM lifecycle_head_versions WHERE path=?",
            (lifecycle_heads.PREFIX + "orphan",),
        )
    lifecycle_inputs.prepare(store._conn())
    assert len(_in_tx(store, lambda conn: lifecycle_inputs.capture_heads(
        conn, store.path, ("good",),
    )).entries) == 1
    with pytest.raises(lifecycle_inputs.LifecycleInputsUnavailable,
                       match="selected head missing generation"):
        _in_tx(store, lambda conn: lifecycle_inputs.capture_heads(
            conn, store.path, ("orphan",),
        ))

    with store._conn():
        store._conn().execute("PRAGMA ignore_check_constraints=ON")
        store._conn().execute(
            "UPDATE lifecycle_head_versions SET generation='bad' WHERE path=?",
            (lifecycle_heads.PREFIX + "good",),
        )
    with pytest.raises(lifecycle_inputs.LifecycleInputsUnavailable,
                       match="invalid selected head generation"):
        _in_tx(store, lambda conn: lifecycle_inputs.capture_heads(
            conn, store.path, ("good",),
        ))
    with store._conn():
        store._conn().execute(
            "UPDATE lifecycle_head_versions SET generation=7 WHERE path=?",
            (lifecycle_heads.PREFIX + "good",),
        )
        store._conn().execute(
            "UPDATE docs SET payload=? WHERE path=?",
            (sqlite3.Binary(b"bad"), lifecycle_heads.PREFIX + "good"),
        )
    with pytest.raises(lifecycle_inputs.LifecycleInputsUnavailable,
                       match="invalid selected head payload"):
        _in_tx(store, lambda conn: lifecycle_inputs.capture_heads(
            conn, store.path, ("good",),
        ))


def test_head_selection_rejects_scope_duplicate_invalid_and_transaction_misuse(
        store, tmp_path):
    lifecycle_inputs.prepare(store._conn())
    conn = store._conn()
    with pytest.raises(sqlite3.OperationalError, match="active transaction"):
        lifecycle_inputs.capture_heads(conn, store.path, ("alice",))
    conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="duplicate lifecycle subjects"):
            lifecycle_inputs.capture_heads(conn, store.path, ("alice", "alice"))
        with pytest.raises(ValueError, match="invalid lifecycle head subject"):
            lifecycle_inputs.capture_heads(conn, store.path, ("bad/name",))
    finally:
        conn.rollback()

    other = Store(tmp_path / "other.sqlite")
    other_conn = other._conn()
    other_conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="another database"):
            lifecycle_inputs.capture_heads(other_conn, store.path, ("alice",))
    finally:
        other_conn.rollback()
        other.close()


def test_subject_range_is_complete_ordered_and_prefix_bounded_with_tombstones(store):
    source = store.publish_document_batch(
        store.capture_document_position("lifecycle-source"), {
            "lifecycle/alice/z": {"v": "z"},
            "lifecycle/alice/a": {"v": "a"},
            "lifecycle/alice2/not-selected": {"v": "other"},
            "lifecycle/bob/not-selected": {"v": "other"},
        }, cursor=1, full=True,
    )
    source = store.publish_document_batch(
        source, {}, cursor=2, deleted_paths=("lifecycle/alice/z",),
    )
    lifecycle_inputs.prepare(store._conn())
    selected = _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
        conn, store.path, source, "alice",
    ))
    assert selected.prefix == "lifecycle/alice/"
    assert selected.subject == "alice" and selected.position == source
    assert selected.records[0].decoded() == {"v": "a"}
    assert [(r.path, r.deleted) for r in selected.records] == [
        ("lifecycle/alice/a", False), ("lifecycle/alice/z", True),
    ]
    assert selected.serialized_bytes > 0
    empty = _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
        conn, store.path, source, "carol",
    ))
    assert empty.prefix == "lifecycle/carol/" and empty.records == ()


def test_malformed_selected_subject_row_is_unavailable_not_absent(store):
    source = store.publish_document_batch(
        store.capture_document_position("source"),
        {"lifecycle/alice/a": {"v": 1}}, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    with store._conn():
        store._conn().execute("PRAGMA ignore_check_constraints=ON")
        store._conn().execute(
            "UPDATE document_observation_records SET payload=?,deleted=0 "
            "WHERE source_id=? AND path=?",
            (sqlite3.Binary(b"bad"), source.source_id, "lifecycle/alice/a"),
        )
        store._conn().execute(
            "UPDATE document_observation_sources SET initialized=1 "
            "WHERE source_id=?", (source.source_id,),
        )
    current = store.capture_document_position(source.source_id)
    with pytest.raises(lifecycle_inputs.LifecycleInputsUnavailable,
                       match="invalid selected lifecycle document"):
        _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
            conn, store.path, current, "alice",
        ))


def test_subject_capture_rejects_changed_source_wrong_scope_and_no_transaction(
        store, tmp_path):
    source = store.publish_document_batch(
        store.capture_document_position("source"),
        {"lifecycle/alice/a": 1}, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    store.publish_document_batch(source, {"lifecycle/alice/b": 2}, cursor=2)
    with pytest.raises(document_observation.DocumentObservationConflict):
        _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
            conn, store.path, source, "alice",
        ))
    with pytest.raises(sqlite3.OperationalError, match="active transaction"):
        lifecycle_inputs.capture_subject(
            store._conn(), store.path, source, "alice",
        )

    other = Store(tmp_path / "other.sqlite")
    lifecycle_inputs.prepare(other._conn())
    conn = other._conn()
    conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="another database"):
            lifecycle_inputs.capture_subject(conn, store.path, source, "alice")
    finally:
        conn.rollback()
        other.close()


def test_subject_match_copies_token_and_detects_raw_record_changes(store):
    source = store.publish_document_batch(
        store.capture_document_position("source"),
        {"lifecycle/alice/a": {"v": 1}}, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    selected = _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
        conn, store.path, source, "alice",
    ))
    assert _in_tx(store, lambda conn: lifecycle_inputs.matches_subject(
        conn, store.path, selected,
    )) is True

    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE document_observation_records SET payload=? "
            "WHERE source_id=? AND path=?",
            ('{"v":2}', source.source_id, "lifecycle/alice/a"),
        )
        # Model different raw data carrying the same supplied position.  Normal
        # record writes invalidate the source; restoring its metadata isolates
        # the final content comparison from that earlier generation fence.
        conn.execute(
            "UPDATE document_observation_sources SET generation=?,cursor=?,initialized=? "
            "WHERE source_id=?",
            (source.generation, source.cursor, int(source.initialized), source.source_id),
        )
        assert lifecycle_inputs.matches_subject(
            conn, store.path, selected,
        ) is False
    finally:
        conn.rollback()


def test_empty_subject_match_rejects_insert_and_source_aba(store):
    source = store.publish_document_batch(
        store.capture_document_position("source"),
        {"lifecycle/bob/a": {"v": 1}}, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    empty = _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
        conn, store.path, source, "alice",
    ))
    assert empty.records == ()

    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO document_observation_records"
            "(source_id,path,payload,deleted) VALUES(?,?,?,0)",
            (source.source_id, "lifecycle/alice/a", '{"v":1}'),
        )
        conn.execute(
            "UPDATE document_observation_sources SET generation=?,cursor=?,initialized=? "
            "WHERE source_id=?",
            (source.generation, source.cursor, int(source.initialized), source.source_id),
        )
        assert lifecycle_inputs.matches_subject(
            conn, store.path, empty,
        ) is False
    finally:
        conn.rollback()

    changed = store.publish_document_batch(
        source, {"lifecycle/alice/a": {"v": 2}}, cursor=2,
    )
    store.publish_document_batch(
        changed, {}, cursor=3, deleted_paths=("lifecycle/alice/a",),
    )
    with pytest.raises(document_observation.DocumentObservationConflict):
        _in_tx(store, lambda conn: lifecycle_inputs.matches_subject(
            conn, store.path, empty,
        ))


def test_subject_match_rejects_forged_scope_and_another_database(store, tmp_path):
    source = store.publish_document_batch(
        store.capture_document_position("source"),
        {"lifecycle/alice/a": 1}, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    selected = _in_tx(store, lambda conn: lifecycle_inputs.capture_subject(
        conn, store.path, source, "alice",
    ))
    forged = lifecycle_inputs.SubjectSelection(
        selected.position, "bob", selected.prefix,
        selected.records, selected.serialized_bytes,
    )
    with pytest.raises(ValueError, match="invalid lifecycle subject binding"):
        _in_tx(store, lambda conn: lifecycle_inputs.matches_subject(
            conn, store.path, forged,
        ))

    other = Store(tmp_path / "other-match.sqlite")
    lifecycle_inputs.prepare(other._conn())
    conn = other._conn()
    conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="another database"):
            lifecycle_inputs.matches_subject(conn, store.path, selected)
    finally:
        conn.rollback()
        other.close()


@pytest.mark.parametrize("kind", ["head-bytes", "subject-rows", "subject-bytes"])
def test_budget_preflight_does_not_materialize_payloads(store, kind):
    store.cache_doc(lifecycle_heads.PREFIX + "large", {"body": "x" * 100_000})
    source = store.publish_document_batch(
        store.capture_document_position("source"), {
            "lifecycle/alice/a": {"body": "x" * 100_000},
            "lifecycle/alice/b": {"body": "y"},
        }, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    statements = []
    conn = store._conn()
    conn.set_trace_callback(statements.append)
    conn.execute("BEGIN")
    try:
        with pytest.raises(OverflowError):
            if kind == "head-bytes":
                lifecycle_inputs.capture_heads(
                    conn, store.path, ("large",), max_bytes=100,
                )
            elif kind == "subject-rows":
                lifecycle_inputs.capture_subject(
                    conn, store.path, source, "alice", max_records=1,
                )
            else:
                lifecycle_inputs.capture_subject(
                    conn, store.path, source, "alice", max_bytes=100,
                )
    finally:
        conn.rollback()
        conn.set_trace_callback(None)
    assert not any(sql.startswith("SELECT payload FROM docs") for sql in statements)
    assert not any(sql.startswith("SELECT path,payload,deleted") for sql in statements)


def test_covering_metadata_bytecode_reads_index_expressions_not_table_payload(store):
    store.cache_doc(lifecycle_heads.PREFIX + "alice", {"v": 1})
    source = store.publish_document_batch(
        store.capture_document_position("source"),
        {"lifecycle/alice/a": {"v": 1}}, cursor=1, full=True,
    )
    lifecycle_inputs.prepare(store._conn())
    conn = store._conn()
    cases = [
        (
            "SELECT typeof(payload),length(CAST(payload AS BLOB)) FROM docs "
            f"INDEXED BY {lifecycle_inputs.HEAD_INDEX} WHERE path=? AND "
            f"{lifecycle_inputs._HEAD_WHERE}",
            (lifecycle_heads.PREFIX + "alice",), "docs", lifecycle_inputs.HEAD_INDEX,
        ),
        (
            "SELECT deleted,coalesce(length(CAST(payload AS BLOB)),0),typeof(payload),"
            "length(CAST(path AS BLOB)),typeof(path) FROM document_observation_records "
            f"INDEXED BY {lifecycle_inputs.SUBJECT_INDEX} WHERE source_id=? "
            "AND path>=? AND path<? ORDER BY path LIMIT ?",
            (source.source_id, "lifecycle/alice/", "lifecycle/alice0", 257),
            "document_observation_records", lifecycle_inputs.SUBJECT_INDEX,
        ),
    ]
    for query, params, table, index_name in cases:
        plan = conn.execute("EXPLAIN QUERY PLAN " + query, params).fetchall()
        assert any("SEARCH" in row[3] and index_name in row[3] for row in plan)
        roots = dict(conn.execute(
            "SELECT name,rootpage FROM sqlite_master WHERE name IN (?,?)",
            (table, index_name),
        ))
        bytecode = conn.execute("EXPLAIN " + query, params).fetchall()
        table_cursors = {r[2] for r in bytecode if r[1] == "OpenRead" and r[3] == roots[table]}
        index_cursors = {r[2] for r in bytecode if r[1] == "OpenRead" and r[3] == roots[index_name]}
        assert any(r[1] == "Column" and r[2] in index_cursors for r in bytecode)
        assert not any(r[1] == "Column" and r[2] in table_cursors for r in bytecode)
        assert not any(r[1] == "Cast" for r in bytecode)


def test_unrelated_head_and_source_scale_do_not_change_selected_vm_work(tmp_path):
    measurements = []
    for count in (1_000, 100_000):
        opened = Store(tmp_path / f"scale-{count}.sqlite")
        try:
            with opened._conn():
                opened._conn().executemany(
                    "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,1)",
                    ((lifecycle_heads.PREFIX + f"other-{i:06}", "{}")
                     for i in range(count)),
                )
            opened.cache_doc(lifecycle_heads.PREFIX + "wanted", {"v": 1})
            documents = {
                f"lifecycle/other-{i:06}/event": {"v": i}
                for i in range(count)
            }
            documents["lifecycle/wanted/event"] = {"v": "wanted"}
            source = opened.publish_document_batch(
                opened.capture_document_position("source"), documents,
                cursor=1, full=True, max_documents=count + 1,
            )
            lifecycle_inputs.prepare(opened._conn())
            steps = [0]
            conn = opened._conn()

            def tick():
                steps[0] += 1
                return 0

            conn.set_progress_handler(tick, 1)
            conn.execute("BEGIN")
            try:
                heads_result = lifecycle_inputs.capture_heads(
                    conn, opened.path, ("wanted",),
                )
                subject_result = lifecycle_inputs.capture_subject(
                    conn, opened.path, source, "wanted",
                )
            finally:
                conn.rollback()
                conn.set_progress_handler(None, 0)
            assert len(heads_result.entries) == len(subject_result.records) == 1
            measurements.append(steps[0])
        finally:
            opened.close()
    assert measurements[0] == measurements[1]
