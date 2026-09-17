"""Opt-in overlay index publication from a broader complete local source."""
from __future__ import annotations

import pytest

from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.store import overlay_index
from agentbridge.store.db import Store


CHAT = "room"
STATE = f"chats/{CHAT}/overlays/state/alice.json"
REACTION = f"chats/{CHAT}/overlays/reactions/alice.json"
OTHER_STATE = "chats/other/overlays/state/alice.json"
SOURCE = "shared-overlay-source"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    try:
        yield opened
    finally:
        opened.close()


def _publish(store, documents, expected=None):
    if expected is None:
        expected = store.capture_document_position(SOURCE)
    return store.publish_document_batch(
        expected, documents, cursor=0, full=True, retain_tombstones=False,
    )


def _prepared(store, position, paths=(STATE, REACTION)):
    selected = store.capture_selected_documents(
        position, paths, max_documents=len(paths), max_bytes=64 * 1024,
    )
    return prepare_overlay_index(selected, CHAT)


def _documents():
    return {
        "users/alice.json": {"name": "alice"},
        f"chats/{CHAT}/meta.json": {"id": CHAT},
        STATE: {"ns": 1, "hidden": [], "starred": []},
        REACTION: {"ns": 1, "v": {"m1": "ok"}},
    }


def test_default_rejects_broad_source_but_explicit_shared_mode_builds(store):
    position = _publish(store, _documents())
    prepared = _prepared(store, position)
    with pytest.raises(ValueError, match="unsupported overlay path"):
        store.publish_overlay_index(prepared)

    indexed = store.publish_overlay_index(prepared, shared_source=True)
    assert indexed.source == position and indexed.chat_id == CHAT
    captured = store.capture_overlay_index(indexed, ("m1",), (STATE,))
    assert {document.path for document in captured.documents} == {STATE, REACTION}
    assert [candidate.target for candidate in captured.candidates] == ["m1"]


@pytest.mark.parametrize("missing", [STATE, REACTION])
def test_shared_mode_requires_every_live_state_and_reaction_document(store, missing):
    position = _publish(store, _documents())
    paths = tuple(path for path in (STATE, REACTION) if path != missing)
    with pytest.raises(overlay_index.OverlayIndexUnavailable,
                       match="incomplete_prepared_documents"):
        store.publish_overlay_index(
            _prepared(store, position, paths), shared_source=True,
        )


def test_shared_mode_rejects_unsupported_path_inside_bound_overlay_prefix(store):
    documents = _documents()
    documents[f"chats/{CHAT}/overlays/unknown/x.json"] = {"bad": True}
    position = _publish(store, documents)
    with pytest.raises(ValueError, match="unsupported overlay path"):
        store.publish_overlay_index(
            _prepared(store, position), shared_source=True,
        )


def test_shared_mode_excludes_another_chat_and_unrelated_documents(store):
    documents = {**_documents(), OTHER_STATE: {"hidden": ["secret"]}}
    position = _publish(store, documents)
    indexed = store.publish_overlay_index(
        _prepared(store, position), shared_source=True,
    )
    selected = store.capture_overlay_index(indexed, (), (STATE,))
    assert all(document.path != OTHER_STATE for document in selected.documents)
    assert store._conn().execute(
        "SELECT count(*) FROM overlay_index_docs WHERE source=? AND path=?",
        (SOURCE, OTHER_STATE),
    ).fetchone() == (0,)


def test_shared_mode_rejects_stale_raw_source_cas(store):
    first = _publish(store, _documents())
    prepared = _prepared(store, first)
    changed = _documents()
    changed[STATE] = {"ns": 2, "hidden": ["m1"], "starred": []}
    _publish(store, changed, expected=first)
    with pytest.raises(overlay_index.OverlayIndexUnavailable,
                       match="source_changed"):
        store.publish_overlay_index(prepared, shared_source=True)


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_shared_source_flag_requires_exact_bool(store, value):
    position = _publish(store, _documents())
    with pytest.raises(ValueError, match="shared_source must be a bool"):
        store.publish_overlay_index(_prepared(store, position), shared_source=value)
