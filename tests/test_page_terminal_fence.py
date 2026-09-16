"""Same-transaction local page/terminal input fence contracts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.store import overlay_index, page_inputs, terminal_observation
from agentbridge.store.db import Store
from agentbridge.store.page_inputs import PageInputPosition


CHAT = "room"
TARGET = "room|alice@m1"


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    store.prepare_page_input_index()
    store.prepare_terminal_observation()
    source = store.publish_document_batch(
        store.capture_document_position("source"), {}, cursor=1, full=True,
    )
    index = store.publish_overlay_index(prepare_overlay_index(
        store.capture_document_observation(source.source_id), CHAT,
    ))
    store.upsert_messages(CHAT, [{
        "id": "m1", "ns": 1, "from": "alice", "kind": "message",
    }])
    store.outbox_add("append_log", TARGET, {"kind": "message"})
    terminal = store.refresh_terminal_observation(TARGET)
    page = store.capture_page_inputs(index, raw_limit=1)
    yield store, source, index, page.position, terminal
    store.close()


def _final(store, page_position, terminal_position, statements=None):
    conn = store._conn()
    if statements is not None:
        conn.set_trace_callback(statements.append)
    conn.execute("BEGIN IMMEDIATE")
    try:
        return (
            page_inputs.matches_position(conn, store.path, page_position),
            terminal_observation.matches(conn, store.path, terminal_position),
        )
    finally:
        conn.rollback()
        if statements is not None:
            conn.set_trace_callback(None)


def test_same_begin_immediate_validates_both_without_payload_or_full_capture(world):
    store, _source, _index, page_position, terminal = world
    statements = []
    assert _final(store, page_position, terminal, statements) == (True, True)
    normalized = [" ".join(sql.split()).upper() for sql in statements]
    assert not any(sql.startswith("SELECT PAYLOAD") for sql in normalized)
    assert not any("LENGTH(CAST(PAYLOAD" in sql for sql in normalized)
    assert not any("ORDER BY NS" in sql for sql in normalized)
    assert not any("FROM TERMINAL_CLASSES" in sql for sql in normalized)


def test_message_write_before_final_cut_rejects_page_only(world):
    store, _source, _index, page_position, terminal = world
    store.upsert_messages(CHAT, [{
        "id": "delayed", "ns": 0, "from": "alice", "kind": "message",
    }])
    assert _final(store, page_position, terminal) == (False, True)


def test_overlay_write_before_final_cut_rejects_page_readiness(world):
    store, source, _index, page_position, terminal = world
    store.publish_document_batch(source, {}, cursor=2, full=True)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(overlay_index.OverlayIndexUnavailable,
                           match="source_changed_or_pending"):
            page_inputs.matches_position(conn, store.path, page_position)
        assert terminal_observation.matches(conn, store.path, terminal)
    finally:
        conn.rollback()


def test_target_outbox_write_before_final_cut_rejects_terminal_only(world):
    store, _source, _index, page_position, terminal = world
    store.outbox_add("blob_upload", TARGET, {"kind": "message"})
    assert _final(store, page_position, terminal) == (True, False)


def test_unready_derived_rows_reject_even_when_source_positions_match(world):
    store, _source, index, page_position, terminal = world
    with store._conn():
        store._conn().execute(
            "DELETE FROM overlay_index_ready WHERE source=?",
            (index.source.source_id,),
        )
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(overlay_index.OverlayIndexUnavailable,
                           match="index_changed_or_pending"):
            page_inputs.matches_position(conn, store.path, page_position)
    finally:
        conn.rollback()

    # Restore the overlay index, then independently remove terminal readiness.
    store.publish_overlay_index(prepare_overlay_index(
        store.capture_document_observation(index.source.source_id), CHAT,
    ))
    with store._conn():
        store._conn().execute("DELETE FROM terminal_ready WHERE target=?", (TARGET,))
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert terminal_observation.matches(conn, store.path, terminal) is False
    finally:
        conn.rollback()


def test_no_transaction_other_database_and_changed_chat_scopes_are_rejected(
        world, tmp_path):
    store, _source, index, page_position, terminal = world
    conn = store._conn()
    with pytest.raises(Exception, match="active transaction"):
        page_inputs.matches_position(conn, store.path, page_position)
    with pytest.raises(Exception, match="active transaction"):
        terminal_observation.matches(conn, store.path, terminal)

    other = Store(tmp_path / "other.sqlite")
    other.prepare_page_input_index()
    other.prepare_terminal_observation()
    other_conn = other._conn()
    other_conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="another database"):
            page_inputs.matches_position(other_conn, other.path, page_position)
        with pytest.raises(ValueError, match="invalid terminal position"):
            terminal_observation.matches(other_conn, other.path, terminal)
    finally:
        other_conn.rollback()
        other.close()

    other_messages = replace(page_position.messages, chat_id="other")
    inconsistent = PageInputPosition(other_messages, index)
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="inconsistent page chat binding"):
            page_inputs.matches_position(conn, store.path, inconsistent)
        changed_target = replace(terminal, target="other")
        assert terminal_observation.matches(conn, store.path, changed_target) is False
    finally:
        conn.rollback()
