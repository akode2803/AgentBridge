"""Diagnostic overlap of a process mirror and a committed local Store cut."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from agentbridge.mesh.local_observation import (
    LocalOverlapObservation,
    LocalOverlapUnavailable,
    capture_local_overlap,
)
from agentbridge.store import chat_inputs
from agentbridge.store.db import Store
from agentbridge.transport.base import Transport, Watcher
from agentbridge.transport.cache import CachingTransport


class MemoryTransport(Transport):
    scheme = "memory"

    def __init__(self):
        self.root = "overlap-root"
        self.cache_key = "overlap-cache"
        self.docs = {"chats/c/meta.json": {"name": "room"}}
        self.calls = 0

    def snapshot_docs(self):
        self.calls += 1
        return dict(self.docs), 1

    def get_doc(self, path, default=None):
        self.calls += 1
        return self.docs.get(path, default)

    def put_doc(self, path, data): self.docs[path] = data
    def delete_doc(self, path): self.docs.pop(path, None)
    def list_docs(self, prefix): return [p for p in self.docs if p.startswith(prefix)]
    def list_chat_ids(self): return ["c"]
    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): pass
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def delete_chat(self, chat_id): pass
    def put_blob(self, path, data): pass
    def put_blob_from(self, local_src, path): pass
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


@pytest.fixture
def sources(tmp_path):
    provider = MemoryTransport()
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "viewer.sqlite")
    store.upsert_messages("c", [{"id": "old", "ns": 1, "from": "a"}])
    store.set_offset("c", "a@box", 1)
    try:
        yield provider, mirror, store
    finally:
        store.close()


def test_unchanged_capture_is_detached_bounded_and_does_no_provider_io(sources):
    provider, mirror, store = sources
    calls = provider.calls
    result = capture_local_overlap(mirror, store, "c")
    assert isinstance(result, LocalOverlapObservation)
    assert result.local_overlap is True
    assert result.attempts == 1
    assert result.mirror.provenance == "provider_observed"
    assert [row["id"] for row in result.local.messages()] == ["old"]
    assert provider.calls == calls
    result.local.messages()[0]["id"] = "changed"
    assert result.local.messages()[0]["id"] == "old"


@pytest.mark.parametrize("phase", ["before", "after"])
def test_mirror_change_around_sqlite_retries_the_whole_pair(
        sources, monkeypatch, phase):
    _provider, mirror, store = sources
    original = store.capture_chat_inputs
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1 and phase == "before":
            mirror.put_doc("chats/c/edit.json", {"body": "before"})
        result = original(*args, **kwargs)
        if len(calls) == 1 and phase == "after":
            mirror.put_doc("chats/c/edit.json", {"body": "after"})
        return result

    monkeypatch.setattr(store, "capture_chat_inputs", capture)
    result = capture_local_overlap(mirror, store, "c")
    assert isinstance(result, LocalOverlapObservation)
    assert result.attempts == 2
    assert len(calls) == 2
    assert result.mirror.documents()["chats/c/edit.json"]["body"] == phase


def test_aba_mirror_changes_are_rejected_and_retry_is_exactly_bounded(
        sources, monkeypatch):
    _provider, mirror, store = sources
    original = store.capture_chat_inputs
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        result = original(*args, **kwargs)
        mirror.put_doc("chats/c/edit.json", {"body": "temporary"})
        mirror.delete_doc("chats/c/edit.json")
        return result

    monkeypatch.setattr(store, "capture_chat_inputs", capture)
    result = capture_local_overlap(mirror, store, "c")
    assert result == LocalOverlapUnavailable("mirror_changed", 2)
    assert len(calls) == 2


@pytest.mark.parametrize("mutate_mirror", [False, True])
def test_sqlite_snapshot_instant_and_concurrent_mirror_validation(
        sources, monkeypatch, mutate_mirror):
    _provider, mirror, store = sources
    writer = Store(store.path)
    connect = chat_inputs.sqlite3.connect
    fired = []

    def open_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)

        def race(sql):
            if not fired and sql.startswith("SELECT length(CAST(payload"):
                fired.append(1)
                if mutate_mirror:
                    mirror.put_doc("chats/c/edit.json", {"body": "during"})
                writer.upsert_messages("c", [{"id": "new", "ns": 2, "from": "a"}])

        conn.set_trace_callback(race)
        return conn

    monkeypatch.setattr(chat_inputs.sqlite3, "connect", open_reader)
    try:
        result = capture_local_overlap(mirror, store, "c")
        assert fired
        assert isinstance(result, LocalOverlapObservation)
        assert result.attempts == (2 if mutate_mirror else 1)
        assert [row["id"] for row in result.local.messages()] == (
            ["old", "new"] if mutate_mirror else ["old"])
        assert [row["id"] for row in store.messages("c")] == ["old", "new"]
    finally:
        writer.close()


def test_combined_budget_includes_both_captures_and_returned_identities(sources):
    _provider, mirror, store = sources
    accepted = capture_local_overlap(mirror, store, "c")
    assert isinstance(accepted, LocalOverlapObservation)
    rejected = capture_local_overlap(
        mirror, store, "c", max_bytes=accepted.serialized_bytes - 1)
    assert rejected == LocalOverlapUnavailable("budget_exceeded", 1)
    exact = capture_local_overlap(
        mirror, store, "c", max_bytes=accepted.serialized_bytes)
    assert isinstance(exact, LocalOverlapObservation)
    assert exact.serialized_bytes == accepted.serialized_bytes


def test_sqlite_busy_fails_immediately_without_mirror_retry(sources, monkeypatch):
    _provider, mirror, store = sources
    calls = []

    def busy(*args, **kwargs):
        calls.append(1)
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise exc

    monkeypatch.setattr(store, "capture_chat_inputs", busy)
    assert capture_local_overlap(mirror, store, "c") == \
        LocalOverlapUnavailable("sqlite_unavailable", 1)
    assert calls == [1]


def test_unavailable_and_invalid_arguments_fail_before_extra_work(sources):
    _provider, mirror, store = sources
    cold = CachingTransport(MemoryTransport(), auto_refresh=False)
    assert capture_local_overlap(cold, store, "c") == \
        LocalOverlapUnavailable("cold", 1)
    assert capture_local_overlap(MemoryTransport(), store, "c") == \
        LocalOverlapUnavailable("unsupported", 1)
    with pytest.raises(ValueError):
        capture_local_overlap(mirror, store, "c", max_bytes=True)
    with pytest.raises(ValueError):
        capture_local_overlap(mirror, store, "")


def test_bootstrap_capture_preserves_unverified_provenance(tmp_path):
    provider = MemoryTransport()
    snapshot_path = tmp_path / "mirror.json"
    online = CachingTransport(
        provider, auto_refresh=False, snapshot_path=snapshot_path)
    online.refresh()
    assert snapshot_path.exists()
    calls = provider.calls

    bootstrap = CachingTransport(
        provider, auto_refresh=False, snapshot_path=snapshot_path)
    store = Store(tmp_path / "bootstrap.sqlite")
    try:
        result = capture_local_overlap(bootstrap, store, "c")
        assert isinstance(result, LocalOverlapObservation)
        assert result.mirror.provenance == "bootstrap_unverified"
        assert provider.calls == calls
    finally:
        store.close()


def test_each_count_budget_and_invalid_mirror_state_fail_closed(sources):
    _provider, mirror, store = sources
    assert capture_local_overlap(mirror, store, "c", max_documents=0).reason \
        == "budget_exceeded"
    assert capture_local_overlap(mirror, store, "c", max_chat_ids=0).reason \
        == "budget_exceeded"
    assert capture_local_overlap(mirror, store, "c", max_messages=0).reason \
        == "budget_exceeded"
    assert capture_local_overlap(mirror, store, "c", max_logs=0).reason \
        == "budget_exceeded"
    with mirror._lock:
        mirror._mirror_invalid_reason = "revision_exhausted"
    assert capture_local_overlap(mirror, store, "c") == \
        LocalOverlapUnavailable("revision_exhausted", 1)


def test_invalid_chat_encoding_is_rejected_before_mirror_access(sources, monkeypatch):
    _provider, mirror, store = sources

    def forbidden(**kwargs):
        raise AssertionError("mirror accessed with invalid input")

    monkeypatch.setattr(mirror, "capture_mirror", forbidden)
    with pytest.raises(ValueError, match="UTF-8"):
        capture_local_overlap(mirror, store, "bad\ud800")


@pytest.mark.parametrize("field", ["chat_id", "database_path"])
def test_capture_checks_store_result_identity(sources, monkeypatch, field):
    _provider, mirror, store = sources
    original = store.capture_chat_inputs

    def wrong_identity(*args, **kwargs):
        return replace(original(*args, **kwargs), **{field: "other"})

    monkeypatch.setattr(store, "capture_chat_inputs", wrong_identity)
    with pytest.raises(RuntimeError, match="identity"):
        capture_local_overlap(mirror, store, "c")


def test_unrelated_sqlite_error_propagates(sources, monkeypatch):
    _provider, mirror, store = sources

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: messages")

    monkeypatch.setattr(store, "capture_chat_inputs", fail)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        capture_local_overlap(mirror, store, "c")
