"""R212 bounded account-authority observation contracts."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentbridge.transport import authority_observation as authority
from agentbridge.transport import cache as cache_module
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "room"
META = f"chats/{CHAT}/meta.json"


class FakeProvider(FolderTransport):
    scheme = "authority-fake"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self, docs=None):
        self.root = "authority-root"
        self.cache_key = "authority-cache"
        self.docs = dict(docs or {})
        self.cursor = 1
        self.calls = []
        self.fail = False
        self.delta = ({}, set(), 1)

    def snapshot_docs(self):
        self.calls.append("snapshot_docs")
        if self.fail:
            raise ConnectionError("offline")
        return dict(self.docs), self.cursor

    def get_docs_delta(self, cursor):
        self.calls.append("get_docs_delta")
        if self.fail:
            raise ConnectionError("offline")
        return self.delta

    def list_chat_ids(self):
        self.calls.append("list_chat_ids")
        return []

    def get_doc(self, path, default=None):
        self.calls.append(("get_doc", path))
        return self.docs.get(path, default)

    def put_doc(self, path, data): self.docs[path] = data
    create_doc = put_doc
    def delete_doc(self, path): self.docs.pop(path, None)
    def list_docs(self, prefix): return []
    def delete_chat(self, chat_id): return None
    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): return None
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def put_blob(self, path, data): return None
    def put_blob_from(self, local_src, path): return None
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


def _online(docs=None):
    provider = FakeProvider(docs)
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    return provider, mirror


def _records(captured):
    return dict(captured.records)


def test_document_capture_selects_meta_users_lifecycle_with_exact_wire_bytes():
    docs = {
        META: {"title": "quotes \" slash \\ line\n snowman ☃"},
        "users/alice.json": {"name": "ålîce", "active": True},
        "lifecycle/alice/1.json": [1, None, False, 1.25],
        "chats/other/meta.json": {"ignored": True},
        "runtime/session.json": {"ignored": True},
    }
    _provider, mirror = _online(docs)
    captured = authority.capture_authority_documents(mirror, CHAT)
    selected = {META, "users/alice.json", "lifecycle/alice/1.json"}
    assert set(_records(captured)) == selected
    assert {path: json.loads(raw) for path, raw in captured.records} == {
        path: docs[path] for path in selected
    }
    overhead = 16 + len(META.encode()) + sum(
        16 + len(path.encode()) for path in selected if path != META
    )
    wire = sum(len(raw.encode()) for _path, raw in captured.records)
    assert captured.serialized_bytes == overhead + wire


def test_missing_meta_is_explicit_and_selection_budgets_fail_without_partial_result():
    _provider, mirror = _online({"users/alice.json": {"name": "alice"}})
    captured = authority.capture_authority_documents(mirror, CHAT)
    assert _records(captured)[META] is None
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_selection_budget"):
        authority.capture_authority_documents(mirror, CHAT, max_documents=1)
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_path_budget"):
        authority.capture_authority_documents(mirror, CHAT, max_examined_paths=0)


class HostileValue:
    def __iter__(self):
        pytest.fail("hostile iterator invoked")

    def __deepcopy__(self, memo):
        pytest.fail("hostile deepcopy invoked")


class AliasDeepcopy:
    def __init__(self, target):
        self.target = target

    def __deepcopy__(self, memo):
        return self.target


@pytest.mark.parametrize("value,reason,max_bytes", [
    (HostileValue(), "invalid_authority_json", authority.MAX_BYTES),
    ({"body": "x" * 1024}, "authority_byte_budget", 300),
    (float("nan"), "invalid_authority_json", authority.MAX_BYTES),
])
def test_invalid_or_oversized_json_rejects_without_custom_hooks(
        value, reason, max_bytes):
    _provider, mirror = _online()
    with mirror._lock:
        mirror._docs["users/alice.json"] = value
    with pytest.raises(authority.AuthorityObservationUnavailable, match=reason):
        authority.capture_authority_documents(mirror, CHAT, max_bytes=max_bytes)


def test_depth_and_global_node_budgets_reject_before_unbounded_copy():
    deep = 0
    for _ in range(authority.MAX_DEPTH + 2):
        deep = [deep]
    _provider, mirror = _online({"users/deep.json": deep})
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_value_ineligible"):
        authority.capture_authority_documents(mirror, CHAT)
    with mirror._lock:
        mirror._authority_unsafe.clear()
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_structure_budget"):
        authority.capture_authority_documents(mirror, CHAT)

    _provider, mirror = _online()
    with mirror._lock:
        mirror._docs["users/nodes.json"] = [None] * (authority.MAX_NODES + 1)
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_structure_budget"):
        authority.capture_authority_documents(mirror, CHAT)


def test_ingress_breaks_alias_returned_by_custom_deepcopy_and_preserves_token():
    provider_value = {"nested": {"name": "before"}}
    provider, mirror = _online({
        "users/alice.json": AliasDeepcopy(provider_value),
    })
    first = authority.capture_authority_documents(mirror, CHAT)
    assert json.loads(_records(first)["users/alice.json"]) == provider_value
    assert mirror._docs["users/alice.json"] is not provider_value
    assert mirror._docs["users/alice.json"]["nested"] is not provider_value["nested"]

    provider_value["nested"]["name"] = "after"
    second = authority.capture_authority_documents(mirror, CHAT)
    assert second == first
    assert mirror.get_doc("users/alice.json") == {"nested": {"name": "before"}}
    assert provider.docs["users/alice.json"].target is provider_value


def test_unsafe_and_overbudget_ingress_remain_canonical_but_authority_rejects(
        monkeypatch):
    hostile = object()
    _provider, mirror = _online({"users/hostile.json": AliasDeepcopy(hostile)})
    assert mirror._docs["users/hostile.json"] is hostile
    assert "users/hostile.json" in mirror._authority_unsafe
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_value_ineligible"):
        authority.capture_authority_documents(mirror, CHAT)

    monkeypatch.setattr(authority, "MAX_BYTES", 128)
    value = {"body": "x" * 200}
    _provider, mirror = _online({"users/large.json": value})
    assert mirror._docs["users/large.json"] == value
    assert "users/large.json" in mirror._authority_unsafe
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_value_ineligible"):
        authority.capture_authority_documents(mirror, CHAT, max_bytes=128)


def test_full_and_delta_recent_write_guards_retain_unsafe_mark_then_safe_write_clears(
        monkeypatch):
    provider, mirror = _online()
    path = "users/alice.json"
    monkeypatch.setattr(cache_module, "_monotonic", lambda: 100.0)
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: 100.0)
    mirror.put_doc(path, object())
    assert path in mirror._authority_unsafe

    provider.docs[path] = {"safe": "provider-full"}
    mirror.refresh()
    assert path in mirror._authority_unsafe
    assert type(mirror._docs[path]) is object

    provider.delta = ({path: {"safe": "provider-delta"}}, set(), 2)
    mirror._refresh_delta()
    assert path in mirror._authority_unsafe
    assert type(mirror._docs[path]) is object

    mirror.put_doc(path, {"safe": "local"})
    assert path not in mirror._authority_unsafe
    assert mirror.get_doc(path) == {"safe": "local"}


def test_delete_full_revocation_and_delta_revocation_remove_unsafe_marks():
    provider, mirror = _online()
    path = "users/alice.json"
    mirror.put_doc(path, object())
    assert path in mirror._authority_unsafe
    mirror.delete_doc(path)
    assert path not in mirror._authority_unsafe and path not in mirror._docs

    mirror.put_doc(path, object())
    mirror._doc_writes.clear()
    provider.docs.pop(path, None)
    mirror.refresh()
    assert path not in mirror._authority_unsafe and path not in mirror._docs

    mirror.put_doc(path, object())
    mirror._doc_writes.clear()
    provider.delta = ({}, {path}, 3)
    mirror._refresh_delta()
    assert path not in mirror._authority_unsafe and path not in mirror._docs


def test_json_serialization_runs_outside_lock_and_revision_race_is_rejected(
        monkeypatch):
    _provider, mirror = _online({"users/alice.json": {"name": "alice"}})
    original = authority.json.dumps
    calls = 0

    def mutate_during_dump(*args, **kwargs):
        nonlocal calls
        assert mirror._lock.acquire(blocking=False), "serialized under mirror lock"
        mirror._lock.release()
        calls += 1
        if calls == 1:
            mirror.put_doc("runtime/race.json", {"changed": True})
        return original(*args, **kwargs)

    monkeypatch.setattr(authority.json, "dumps", mutate_during_dump)
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_mirror_changed"):
        authority.capture_authority_documents(mirror, CHAT)
    assert calls >= 1


def test_lookup_policy_covers_present_negative_offline_and_online_unknown_without_io():
    provider, mirror = _online({META: {"id": CHAT}, "users/present.json": {"name": "p"}})
    assert mirror.get_doc("users/negative.json") is None
    calls = list(provider.calls)
    online = authority.capture_lookup_policy(
        mirror, CHAT, ("present", "negative", "unknown"),
    )
    assert online.accounts == (
        ("users/present.json", "present"),
        ("users/negative.json", "known_negative"),
        ("users/unknown.json", "online_readthrough_required"),
    )
    assert online.meta == (META, "present")
    assert provider.calls == calls

    provider.fail = True
    with pytest.raises(ConnectionError):
        mirror.refresh()
    offline = authority.capture_lookup_policy(mirror, CHAT, ("unknown",))
    assert offline.mirror == online.mirror
    assert offline.accounts == (("users/unknown.json", "offline_absent"),)
    assert provider.calls == calls + ["snapshot_docs"]


def test_health_change_invalidates_policy_at_same_mirror_and_health_aba_is_safe():
    provider, mirror = _online()
    expected = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    revision = expected.mirror.revision
    provider.fail = True
    with pytest.raises(ConnectionError):
        mirror.refresh()
    assert mirror._mirror_revision == revision
    assert authority.matches_lookup_policy(mirror, expected) is False
    mirror._record_success()
    assert mirror._mirror_revision == revision
    assert authority.matches_lookup_policy(mirror, expected) is True


def test_negative_cache_change_invalidates_policy_without_mirror_revision():
    provider, mirror = _online()
    expected = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    revision = expected.mirror.revision
    assert mirror.get_doc("users/alice.json") is None
    assert mirror._mirror_revision == revision
    calls = list(provider.calls)
    assert authority.matches_lookup_policy(mirror, expected) is False
    assert provider.calls == calls


def test_mirror_content_aba_rejects_old_policy_even_when_status_returns():
    _provider, mirror = _online()
    expected = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    mirror.put_doc("users/alice.json", {"name": "alice"})
    mirror.delete_doc("users/alice.json")
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_mirror_changed"):
        authority.matches_lookup_policy(mirror, expected)


def test_wrong_selectors_and_tokens_reject():
    _provider, mirror = _online()
    policy = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    for names in (["alice"], ("alice", "alice"), ("bad/name",)):
        with pytest.raises(ValueError):
            authority.capture_lookup_policy(mirror, CHAT, names)
    with pytest.raises(ValueError, match="expected authority mirror"):
        authority.capture_lookup_policy(mirror, CHAT, expected=object())
    with pytest.raises(ValueError, match="lookup policy"):
        authority.matches_lookup_policy(mirror, object())
    with pytest.raises(ValueError):
        authority.matches_lookup_policy(mirror, replace(policy, accounts=[]))
    with pytest.raises(ValueError):
        authority.matches_lookup_policy(
            mirror, replace(policy, accounts=(("runtime/x.json", "present"),)),
        )
    wrong = replace(
        policy.mirror, revision=policy.mirror.revision + 1,
    )
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_mirror_changed"):
        authority.capture_lookup_policy(mirror, CHAT, expected=wrong)


def test_cold_bootstrap_and_bare_transports_are_unavailable(tmp_path):
    provider = FakeProvider()
    cold = CachingTransport(provider, auto_refresh=False)
    for call in (
        lambda: authority.capture_authority_documents(cold, CHAT),
        lambda: authority.capture_lookup_policy(cold, CHAT),
    ):
        with pytest.raises(authority.AuthorityObservationUnavailable,
                           match="authority_mirror_pending"):
            call()

    snapshot = tmp_path / "mirror-snapshot.json"
    source = CachingTransport(
        FakeProvider({META: {"id": CHAT}}), auto_refresh=False,
        snapshot_path=snapshot,
    )
    source.refresh()
    bootstrap = CachingTransport(
        FakeProvider(), auto_refresh=False, snapshot_path=snapshot,
    )
    assert bootstrap.mirror_status()["state"] == "cached"
    with pytest.raises(authority.AuthorityObservationUnavailable,
                       match="authority_mirror_pending"):
        authority.capture_authority_documents(bootstrap, CHAT)

    bare = FolderTransport(tmp_path / "bare")
    for call in (
        lambda: authority.capture_authority_documents(bare, CHAT),
        lambda: authority.capture_lookup_policy(bare, CHAT),
    ):
        with pytest.raises(authority.AuthorityObservationUnavailable,
                           match="unsupported"):
            call()
