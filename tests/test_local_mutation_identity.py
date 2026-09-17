"""Canonical built-in transport identity and inactive factory contracts."""
from __future__ import annotations

import pytest

from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import (
    LocalMutationTransport,
    owned_transport,
    root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport


def _cloud(url, root="mesh", **credentials):
    env = {"SUPABASE_URL": url, **credentials}
    return SupabaseTransport(root, env=env, client=object())


def test_folder_absolute_dot_and_symlink_aliases_share_exact_identity(tmp_path):
    root = tmp_path / "mesh"
    direct = FolderTransport(root)
    dotted = FolderTransport(root / ".")
    alias = tmp_path / "mesh-alias"
    alias.symlink_to(root, target_is_directory=True)
    linked = FolderTransport(alias)

    assert root_identity(direct) == root_identity(dotted) == root_identity(linked)
    assert root_identity(FolderTransport(tmp_path / "other")) != root_identity(direct)
    assert root_identity(direct).startswith('["local-root-v1","folder",')


def test_cloud_identity_binds_canonical_endpoint_and_root_not_credentials():
    first = _cloud(
        "https://example.test", "team",
        SUPABASE_SECRET_KEY="secret-one",
    )
    changed_credentials = _cloud(
        "https://example.test/", "team",
        SUPABASE_MEMBER_EMAIL="alice@example.test",
        SUPABASE_MEMBER_PASSWORD="different-password",
        SUPABASE_PUBLISHABLE_KEY="different-key",
    )
    different_endpoint = _cloud(
        "https://other.test", "team", SUPABASE_SECRET_KEY="secret-one",
    )
    different_root = _cloud(
        "https://example.test", "other", SUPABASE_SECRET_KEY="secret-one",
    )

    assert root_identity(first) == root_identity(changed_credentials)
    assert root_identity(first) != root_identity(different_endpoint)
    assert root_identity(first) != root_identity(different_root)
    identity = root_identity(first)
    assert "secret-one" not in identity
    assert "different-password" not in identity
    assert "different-key" not in identity


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("HTTPS://Example.TEST:443/", "https://example.test"),
        ("http://Example.TEST:80/", "http://example.test"),
        ("https://[2001:DB8::1]:443/", "https://[2001:db8::1]"),
    ],
)
def test_cloud_url_case_default_port_and_trailing_slash_are_canonical(left,
                                                                       right):
    assert root_identity(_cloud(left)) == root_identity(_cloud(right))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.test/root",
        "https://user:password@example.test/root",
        "https://example.test/root?project=other",
        "https://example.test/root#fragment",
        "https://example.test/root with-space",
        "https://example.test:bad/root",
        "https://example.test/a/../b",
        "https://example.test./",
        "https://tést.example/",
    ],
)
def test_malformed_or_credential_bearing_cloud_urls_are_rejected(url):
    with pytest.raises(ValueError):
        root_identity(_cloud(url))


def test_unknown_nested_and_unknown_cached_transport_owners_are_rejected(tmp_path):
    class UnknownFolder(FolderTransport):
        pass

    unknown = UnknownFolder(tmp_path / "unknown")
    with pytest.raises(ValueError, match="unsupported transport owner"):
        root_identity(unknown)

    direct = FolderTransport(tmp_path / "mesh")
    wrapped = owned_transport(direct, tmp_path / "home")
    with pytest.raises(ValueError, match="unsupported transport owner"):
        root_identity(wrapped)

    unknown_cache = CachingTransport(unknown, auto_refresh=False)
    try:
        with pytest.raises(ValueError, match="unsupported nested transport owner"):
            root_identity(unknown_cache)
    finally:
        unknown_cache.close()


def test_cache_identity_must_still_match_its_captured_inner_owner(tmp_path):
    inner = FolderTransport(tmp_path / "mesh")
    cached = CachingTransport(inner, auto_refresh=False)
    try:
        assert root_identity(cached) == root_identity(inner)
        cached._mirror_root_identity = "different-root"
        with pytest.raises(ValueError, match="cache identity changed"):
            root_identity(cached)
    finally:
        cached.close()


def test_factory_uses_exact_root_identity_and_folder_close_is_safe(tmp_path):
    inner = FolderTransport(tmp_path / "mesh")
    expected = root_identity(inner)
    wrapped = owned_transport(inner, tmp_path / "home")
    assert type(wrapped) is LocalMutationTransport
    assert wrapped._transport is inner
    assert wrapped._coordinator.identity == expected
    assert wrapped._coordinator.path.parent == (tmp_path / "home").resolve() / (
        "local-input-owners"
    )
    # FolderTransport owns no close hook. The outer factory must preserve that
    # no-op lifecycle rather than exposing a method that raises AttributeError.
    wrapped.close()


def test_cloud_nondefault_ports_remain_identity_separators():
    base = root_identity(_cloud("https://example.test"))
    assert base != root_identity(_cloud("https://example.test:8443"))


def test_cloud_root_normalization_is_stable():
    assert root_identity(_cloud("https://example.test", "/team/")) == root_identity(
        _cloud("https://example.test/", "team"),
    )
