"""Focused publication/read fences for bounded raw overlay inputs."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge.mesh.overlay_source import (
    OverlaySourceReceipt,
    OverlaySourceUnavailable,
    capture_overlay_inputs,
    publish_overlay_source,
)
from agentbridge.store.db import DocumentObservationConflict, Store
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "room"
EDIT = f"chats/{CHAT}/overlays/edits/m1.json"
REDACTION = f"chats/{CHAT}/overlays/redactions/m2.json"
REACTION = f"chats/{CHAT}/overlays/reactions/alice.json"
PIN = f"chats/{CHAT}/overlays/pins/m3.json"
STATE = f"chats/{CHAT}/overlays/state/alice.json"
OVERLAYS = {
    EDIT: {"body": "edited"},
    REDACTION: {"by": "alice"},
    REACTION: {"v": {"m1": "👍"}},
    PIN: {"by": "alice"},
    STATE: {"starred": ["m1"]},
}


class FakeProvider(FolderTransport):
    """Memory provider with real CachingTransport ownership semantics."""

    scheme = "fake"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self, docs=None):
        self.root = "fake-root"
        self.cache_key = "fake-cache"
        self.docs = dict(docs or {})
        self.chat_ids = [CHAT]
        self.cursor = 1
        self.delta = ({}, set(), 1)
        self.calls = 0

    def snapshot_docs(self):
        self.calls += 1
        return dict(self.docs), self.cursor

    def get_docs_delta(self, cursor):
        self.calls += 1
        return self.delta

    def list_chat_ids(self):
        self.calls += 1
        return list(self.chat_ids)

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

    def delete_chat(self, chat_id):
        self.calls += 1
        prefix = f"chats/{chat_id}/"
        self.docs = {p: v for p, v in self.docs.items() if not p.startswith(prefix)}
        self.chat_ids = [item for item in self.chat_ids if item != chat_id]

    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): return None
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def put_blob(self, path, data): return None
    def put_blob_from(self, local_src, path): return None
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


@pytest.fixture
def environment(tmp_path):
    provider = FakeProvider({
        **OVERLAYS,
        "chats/other/overlays/state/alice.json": {"hidden": ["secret"]},
        f"chats/{CHAT}/keys/1.json": {"key": "not-an-overlay"},
        "users/alice.json": {"trust": "not-an-overlay"},
        "runtime/session.json": {"session": "not-an-overlay"},
    })
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    yield provider, mirror, store
    store.close()


def _published(mirror, store):
    return publish_overlay_source(mirror, store, CHAT)


def test_publish_selects_only_observed_overlay_prefixes_and_capture_is_exact(
        environment):
    provider, mirror, store = environment
    receipt = _published(mirror, store)
    observed = store.capture_document_observation(receipt.position.source_id)
    assert observed.documents() == OVERLAYS

    calls = provider.calls
    selected = capture_overlay_inputs(
        mirror, store, receipt, (STATE, EDIT, f"chats/{CHAT}/overlays/pins/missing.json"),
    )
    assert selected.position == receipt.position
    assert selected.decoded_records() == [
        (STATE, OVERLAYS[STATE], False),
        (EDIT, OVERLAYS[EDIT], False),
        (f"chats/{CHAT}/overlays/pins/missing.json", None, True),
    ]
    assert provider.calls == calls, "bounded capture must remain mirror/SQLite local"


@pytest.mark.parametrize("path", [
    "chats/other/overlays/state/alice.json",
    f"chats/{CHAT}/keys/1.json",
    "users/alice.json",
    "runtime/session.json",
    f"chats/{CHAT}/overlays/unknown/x.json",
])
def test_capture_allowlist_rejects_other_chat_key_trust_session_and_kind(
        environment, path):
    _provider, mirror, store = environment
    receipt = _published(mirror, store)
    with pytest.raises(ValueError, match="exact overlay paths"):
        capture_overlay_inputs(mirror, store, receipt, (path,))


def test_unrelated_oversized_document_is_not_charged_or_materialized(tmp_path):
    huge = "x" * (2 * 1024 * 1024)
    provider = FakeProvider({**OVERLAYS, "unrelated/huge.json": huge})
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    try:
        receipt = publish_overlay_source(mirror, store, CHAT, max_bytes=16_000)
        captured = capture_overlay_inputs(
            mirror, store, receipt, (EDIT,), max_bytes=1_000)
        assert captured.documents() == {EDIT: OVERLAYS[EDIT]}
        assert "unrelated/huge.json" not in store.capture_document_observation(
            receipt.position.source_id).documents()
    finally:
        store.close()


@pytest.mark.parametrize("mutation", [
    "local_put", "local_delete", "full_refresh", "full_revocation",
    "delta_refresh", "delta_revocation", "readthrough", "delete_chat",
])
def test_every_representative_mirror_mutation_rejects_old_receipt(
        environment, mutation):
    provider, mirror, store = environment
    receipt = _published(mirror, store)

    if mutation == "local_put":
        mirror.put_doc(STATE, {"starred": ["m2"]})
    elif mutation == "local_delete":
        mirror.delete_doc(REACTION)
    elif mutation == "full_refresh":
        provider.docs[EDIT] = {"body": "provider changed"}
        provider.cursor += 1
        mirror.refresh()
    elif mutation == "full_revocation":
        provider.docs = {p: v for p, v in provider.docs.items()
                         if not p.startswith(f"chats/{CHAT}/")}
        provider.chat_ids = []
        provider.cursor += 1
        mirror.refresh()
    elif mutation == "delta_refresh":
        provider.delta = ({EDIT: {"body": "delta changed"}}, set(), 2)
        mirror._refresh_delta()
    elif mutation == "delta_revocation":
        provider.chat_ids = []
        provider.delta = ({}, set(), 2)
        mirror._refresh_delta()
    elif mutation == "readthrough":
        provider.docs[f"chats/{CHAT}/keys/99.json"] = {"key": "new"}
        assert mirror.get_doc(f"chats/{CHAT}/keys/99.json") == {"key": "new"}
    elif mutation == "delete_chat":
        mirror.delete_chat(CHAT)

    with pytest.raises(OverlaySourceUnavailable):
        capture_overlay_inputs(mirror, store, receipt, (EDIT,))


def test_snapshot_hydration_and_new_instance_cannot_reuse_old_receipt(
        environment, tmp_path):
    provider, mirror, store = environment
    receipt = _published(mirror, store)
    snapshot = tmp_path / "mirror.json"
    snapshot.write_text(json.dumps({
        "v": 3, "root": provider.root, "cache_key": provider.cache_key,
        "docs": provider.docs, "chat_ids": provider.chat_ids,
        "cursor": provider.cursor, "provenance": "provider_observed",
    }), encoding="utf-8")
    replacement = CachingTransport(
        provider, auto_refresh=False, snapshot_path=snapshot)
    replacement.refresh()
    with pytest.raises(OverlaySourceUnavailable):
        capture_overlay_inputs(replacement, store, receipt, (EDIT,))


def test_three_fresh_mirror_instances_reuse_one_store_source_and_retire_receipts(
        tmp_path):
    provider = FakeProvider({EDIT: {"version": 1}})
    store = Store(tmp_path / "store.sqlite")
    mirrors = []
    receipts = []
    try:
        for version in (1, 2, 3):
            provider.docs = {EDIT: {"version": version}}
            provider.cursor = version
            mirror = CachingTransport(provider, auto_refresh=False)
            mirror.refresh()
            mirrors.append(mirror)
            receipts.append(publish_overlay_source(mirror, store, CHAT))
        assert len({receipt.position.source_id for receipt in receipts}) == 1
        assert [receipt.position.generation for receipt in receipts] == [2, 4, 6]
        assert capture_overlay_inputs(
            mirrors[-1], store, receipts[-1], (EDIT,)
        ).documents() == {EDIT: {"version": 3}}
        for mirror, stale in zip(mirrors[:-1], receipts[:-1]):
            with pytest.raises(DocumentObservationConflict):
                capture_overlay_inputs(mirror, store, stale, (EDIT,))
        with pytest.raises(OverlaySourceUnavailable):
            capture_overlay_inputs(mirrors[-1], store, receipts[0], (EDIT,))
    finally:
        store.close()


def test_full_replacement_retires_rows_and_keeps_small_budget_bounded(tmp_path):
    provider = FakeProvider({EDIT: {"round": 1}})
    store = Store(tmp_path / "store.sqlite")
    paths = (EDIT, REACTION, STATE)
    receipts = []
    try:
        for round_no, path in enumerate(paths, 1):
            provider.docs = {path: {"round": round_no}}
            provider.cursor = round_no
            mirror = CachingTransport(provider, auto_refresh=False)
            mirror.refresh()
            receipt = publish_overlay_source(
                mirror, store, CHAT, max_documents=1, max_bytes=2_000)
            receipts.append(receipt)
            observation = store.capture_document_observation(receipt.position.source_id)
            assert observation.documents() == {path: {"round": round_no}}
            count = store._conn().execute(
                "SELECT count(*) FROM document_observation_records WHERE source_id=?",
                (receipt.position.source_id,),
            ).fetchone()[0]
            assert count == 1
        assert len({receipt.position.source_id for receipt in receipts}) == 1
    finally:
        store.close()


def test_persisted_bootstrap_mirror_is_rejected_until_provider_observed(tmp_path):
    provider = FakeProvider({EDIT: {"version": 1}})
    snapshot = tmp_path / "mirror.json"
    observed = CachingTransport(
        provider, auto_refresh=False, snapshot_path=snapshot)
    observed.refresh()
    assert snapshot.exists()
    bootstrap = CachingTransport(
        provider, auto_refresh=False, snapshot_path=snapshot)
    store = Store(tmp_path / "store.sqlite")
    try:
        assert bootstrap.mirror_status()["warm"]
        with pytest.raises(OverlaySourceUnavailable, match="bootstrap_unverified"):
            publish_overlay_source(bootstrap, store, CHAT)
        bootstrap.refresh()
        assert publish_overlay_source(bootstrap, store, CHAT).position.initialized
    finally:
        store.close()


@pytest.mark.parametrize("domain", ["store", "mirror"])
def test_mutation_during_selected_capture_fails_final_bracket(
        environment, monkeypatch, domain):
    _provider, mirror, store = environment
    receipt = _published(mirror, store)
    capture = store.capture_selected_documents

    def capture_then_move(*args, **kwargs):
        result = capture(*args, **kwargs)
        if domain == "store":
            store.invalidate_document_observation(receipt.position)
        else:
            mirror.put_doc(STATE, {"starred": ["raced"]})
        return result

    monkeypatch.setattr(store, "capture_selected_documents", capture_then_move)
    expected = (DocumentObservationConflict if domain == "store"
                else OverlaySourceUnavailable)
    with pytest.raises(expected):
        capture_overlay_inputs(mirror, store, receipt, (EDIT,))


def test_late_publication_after_mirror_change_returns_unavailable(
        environment, monkeypatch):
    _provider, mirror, store = environment
    publish = store.publish_document_batch
    committed = []

    def publish_then_move(*args, **kwargs):
        result = publish(*args, **kwargs)
        committed.append(result)
        mirror.put_doc(STATE, {"starred": ["late"]})
        return result

    monkeypatch.setattr(store, "publish_document_batch", publish_then_move)
    with pytest.raises(OverlaySourceUnavailable):
        publish_overlay_source(mirror, store, CHAT)
    assert committed and store.capture_document_position(
        committed[0].source_id) == committed[0]


def test_pending_or_competing_sqlite_publication_prevents_old_receipt_read(
        environment):
    _provider, mirror, store = environment
    receipt = _published(mirror, store)
    pending = store.invalidate_document_observation(receipt.position)
    with pytest.raises(DocumentObservationConflict):
        capture_overlay_inputs(mirror, store, receipt, (EDIT,))
    replacement = store.publish_document_batch(
        pending, {EDIT: {"body": "competitor"}}, cursor=1, full=True)
    with pytest.raises(DocumentObservationConflict):
        capture_overlay_inputs(mirror, store, receipt, (EDIT,))
    assert capture_overlay_inputs(
        mirror, store, OverlaySourceReceipt(CHAT, replacement, receipt.mirror), (EDIT,),
    ).documents() == {EDIT: {"body": "competitor"}}


def test_bare_folder_is_honestly_unsupported_and_store_unchanged(tmp_path):
    transport = FolderTransport(tmp_path / "folder")
    store = Store(tmp_path / "store.sqlite")
    try:
        before = store.capture_document_position("unrelated")
        with pytest.raises(OverlaySourceUnavailable, match="unsupported"):
            publish_overlay_source(transport, store, CHAT)
        assert store.capture_document_position("unrelated") == before
    finally:
        store.close()


def test_receipt_binds_exact_source_store_path_and_incarnation(environment, tmp_path):
    _provider, mirror, store = environment
    receipt = _published(mirror, store)
    other = Store(tmp_path / "other.sqlite")
    try:
        with pytest.raises(DocumentObservationConflict):
            capture_overlay_inputs(mirror, other, receipt, (EDIT,))
        with pytest.raises(ValueError, match="source binding"):
            capture_overlay_inputs(
                mirror, store, replace(receipt, chat_id="other"), (EDIT,))
        with pytest.raises(DocumentObservationConflict):
            capture_overlay_inputs(
                mirror, store,
                replace(receipt, position=replace(
                    receipt.position, incarnation="different-incarnation")),
                (EDIT,),
            )
    finally:
        other.close()
