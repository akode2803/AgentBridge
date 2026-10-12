"""Private ownership fences for future coupled publication candidates."""
from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

from agentbridge.store import (
    coupled_stage, local_source, staged_publication, staged_source,
)
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "node-source"
META = "chats/room/meta.json"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    coupled_stage.initialize(value)
    yield value
    value.close()


def _claim(store):
    return local_source.claim_collection(
        store, local_source.capture(store, SOURCE),
    )


def _finish(store, handle):
    staged_source.append(
        store, handle.document_stage, {META: {"value": 1}},
    )
    staged_source.finish(store, handle.document_stage)


def test_generic_publisher_cannot_admit_coupled_document_stage(store):
    expected = _claim(store)
    handle = coupled_stage.begin(store, expected, CHAT)
    assert handle.document_stage.source_id.startswith("coupled:")
    _finish(store, handle)

    with pytest.raises(
            coupled_stage.CoupledStageChanged,
            match="requires_coupled_publication"):
        staged_publication.admit(
            store, expected, handle.document_stage, observed_ns=10,
        )
    assert local_source.capture(store, SOURCE) == expected


def test_coupled_prefix_fails_closed_if_owner_table_disappears(store):
    expected = _claim(store)
    handle = coupled_stage.begin(store, expected, CHAT)
    _finish(store, handle)
    with store._conn() as conn:
        conn.execute("DROP TABLE coupled_stage_owners")
    with pytest.raises(
            coupled_stage.CoupledStageChanged,
            match="requires_coupled_publication"):
        staged_publication.admit(
            store, expected, handle.document_stage, observed_ns=10,
        )


def test_ordinary_stage_keeps_generic_admission_path(store):
    expected = _claim(store)
    stage = staged_source.begin(
        store, expected.source_id, CHAT, expected=expected,
    )
    staged_source.append(store, stage, {META: {"value": 1}})
    staged_source.finish(store, stage)
    admitted, _index = staged_publication.admit(
        store, expected, stage, observed_ns=10,
    )
    assert admitted.ready


def test_abort_and_bounded_cleanup_remove_coupled_owner(store):
    expected = _claim(store)
    handle = coupled_stage.begin(store, expected, CHAT)
    staged_source.append(
        store, handle.document_stage, {META: {"value": 1}},
    )
    coupled_stage.abort(store, handle)
    for _ in range(4):
        staged_source.cleanup(store, max_rows=1)
    conn = store._conn()
    assert conn.execute(
        "SELECT 1 FROM coupled_stage_owners WHERE stage=?",
        (handle.document_stage.source_id,),
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM staged_sources WHERE source=?",
        (handle.document_stage.source_id,),
    ).fetchone() is None


def test_tampered_owner_schema_blocks_ordinary_admission_too(store):
    expected = _claim(store)
    stage = staged_source.begin(
        store, expected.source_id, CHAT, expected=expected,
    )
    staged_source.append(store, stage, {META: {"value": 1}})
    staged_source.finish(store, stage)
    with store._conn() as conn:
        conn.execute(
            "CREATE TRIGGER coupled_owner_evil AFTER INSERT "
            "ON coupled_stage_owners BEGIN SELECT 1; END",
        )
    with pytest.raises(
            coupled_stage.CoupledStageChanged,
            match="triggers changed"):
        staged_publication.admit(store, expected, stage, observed_ns=10)


def test_invalid_chat_does_not_strand_active_coupled_stage(store):
    expected = _claim(store)
    with pytest.raises(ValueError):
        coupled_stage.begin(store, expected, "")
    assert store._conn().execute(
        "SELECT 1 FROM staged_sources WHERE phase='building'",
    ).fetchone() is None
    assert coupled_stage.begin(store, expected, CHAT)


def test_abort_validates_owner_before_mutating_document_stage(store):
    expected = _claim(store)
    handle = coupled_stage.begin(store, expected, CHAT)
    stale = replace(handle, epoch="0" * 32)
    with pytest.raises(
            coupled_stage.CoupledStageChanged,
            match="owner changed"):
        coupled_stage.abort(store, stale)
    assert store._conn().execute(
        "SELECT phase FROM staged_sources WHERE source=?",
        (handle.document_stage.source_id,),
    ).fetchone() == ("building",)


def test_owner_row_shape_is_bounded_before_values_are_materialized(store):
    expected = _claim(store)
    handle = coupled_stage.begin(store, expected, CHAT)
    with store._conn() as conn:
        conn.execute(
            "UPDATE coupled_stage_owners SET logical_source=zeroblob(1048576) "
            "WHERE stage=?", (handle.document_stage.source_id,),
        )
    with pytest.raises(
            coupled_stage.CoupledStageChanged,
            match="malformed coupled stage owner"):
        coupled_stage.abort(store, handle)


def test_abort_never_rolls_back_callers_transaction(store):
    expected = _claim(store)
    handle = coupled_stage.begin(store, expected, CHAT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE caller_work(value)")
    conn.execute("INSERT INTO caller_work VALUES(1)")
    with pytest.raises(
            sqlite3.OperationalError, match="abort needs an owned transaction"):
        coupled_stage.abort(store, handle)
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM caller_work").fetchone() == (1,)
    conn.rollback()
    assert store._conn().execute(
        "SELECT phase FROM coupled_stage_owners WHERE stage=?",
        (handle.document_stage.source_id,),
    ).fetchone() == ("building",)
