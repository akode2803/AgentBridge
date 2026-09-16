"""Raw authority source publication and foreground capture fences."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge.mesh.authority_source import (
    AuthorityReadThroughRequired,
    AuthoritySourceUnavailable,
    capture_authority_inputs,
    capture_authority_subject,
    publish_authority_source,
)
from agentbridge.store import lifecycle_inputs
from agentbridge.store.db import DocumentObservationConflict, Store
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "room"
META = f"chats/{CHAT}/meta.json"
ALICE = "users/alice.json"
BOB = "users/bob.json"
ALICE_LIFECYCLE = "lifecycle/alice/0001.json"


class FakeProvider(FolderTransport):
    scheme = "fake"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self, docs=None):
        self.root = "authority-root"
        self.cache_key = "authority-cache"
        self.docs = dict(docs or {})
        self.cursor = 1
        self.calls = 0

    def snapshot_docs(self):
        self.calls += 1
        return dict(self.docs), self.cursor

    def get_docs_delta(self, cursor):
        self.calls += 1
        return {}, set(), self.cursor

    def list_chat_ids(self):
        self.calls += 1
        return [CHAT]

    def get_doc(self, path, default=None):
        self.calls += 1
        return self.docs.get(path, default)

    def put_doc(self, path, data):
        self.calls += 1
        self.docs[path] = data

    create_doc = put_doc

    def delete_doc(self, path):
        self.calls += 1
        self.docs.pop(path, None)

    def list_docs(self, prefix):
        self.calls += 1
        return sorted(path for path in self.docs if path.startswith(prefix))

    def delete_chat(self, chat_id): self.calls += 1
    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): return None
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def put_blob(self, path, data): return None
    def put_blob_from(self, local_src, path): return None
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


@pytest.fixture
def authority(tmp_path):
    provider = FakeProvider({
        META: {"id": CHAT, "members": ["alice"]},
        ALICE: {"name": "alice", "kind": "human"},
        ALICE_LIFECYCLE: {"id": "alice-bootstrap", "name": "alice"},
    })
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    lifecycle_inputs.prepare(store._conn())
    yield provider, mirror, store
    store.close()


def _records(inputs):
    return {
        record.path: (None if record.deleted else json.loads(record.payload_json))
        for record in inputs.documents.records
    }


def test_present_negative_and_offline_absence_are_distinct(authority):
    provider, mirror, store = authority
    receipt = publish_authority_source(mirror, store, CHAT)
    calls = provider.calls

    present = capture_authority_inputs(mirror, store, receipt, ("alice",))
    assert present.policy.accounts == ((ALICE, "present"),)
    assert _records(present) == {
        META: {"id": CHAT, "members": ["alice"]},
        ALICE: {"name": "alice", "kind": "human"},
    }

    with pytest.raises(AuthorityReadThroughRequired) as pending:
        capture_authority_inputs(mirror, store, receipt, ("bob",))
    assert pending.value.paths == (BOB,)
    assert provider.calls == calls

    assert mirror.get_doc(BOB) is None
    assert provider.calls == calls + 1
    negative_receipt = publish_authority_source(mirror, store, CHAT)
    negative = capture_authority_inputs(mirror, store, negative_receipt, ("bob",))
    assert negative.policy.accounts == ((BOB, "known_negative"),)
    assert _records(negative)[BOB] is None

    with mirror._lock:
        mirror._health_state = "cached"
        mirror._neg.discard(BOB)
    offline_receipt = publish_authority_source(mirror, store, CHAT)
    offline = capture_authority_inputs(mirror, store, offline_receipt, ("bob",))
    assert offline.policy.accounts == ((BOB, "offline_absent"),)
    assert _records(offline)[BOB] is None


@pytest.mark.parametrize("meta_value", [{"members": ["alice"]}, None])
def test_metadata_present_object_or_json_null_is_not_absence(tmp_path, meta_value):
    provider = FakeProvider({META: meta_value})
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    lifecycle_inputs.prepare(store._conn())
    try:
        receipt = publish_authority_source(mirror, store, CHAT)
        inputs = capture_authority_inputs(mirror, store, receipt)
        assert inputs.policy.meta == (META, "present")
        assert _records(inputs)[META] == meta_value
    finally:
        store.close()


def test_missing_metadata_remains_explicit_absence(tmp_path):
    provider = FakeProvider()
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    lifecycle_inputs.prepare(store._conn())
    try:
        receipt = publish_authority_source(mirror, store, CHAT)
        inputs = capture_authority_inputs(mirror, store, receipt)
        assert inputs.policy.meta == (META, "mirror_absent")
        assert _records(inputs) == {META: None}
    finally:
        store.close()


def test_republication_replaces_and_tombstones_raw_documents(authority):
    provider, mirror, store = authority
    first = publish_authority_source(mirror, store, CHAT)
    provider.docs[ALICE] = {"name": "alice", "kind": "agent"}
    provider.docs.pop(META)
    provider.cursor += 1
    mirror.refresh()
    second = publish_authority_source(mirror, store, CHAT)

    assert second.position.generation > first.position.generation
    inputs = capture_authority_inputs(mirror, store, second, ("alice",))
    assert inputs.policy.meta == (META, "mirror_absent")
    assert _records(inputs) == {
        META: None,
        ALICE: {"name": "alice", "kind": "agent"},
    }
    with pytest.raises(AuthoritySourceUnavailable):
        capture_authority_inputs(mirror, store, first, ("alice",))


def test_receipt_binds_database_source_and_mirror(authority, tmp_path):
    _provider, mirror, store = authority
    receipt = publish_authority_source(mirror, store, CHAT)
    other = Store(tmp_path / "other.sqlite")
    lifecycle_inputs.prepare(other._conn())
    try:
        with pytest.raises(DocumentObservationConflict):
            capture_authority_inputs(mirror, other, receipt, ("alice",))
        with pytest.raises(ValueError, match="source binding"):
            capture_authority_inputs(
                mirror,
                store,
                replace(receipt, position=replace(receipt.position, source_id="wrong")),
                ("alice",),
            )
        replacement = CachingTransport(_provider, auto_refresh=False)
        replacement.refresh()
        with pytest.raises(AuthoritySourceUnavailable):
            capture_authority_inputs(replacement, store, receipt, ("alice",))
    finally:
        other.close()


@pytest.mark.parametrize("race", ["source", "mirror", "policy"])
def test_capture_rejects_source_mirror_and_policy_races(authority, monkeypatch, race):
    _provider, mirror, store = authority
    receipt = publish_authority_source(mirror, store, CHAT)
    original = store.capture_selected_documents

    def capture_then_race(*args, **kwargs):
        result = original(*args, **kwargs)
        if race == "source":
            store.invalidate_document_observation(receipt.position)
        elif race == "mirror":
            mirror.put_doc(META, {"id": "changed"})
        else:
            with mirror._lock:
                mirror._docs.pop(ALICE, None)
                mirror._neg.add(ALICE)
        return result

    monkeypatch.setattr(store, "capture_selected_documents", capture_then_race)
    with pytest.raises((AuthoritySourceUnavailable, DocumentObservationConflict)):
        capture_authority_inputs(mirror, store, receipt, ("alice",))


def test_subject_capture_uses_same_source_and_has_final_fences(authority, monkeypatch):
    provider, mirror, store = authority
    receipt = publish_authority_source(mirror, store, CHAT)
    calls = provider.calls
    selected = capture_authority_subject(mirror, store, receipt, "alice")
    assert [record.path for record in selected.records] == [ALICE_LIFECYCLE]
    assert provider.calls == calls

    original = lifecycle_inputs.capture_subject

    def capture_then_invalidate(*args, **kwargs):
        result = original(*args, **kwargs)
        store.invalidate_document_observation(receipt.position)
        return result

    monkeypatch.setattr(lifecycle_inputs, "capture_subject", capture_then_invalidate)
    with pytest.raises(AuthoritySourceUnavailable, match="source_changed"):
        capture_authority_subject(mirror, store, receipt, "alice")


def test_foreground_capture_never_calls_provider_or_serializes(authority, monkeypatch):
    provider, mirror, store = authority
    receipt = publish_authority_source(mirror, store, CHAT)
    calls = provider.calls

    def forbidden(*_args, **_kwargs):
        raise AssertionError("foreground capture attempted provider/serialization work")

    monkeypatch.setattr(provider, "get_doc", forbidden)
    monkeypatch.setattr(provider, "snapshot_docs", forbidden)
    monkeypatch.setattr(mirror, "capture_mirror_selection", forbidden)
    inputs = capture_authority_inputs(mirror, store, receipt, ("alice",))
    subject = capture_authority_subject(mirror, store, receipt, "alice")
    assert inputs.documents.document(ALICE)["name"] == "alice"
    assert subject.records[0].path == ALICE_LIFECYCLE
    assert provider.calls == calls
