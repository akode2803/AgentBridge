"""Lifecycle source publication and exact-subject capture fences."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge.mesh.lifecycle_source import (
    LifecycleSourceUnavailable,
    capture_lifecycle_inputs,
    publish_lifecycle_source,
)
from agentbridge.store import lifecycle_inputs
from agentbridge.store.db import DocumentObservationConflict, Store
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


ALICE_ONE = "lifecycle/alice/0001.json"
ALICE_TWO = "lifecycle/alice/0002.json"
BOB_ONE = "lifecycle/bob/0001.json"
LIFECYCLE = {
    ALICE_ONE: {"id": "alice-1", "name": "alice"},
    ALICE_TWO: {"id": "alice-2", "name": "alice"},
    BOB_ONE: {"id": "bob-1", "name": "bob"},
}


class FakeProvider(FolderTransport):
    scheme = "fake"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self, docs=None):
        self.root = "fake-root"
        self.cache_key = "fake-cache"
        self.docs = dict(docs or {})
        self.chat_ids = []
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
        **LIFECYCLE,
        "accounts/alice.json": {"not": "lifecycle"},
        "chats/room/meta.json": {"not": "lifecycle"},
    })
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "store.sqlite")
    lifecycle_inputs.prepare(store._conn())
    yield provider, mirror, store
    store.close()


def _decoded(selection):
    return [
        (record.path, None if record.deleted else json.loads(record.payload_json))
        for record in selection.records
    ]


def test_publication_and_capture_are_complete_per_subject_and_raw_only(environment):
    provider, mirror, store = environment
    receipt = publish_lifecycle_source(mirror, store)
    observed = store.capture_document_observation(receipt.position.source_id)
    assert observed.documents() == LIFECYCLE

    calls = provider.calls
    alice = capture_lifecycle_inputs(mirror, store, receipt, "alice")
    missing = capture_lifecycle_inputs(mirror, store, receipt, "missing")
    assert _decoded(alice) == [
        (ALICE_ONE, LIFECYCLE[ALICE_ONE]),
        (ALICE_TWO, LIFECYCLE[ALICE_TWO]),
    ]
    assert alice.prefix == "lifecycle/alice/"
    assert missing.records == () and missing.prefix == "lifecycle/missing/"
    assert provider.calls == calls


def test_full_republication_removes_departed_subject_range(environment):
    provider, mirror, store = environment
    first = publish_lifecycle_source(mirror, store)
    assert len(capture_lifecycle_inputs(mirror, store, first, "alice").records) == 2

    provider.docs.pop(ALICE_ONE)
    provider.docs.pop(ALICE_TWO)
    provider.cursor += 1
    mirror.refresh()
    second = publish_lifecycle_source(mirror, store)
    assert second.position.source_id == first.position.source_id
    assert second.position.generation > first.position.generation
    assert capture_lifecycle_inputs(mirror, store, second, "alice").records == ()
    assert _decoded(capture_lifecycle_inputs(mirror, store, second, "bob")) == [
        (BOB_ONE, LIFECYCLE[BOB_ONE]),
    ]


def test_bare_cold_and_bootstrap_only_transports_are_rejected(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    lifecycle_inputs.prepare(store._conn())
    provider = FakeProvider(LIFECYCLE)
    cold = CachingTransport(provider, auto_refresh=False)
    try:
        with pytest.raises(LifecycleSourceUnavailable, match="unsupported"):
            publish_lifecycle_source(FolderTransport(tmp_path / "folder"), store)
        with pytest.raises(LifecycleSourceUnavailable):
            publish_lifecycle_source(cold, store)

        snapshot = tmp_path / "mirror.json"
        observed = CachingTransport(provider, auto_refresh=False, snapshot_path=snapshot)
        observed.refresh()
        bootstrap = CachingTransport(provider, auto_refresh=False, snapshot_path=snapshot)
        assert bootstrap.mirror_status()["warm"]
        with pytest.raises(
            LifecycleSourceUnavailable,
            match="lifecycle_source_not_provider_observed",
        ):
            publish_lifecycle_source(bootstrap, store)
    finally:
        store.close()


def test_receipt_binds_store_source_and_mirror_identity(environment, tmp_path):
    _provider, mirror, store = environment
    receipt = publish_lifecycle_source(mirror, store)
    other = Store(tmp_path / "other.sqlite")
    lifecycle_inputs.prepare(other._conn())
    try:
        with pytest.raises(DocumentObservationConflict):
            capture_lifecycle_inputs(mirror, other, receipt, "alice")
        wrong_source = replace(
            receipt.position, source_id="lifecycle-v1:" + "0" * 64,
        )
        with pytest.raises(ValueError, match="source binding"):
            capture_lifecycle_inputs(
                mirror, store, replace(receipt, position=wrong_source), "alice",
            )
        with pytest.raises(TypeError, match="LifecycleSourceReceipt"):
            capture_lifecycle_inputs(mirror, store, object(), "alice")
    finally:
        other.close()


@pytest.mark.parametrize("domain", ["store", "mirror"])
def test_mutation_during_subject_capture_fails_final_bracket(
    environment, monkeypatch, domain,
):
    _provider, mirror, store = environment
    receipt = publish_lifecycle_source(mirror, store)
    original = lifecycle_inputs.capture_subject

    def capture_then_move(*args, **kwargs):
        result = original(*args, **kwargs)
        if domain == "store":
            store.invalidate_document_observation(receipt.position)
        else:
            mirror.put_doc(ALICE_ONE, {"id": "raced"})
        return result

    monkeypatch.setattr(lifecycle_inputs, "capture_subject", capture_then_move)
    with pytest.raises(LifecycleSourceUnavailable):
        capture_lifecycle_inputs(mirror, store, receipt, "alice")


def test_late_publication_after_mirror_change_is_unavailable(environment, monkeypatch):
    _provider, mirror, store = environment
    original = store.publish_document_batch
    committed = []

    def publish_then_move(*args, **kwargs):
        position = original(*args, **kwargs)
        committed.append(position)
        mirror.put_doc(ALICE_ONE, {"id": "late"})
        return position

    monkeypatch.setattr(store, "publish_document_batch", publish_then_move)
    with pytest.raises(LifecycleSourceUnavailable, match="mirror_changed"):
        publish_lifecycle_source(mirror, store)
    assert committed
    assert store.capture_document_position(committed[0].source_id) == committed[0]


def test_store_generation_and_fresh_mirror_instance_reject_old_receipt(
    environment, tmp_path,
):
    provider, mirror, store = environment
    receipt = publish_lifecycle_source(mirror, store)
    pending = store.invalidate_document_observation(receipt.position)
    with pytest.raises(DocumentObservationConflict):
        capture_lifecycle_inputs(mirror, store, receipt, "alice")
    store.publish_document_batch(
        pending, LIFECYCLE, cursor=pending.cursor, full=True,
        retain_tombstones=False,
    )
    with pytest.raises(DocumentObservationConflict):
        capture_lifecycle_inputs(mirror, store, receipt, "alice")

    snapshot = tmp_path / "replacement.json"
    fresh = CachingTransport(provider, auto_refresh=False, snapshot_path=snapshot)
    fresh.refresh()
    with pytest.raises(LifecycleSourceUnavailable):
        capture_lifecycle_inputs(fresh, store, receipt, "alice")
    fresh_receipt = publish_lifecycle_source(fresh, store)
    assert len(capture_lifecycle_inputs(
        fresh, store, fresh_receipt, "alice",
    ).records) == 2


def test_background_limit_failure_stays_pending_and_no_capture_full_scan(
    environment, monkeypatch,
):
    provider, mirror, store = environment
    with pytest.raises(LifecycleSourceUnavailable):
        publish_lifecycle_source(mirror, store, max_documents=1)

    receipt = publish_lifecycle_source(mirror, store)
    provider_calls = provider.calls

    def forbidden_full_scan(*_args, **_kwargs):
        raise AssertionError("foreground lifecycle capture attempted mirror scan")

    monkeypatch.setattr(mirror, "capture_mirror_selection", forbidden_full_scan)
    selected = capture_lifecycle_inputs(
        mirror, store, receipt, "alice", max_records=2, max_bytes=10_000,
    )
    assert len(selected.records) == 2
    assert provider.calls == provider_calls


def test_capture_row_and_byte_limits_are_explicit(environment):
    _provider, mirror, store = environment
    receipt = publish_lifecycle_source(mirror, store)
    with pytest.raises(OverflowError, match="row budget"):
        capture_lifecycle_inputs(mirror, store, receipt, "alice", max_records=1)
    baseline = capture_lifecycle_inputs(mirror, store, receipt, "alice")
    with pytest.raises(OverflowError, match="byte budget"):
        capture_lifecycle_inputs(
            mirror, store, receipt, "alice",
            max_bytes=baseline.serialized_bytes - 1,
        )
