"""Offline backing isolation, concurrency and real-driver fixture contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import fake_cloud
from fake_cloud import CloudRegistry, FakeClient, refresh_transport
from agentbridge.core.errors import ValidationError
from agentbridge.core.models import ChatKind, UserKind
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.local_mutations import LocalMutationTransport, root_identity
from agentbridge.transport.supabase import SupabaseTransport


def _seed_effect_context(client, root="r"):
    client.seed_documents(root, {
        "users/helper.json": {"kind": "agent", "active": True, "agent": {"owner": "owner"}},
        "chats/chat/meta.json": {"members": {"owner": {}, "helper": {}}},
    })


def _effect_params(value=1, *, root="r", filename="claim.json"):
    return {
        "p_root": root,
        "p_path": f"chats/chat/runtime/effects/run/call/{filename}",
        "p_data": {"nested": [value], "meta": {
            "kind": "effect", "actor": "helper", "signer": "helper",
            "chat_id": "chat", "run_id": "run", "root_run_id": "run", "call_id": "call",
        }},
        "p_grant_ask": {"header": {
            "kind": "permission_ask", "sender": "helper", "recipient": "owner",
            "agent": "helper", "chat_id": "chat",
        }},
        "p_grant_decision": {"header": {
            "kind": "permission_decision", "sender": "owner", "recipient": "helper",
            "agent": "helper", "chat_id": "chat",
        }},
    }


def test_query_payloads_results_and_updated_rows_are_detached():
    client = FakeClient()
    data = {"nested": {"values": [1]}}
    query = client.table("ab_docs").insert({"root": "r", "path": "a.json", "data": data})
    data["nested"]["values"].append(2)
    result = query.execute()
    result.data[0]["data"]["nested"]["values"].append(3)
    assert client.db["ab_docs"][0]["data"] == {"nested": {"values": [1]}}
    client.table("ab_docs").insert({"root": "r", "path": "b.json", "data": {}}).execute()
    patch = {"data": {"values": [4]}}
    update = client.table("ab_docs").update(patch).eq("root", "r")
    patch["data"]["values"].append(5)
    updated = update.execute()
    updated.data[0]["data"]["values"].append(6)
    selected = client.table("ab_docs").select().eq("root", "r").execute()
    assert [row["data"] for row in selected.data] == [{"values": [4]}, {"values": [4]}]
    selected.data[0]["data"]["values"].append(7)
    assert client.db["ab_docs"][0]["data"] == {"values": [4]}
    assert client.db["ab_docs"][0]["data"] is not client.db["ab_docs"][1]["data"]
    # The original insert detached both its query input and write result.
    assert query._op[1]["data"] == {"nested": {"values": [1]}}


def test_postgrest_wire_normalizes_json_values_before_real_mirror_observation(tmp_path):
    with CloudRegistry() as registry:
        root = registry.root(tmp_path / "wire")
        driver = registry.bare(root)
        cache = registry.cached(root)
        driver.put_doc("users/alice.json", {"kind": UserKind.HUMAN, "extra": (1, 2)})
        cache.put_doc("chats/chat/meta.json", {"kind": ChatKind.GROUP, "members": {}})
        registry.seed_documents(root, {"users/seeded.json": {"kind": UserKind.AGENT}})
        driver._client.table("ab_docs").update({"data": {"kind": UserKind.AGENT}}).eq(
            "path", "users/alice.json").execute()
        refresh_transport(cache)
        assert cache._authority_unsafe == set()
        observed = driver.get_docs()
        assert type(observed["users/alice.json"]["kind"]) is str
        assert type(observed["users/seeded.json"]["kind"]) is str
        assert type(observed["chats/chat/meta.json"]["kind"]) is str
        driver.put_doc("fixture/tuple.json", {"items": (1, 2)})
        assert driver.get_doc("fixture/tuple.json") == {"items": [1, 2]}
        client = driver._client
        before = client.db["_docseq"]
        with pytest.raises(TypeError):
            client.table("ab_docs").insert({"root": driver.root, "path": "bad.json",
                                             "data": object()})
        assert client.db["_docseq"] == before
        with pytest.raises(TypeError):
            registry.seed_documents(root, {"users/seeded.json": {"changed": True},
                                           "bad.json": object()})
        assert driver.get_doc("users/seeded.json") == {"kind": "agent"}


def test_explicit_refresh_observes_exact_mirror_behind_mutation_owner(tmp_path):
    with CloudRegistry() as registry:
        root = registry.root(tmp_path / "wrapped")
        cache = registry.cached(root)
        owner = LocalMutationTransport(cache, MutationCoordinator(
            tmp_path / "coordinator", root_identity(cache)))
        peer = registry.bare(root)
        peer.put_doc("fixture/observed.json", {"value": 1})
        with pytest.raises(AttributeError):
            owner.refresh()
        assert refresh_transport(owner) is cache
        assert cache.get_doc("fixture/observed.json") == {"value": 1}
        assert cache._mirror_provenance == "provider_observed"
        assert type(cache) is CachingTransport and type(cache.inner) is SupabaseTransport
        with pytest.raises(TypeError, match="exact offline cloud mirror"):
            refresh_transport(peer)


def test_rpc_reads_and_mutations_are_lazy_and_params_are_detached():
    client = FakeClient()
    listing = client.rpc("ab_chat_ids", {"p_root": "r"})
    params = _effect_params()
    transition = client.rpc("ab_effect_transition", params)
    params["p_data"]["nested"].append(2)
    assert "ab_docs" not in client.db
    _seed_effect_context(client)
    assert listing.execute().data == [{"chat_id": "chat"}]
    assert transition.execute().data is True
    row = next(row for row in client.db["ab_docs"] if row["path"] == params["p_path"])
    assert row["data"]["nested"] == [1]
    assert row["seq"] == 5
    assert transition.execute().data is True
    assert client.db["_docseq"] == 5


def test_concurrent_creates_are_exclusive_and_have_one_sequence():
    client = FakeClient()

    def create(value):
        try:
            client.table("ab_docs").insert({"root": "r", "path": "claim.json",
                                             "data": {"winner": value}}).execute()
            return True
        except RuntimeError as exc:
            assert "23505" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(32)))
    assert results.count(True) == 1
    assert len(client.db["ab_docs"]) == client.db["_docseq"] == 1


def test_concurrent_rpc_claims_and_storage_operations_share_backing_gate():
    client = FakeClient()
    _seed_effect_context(client)
    bucket = client.storage.from_("files")

    def compete(value):
        bucket.upload(f"r/{value}", bytearray(b"abc"))
        assert isinstance(bucket.list("r"), list)
        result = client.rpc("ab_effect_transition", _effect_params(value)).execute().data
        bucket.remove([f"r/{value}"])
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(32)))
    assert results.count(True) == 1
    assert client.db["_docseq"] == 5
    assert bucket.list("r") == []


def test_registry_peers_are_exact_distinct_drivers_over_shared_isolated_clients(tmp_path):
    with CloudRegistry() as registry, CloudRegistry() as independent:
        root = registry.root(tmp_path / "one")
        assert root.startswith("supabase://test-")
        assert registry.root(tmp_path / "one" / ".") == root
        peer = registry.bare(root)
        writer = registry.bare(peer.root)
        other = registry.bare(tmp_path / "two")
        separate = independent.bare(tmp_path / "one")
        assert type(peer) is type(writer) is SupabaseTransport
        assert peer is not writer and peer._client is writer._client
        assert other._client is not writer._client and separate._client is not writer._client
        writer.put_doc("users/alice.json", {"name": "Alice"})
        assert peer.get_doc("users/alice.json") == {"name": "Alice"}
        assert other.get_doc("users/alice.json") is None
        assert separate.get_doc("users/alice.json") is None
        writer.close()
        peer.put_doc("users/bob.json", {"name": "Bob"})
        assert peer.get_doc("users/bob.json") == {"name": "Bob"}


def test_network_realtime_is_disabled_before_real_driver_writes_and_watches(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline registry attempted network Realtime")

    monkeypatch.setattr("agentbridge.transport.supabase._RealtimeThread", forbidden)
    with CloudRegistry() as registry:
        driver = registry.bare(tmp_path / "root")
        driver.put_doc("users/alice.json", {})
        driver.append_log("chat", "alice@m", {"id": "one", "from": "alice"})
        watcher = driver.watch()
        try:
            assert watcher.wait(0) is False
            driver.hint_now()
            assert driver._rt is None
        finally:
            watcher.close()


def test_cached_helper_preserves_exact_owner_identity_and_explicit_coldness(tmp_path):
    with CloudRegistry() as registry:
        root = registry.root(tmp_path / "root")
        registry.seed_documents(root, {"users/alice.json": {"name": "Alice"}})
        cold = registry.cached(root, warm=False)
        warm = registry.cached(root)
        assert type(cold) is type(warm) is CachingTransport
        assert type(warm.inner) is SupabaseTransport
        assert cold.mirror_status()["warm"] is False
        assert warm.mirror_status()["warm"] is True
        assert warm.get_doc("users/alice.json") == {"name": "Alice"}
        assert root_identity(warm) == root_identity(warm.inner)
        assert warm._mirror_root_identity == warm.inner.root
        warm.inner.root = "changed"
        with pytest.raises(ValueError, match="cache identity changed"):
            root_identity(warm)


def test_bulk_seed_exceeds_one_provider_page_and_preserves_delta_order_and_other_roots(tmp_path):
    with CloudRegistry() as registry:
        root = registry.root(tmp_path / "root")
        documents = {f"items/{i:05}.json": {"values": [i]} for i in range(20_011)}
        registry.seed_documents(root, documents)
        documents["items/00000.json"]["values"].append(-1)
        writer = registry.bare(root)
        snapshot, cursor = writer.snapshot_docs()
        assert len(snapshot) == 20_011 and cursor == 20_011
        assert snapshot["items/00000.json"] == {"values": [0]}
        registry.seed_documents(root, {"items/00000.json": {"values": [99]},
                                       "items/new.json": {"values": [100]}})
        changed, deleted, advanced = writer.get_docs_delta(cursor)
        assert changed == {"items/00000.json": {"values": [99]},
                           "items/new.json": {"values": [100]}}
        assert deleted == set() and advanced == cursor + 2
        client = registry.client(root)
        client.seed_documents("another-root", {"items/00000.json": {"values": [101]}})
        registry.seed_documents(root, {"items/00000.json": {"values": [102]}})
        assert next(row["data"] for row in client.db["ab_docs"]
                    if row["root"] == "another-root") == {"values": [101]}


def test_bulk_seed_validation_failure_does_not_partially_replace_documents():
    client = FakeClient()
    client.seed_documents("r", {"items/a.json": {"n": 1}})
    with pytest.raises(ValidationError):
        client.seed_documents("r", {"items/a.json": {"n": 2}, "../bad.json": {}})
    assert client.db["_docseq"] == 1
    assert client.db["ab_docs"][0]["data"] == {"n": 1}


def test_replace_log_is_detached_scoped_and_advances_real_driver_high_water(tmp_path):
    with CloudRegistry() as registry:
        root = registry.root(tmp_path / "root")
        driver = registry.bare(root)
        driver.append_log("chat", "alice@m", {"id": "old", "from": "alice"})
        driver.append_log("other", "bob@m", {"id": "keep", "from": "bob"})
        _, cursor = driver.changed_logs(0)
        records = [{"id": "replacement", "from": "alice", "files": [{"name": "safe"}]}]
        registry.replace_log(root, "chat", "alice@m", records)
        records[0]["files"][0]["name"] = "changed-after-replacement"
        read, offset = driver.read_log("chat", "alice@m")
        assert read[0]["id"] == "replacement" and read[0]["files"] == [{"name": "safe"}]
        assert offset > cursor
        assert driver.read_log("other", "bob@m")[0] == [{"id": "keep", "from": "bob"}]
        assert driver.changed_logs(cursor) == ([("chat", "alice@m")], offset)


def test_effect_capability_is_explicit_and_transitions_use_document_delta_sequence(tmp_path):
    assert FakeClient().rpc("ab_effects_ready").execute().data == 1
    with CloudRegistry() as unavailable, CloudRegistry(effects_ready=True) as available:
        assert unavailable.client(tmp_path / "root").rpc("ab_effects_ready").execute().data == 0
        tx = available.bare(tmp_path / "root")
        tx.auth_mode = "member:fixture"  # Explicit synthetic protocol activation.
        _seed_effect_context(tx._client, tx.root)
        _, cursor = tx.snapshot_docs()
        assert tx.effect_claims_ready() is True
        params = _effect_params(root=tx.root)
        tx.create_effect_doc(params["p_path"], params["p_data"],
                             ask_envelope=params["p_grant_ask"],
                             decision_envelope=params["p_grant_decision"])
        changed, deleted, advanced = tx.get_docs_delta(cursor)
        base = params["p_path"].rsplit("/", 1)[0]
        assert changed == {params["p_path"]: params["p_data"],
                           f"{base}/grant-ask.json": params["p_grant_ask"],
                           f"{base}/grant-decision.json": params["p_grant_decision"]}
        assert deleted == set() and advanced == cursor + 3


def test_effect_protocol_requires_order_and_matching_atomic_companions():
    client = FakeClient()
    _seed_effect_context(client)
    claim = _effect_params()
    third = _effect_params(filename="state-3.json")
    assert client.rpc("ab_effect_transition", third).execute().data is False
    bad = _effect_params()
    bad["p_grant_decision"]["header"]["sender"] = "outsider"
    assert client.rpc("ab_effect_transition", bad).execute().data is False
    assert len(client.db["ab_docs"]) == client.db["_docseq"] == 2
    assert client.rpc("ab_effect_transition", claim).execute().data is True
    different = _effect_params()
    different["p_grant_ask"]["different"] = True
    assert client.rpc("ab_effect_transition", different).execute().data is False
    assert client.db["_docseq"] == 5 and len(client.db["ab_docs"]) == 5
    assert client.rpc("ab_effect_transition", third).execute().data is False
    second = _effect_params(filename="state-2.json")
    assert client.rpc("ab_effect_transition", second).execute().data is True
    assert client.rpc("ab_effect_transition", third).execute().data is True
    assert client.db["_docseq"] == 7


def test_conflicting_grant_or_removed_membership_leaves_no_partial_effect():
    client = FakeClient()
    _seed_effect_context(client)
    params = _effect_params()
    base = params["p_path"].rsplit("/", 1)[0]
    client.seed_documents("r", {f"{base}/grant-decision.json": {"conflicting": True}})
    assert client.rpc("ab_effect_transition", params).execute().data is False
    assert len(client.db["ab_docs"]) == client.db["_docseq"] == 3
    client.table("ab_docs").delete().eq("path", f"{base}/grant-decision.json").execute()
    client.seed_documents("r", {"chats/chat/meta.json": {"members": ["owner"]}})
    assert client.rpc("ab_effect_transition", params).execute().data is False
    assert len(client.db["ab_docs"]) == 2


def test_registry_factory_intercepts_only_registered_uris_and_close_releases_resources(
        tmp_path, monkeypatch):
    calls = []
    sentinel = object()

    def real_factory(spec, home=None, *, offline_cache=False):
        calls.append((spec, home, offline_cache))
        return sentinel

    monkeypatch.setattr(fake_cloud, "_production_factory", real_factory)
    registry = CloudRegistry()
    root = registry.root(tmp_path / "root")
    first = registry.make_transport(root)
    second = registry.make_transport(root)
    assert type(first) is type(second) is CachingTransport and first is not second
    assert first.inner._client is second.inner._client
    unknown = "supabase://not-registered"
    assert registry.make_transport(unknown, tmp_path, offline_cache=True) is sentinel
    path = tmp_path / "unregistered-folder"
    assert registry.make_transport(path) is sentinel
    assert calls == [(unknown, tmp_path, True), (path, None, False)]
    registry.close()
    assert first._stop.is_set() and second._stop.is_set()
    assert first.inner._closed and second.inner._closed
    registry.close()
    with pytest.raises(RuntimeError, match="closed"):
        registry.bare(root)


def test_unmetered_profile_is_optional_and_does_not_change_class_defaults(tmp_path):
    with CloudRegistry() as registry:
        domain = registry.bare(tmp_path / "root")
        metered = registry.bare(tmp_path / "root", unmetered=False)
        assert domain.profile.metered is False
        assert metered.profile is SupabaseTransport.profile
        assert metered.profile.metered is True


def test_factory_peers_observe_document_changes_without_manual_refresh(tmp_path):
    observed = threading.Event()
    with CloudRegistry() as registry:
        root = registry.root(tmp_path / "root")
        writer = registry.make_transport(root)
        peer = registry.make_transport(root)
        unsubscribe = peer.subscribe_changes(observed.set)
        try:
            writer.put_doc("users/alice.json", {"name": "Alice"})
            assert observed.wait(2.0), "factory peer did not refresh its provider snapshot"
            assert peer.get_doc("users/alice.json") == {"name": "Alice"}
        finally:
            unsubscribe()


def test_factory_manual_mode_keeps_source_capture_steps_explicit(tmp_path):
    with CloudRegistry(factory_auto_refresh=False) as registry:
        root = registry.root(tmp_path / "root")
        cache = registry.make_transport(root)
        assert cache.auto_refresh is False
        assert cache._thread is None
