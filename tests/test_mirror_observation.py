"""R168 bounded capture of the process-local CachingTransport mirror."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agentbridge.transport.base import Transport, TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.mirror_observation import (
    MAX_MIRROR_INTEGER,
    MirrorObservation,
)


class MemoryProvider(Transport):
    scheme = "memory"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self) -> None:
        self.root = "memory-root"
        self.cache_key = "memory:key"
        self.docs = {}
        self.chat_ids = []
        self.cursor = 0
        self.delta = ({}, set(), 0)
        self.calls = 0

    def snapshot_docs(self):
        self.calls += 1
        return dict(self.docs), self.cursor

    def get_docs_delta(self, cursor):
        self.calls += 1
        return self.delta

    def get_doc(self, path, default=None):
        self.calls += 1
        return self.docs.get(path, default)

    def put_doc(self, path, data): self.docs[path] = data
    def create_doc(self, path, data): self.docs[path] = data
    def delete_doc(self, path): self.docs.pop(path, None)
    def list_docs(self, prefix):
        self.calls += 1
        return sorted(path for path in self.docs if path.startswith(prefix))
    def list_chat_ids(self):
        self.calls += 1
        return list(self.chat_ids)
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
def mirror():
    provider = MemoryProvider()
    return provider, CachingTransport(provider, auto_refresh=False)


def test_base_and_cold_capture_are_explicit_and_do_no_io(mirror):
    provider, cached = mirror
    assert provider.capture_mirror().reason == "unsupported"
    assert cached.capture_mirror().reason == "cold"
    assert provider.calls == 0


def test_full_capture_is_immutable_detached_and_instance_scoped(mirror):
    provider, cached = mirror
    nested = {"items": [{"n": 1}], "null": None}
    provider.docs["users/a.json"] = nested
    provider.chat_ids = ["c2", "c1"]
    provider.cursor = 7
    cached.refresh()

    capture = cached.capture_mirror()
    assert isinstance(capture, MirrorObservation)
    assert capture.provenance == "provider_observed"
    assert capture.provider_cursor == 7
    assert capture.chat_ids == ("c1", "c2")
    assert capture.documents() == {"users/a.json": nested}
    revision = capture.revision

    nested["items"][0]["n"] = 99
    decoded = capture.documents()
    decoded["users/a.json"]["items"][0]["n"] = 88
    assert cached.get_doc("users/a.json")["items"][0]["n"] == 1
    assert cached.capture_mirror().revision == revision
    assert CachingTransport(provider, auto_refresh=False)._mirror_instance_nonce \
        != capture.instance_nonce


def test_capture_does_not_revisit_provider_properties_or_callbacks():
    class PropertyProvider(MemoryProvider):
        def __init__(self):
            self.identity_calls = 0
            self._root = "memory-root"
            self._cache_key = "memory:key"
            super().__init__()

        @property
        def root(self):
            self.identity_calls += 1
            return self._root

        @root.setter
        def root(self, value):
            self._root = value

        @property
        def cache_key(self):
            self.identity_calls += 1
            return self._cache_key

        @cache_key.setter
        def cache_key(self, value):
            self._cache_key = value

    provider = PropertyProvider()
    cached = CachingTransport(provider, auto_refresh=False)
    provider.docs = {"users/a.json": {"v": 1}}
    cached.refresh()
    callback_calls = []
    cached.subscribe_changes(lambda: callback_calls.append("called"))
    calls_before = provider.calls
    identity_calls_before = provider.identity_calls
    capture = cached.capture_mirror()
    assert isinstance(capture, MirrorObservation)
    assert capture.root_identity == "memory-root"
    assert provider.calls == calls_before
    assert provider.identity_calls == identity_calls_before
    assert callback_calls == []


def test_refresh_callback_can_reenter_capture_without_lock_inversion(mirror):
    provider, cached = mirror
    cached.refresh()
    captures = []
    cached.subscribe_changes(lambda: captures.append(cached.capture_mirror()))
    provider.docs["users/new.json"] = {"v": 1}
    cached.refresh()
    assert len(captures) == 1
    assert captures[0].documents() == {"users/new.json": {"v": 1}}


def test_bootstrap_then_empty_provider_observation_changes_provenance(
        tmp_path: Path):
    provider = MemoryProvider()
    snapshot = tmp_path / "mirror.json"
    snapshot.write_text(json.dumps({
        "v": 1,
        "cache_key": provider.cache_key,
        "saved": 1,
        "cursor": 3,
        "docs": {},
        "chat_ids": [],
    }), encoding="utf-8")
    cached = CachingTransport(provider, auto_refresh=False, snapshot_path=snapshot)
    first = cached.capture_mirror()
    assert first.provenance == "bootstrap_unverified"
    provider.cursor = 3
    cached.refresh()
    second = cached.capture_mirror()
    assert second.provenance == "provider_observed"
    assert second.revision == first.revision + 1


def test_delta_alias_cursor_only_revocation_and_tombstone(mirror):
    provider, cached = mirror
    provider.docs = {
        "chats/c1/meta.json": {"name": "one"},
        "chats/c2/meta.json": {"name": "two"},
    }
    provider.chat_ids = ["c1", "c2"]
    provider.cursor = 2
    cached.refresh()
    before = cached.capture_mirror()

    changed = {"users/u.json": {"nested": [1]}}
    provider.chat_ids = ["c1"]
    provider.delta = (changed, {"chats/c1/meta.json"}, 3)
    cached._refresh_delta()
    after = cached.capture_mirror()
    changed["users/u.json"]["nested"][0] = 9
    assert after.revision == before.revision + 1
    assert after.provider_cursor == 3
    assert after.chat_ids == ("c1",)
    assert after.documents() == {"users/u.json": {"nested": [1]}}

    provider.delta = ({}, set(), 4)
    cached._refresh_delta()
    assert cached.capture_mirror().revision == after.revision + 1


def test_local_mutation_paths_revision_the_same_cut(mirror):
    provider, cached = mirror
    cached.refresh()
    revision = cached.capture_mirror().revision

    cached.put_doc("users/a.json", {"v": 1})
    assert cached.capture_mirror().revision == (revision := revision + 1)
    provider.docs["users/b.json"] = {"v": 2}
    assert cached.get_doc("users/b.json") == {"v": 2}
    assert cached.capture_mirror().revision == (revision := revision + 1)
    cached.append_log("new", "a@m", {"id": "1"})
    assert cached.capture_mirror().revision == (revision := revision + 1)
    cached.append_log("new", "a@m", {"id": "2"})
    assert cached.capture_mirror().revision == revision
    cached.delete_doc("users/a.json")
    assert cached.capture_mirror().revision == (revision := revision + 1)
    cached.delete_chat("new")
    capture = cached.capture_mirror()
    assert capture.revision == revision + 1
    assert "new" not in capture.chat_ids


def test_effect_side_records_each_revision_the_cut(mirror):
    provider, cached = mirror
    cached.refresh()
    provider.create_effect_doc = lambda path, data, **kwargs: None
    before = cached.capture_mirror()
    prefix = "chats/c/runtime/effects/run/call"
    cached.create_effect_doc(
        f"{prefix}/claim.json",
        {"claim": 1},
        ask_envelope={"ask": 1},
        decision_envelope={"decision": 1},
    )
    after = cached.capture_mirror()
    assert after.revision == before.revision + 3
    assert after.documents() == {
        f"{prefix}/claim.json": {"claim": 1},
        f"{prefix}/grant-ask.json": {"ask": 1},
        f"{prefix}/grant-decision.json": {"decision": 1},
    }


def test_capture_barrier_returns_one_revision_cut(mirror, monkeypatch):
    provider, cached = mirror
    provider.docs = {"users/a.json": {"v": 1}}
    cached.refresh()
    entered = threading.Event()
    release = threading.Event()
    original_dumps = json.dumps

    def paused_dumps(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(
        "agentbridge.transport.mirror_observation.json.dumps", paused_dumps,
    )
    captured = []
    capture_thread = threading.Thread(target=lambda: captured.append(
        cached.capture_mirror()
    ))
    capture_thread.start()
    assert entered.wait(2)
    writer = threading.Thread(
        target=lambda: cached.put_doc("users/b.json", {"v": 2}),
    )
    writer.start()
    assert writer.is_alive()  # blocked behind the capture's mirror mutex
    release.set()
    capture_thread.join(2)
    writer.join(2)
    assert not capture_thread.is_alive() and not writer.is_alive()
    assert captured[0].documents() == {"users/a.json": {"v": 1}}
    after = cached.capture_mirror()
    assert after.revision == captured[0].revision + 1
    assert after.documents()["users/b.json"] == {"v": 2}


def test_invalid_payloads_and_budgets_are_content_free(mirror):
    provider, cached = mirror
    provider.docs = {"users/a.json": {"x": 1}}
    provider.chat_ids = ["chat"]
    cached.refresh()
    assert cached.capture_mirror(max_documents=0).reason == "budget_exceeded"
    assert cached.capture_mirror(max_chat_ids=0, max_bytes=0).reason \
        == "budget_exceeded"  # pinned identity alone consumes budget
    with pytest.raises(ValueError):
        cached.capture_mirror(max_bytes=-1)

    capture = cached.capture_mirror()
    exact_bytes = sum(len(value.encode("utf-8")) for value in (
        capture.instance_nonce,
        capture.root_identity,
        capture.cache_identity,
        *capture.chat_ids,
    )) + sum(
        len(record.path.encode("utf-8"))
        + len(record.payload_json.encode("utf-8"))
        for record in capture.records
    )
    assert isinstance(cached.capture_mirror(max_bytes=exact_bytes), MirrorObservation)
    assert cached.capture_mirror(max_bytes=exact_bytes - 1).reason \
        == "budget_exceeded"

    with cached._lock:
        cached._docs["users/bad.json"] = {1: "coercion forbidden"}
    assert cached.capture_mirror().reason == "invalid_payload"


def test_exact_type_rejection_invokes_no_payload_hooks_under_lock(mirror):
    _provider, cached = mirror
    cached.refresh()
    calls = []

    class HookMeta(type):
        def __hash__(cls):
            calls.append("hash")
            return super().__hash__()

        def __eq__(cls, other):
            calls.append("eq")
            return super().__eq__(other)

    class Hooked(metaclass=HookMeta):
        pass

    class HookedString(str):
        def __bool__(self):
            calls.append("bool")
            return True

        def encode(self, *args, **kwargs):
            calls.append("encode")
            return super().encode(*args, **kwargs)

    with cached._lock:
        cached._docs = {"users/value.json": Hooked()}
    assert cached.capture_mirror().reason == "invalid_payload"
    assert calls == []
    with cached._lock:
        cached._docs = {HookedString("users/path.json"): {"v": 1}}
    assert cached.capture_mirror().reason == "invalid_payload"
    assert calls == []
    with cached._lock:
        cached._docs = {"users/\ud800.json": {"x": 1}}
    assert cached.capture_mirror().reason == "invalid_payload"
    deep = []
    for _ in range(1200):
        deep = [deep]
    with cached._lock:
        cached._docs = {"users/deep.json": deep}
    assert cached.capture_mirror().reason == "invalid_payload"


def test_identity_is_pinned_and_invalid_identity_does_not_break_serving():
    provider = MemoryProvider()
    cached = CachingTransport(provider, auto_refresh=False)
    provider.root = "changed-later"
    cached.refresh()
    assert cached.capture_mirror().root_identity == "memory-root"

    provider = MemoryProvider()
    provider.cache_key = "bad\ud800"
    cached = CachingTransport(provider, auto_refresh=False)
    cached.refresh()
    assert cached.get_doc("missing") is None
    assert cached.capture_mirror().reason == "invalid_identity"


def test_revision_exhaustion_and_interruption_poison_capture_but_keep_serving(
        mirror):
    _provider, cached = mirror
    cached.refresh()
    with cached._lock:
        cached._mirror_revision = MAX_MIRROR_INTEGER
    cached.put_doc("users/a.json", {"v": 1})
    assert cached.get_doc("users/a.json") == {"v": 1}
    assert cached.capture_mirror().reason == "revision_exhausted"

    provider = MemoryProvider()
    interrupted = CachingTransport(provider, auto_refresh=False)
    interrupted.refresh()

    class PartialDict(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            raise KeyboardInterrupt

    with interrupted._lock:
        interrupted._docs = PartialDict(interrupted._docs)
    with pytest.raises(KeyboardInterrupt):
        interrupted.put_doc("users/partial.json", {"v": 1})
    assert interrupted.capture_mirror().reason == "mutation_interrupted"
