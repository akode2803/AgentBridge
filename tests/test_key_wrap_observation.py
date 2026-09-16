"""Bounded key-wrap observations over the provider-observed mirror."""
from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from agentbridge.transport import cache as cache_module
from agentbridge.transport.authority_observation import AuthorityObservationUnavailable
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.key_observation import (
    capture_key_wrap,
    locked_matching_key_wrap,
    locked_matching_key_wraps,
    matches_key_wrap,
)


CHAT = "room"
EPOCH = 7
VIEWER = "alice"
PATH = f"chats/{CHAT}/keys/{EPOCH}.json"
VALID = {"eph": "e", "nonce": "n", "ct": "c"}


class FakeProvider(FolderTransport):
    scheme = "key-observation-fake"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self, docs=None):
        self.root = "key-root"
        self.cache_key = "key-cache"
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

    def delete_doc(self, path): self.docs.pop(path, None)
    def list_docs(self, prefix): return sorted(p for p in self.docs if p.startswith(prefix))
    def delete_chat(self, chat_id): return None
    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): return None
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def put_blob(self, path, data): return None
    def put_blob_from(self, local_src, path): return None
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


def _mirror(docs=None):
    provider = FakeProvider(docs)
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    return provider, mirror


def _install(mirror, value, *, safe=True):
    with mirror._lock:
        mirror._docs[PATH] = value
        if safe:
            mirror._authority_unsafe.discard(PATH)
        else:
            mirror._authority_unsafe.add(PATH)


def test_valid_capture_selects_only_viewer_and_has_constant_accounting(monkeypatch):
    provider, mirror = _mirror({PATH: {"wrapped": {VIEWER: VALID}}})
    with mirror._lock:
        mirror._docs[PATH]["wrapped"].update({
            f"other-{index}": object() for index in range(10_000)
        })
    calls = provider.calls
    monkeypatch.setattr(
        cache_module.copy,
        "deepcopy",
        lambda *_a, **_k: pytest.fail("foreground key observation deep-copied epoch doc"),
    )
    monkeypatch.setattr(
        provider,
        "get_doc",
        lambda *_a, **_k: pytest.fail("foreground key observation read provider"),
    )
    observed = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert observed.mode == "present" and observed.shape == "valid"
    assert observed.fields == ("e", "n", "c")
    assert observed.captured_bytes == len(PATH.encode()) + len(VIEWER.encode()) + 3
    assert provider.calls == calls


@pytest.mark.parametrize(
    ("document", "shape"),
    [
        ([], "document_not_dict"),
        ({}, "wrapped_not_dict"),
        ({"wrapped": []}, "wrapped_not_dict"),
        ({"wrapped": {VIEWER: []}}, "viewer_not_dict"),
        ({"wrapped": {VIEWER: {}}}, "invalid_fields"),
        ({"wrapped": {VIEWER: {"eph": "e", "nonce": "n", "ct": 1}}}, "invalid_fields"),
    ],
)
def test_present_malformed_shapes_are_explicit(document, shape):
    _provider, mirror = _mirror()
    _install(mirror, document)
    observed = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert (observed.mode, observed.shape, observed.fields) == ("present", shape, None)


def test_size_budget_counts_selected_strings_only_and_preflights_large_field():
    _provider, mirror = _mirror()
    base = len(PATH.encode()) + len(VIEWER.encode())
    _install(mirror, {"wrapped": {
        VIEWER: {"eph": "é", "nonce": "n", "ct": "c"},
        **{f"other-{index}": {"huge": "x" * 1000} for index in range(200)},
    }})
    selected = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert selected.captured_bytes == base + len("é".encode()) + 2
    assert capture_key_wrap(
        mirror, CHAT, EPOCH, VIEWER, max_bytes=selected.captured_bytes,
    ) == selected
    with pytest.raises(AuthorityObservationUnavailable, match="key_wrap_budget"):
        capture_key_wrap(
            mirror, CHAT, EPOCH, VIEWER, max_bytes=selected.captured_bytes - 1,
        )

    _install(mirror, {"wrapped": {
        VIEWER: {"eph": "x" * 1_000_000, "nonce": "n", "ct": "c"},
    }})
    with pytest.raises(AuthorityObservationUnavailable, match="key_wrap_budget"):
        capture_key_wrap(mirror, CHAT, EPOCH, VIEWER, max_bytes=base + 10)


def test_lookup_modes_change_independently_at_the_same_mirror_revision():
    _provider, mirror = _mirror()
    revision = mirror._mirror_revision
    online = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert (online.mode, online.shape) == ("online_readthrough_required", "absent")

    with mirror._lock:
        mirror._neg.add(PATH)
    negative = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert negative.mode == "known_negative" and negative.mirror.revision == revision

    with mirror._lock:
        mirror._neg.discard(PATH)
        mirror._health_state = "cached"
    offline = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert offline.mode == "offline_absent" and offline.mirror.revision == revision
    assert not matches_key_wrap(mirror, online)
    assert not matches_key_wrap(mirror, negative)
    assert matches_key_wrap(mirror, offline)


def test_only_provider_observed_caching_transport_is_eligible(tmp_path):
    with pytest.raises(AuthorityObservationUnavailable, match="unsupported"):
        capture_key_wrap(FolderTransport(tmp_path / "bare"), CHAT, EPOCH, VIEWER)

    provider = FakeProvider({PATH: {"wrapped": {VIEWER: VALID}}})
    cold = CachingTransport(provider, auto_refresh=False)
    with pytest.raises(AuthorityObservationUnavailable, match="pending"):
        capture_key_wrap(cold, CHAT, EPOCH, VIEWER)
    cold.refresh()
    with cold._lock:
        cold._mirror_provenance = "persisted_bootstrap"
    with pytest.raises(AuthorityObservationUnavailable, match="pending"):
        capture_key_wrap(cold, CHAT, EPOCH, VIEWER)


def test_selected_change_rejects_but_unrelated_wrap_change_does_not():
    _provider, mirror = _mirror({PATH: {"wrapped": {
        VIEWER: VALID, "bob": {"eph": "b", "nonce": "b", "ct": "b"},
    }}})
    expected = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    with mirror._lock:
        mirror._docs[PATH]["wrapped"]["bob"]["ct"] = "changed"
    assert matches_key_wrap(mirror, expected)
    with mirror._lock:
        mirror._docs[PATH]["wrapped"][VIEWER]["ct"] = "changed"
    assert not matches_key_wrap(mirror, expected)


def test_locked_match_excludes_mirror_writer_until_scope_exits():
    _provider, mirror = _mirror({PATH: {"wrapped": {VIEWER: VALID}}})
    expected = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    entered = threading.Event()
    finished = threading.Event()

    def writer():
        entered.set()
        mirror.put_doc(PATH, {"wrapped": {VIEWER: {"eph": "x", "nonce": "x", "ct": "x"}}})
        finished.set()

    thread = threading.Thread(target=writer)
    with locked_matching_key_wrap(mirror, expected) as matched:
        assert matched
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.1)
    thread.join(5)
    assert finished.is_set() and not thread.is_alive()
    assert not matches_key_wrap(mirror, expected)


def test_batch_matches_multiple_epochs_under_one_nonreentrant_lock():
    second_path = f"chats/{CHAT}/keys/{EPOCH + 1}.json"
    _provider, mirror = _mirror({
        PATH: {"wrapped": {VIEWER: VALID}},
        second_path: {"wrapped": {VIEWER: {"eph": "2", "nonce": "2", "ct": "2"}}},
    })
    expected = (
        capture_key_wrap(mirror, CHAT, EPOCH, VIEWER),
        capture_key_wrap(mirror, CHAT, EPOCH + 1, VIEWER),
    )
    underlying = mirror._lock

    class NonReentrantCountingLock:
        def __init__(self):
            self.active = False
            self.entries = 0

        def __enter__(self):
            if self.active:
                raise AssertionError("batch attempted nested mirror lock")
            underlying.acquire()
            self.active = True
            self.entries += 1
            return self

        def __exit__(self, *_args):
            self.active = False
            underlying.release()
            return False

    observed_lock = NonReentrantCountingLock()
    mirror._lock = observed_lock
    with locked_matching_key_wraps(mirror, expected) as matched:
        assert matched is True
        assert observed_lock.active is True
    assert observed_lock.entries == 1 and observed_lock.active is False


def test_batch_fails_when_one_epoch_changes():
    second_path = f"chats/{CHAT}/keys/{EPOCH + 1}.json"
    _provider, mirror = _mirror({
        PATH: {"wrapped": {VIEWER: VALID}},
        second_path: {"wrapped": {VIEWER: {"eph": "2", "nonce": "2", "ct": "2"}}},
    })
    expected = (
        capture_key_wrap(mirror, CHAT, EPOCH, VIEWER),
        capture_key_wrap(mirror, CHAT, EPOCH + 1, VIEWER),
    )
    with mirror._lock:
        mirror._docs[second_path]["wrapped"][VIEWER]["ct"] = "stale"
    with locked_matching_key_wraps(mirror, expected) as matched:
        assert matched is False


def test_batch_rejects_duplicate_invalid_and_oversized_selections_before_lock(monkeypatch):
    _provider, mirror = _mirror({PATH: {"wrapped": {VIEWER: VALID}}})
    expected = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)

    class NoLock:
        def __enter__(self):
            pytest.fail("invalid batch reached mirror lock")

        def __exit__(self, *_args): return False

    monkeypatch.setattr(mirror, "_lock", NoLock())
    with pytest.raises(ValueError, match="duplicate"):
        with locked_matching_key_wraps(mirror, (expected, expected)):
            pytest.fail("duplicate batch accepted")
    with pytest.raises(ValueError, match="epoch selection"):
        with locked_matching_key_wraps(mirror, [expected]):
            pytest.fail("non-tuple batch accepted")
    with pytest.raises(ValueError, match="epoch selection"):
        with locked_matching_key_wraps(mirror, (expected,) * 65):
            pytest.fail("oversized batch accepted")


class AliasingDocument(dict):
    def __deepcopy__(self, memo):
        return self.alias


def test_ingress_breaks_malicious_deepcopy_alias_for_key_documents():
    aliased = {"wrapped": {VIEWER: dict(VALID)}}
    malicious = AliasingDocument()
    malicious.alias = aliased
    _provider, mirror = _mirror({PATH: malicious})
    before = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    aliased["wrapped"][VIEWER]["ct"] = "mutated-after-refresh"
    after = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)
    assert before == after
    assert after.fields == ("e", "n", "c")


def test_uncloneable_cyclic_key_document_is_marked_ineligible():
    cyclic = {"wrapped": {VIEWER: dict(VALID)}}
    cyclic["cycle"] = cyclic
    _provider, mirror = _mirror({PATH: cyclic})
    assert PATH in mirror._authority_unsafe
    with pytest.raises(AuthorityObservationUnavailable, match="key_wrap_ineligible"):
        capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)


def test_malformed_observation_tokens_fail_before_lock(monkeypatch):
    _provider, mirror = _mirror({PATH: {"wrapped": {VIEWER: VALID}}})
    expected = capture_key_wrap(mirror, CHAT, EPOCH, VIEWER)

    class NoLock:
        def __enter__(self):
            pytest.fail("malformed token reached mirror lock")

        def __exit__(self, *_args): return False

    monkeypatch.setattr(mirror, "_lock", NoLock())
    with pytest.raises(ValueError):
        matches_key_wrap(mirror, replace(expected, fields=("e", "n", 1)))
    with pytest.raises(ValueError):
        matches_key_wrap(mirror, replace(expected, captured_bytes=0))
    with pytest.raises(ValueError):
        matches_key_wrap(mirror, object())
