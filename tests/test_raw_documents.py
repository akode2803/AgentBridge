"""Bounded complete raw-document collection for inactive local ingestion."""
from __future__ import annotations

import copy

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport.authority_observation import detach_ingress_documents
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.raw_documents import RawCollectionUnavailable


S = source_selectors.Selector


def _definition(transport, *selectors, build="raw-test"):
    return source_selectors.definition(
        root_identity(transport), tuple(selectors), build=build,
    )


def _online_cache_seed(clouds, tmp_path, documents):
    clouds.seed_documents(tmp_path / "provider", documents)
    return clouds.cached(tmp_path / "provider")


def _publisher(tmp_path, transport, definition, name):
    store = Store(tmp_path / f"{name}.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    coordinator = MutationCoordinator(tmp_path / f"{name}-home", root_identity(transport))
    coordinator.register_store(store)
    return store, SourcePublisher(coordinator, store, definition)


def test_root_bound_exact_prefix_and_legitimate_absence(clouds, tmp_path):
    provider = _online_cache_seed(clouds, tmp_path, {
        "accounts/alice.json": {"name": "Alice"},
        "chats/room/state/alice.json": {"read": 7},
        "chats/other/state/bob.json": {"read": 9},
    })
    definition = _definition(
        provider,
        S("doc_exact", "accounts/alice.json"),
        S("doc_exact", "accounts/missing.json"),
        S("doc_prefix", "chats/room/state"),
        S("doc_prefix", "chats/missing/state"),
    )
    assert raw_documents.collect_documents(provider, definition) == {
        "accounts/alice.json": {"name": "Alice"},
        "chats/room/state/alice.json": {"read": 7},
    }

    other = clouds.cached(tmp_path / "other-provider")
    with pytest.raises(ValueError, match="another transport root"):
        raw_documents.collect_documents(other, definition)


@pytest.mark.parametrize("streaming", [False, True])
def test_bare_cloud_is_not_an_admitted_raw_document_mirror(clouds, tmp_path, streaming):
    provider = clouds.bare(tmp_path / "provider")
    provider.put_doc("scope/a.json", {"value": 1})
    definition = _definition(provider, S("doc_prefix", "scope"))
    with pytest.raises(RawCollectionUnavailable, match="unsupported_transport"):
        if streaming:
            raw_documents.collect_document_batches(provider, definition, consume=lambda _batch: None)
        else:
            raw_documents.collect_documents(provider, definition)


def test_byte_limit_rejects_oversize_decoded_payload(clouds, tmp_path):
    cache = _online_cache_seed(clouds, tmp_path, {
        "accounts/large.json": {"body": "x" * 100},
    })
    definition = _definition(cache, S("doc_exact", "accounts/large.json"))
    with pytest.raises(RawCollectionUnavailable, match="invalid_or_oversize_payload"):
        raw_documents.collect_documents(cache, definition, max_bytes=32)


def test_path_document_and_payload_depth_budgets_fail_closed(clouds, tmp_path):
    provider = _online_cache_seed(clouds, tmp_path, {
        "scope/a.json": {"a": 1},
        "scope/b.json": {"b": 2},
    })
    definition = _definition(provider, S("doc_prefix", "scope"))
    with pytest.raises(RawCollectionUnavailable, match="path_budget"):
        raw_documents.collect_documents(provider, definition, max_examined_paths=1)
    with pytest.raises(RawCollectionUnavailable, match="document_budget"):
        raw_documents.collect_documents(provider, definition, max_documents=1)

    deep = {}
    for _ in range(100):
        deep = {"nested": deep}
    # Fault injection targets collector structure accounting independently of
    # the cache ingress detachment guard.
    with provider._lock:
        provider._docs["scope/deep.json"] = deep
        provider._mirror_revision += 1
    deep_definition = _definition(provider, S("doc_prefix", "scope"), build="deep")
    with pytest.raises(RawCollectionUnavailable, match="invalid_or_oversize_payload"):
        raw_documents.collect_documents(provider, deep_definition)


def test_subset_excludes_unselected_documents_and_logs(clouds, tmp_path):
    provider = _online_cache_seed(clouds, tmp_path, {
        "accounts/alice.json": {"name": "Alice"},
        "accounts/bob.json": {"name": "Bob"},
        "chats/room/meta.json": {"name": "Room"},
    })
    provider.append_log("room", "alice@box", {"id": "m1", "ns": 1})
    definition = _definition(
        provider,
        S("doc_exact", "accounts/alice.json"),
        S("log_chat", "room"),
    )
    assert raw_documents.collect_documents(provider, definition) == {
        "accounts/alice.json": {"name": "Alice"},
    }


def test_cache_requires_provider_observed_safe_owned_values_and_copies_off_lock(clouds,
        tmp_path, monkeypatch):
    documents = {
        "accounts/alice.json": {"nested": {"name": "Alice"}},
        "unrelated/large.json": {"body": "unselected"},
    }
    cold = clouds.cached(tmp_path / "provider", warm=False)
    definition = _definition(cold, S("doc_exact", "accounts/alice.json"))
    try:
        with pytest.raises(RawCollectionUnavailable, match="mirror_pending"):
            raw_documents.collect_documents(cold, definition)
        with cold._lock:
            cold._docs = copy.deepcopy(documents)
            cold._warm = True
            cold._mirror_revision = 1
            cold._mirror_provenance = "provider_observed"
        original = raw_documents._JSONBudget.copy
        lock_states = []

        def observed_copy(owner, value, depth=0):
            lock_states.append(cold._lock.locked())
            return original(owner, value, depth)

        monkeypatch.setattr(raw_documents._JSONBudget, "copy", observed_copy)
        result = raw_documents.collect_documents(cold, definition)
        assert lock_states and not any(lock_states)
        result["accounts/alice.json"]["nested"]["name"] = "Changed"
        assert cold.get_doc("accounts/alice.json")["nested"]["name"] == "Alice"

        with cold._lock:
            cold._authority_unsafe.add("accounts/alice.json")
        with pytest.raises(RawCollectionUnavailable, match="unsafe_cached_value"):
            raw_documents.collect_documents(cold, definition)
    finally:
        cold.close()


def test_cache_path_and_selected_reference_budgets_are_bounded(clouds, tmp_path):
    provider = _online_cache_seed(clouds, tmp_path, {
        "scope/a.json": {"a": 1},
        "scope/b.json": {"b": 2},
        "unrelated/c.json": {"c": 3},
    })
    cache = clouds.cached(provider.root)
    definition = _definition(cache, S("doc_prefix", "scope"))
    try:
        with pytest.raises(RawCollectionUnavailable, match="path_budget"):
            raw_documents.collect_documents(
                cache, definition, max_examined_paths=2,
            )
        with pytest.raises(RawCollectionUnavailable, match="document_budget"):
            raw_documents.collect_documents(cache, definition, max_documents=1)
    finally:
        cache.close()


def test_independent_provider_observed_caches_publish_equivalent_admitted_content(clouds,
        tmp_path):
    documents = {
        "accounts/alice.json": {"name": "Alice"},
        "chats/room/state/alice.json": {"read": 3},
        "unrelated/ignored.json": {"ignore": True},
    }
    provider = _online_cache_seed(clouds, tmp_path, documents)
    cache = clouds.cached(provider.root)
    definition = _definition(
        provider,
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/room/state"),
    )
    cache_definition = _definition(
        cache,
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/room/state"),
    )
    provider_store, provider_publisher = _publisher(
        tmp_path, provider, definition, "provider",
    )
    cache_store, cache_publisher = _publisher(
        tmp_path, cache, cache_definition, "cache",
    )
    try:
        provider_docs = raw_documents.collect_documents(provider, definition)
        cache_docs = raw_documents.collect_documents(cache, cache_definition)
        assert provider_docs == cache_docs
        provider_ready = provider_publisher.publish(
            provider_publisher.capture(), provider_docs, observed_ns=1,
        )
        cache_ready = cache_publisher.publish(
            cache_publisher.capture(), cache_docs, observed_ns=1,
        )
        assert provider_ready.ready and cache_ready.ready
        assert provider_store.capture_document_observation(
            definition.source,
        ).documents() == cache_store.capture_document_observation(
            cache_definition.source,
        ).documents()
    finally:
        provider_store.close()
        cache_store.close()
        cache.close()


def test_hostile_deepcopy_alias_for_overlay_is_detached_before_cache_admission(clouds,
        tmp_path):
    class AliasDeepcopy:
        def __init__(self, target):
            self.target = target

        def __deepcopy__(self, _memo):
            return self.target

    path = "chats/room/state/alice.json"
    provider_value = {"read": 1, "nested": {"value": "before"}}
    copied, unsafe = detach_ingress_documents(copy.deepcopy({
        path: AliasDeepcopy(provider_value),
    }))
    assert unsafe == set()
    assert copied[path] == provider_value
    assert copied[path] is not provider_value
    assert copied[path]["nested"] is not provider_value["nested"]

    cache = clouds.cached(tmp_path / "provider", warm=False)
    definition = _definition(cache, S("doc_exact", path))
    try:
        with cache._lock:
            cache._docs = copied
            cache._authority_unsafe = unsafe
            cache._warm = True
            cache._mirror_revision = 1
            cache._mirror_provenance = "provider_observed"
        provider_value["nested"]["value"] = "after"
        assert raw_documents.collect_documents(cache, definition) == {
            path: {"read": 1, "nested": {"value": "before"}},
        }
    finally:
        cache.close()
