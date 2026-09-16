"""Bounded Store capture contracts for raw message-page inputs."""

from __future__ import annotations

import pytest

from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.store import overlay_index, page_inputs
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "mirror:room"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _message(ident, ns, sender="writer", body=None):
    return {
        "id": ident, "ns": ns, "from": sender, "kind": "message",
        "body": body if body is not None else ident,
    }


def _ready_index(store, documents=None):
    store.prepare_page_input_index()
    source = store.publish_document_batch(
        store.capture_document_position(SOURCE), documents or {},
        cursor=1, full=True, retain_tombstones=False,
    )
    observation = store.capture_document_observation(SOURCE)
    assert observation.position == source
    return store.publish_overlay_index(prepare_overlay_index(observation, CHAT))


def test_page_input_index_requires_explicit_preparation(store):
    store.upsert_messages(CHAT, [_message("m1", 1)])
    source = store.publish_document_batch(
        store.capture_document_position(SOURCE), {},
        cursor=1, full=True, retain_tombstones=False,
    )
    observation = store.capture_document_observation(SOURCE)
    assert observation.position == source
    index = store.publish_overlay_index(prepare_overlay_index(observation, CHAT))

    with pytest.raises(page_inputs.PageInputsChanged, match="message_index_(pending|changed)"):
        store.capture_page_inputs(index, raw_limit=1)

    store.prepare_page_input_index()
    captured = store.capture_page_inputs(index, raw_limit=1)
    assert [row.key.id for row in captured.rows] == ["m1"]


def test_capture_owns_copies_before_opening_reader(store, monkeypatch):
    store.upsert_messages(CHAT, [_message("old", 1), _message("new", 2)])
    index = _ready_index(store)
    expected = store.capture_page_inputs(index, raw_limit=1).position
    before = page_inputs.MessageKey(2, "writer", "new")
    original_open = page_inputs.source._open_reader
    mutations = []

    def mutate_original_inputs(path):
        mutations.append(True)
        object.__setattr__(before, "id", "mutated")
        object.__setattr__(index, "chat_id", "other-room")
        object.__setattr__(expected.messages, "chat_id", "other-room")
        object.__setattr__(expected.overlays, "build", "0" * 32)
        return original_open(path)

    monkeypatch.setattr(page_inputs.source, "_open_reader", mutate_original_inputs)
    captured = store.capture_page_inputs(
        index, expected=expected, before=before, raw_limit=1,
    )

    assert mutations == [True]
    assert [row.key.id for row in captured.rows] == ["old"]
    assert captured.position.messages.chat_id == CHAT
    assert captured.position.overlays.chat_id == CHAT
    assert captured.position.overlays.build != "0" * 32


def test_reverse_composite_order_and_equal_key_tiebreakers(store):
    store.upsert_messages(CHAT, [
        _message("a", 9, "z"), _message("c", 9, "a"),
        _message("b", 9, "a"), _message("old", 8, "z"),
    ])
    index = _ready_index(store)
    captured = store.capture_page_inputs(index, raw_limit=4)
    assert [(r.key.ns, r.key.sender, r.key.id) for r in captured.rows] == [
        (9, "z", "a"), (9, "a", "c"), (9, "a", "b"), (8, "z", "old"),
    ]
    assert captured.lookahead is None
    assert [r.decoded()["id"] for r in captured.rows] == ["a", "c", "b", "old"]


def test_deep_seek_traversal_is_finite_and_gap_free(store):
    records = [_message(f"m{i:03d}", 100 + i, sender=f"s{i % 3}") for i in range(73)]
    store.upsert_messages(CHAT, records)
    index = _ready_index(store)
    before = None
    seen = []
    for _ in range(20):
        page = store.capture_page_inputs(index, before=before, raw_limit=7)
        keys = [row.key for row in page.rows]
        assert keys == sorted(keys, reverse=True)
        seen.extend(row.key.id for row in page.rows)
        if not page.rows:
            break
        before = page.rows[-1].key
    else:  # pragma: no cover - bounded traversal must terminate
        pytest.fail("deep keyset traversal did not terminate")
    assert seen == [f"m{i:03d}" for i in reversed(range(73))]
    assert len(seen) == len(set(seen))


def test_exact_ids_preserve_request_order_and_report_missing(store):
    store.upsert_messages(CHAT, [
        _message("one", 1), _message("two", 2), _message("three", 3),
    ])
    index = _ready_index(store)
    captured = store.capture_page_inputs(
        index, raw_limit=1, exact_ids=("one", "missing", "three"),
    )
    assert [row.key.id for row in captured.rows] == ["three"]
    assert [row.key.id for row in captured.exact_rows] == ["one", "three"]
    assert captured.absent_ids == ("missing",)
    assert captured.rows[0] is captured.exact_rows[1]


def test_expected_position_rejects_message_source_index_and_reset_changes(store):
    store.upsert_messages(CHAT, [_message("old", 1)])
    index = _ready_index(store)
    first = store.capture_page_inputs(index, raw_limit=1)

    store.upsert_messages(CHAT, [_message("late", 2)])
    with pytest.raises(page_inputs.PageInputsChanged, match="message_position_changed"):
        store.capture_page_inputs(index, expected=first.position, raw_limit=1)

    current = store.capture_page_inputs(index, raw_limit=1)
    store.forget_chat(CHAT)
    with pytest.raises(page_inputs.PageInputsChanged, match="message_position_changed"):
        store.capture_page_inputs(index, expected=current.position, raw_limit=1)

    # Re-seed a message position, then invalidate the raw overlay source.
    store.upsert_messages(CHAT, [_message("again", 3)])
    before_source = store.capture_page_inputs(index, raw_limit=1)
    store.invalidate_document_observation(index.source)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="source_changed"):
        store.capture_page_inputs(index, expected=before_source.position, raw_limit=1)


def test_index_mutation_and_message_payload_identity_mismatch_fail_closed(store):
    store.upsert_messages(CHAT, [_message("m1", 1)])
    index = _ready_index(store)
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO overlay_index_candidates VALUES(?,?,?,?,?,?)",
            (SOURCE, "reaction", "m1", "fake", "x", 1),
        )
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
        store.capture_page_inputs(index, raw_limit=1)

    # A column/payload disagreement is rejected after the bounded snapshot.
    new_index = _ready_index(store)
    with store._conn() as conn:
        conn.execute(
            "UPDATE messages SET payload=? WHERE chat_id=? AND id=?",
            ('{"id":"other","ns":1,"from":"writer","kind":"message"}', CHAT, "m1"),
        )
    with pytest.raises(page_inputs.PageInputsChanged, match="payload_identity_mismatch"):
        store.capture_page_inputs(new_index, raw_limit=1)


@pytest.mark.parametrize("damage", ["drop", "malformed"])
def test_message_index_damage_is_rejected(store, damage):
    store.upsert_messages(CHAT, [_message("m1", 1)])
    index = _ready_index(store)
    with store._conn() as conn:
        conn.execute(f"DROP INDEX {page_inputs.INDEX}")
        if damage == "malformed":
            conn.execute(f"CREATE INDEX {page_inputs.INDEX} ON messages(chat_id,id)")
    with pytest.raises(page_inputs.PageInputsChanged, match="message_index_(pending|changed)"):
        store.capture_page_inputs(index, raw_limit=1)


def test_row_and_byte_budgets_fail_without_consuming_position(store):
    store.upsert_messages(CHAT, [
        _message("one", 1, body="x" * 100),
        _message("two", 2, body="y" * 100),
    ])
    index = _ready_index(store)
    with pytest.raises(ValueError, match="raw row limit"):
        store.capture_page_inputs(index, raw_limit=page_inputs.MAX_RAW_ROWS + 1)
    with pytest.raises(ValueError, match="exact message selectors"):
        store.capture_page_inputs(
            index, raw_limit=0,
            exact_ids=tuple(f"missing-{i}" for i in range(page_inputs.MAX_EXACT_IDS + 1)),
        )
    with pytest.raises(OverflowError, match="message page exceeds byte budget"):
        store.capture_page_inputs(index, raw_limit=2, max_bytes=50)
    assert [r.key.id for r in store.capture_page_inputs(index, raw_limit=2).rows] == [
        "two", "one",
    ]


def test_capture_uses_bounded_point_and_composite_queries(store, monkeypatch):
    store.upsert_messages(CHAT, [_message(f"m{i}", i + 1) for i in range(5)])
    index = _ready_index(store)
    real_connect = page_inputs.source.sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(page_inputs.source.sqlite3, "connect", traced)
    store.capture_page_inputs(index, raw_limit=2, exact_ids=("m0", "missing"))
    message_reads = [sql for sql in statements if " FROM messages" in sql]
    assert any(f"INDEXED BY {page_inputs.INDEX}" in sql and " LIMIT " in sql
               for sql in message_reads)
    assert all("chat_id=" in sql for sql in message_reads)
    assert not any("SELECT count(" in sql.lower() for sql in message_reads)


def test_message_and_overlay_reads_share_one_transaction(store, monkeypatch):
    store.upsert_messages(CHAT, [_message("old", 1)])
    index = _ready_index(store)
    expected = store.capture_page_inputs(index, raw_limit=1).position
    writer = Store(store.path)
    original = page_inputs.overlays._capture
    fired = []

    def mutate_between_message_and_overlay_reads(conn, path, wanted, *args, **kwargs):
        if not fired:
            fired.append(True)
            writer.upsert_messages(CHAT, [_message("late", 2)])
        return original(conn, path, wanted, *args, **kwargs)

    monkeypatch.setattr(page_inputs.overlays, "_capture", mutate_between_message_and_overlay_reads)
    try:
        captured = store.capture_page_inputs(index, expected=expected, raw_limit=2)
    finally:
        writer.close()
    assert fired
    assert [row.key.id for row in captured.rows] == ["old"]
    assert captured.position == expected
    with pytest.raises(page_inputs.PageInputsChanged, match="message_position_changed"):
        store.capture_page_inputs(index, expected=expected, raw_limit=2)


def test_metadata_vm_reads_covering_index_without_loading_payload(store):
    store.upsert_messages(CHAT, [_message("m1", 1, body="x" * 100_000)])
    store.prepare_page_input_index()
    conn = store._conn()
    roots = dict(conn.execute(
        "SELECT name,rootpage FROM sqlite_master WHERE name IN ('messages',?)",
        (page_inputs.INDEX,),
    ))
    assert set(roots) == {"messages", page_inputs.INDEX}
    queries = [
        (
            f"SELECT ns,sender,id,kind,length(CAST(payload AS BLOB)) "
            f"FROM messages INDEXED BY {page_inputs.INDEX} "
            "WHERE chat_id=? AND ns>0 "
            "ORDER BY ns DESC,sender DESC,id DESC LIMIT ?",
            (CHAT, 2),
        ),
        (
            f"SELECT ns,sender,id,kind,length(CAST(payload AS BLOB)) "
            f"FROM messages INDEXED BY {page_inputs.INDEX} "
            "WHERE chat_id=? AND ns=? AND sender=? AND id=?",
            (CHAT, 1, "writer", "m1"),
        ),
    ]
    for sql, params in queries:
        ops = conn.execute("EXPLAIN " + sql, params).fetchall()
        cursors = {
            row[2]: row[3] for row in ops
            if row[1] == "OpenRead" and row[3] in roots.values()
        }
        index_cursor = next(
            cursor for cursor, rootpage in cursors.items()
            if rootpage == roots[page_inputs.INDEX]
        )
        table_cursor = next(
            cursor for cursor, rootpage in cursors.items()
            if rootpage == roots["messages"]
        )
        assert any(row[1] == "Column" and row[2] == index_cursor for row in ops)
        assert not any(row[1] == "Column" and row[2] == table_cursor for row in ops)
        assert not any(row[1] in {"Cast", "Function", "PureFunc"} for row in ops)


def test_oversized_selected_exact_and_lookahead_payloads_are_not_fetched(
    store, monkeypatch,
):
    huge = "h" * 200_000
    store.upsert_messages(CHAT, [
        _message("lookahead-huge", 1, body=huge),
        _message("selected-small", 2, body="small"),
        _message("selected-huge", 3, body=huge),
    ])
    index = _ready_index(store)
    real_connect = page_inputs.source.sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(page_inputs.source.sqlite3, "connect", traced)
    with pytest.raises(OverflowError, match="message page exceeds byte budget"):
        store.capture_page_inputs(index, raw_limit=1, max_bytes=1_000)
    assert not any(sql.startswith("SELECT payload FROM messages") for sql in statements)

    statements.clear()
    with pytest.raises(OverflowError, match="message page exceeds byte budget"):
        store.capture_page_inputs(
            index, raw_limit=0, exact_ids=("selected-huge",), max_bytes=1_000,
        )
    assert not any(sql.startswith("SELECT payload FROM messages") for sql in statements)

    # Seek below the oversized newest row. The oversized second row is only
    # lookahead metadata; its payload must not be fetched or charged.
    statements.clear()
    page = store.capture_page_inputs(
        index,
        before=page_inputs.MessageKey(3, "writer", "selected-huge"),
        raw_limit=1,
        max_bytes=1_000,
    )
    assert [row.key.id for row in page.rows] == ["selected-small"]
    assert page.lookahead.id == "lookahead-huge"
    payload_reads = [
        sql for sql in statements if sql.startswith("SELECT payload FROM messages")
    ]
    assert len(payload_reads) == 1 and "selected-small" in payload_reads[0]
    assert "lookahead-huge" not in payload_reads[0]


def test_numeric_id_and_boolean_ns_payload_identity_compatibility(store):
    # Store ingestion and the current read model historically accept these int
    # subclasses/coercions; page capture must preserve that compatibility.
    store.upsert_messages(CHAT, [{
        "id": 7, "ns": True, "from": "writer", "kind": "message",
        "body": "legacy",
    }])
    index = _ready_index(store)
    captured = store.capture_page_inputs(index, raw_limit=1, exact_ids=("7",))
    assert captured.rows[0].key == page_inputs.MessageKey(1, "writer", "7")
    assert captured.rows[0].decoded()["id"] == 7
    assert captured.rows[0].decoded()["ns"] is True
    assert captured.exact_rows[0] is captured.rows[0]


def test_interrupted_background_index_build_rolls_back_without_readiness(store):
    import sqlite3

    store.upsert_messages(CHAT, [_message(f"m{i}", i + 1) for i in range(500)])
    before = store.capture_membership_input_position(CHAT)
    progress = [0, False]

    def interrupt_once():
        progress[0] += 1
        if progress[0] > 100 and not progress[1]:
            progress[1] = True
            return 1
        return 0

    conn = store._conn()
    conn.set_progress_handler(interrupt_once, 1)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            store.prepare_page_input_index()
    finally:
        conn.set_progress_handler(None, 0)
    assert progress[1] and not conn.in_transaction
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (page_inputs.INDEX,)).fetchone() is None
    assert store.capture_membership_input_position(CHAT) == before
    store.prepare_page_input_index()
    assert store.capture_membership_input_position(CHAT) == before
