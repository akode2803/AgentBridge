"""Bounded complete raw-document collection for inactive local ingestion."""
from __future__ import annotations

import copy
import os

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport import folder_raw
from agentbridge.transport.authority_observation import detach_ingress_documents
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.raw_documents import RawCollectionUnavailable
from agentbridge.transport.supabase import SupabaseTransport


S = source_selectors.Selector


def _definition(transport, *selectors, build="raw-test"):
    return source_selectors.definition(
        root_identity(transport), tuple(selectors), build=build,
    )


def _online_folder(tmp_path, documents):
    folder = FolderTransport(tmp_path / "provider")
    folder.cache_key = "raw-documents-test-cache"
    for path, value in documents.items():
        folder.put_doc(path, value)
    return folder


def _online_cache(folder):
    documents = folder.get_docs("")
    provider = SupabaseTransport(
        "mesh", env={"SUPABASE_URL": "https://raw-documents.test"},
        client=object(),
    )
    cache = CachingTransport(provider, auto_refresh=False)
    with cache._lock:
        cache._docs = copy.deepcopy(documents)
        cache._warm = True
        cache._mirror_revision = 1
        cache._mirror_provenance = "provider_observed"
    return cache


def _publisher(tmp_path, transport, definition, name):
    store = Store(tmp_path / f"{name}.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    coordinator = MutationCoordinator(tmp_path / f"{name}-home", root_identity(transport))
    coordinator.register_store(store)
    return store, SourcePublisher(coordinator, store, definition)


def _patch_open(monkeypatch, replacement):
    supported = set(folder_raw.os.supports_dir_fd)
    supported.add(replacement)
    monkeypatch.setattr(folder_raw.os, "open", replacement)
    monkeypatch.setattr(folder_raw.os, "supports_dir_fd", supported)


def test_root_bound_exact_prefix_and_legitimate_absence(tmp_path):
    folder = _online_folder(tmp_path, {
        "accounts/alice.json": {"name": "Alice"},
        "chats/room/state/alice.json": {"read": 7},
        "chats/other/state/bob.json": {"read": 9},
    })
    definition = _definition(
        folder,
        S("doc_exact", "accounts/alice.json"),
        S("doc_exact", "accounts/missing.json"),
        S("doc_prefix", "chats/room/state"),
        S("doc_prefix", "chats/missing/state"),
    )
    assert raw_documents.collect_documents(folder, definition) == {
        "accounts/alice.json": {"name": "Alice"},
        "chats/room/state/alice.json": {"read": 7},
    }

    other = FolderTransport(tmp_path / "other-provider")
    with pytest.raises(ValueError, match="another transport root"):
        raw_documents.collect_documents(other, definition)


@pytest.mark.parametrize("failure", [
    "malformed",
    pytest.param(
        "io",
        marks=pytest.mark.skipif(os.name == "nt", reason="POSIX os.open injection"),
    ),
])
def test_malformed_and_io_are_unavailable_not_absence(tmp_path, monkeypatch,
                                                       failure):
    folder = FolderTransport(tmp_path / "provider")
    path = folder.root / "accounts" / "alice.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not-json")
    definition = _definition(folder, S("doc_exact", "accounts/alice.json"))
    if failure == "io":
        original = folder_raw.os.open

        def denied(owner, *args, **kwargs):
            if owner == "alice.json":
                raise OSError("unreadable")
            return original(owner, *args, **kwargs)

        _patch_open(monkeypatch, denied)
        reason = "io"
    else:
        reason = "malformed_document"
    with pytest.raises(RawCollectionUnavailable, match=reason):
        raw_documents.collect_documents(folder, definition)


@pytest.mark.skipif(os.name == "nt", reason="POSIX os.open injection")
def test_discovered_prefix_file_deleted_before_read_is_selection_changed(
        tmp_path, monkeypatch):
    folder = _online_folder(tmp_path, {
        "chats/room/state/alice.json": {"read": 1},
    })
    target = folder.root / "chats" / "room" / "state" / "alice.json"
    definition = _definition(folder, S("doc_prefix", "chats/room/state"))
    original = folder_raw.os.open
    removed = False

    def delete_then_open(owner, *args, **kwargs):
        nonlocal removed
        if owner == "alice.json" and not removed:
            removed = True
            target.unlink()
        return original(owner, *args, **kwargs)

    _patch_open(monkeypatch, delete_then_open)
    with pytest.raises(RawCollectionUnavailable, match="selection_changed"):
        raw_documents.collect_documents(folder, definition)
    assert removed


def test_symlink_in_exact_ancestor_or_prefix_selection_is_rejected(tmp_path):
    folder = FolderTransport(tmp_path / "provider")
    external = tmp_path / "external"
    external.mkdir()
    (external / "alice.json").write_text('{"name":"Alice"}', encoding="utf-8")
    (folder.root / "accounts").symlink_to(external, target_is_directory=True)
    exact = _definition(folder, S("doc_exact", "accounts/alice.json"), build="exact")
    prefix = _definition(folder, S("doc_prefix", "accounts"), build="prefix")
    for definition in (exact, prefix):
        with pytest.raises(RawCollectionUnavailable, match="symlink_in_selection|io"):
            raw_documents.collect_documents(folder, definition)


def test_byte_limit_rejects_before_json_parse(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / "provider")
    path = folder.root / "accounts" / "large.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"body":"' + b"x" * 100 + b'"}')
    definition = _definition(folder, S("doc_exact", "accounts/large.json"))
    parsed = []

    real_loads = raw_documents.json.loads

    def forbidden_payload_parse(value, *args, **kwargs):
        if isinstance(value, str) and value.startswith('{"body"'):
            parsed.append(True)
            pytest.fail("oversized payload reached JSON parsing")
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(raw_documents.json, "loads", forbidden_payload_parse)
    with pytest.raises(RawCollectionUnavailable, match="byte_budget"):
        raw_documents.collect_documents(folder, definition, max_bytes=32)
    assert parsed == []


def test_path_document_and_depth_budgets_fail_closed(tmp_path):
    folder = _online_folder(tmp_path, {
        "scope/a.json": {"a": 1},
        "scope/b.json": {"b": 2},
    })
    definition = _definition(folder, S("doc_prefix", "scope"))
    with pytest.raises(RawCollectionUnavailable, match="path_budget"):
        raw_documents.collect_documents(folder, definition, max_examined_paths=1)
    with pytest.raises(RawCollectionUnavailable, match="document_budget"):
        raw_documents.collect_documents(folder, definition, max_documents=1)

    deep = "scope/" + "/".join(["deep"] * 32 + ["leaf.json"])
    deep_path = folder.root / deep
    deep_path.parent.mkdir(parents=True)
    deep_path.write_text("{}", encoding="utf-8")
    deep_definition = _definition(folder, S("doc_prefix", "scope"), build="deep")
    with pytest.raises(RawCollectionUnavailable, match="path_budget"):
        raw_documents.collect_documents(folder, deep_definition)


def test_subset_excludes_unselected_documents_and_logs(tmp_path):
    folder = _online_folder(tmp_path, {
        "accounts/alice.json": {"name": "Alice"},
        "accounts/bob.json": {"name": "Bob"},
        "chats/room/meta.json": {"name": "Room"},
    })
    folder.append_log("room", "alice@box", {"id": "m1", "ns": 1})
    definition = _definition(
        folder,
        S("doc_exact", "accounts/alice.json"),
        S("log_chat", "room"),
    )
    assert raw_documents.collect_documents(folder, definition) == {
        "accounts/alice.json": {"name": "Alice"},
    }


def test_cache_requires_provider_observed_safe_owned_values_and_copies_off_lock(
        tmp_path, monkeypatch):
    documents = {
        "accounts/alice.json": {"nested": {"name": "Alice"}},
        "unrelated/large.json": {"body": "unselected"},
    }
    provider = SupabaseTransport(
        "mesh", env={"SUPABASE_URL": "https://raw-documents.test"},
        client=object(),
    )
    cold = CachingTransport(provider, auto_refresh=False)
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


def test_cache_path_and_selected_reference_budgets_are_bounded(tmp_path):
    folder = _online_folder(tmp_path, {
        "scope/a.json": {"a": 1},
        "scope/b.json": {"b": 2},
        "unrelated/c.json": {"c": 3},
    })
    cache = _online_cache(folder)
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


def test_folder_and_provider_observed_cache_publish_equivalent_admitted_content(
        tmp_path):
    documents = {
        "accounts/alice.json": {"name": "Alice"},
        "chats/room/state/alice.json": {"read": 3},
        "unrelated/ignored.json": {"ignore": True},
    }
    folder = _online_folder(tmp_path, documents)
    cache = _online_cache(folder)
    definition = _definition(
        folder,
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/room/state"),
    )
    cache_definition = _definition(
        cache,
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/room/state"),
    )
    folder_store, folder_publisher = _publisher(
        tmp_path, folder, definition, "folder",
    )
    cache_store, cache_publisher = _publisher(
        tmp_path, cache, cache_definition, "cache",
    )
    try:
        folder_docs = raw_documents.collect_documents(folder, definition)
        cache_docs = raw_documents.collect_documents(cache, cache_definition)
        assert folder_docs == cache_docs
        folder_ready = folder_publisher.publish(
            folder_publisher.capture(), folder_docs, observed_ns=1,
        )
        cache_ready = cache_publisher.publish(
            cache_publisher.capture(), cache_docs, observed_ns=1,
        )
        assert folder_ready.ready and cache_ready.ready
        assert folder_store.capture_document_observation(
            definition.source,
        ).documents() == cache_store.capture_document_observation(
            cache_definition.source,
        ).documents()
    finally:
        folder_store.close()
        cache_store.close()
        cache.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX capability gate")
def test_handle_relative_capability_absence_fails_closed(tmp_path, monkeypatch):
    folder = _online_folder(tmp_path, {
        "accounts/alice.json": {"name": "Alice"},
    })
    definition = _definition(folder, S("doc_exact", "accounts/alice.json"))
    monkeypatch.setattr(folder_raw.os, "supports_dir_fd", set())
    monkeypatch.setattr(folder_raw.os, "supports_fd", set())
    with pytest.raises(RawCollectionUnavailable,
                       match="handle_relative_reads_unsupported"):
        raw_documents.collect_documents(folder, definition)


@pytest.mark.skipif(os.name == "nt", reason="POSIX os.open injection")
def test_file_swapped_to_symlink_between_enumeration_and_open_is_rejected(
        tmp_path, monkeypatch):
    folder = _online_folder(tmp_path, {
        "scope/alice.json": {"name": "inside"},
    })
    target = folder.root / "scope" / "alice.json"
    external = tmp_path / "outside.json"
    external.write_text('{"name":"outside"}', encoding="utf-8")
    definition = _definition(folder, S("doc_prefix", "scope"))
    original = folder_raw.os.open
    swapped = False

    def swap_then_open(name, *args, **kwargs):
        nonlocal swapped
        if name == "alice.json" and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(external)
        return original(name, *args, **kwargs)

    _patch_open(monkeypatch, swap_then_open)
    with pytest.raises(RawCollectionUnavailable):
        raw_documents.collect_documents(folder, definition)
    assert swapped


def test_hostile_deepcopy_alias_for_overlay_is_detached_before_cache_admission(
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

    provider = SupabaseTransport(
        "mesh", env={"SUPABASE_URL": "https://raw-documents.test"},
        client=object(),
    )
    cache = CachingTransport(provider, auto_refresh=False)
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
