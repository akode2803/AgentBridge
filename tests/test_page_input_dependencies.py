"""Bounded Store capture of raw page rows and exact overlay dependencies."""
from __future__ import annotations

import sqlite3

import pytest

from agentbridge import crypto
from agentbridge.mesh.events import reaction_signing_bytes, state_signing_bytes
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.overlay_source import publish_overlay_source
from agentbridge.mesh.page_inputs import capture_page_inputs as capture_live_page_inputs
from agentbridge.store import document_observation, overlay_index
from agentbridge.store.db import Store
from agentbridge.store.page_inputs import PageInputsChanged
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "room"
REACTION = f"chats/{CHAT}/overlays/reactions/alice.json"
HUGE_REACTION = f"chats/{CHAT}/overlays/reactions/huge.json"
STATE = f"chats/{CHAT}/overlays/state/viewer.json"


def _edit(message_id):
    return f"chats/{CHAT}/overlays/edits/{message_id}.json"


def _redaction(message_id):
    return f"chats/{CHAT}/overlays/redactions/{message_id}.json"


def _message(message_id, ns, sender="alice"):
    return {
        "id": message_id, "ns": ns, "from": sender, "kind": "message",
        "epoch": 1, "nonce": "n", "ct": "ciphertext", "sig": "signature",
    }


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    store.prepare_page_input_index()
    valid_bundle = crypto.generate_identity()
    valid_public, _agree = crypto.identity_pubs(valid_bundle)
    bad_public, _agree = crypto.identity_pubs(crypto.generate_identity())
    absent_public, _agree = crypto.identity_pubs(crypto.generate_identity())
    reaction_map = {"m1": "✅", "offpage-parent": "👀"}
    reaction_ns = 11
    reaction_doc = {
        "v": reaction_map,
        "ns": reaction_ns,
        "sig": crypto.sign(valid_bundle, reaction_signing_bytes(
            CHAT, "alice", reaction_ns, reaction_map)),
    }
    state_fields = {
        "hidden": ["m1"], "starred": ["m2"], "read_ns": 2,
    }
    state_ns = 12
    state_doc = {
        **state_fields, "ns": state_ns,
        "sig": crypto.sign(valid_bundle, state_signing_bytes(
            CHAT, "viewer", state_ns, state_fields)),
    }
    huge_doc = {
        "v": {f"never-selected-{index:05d}": "x" for index in range(10_000)},
        "ns": 13, "sig": "bad",
    }
    documents = {
        REACTION: reaction_doc,
        HUGE_REACTION: huge_doc,
        STATE: state_doc,
        _edit("m1"): {"body": "edited m1"},
        _redaction("m1"): {"by": "alice"},
        _edit("m2"): {"body": "edited m2"},
        _redaction("m2"): {"by": "alice"},
        _edit("offpage-parent"): {"body": "edited parent"},
        _redaction("offpage-parent"): {"by": "alice"},
    }
    source = store.publish_document_batch(
        store.capture_document_position("overlay-source"), documents,
        cursor=1, full=True, retain_tombstones=False,
    )
    observed = store.capture_document_observation(source.source_id)
    indexed = store.publish_overlay_index(prepare_overlay_index(observed, CHAT))
    assert store.verify_overlay_signature(indexed, REACTION, bad_public) is False
    store.upsert_messages(CHAT, [
        _message("offpage-parent", 1), _message("m1", 2), _message("m2", 3),
    ])
    yield {
        "store": store, "index": indexed,
        "valid_public": valid_public, "bad_public": bad_public,
        "absent_public": absent_public,
    }
    store.close()


def test_single_capture_has_messages_exact_documents_index_entries_and_proofs(world):
    store, index = world["store"], world["index"]
    result = store.capture_page_inputs(
        index, raw_limit=2, state_paths=(STATE,),
        proof_keys=((REACTION, world["bad_public"]),
                    (STATE, world["absent_public"])),
    )

    assert [row.key.id for row in result.rows] == ["m2", "m1"]
    assert result.lookahead.id == "offpage-parent"
    assert result.exact_rows == () and result.absent_ids == ()
    assert result.documents.decoded_records() == [
        (_edit("m1"), {"body": "edited m1"}, False),
        (_redaction("m1"), {"by": "alice"}, False),
        (_edit("m2"), {"body": "edited m2"}, False),
        (_redaction("m2"), {"by": "alice"}, False),
    ]
    assert {(item.kind, item.target, item.value)
            for item in result.indexed.candidates} == {
        ("reaction", "m1", "✅"),
        ("hidden", "m1", ""),
        ("starred", "m2", ""),
    }
    assert {item.path for item in result.indexed.documents} == {REACTION, STATE}
    assert result.proofs == (
        (REACTION, world["bad_public"], False),
        (STATE, world["absent_public"], None),
    )
    assert result.captured_bytes > 0
    assert store.capture_page_inputs(
        index, raw_limit=2, state_paths=(STATE,),
        proof_keys=((REACTION, world["bad_public"]),
                    (STATE, world["absent_public"])),
        max_bytes=result.captured_bytes,
    ).captured_bytes == result.captured_bytes
    with pytest.raises(OverflowError, match="byte budget"):
        store.capture_page_inputs(
            index, raw_limit=2, state_paths=(STATE,),
            proof_keys=((REACTION, world["bad_public"]),
                        (STATE, world["absent_public"])),
            max_bytes=result.captured_bytes - 1,
        )


def test_proof_request_for_unselected_document_is_rejected(world):
    with pytest.raises(ValueError, match="selected document"):
        world["store"].capture_page_inputs(
            world["index"], raw_limit=1,
            proof_keys=((HUGE_REACTION, world["bad_public"]),),
        )


def test_offpage_exact_parent_capture_reuses_exact_expected_positions(world):
    store, index = world["store"], world["index"]
    first = store.capture_page_inputs(index, raw_limit=2, state_paths=(STATE,))
    parent = store.capture_page_inputs(
        index, expected=first.position, raw_limit=0,
        exact_ids=("offpage-parent",), state_paths=(STATE,),
    )

    assert parent.position == first.position
    assert parent.rows == () and parent.lookahead is None
    assert [row.key.id for row in parent.exact_rows] == ["offpage-parent"]
    assert parent.absent_ids == ()
    assert parent.documents.decoded_records() == [
        (_edit("offpage-parent"), {"body": "edited parent"}, False),
        (_redaction("offpage-parent"), {"by": "alice"}, False),
    ]
    assert any(item.kind == "reaction" and item.target == "offpage-parent"
               for item in parent.indexed.candidates)


@pytest.mark.parametrize("selection", [
    {"raw_limit": 2, "max_bytes": 1},
    {"raw_limit": 201},
    {"raw_limit": 0, "exact_ids": tuple(f"m{i}" for i in range(65))},
])
def test_budget_failures_return_no_partial_result_and_leave_next_capture_unchanged(
        world, selection):
    store, index = world["store"], world["index"]
    baseline = store.capture_page_inputs(index, raw_limit=1, state_paths=(STATE,))
    expected = OverflowError if selection.get("max_bytes") == 1 else ValueError
    with pytest.raises(expected):
        store.capture_page_inputs(index, **selection)
    assert store.capture_page_inputs(
        index, raw_limit=1, state_paths=(STATE,)) == baseline


def test_large_unselected_overlay_map_is_not_decoded_on_foreground_capture(
        world, monkeypatch):
    store, index = world["store"], world["index"]
    original_source_loads = document_observation.json.loads
    original_index_loads = overlay_index.json.loads

    def reject_large(raw, *args, **kwargs):
        if isinstance(raw, str) and "never-selected" in raw:
            raise AssertionError("foreground decoded unselected overlay source")
        return original_source_loads(raw, *args, **kwargs)

    def reject_index_json(raw, *args, **kwargs):
        if isinstance(raw, str) and "never-selected" in raw:
            raise AssertionError("foreground decoded unselected index payload")
        return original_index_loads(raw, *args, **kwargs)

    monkeypatch.setattr(document_observation.json, "loads", reject_large)
    monkeypatch.setattr(overlay_index.json, "loads", reject_index_json)
    result = store.capture_page_inputs(index, raw_limit=1, state_paths=(STATE,))
    assert [row.key.id for row in result.rows] == ["m2"]
    assert HUGE_REACTION not in {item.path for item in result.indexed.documents}


def test_selected_document_size_preflight_is_covering_and_does_not_fetch_payload(
        tmp_path, monkeypatch):
    store = Store(tmp_path / "store.sqlite")
    try:
        path = _edit("oversized")
        source = store.publish_document_batch(
            store.capture_document_position("source"),
            {path: {"body": "x" * 300_000}}, cursor=1, full=True,
        )
        query = (
            "SELECT coalesce(length(CAST(payload AS BLOB)),0) "
            "FROM document_observation_records INDEXED BY "
            "idx_document_observation_selected_size "
            "WHERE source_id=? AND path=?"
        )
        conn = store._conn()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + query, (source.source_id, path),
        ).fetchall()
        assert any(
            row[3].startswith("SEARCH document_observation_records ")
            and "idx_document_observation_selected_size" in row[3]
            for row in plan
        ), plan
        roots = dict(conn.execute(
            "SELECT name,rootpage FROM sqlite_master WHERE name IN "
            "('document_observation_records','idx_document_observation_selected_size')"
        ))
        bytecode = conn.execute("EXPLAIN " + query, (source.source_id, path)).fetchall()
        table_cursors = {
            row[2] for row in bytecode
            if row[1] == "OpenRead" and row[3] == roots["document_observation_records"]
        }
        # SQLite versions differ in whether EQP labels an expression index
        # COVERING. The VM must read its stored size without reading the table.
        index_cursors = {
            row[2] for row in bytecode
            if row[1] == "OpenRead" and row[3] == roots["idx_document_observation_selected_size"]
        }
        assert any(
            row[1] == "Column" and row[2] in index_cursors and row[3] == 2
            for row in bytecode
        ), bytecode
        assert not any(
            row[1] == "Column" and row[2] in table_cursors for row in bytecode
        ), bytecode
        assert not any(
            row[1] in ("Cast", "Function", "Function0", "PureFunc")
            for row in bytecode
        ), bytecode

        statements = []
        real_connect = document_observation.sqlite3.connect

        def traced_reader(*args, **kwargs):
            reader = real_connect(*args, **kwargs)
            reader.set_trace_callback(statements.append)
            return reader

        monkeypatch.setattr(document_observation.sqlite3, "connect", traced_reader)
        with pytest.raises(OverflowError, match="byte budget"):
            store.capture_selected_documents(source, (path,), max_bytes=100)
        assert not any(
            sql.startswith("SELECT payload,deleted") for sql in statements
        ), "oversized selected payload must not be fetched after indexed preflight"
    finally:
        store.close()


def test_missing_selection_index_fails_closed_and_store_reopen_rebuilds_it(tmp_path):
    database = tmp_path / "store.sqlite"
    store = Store(database)
    source = store.publish_document_batch(
        store.capture_document_position("source"), {"doc": {"v": 1}},
        cursor=1, full=True,
    )
    with store._conn():
        store._conn().execute("DROP INDEX idx_document_observation_selected_size")
    with pytest.raises(sqlite3.OperationalError, match="selection size index"):
        store.capture_selected_documents(source, ("doc",))
    store.close()

    reopened = Store(database)
    try:
        assert reopened.capture_selected_documents(source, ("doc",)).decoded_records() == [
            ("doc", {"v": 1}, False),
        ]
        assert reopened.capture_document_position("source") == source
    finally:
        reopened.close()


def test_live_mirror_wrapper_rejects_message_mutation_after_initial_capture(
        tmp_path, monkeypatch):
    provider = FolderTransport(tmp_path / "provider")
    provider.put_doc(REACTION, {"v": {"newest": "✅"}, "ns": 1, "sig": "invalid"})
    mirror = CachingTransport(provider, auto_refresh=False)
    # Folder transports do not normally publish cloud cache identities. Give
    # this disposable mirror the same explicit stable identities as the
    # existing real-folder capture fixtures.
    mirror._mirror_root_identity = str(provider.root)
    mirror._mirror_cache_identity = "page-input-fixture-cache"
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    store.prepare_page_input_index()
    try:
        receipt = publish_overlay_source(mirror, store, CHAT)
        observed = store.capture_document_observation(receipt.position.source_id)
        index = store.publish_overlay_index(prepare_overlay_index(observed, CHAT))
        store.upsert_messages(CHAT, [_message("newest", 2)])
        real_capture = store.capture_page_inputs
        calls = 0

        def capture_then_insert_older(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_capture(*args, **kwargs)
            if calls == 1:
                store.upsert_messages(CHAT, [_message("older", 1)])
            return result

        monkeypatch.setattr(store, "capture_page_inputs", capture_then_insert_older)
        with pytest.raises(PageInputsChanged, match="message_position_changed"):
            capture_live_page_inputs(
                mirror, store, receipt, index, raw_limit=1,
            )
        assert calls == 2
    finally:
        store.close()
