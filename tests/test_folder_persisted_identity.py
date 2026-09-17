"""Folder mirror diagnostics must not rename legacy persisted Mesh state."""
from __future__ import annotations

import hashlib

from agentbridge.mesh.pins import KeyPinStore
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity


def test_direct_and_cached_folder_keep_legacy_store_pin_and_lock_namespace(tmp_path):
    root = tmp_path / "provider"
    home = tmp_path / "home"
    inner = FolderTransport(root)
    assert not hasattr(inner, "cache_key")

    legacy_tag = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]
    legacy_store_path = home / "cache" / f"alice@machine-{legacy_tag}.sqlite"
    seeded = Store(legacy_store_path)
    try:
        seeded.cache_doc("sentinel.json", {"namespace": "legacy"})
    finally:
        seeded.close()

    direct = Mesh(inner, "alice", "machine", home=home)
    try:
        assert direct.store.path == legacy_store_path
        assert direct.store.cached_doc("sentinel.json") == {"namespace": "legacy"}
        direct.key_pins.trusted("bob", "sign", "agree")
        direct.key_pins.mark_verified("bob")
        expected_store = direct.store.path
        expected_pin = direct.key_pins.path
        expected_lock = direct.key_pins._storage.lock_path
        expected_verified = direct.key_pins.verified("bob")
        assert expected_verified
    finally:
        direct.close()

    cached_tx = CachingTransport(FolderTransport(root), auto_refresh=False)
    cached = Mesh(cached_tx, "alice", "machine", home=home)
    try:
        assert cached.store.path == expected_store
        assert cached.store.cached_doc("sentinel.json") == {"namespace": "legacy"}
        assert cached.key_pins.path == expected_pin
        assert cached.key_pins._storage.lock_path == expected_lock
        assert cached.key_pins.trusted("bob", "sign", "agree") == (
            "sign", "agree",
        )
        assert cached.key_pins.trusted(
            "bob", "replacement-sign", "replacement-agree",
        ) == ("sign", "agree")
        assert cached.key_pins.verified("bob") == expected_verified

        legacy = KeyPinStore(home, str(root.resolve()))
        assert legacy.path == expected_pin
        assert legacy._storage.lock_path == expected_lock
        assert legacy.verified("bob") == expected_verified
    finally:
        cached.close()


def test_cached_folder_mirror_identity_is_valid_without_public_cache_key(tmp_path):
    inner = FolderTransport(tmp_path / "provider")
    cached = CachingTransport(inner, auto_refresh=False)
    try:
        cached.refresh()
        observed = cached.capture_mirror()
        assert observed.root_identity == str(inner.root)
        assert observed.cache_identity.startswith("folder:")
        assert root_identity(cached) == root_identity(inner)
        assert not hasattr(inner, "cache_key")
    finally:
        cached.close()


def test_explicit_folder_cache_key_remains_the_diagnostic_and_mesh_namespace(tmp_path):
    inner = FolderTransport(tmp_path / "provider")
    inner.cache_key = "explicit-folder-cache"
    cached = CachingTransport(inner, auto_refresh=False)
    mesh = Mesh(cached, "alice", "machine", home=tmp_path / "home")
    try:
        cached.refresh()
        observed = cached.capture_mirror()
        assert observed.cache_identity == "explicit-folder-cache"
        assert mesh.key_pins.path == KeyPinStore(
            mesh.home, "explicit-folder-cache",
        ).path
        assert root_identity(cached) == root_identity(inner)
    finally:
        mesh.close()
