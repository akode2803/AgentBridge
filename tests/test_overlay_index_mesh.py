"""Mesh preparation parity and source fences for indexed overlay candidates."""
from __future__ import annotations

import pytest

from agentbridge.mesh.events import reaction_signing_bytes, state_signing_bytes
from agentbridge.mesh.overlay_index import (
    capture_indexed_overlay_inputs,
    prepare_overlay_index,
)
from agentbridge.mesh.overlay_source import (
    OverlaySourceUnavailable,
    publish_overlay_source,
)
from agentbridge.store.overlay_index import OverlayIndexUnavailable
from agentbridge.mesh.overlays import UserState, reaction_map
from agentbridge.store.db import DocumentObservationConflict, Store
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "room"
REACTION = f"chats/{CHAT}/overlays/reactions/alice.json"
STATE = f"chats/{CHAT}/overlays/state/alice.json"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _observation(store, documents, *, deleted_paths=()):
    source = "overlay-source"
    position = store.publish_document_batch(
        store.capture_document_position(source), documents, cursor=1,
        full=True,
    )
    if deleted_paths:
        position = store.publish_document_batch(
            position, {}, cursor=2, deleted_paths=deleted_paths,
        )
    return store.capture_document_observation(position.source_id)


def _document(prepared, path):
    return next(item for item in prepared.documents if item.path == path)


class SnapshotProvider(FolderTransport):
    """Disposable provider with stable identities and in-memory documents."""

    scheme = "snapshot"
    profile = TransportProfile()

    def __init__(self, documents):
        self.root = "overlay-index-root"
        self.cache_key = "overlay-index-cache"
        self.docs = dict(documents)

    def snapshot_docs(self): return dict(self.docs), 1
    def list_chat_ids(self): return [CHAT]
    def get_doc(self, path, default=None): return self.docs.get(path, default)
    def put_doc(self, path, data): self.docs[path] = data
    create_doc = put_doc
    def delete_doc(self, path): self.docs.pop(path, None)
    def list_docs(self, prefix):
        return sorted(path for path in self.docs if path.startswith(prefix))
    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): return None
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def delete_chat(self, chat_id): return None
    def put_blob(self, path, data): return None
    def put_blob_from(self, local_src, path): return None
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


def test_reaction_candidates_match_reaction_map_and_existing_signing_recipe(store):
    doc = {
        "v": {"m-valid": "✅", "m-empty": "", "m-number": 7},
        "ns": 42,
        "sig": "candidate-signature-not-yet-verified",
        "ignored": {"future": True},
    }
    observed = _observation(store, {REACTION: doc})
    prepared = prepare_overlay_index(observed, CHAT)
    mapping = reaction_map(doc)
    indexed = _document(prepared, REACTION)

    assert mapping == {"m-valid": "✅"}
    assert indexed.signing_bytes == reaction_signing_bytes(
        CHAT, "alice", doc["ns"], mapping)
    assert indexed.signature == doc["sig"]
    assert not indexed.empty and indexed.shape_error == ""
    assert [(item.kind, item.target, item.value) for item in prepared.candidates] == [
        ("reaction", "m-valid", "✅"),
    ]


def test_invalid_signature_is_preserved_without_dropping_candidates(store):
    doc = {"v": {"m1": "👀", "m2": "👍"}, "ns": 7, "sig": "invalid"}
    prepared = prepare_overlay_index(_observation(store, {REACTION: doc}), CHAT)
    indexed = _document(prepared, REACTION)

    assert indexed.signature == "invalid"
    assert indexed.signing_bytes is not None
    assert {(item.target, item.value) for item in prepared.candidates} == {
        ("m1", "👀"), ("m2", "👍"),
    }


@pytest.mark.parametrize(("raw_ns", "converted"), [
    (pytest.param(None, 0, id="missing")),
    (True, 1),
    ("17", 17),
    (8.75, 8),
])
def test_reaction_ns_uses_existing_int_conversion_for_signing(
        store, raw_ns, converted):
    doc = {"v": {"m1": "✅"}, "sig": "candidate"}
    if raw_ns is not None:
        doc["ns"] = raw_ns
    prepared = prepare_overlay_index(_observation(store, {REACTION: doc}), CHAT)
    indexed = _document(prepared, REACTION)

    assert indexed.signing_bytes == reaction_signing_bytes(
        CHAT, "alice", converted, {"m1": "✅"})
    assert indexed.shape_error == ""
    assert [(item.target, item.value) for item in prepared.candidates] == [
        ("m1", "✅"),
    ]


@pytest.mark.parametrize("bad_ns", ["not-a-number", None, {}, []])
def test_bad_reaction_ns_is_explicit_but_candidates_remain_pending(
        store, bad_ns):
    doc = {"v": {"m1": "👀"}, "ns": bad_ns, "sig": "candidate"}
    prepared = prepare_overlay_index(_observation(store, {REACTION: doc}), CHAT)
    indexed = _document(prepared, REACTION)

    assert indexed.signing_bytes is None
    assert indexed.shape_error == "signing_input"
    assert [(item.kind, item.target, item.value) for item in prepared.candidates] == [
        ("reaction", "m1", "👀"),
    ]


def test_legacy_bare_reaction_map_and_non_dict_document_match_current_parser(store):
    bare = f"chats/{CHAT}/overlays/reactions/bare.json"
    non_dict = f"chats/{CHAT}/overlays/reactions/list.json"
    bare_doc = {"m2": "👍", "empty": ""}
    observed = _observation(store, {bare: bare_doc, non_dict: ["m3", "✅"]})
    prepared = prepare_overlay_index(observed, CHAT)

    bare_index = _document(prepared, bare)
    assert bare_index.signing_bytes == reaction_signing_bytes(
        CHAT, "bare", 0, reaction_map(bare_doc))
    assert [(item.path, item.target, item.value) for item in prepared.candidates] == [
        (bare, "m2", "👍"),
    ]
    list_index = _document(prepared, non_dict)
    assert list_index.empty and list_index.shape_error == "document_shape"
    assert list_index.signing_bytes == reaction_signing_bytes(
        CHAT, "list", 0, {})


def test_non_dict_v_and_signature_shapes_are_explicit_without_invented_candidates(
        store):
    bad_map = f"chats/{CHAT}/overlays/reactions/bad-map.json"
    bad_sig = f"chats/{CHAT}/overlays/reactions/bad-sig.json"
    falsey_sigs = {
        f"chats/{CHAT}/overlays/reactions/none-sig.json": (None, "m3"),
        f"chats/{CHAT}/overlays/reactions/false-sig.json": (False, "m4"),
        f"chats/{CHAT}/overlays/reactions/zero-sig.json": (0, "m5"),
    }
    observed = _observation(store, {
        bad_map: {"v": ["m1", "✅"], "ns": 1, "sig": "candidate"},
        bad_sig: {"v": {"m2": "👍"}, "ns": 2, "sig": 7},
        **{
            path: {"v": {target: "👀"}, "ns": 3, "sig": signature}
            for path, (signature, target) in falsey_sigs.items()
        },
    })
    prepared = prepare_overlay_index(observed, CHAT)

    assert _document(prepared, bad_map).shape_error == "reaction_map"
    assert not any(item.path == bad_map for item in prepared.candidates)
    invalid = _document(prepared, bad_sig)
    assert invalid.shape_error == "signature_shape" and invalid.signature == ""
    assert any(item.path == bad_sig and item.target == "m2"
               for item in prepared.candidates)
    for path, (_signature, target) in falsey_sigs.items():
        malformed = _document(prepared, path)
        assert malformed.shape_error == "signature_shape"
        assert malformed.signature == ""
        assert any(item.path == path and item.target == target
                   for item in prepared.candidates)


def test_state_unknown_fields_remain_inside_existing_signed_field_payload(store):
    doc = {
        "ns": 91,
        "sig": "unverified-state-signature",
        "hidden": ["m2", "m1", "m2"],
        "starred": ["m3"],
        "read_ns": 55,
        "future_policy": {"mode": "strict", "generation": 3},
    }
    prepared = prepare_overlay_index(_observation(store, {STATE: doc}), CHAT)
    indexed = _document(prepared, STATE)
    fields = UserState.signed_fields(doc)

    assert "future_policy" in fields
    assert indexed.signing_bytes == state_signing_bytes(
        CHAT, "alice", doc["ns"], fields)
    assert indexed.signature == doc["sig"]
    assert indexed.scalars_json == '{"read_ns":55}'
    assert {(item.kind, item.target) for item in prepared.candidates} == {
        ("hidden", "m1"), ("hidden", "m2"), ("starred", "m3"),
    }


def test_state_string_and_dict_ids_follow_existing_set_iterable_semantics(store):
    doc = {
        "ns": 5,
        "hidden": "baab",
        "starred": {"m2": False, "m1": True},
    }
    prepared = prepare_overlay_index(_observation(store, {STATE: doc}), CHAT)
    assert _document(prepared, STATE).shape_error == ""
    assert [(item.kind, item.target) for item in prepared.candidates] == [
        ("hidden", "a"), ("hidden", "b"),
        ("starred", "m1"), ("starred", "m2"),
    ]


def test_legacy_clear_delete_and_scalar_shapes_are_preserved_verbatim(store):
    doc = {
        "ns": 6,
        "cleared": {"ns": "12", "keep_starred": 1, "at": "legacy"},
        "deleted": True,
        "read_ns": "9",
        "delivered_ns": 7.5,
        "mute": {"until": 10},
        "archived": 1,
        "pinned": "legacy-truthy",
        "hidden_runtime": ["not-an-indexed-scalar"],
    }
    prepared = prepare_overlay_index(_observation(store, {STATE: doc}), CHAT)
    indexed = _document(prepared, STATE)

    assert indexed.signing_bytes == state_signing_bytes(
        CHAT, "alice", 6, UserState.signed_fields(doc))
    assert indexed.scalars_json == (
        '{"archived":1,"cleared":{"at":"legacy","keep_starred":1,'
        '"ns":"12"},"deleted":true,"delivered_ns":7.5,'
        '"mute":{"until":10},"pinned":"legacy-truthy","read_ns":"9"}'
    )


def test_empty_absent_and_nonempty_unsigned_state_remain_distinct(store):
    empty = f"chats/{CHAT}/overlays/state/empty.json"
    unsigned = f"chats/{CHAT}/overlays/state/unsigned.json"
    absent = f"chats/{CHAT}/overlays/state/absent.json"
    observed = _observation(
        store,
        {empty: {}, unsigned: {"hidden": ["m1"]}},
        deleted_paths=(absent,),
    )
    prepared = prepare_overlay_index(observed, CHAT)

    assert {item.path for item in prepared.documents} == {empty, unsigned}
    empty_doc = _document(prepared, empty)
    unsigned_doc = _document(prepared, unsigned)
    assert empty_doc.empty and empty_doc.signature == ""
    assert empty_doc.signing_bytes == state_signing_bytes(CHAT, "empty", 0, {})
    assert not unsigned_doc.empty and unsigned_doc.signature == ""
    assert unsigned_doc.signing_bytes == state_signing_bytes(
        CHAT, "unsigned", 0, {"hidden": ["m1"]})
    assert [(item.path, item.kind, item.target) for item in prepared.candidates] == [
        (unsigned, "hidden", "m1"),
    ]


@pytest.mark.parametrize("bad_field", [
    {"hidden": None, "starred": ["otherwise-valid"]},
    {"hidden": ["valid", 7], "starred": ["otherwise-valid"]},
    {"hidden": ["valid"], "starred": [""]},
])
def test_malformed_hidden_or_starred_is_explicit_and_emits_no_partial_candidates(
        store, bad_field):
    doc = {"ns": 8, "sig": "unverified", **bad_field}
    prepared = prepare_overlay_index(_observation(store, {STATE: doc}), CHAT)
    indexed = _document(prepared, STATE)

    assert indexed.shape_error == "viewer_ids"
    assert indexed.signing_bytes == state_signing_bytes(
        CHAT, "alice", 8, UserState.signed_fields(doc))
    assert prepared.candidates == ()


def test_indexed_capture_requires_current_source_and_originating_mirror(tmp_path):
    provider = SnapshotProvider({
        REACTION: {"v": {"m1": "✅"}, "ns": 1, "sig": "invalid"},
    })
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    try:
        receipt = publish_overlay_source(mirror, store, CHAT)
        observed = store.capture_document_observation(receipt.position.source_id)
        position = store.publish_overlay_index(prepare_overlay_index(observed, CHAT))

        captured = capture_indexed_overlay_inputs(
            mirror, store, receipt, position, ("m1",))
        assert [(item.kind, item.target, item.value)
                for item in captured.candidates] == [("reaction", "m1", "✅")]

        store.invalidate_document_observation(receipt.position)
        with pytest.raises(DocumentObservationConflict):
            capture_indexed_overlay_inputs(
                mirror, store, receipt, position, ("m1",))

        # Rebuild a fresh source/index, then move the originating live mirror.
        fresh = publish_overlay_source(mirror, store, CHAT)
        fresh_observed = store.capture_document_observation(fresh.position.source_id)
        fresh_index = store.publish_overlay_index(
            prepare_overlay_index(fresh_observed, CHAT))
        mirror.put_doc(f"chats/{CHAT}/keys/2.json", {"key": "changed"})
        with pytest.raises(OverlaySourceUnavailable):
            capture_indexed_overlay_inputs(
                mirror, store, fresh, fresh_index, ("m1",))
    finally:
        store.close()


def test_combined_helper_rejects_index_rebuilt_after_first_capture(
        tmp_path, monkeypatch):
    provider = SnapshotProvider({
        REACTION: {"v": {"m1": "✅"}, "ns": 1, "sig": "invalid"},
    })
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    try:
        receipt = publish_overlay_source(mirror, store, CHAT)
        observed = store.capture_document_observation(receipt.position.source_id)
        prepared = prepare_overlay_index(observed, CHAT)
        position = store.publish_overlay_index(prepared)
        real_capture = store.capture_overlay_index
        calls = 0

        def capture_then_rebuild(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_capture(*args, **kwargs)
            if calls == 1:
                replacement = store.publish_overlay_index(prepared)
                assert replacement.build != position.build
            return result

        monkeypatch.setattr(store, "capture_overlay_index", capture_then_rebuild)
        with pytest.raises(OverlayIndexUnavailable):
            capture_indexed_overlay_inputs(
                mirror, store, receipt, position, ("m1",))
        assert calls == 2
    finally:
        store.close()
