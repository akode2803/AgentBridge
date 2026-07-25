"""Transport layer: the only code that touches bytes-at-rest (FORMAT2)."""

import hashlib
from pathlib import Path

from .base import Transport, Watcher
from .cache import CachingTransport
from .folder import FolderTransport

__all__ = [
    "Transport", "Watcher", "FolderTransport", "CachingTransport",
    "make_transport",
]


def make_transport(spec, home: Path | None = None, *, offline_cache: bool = False) -> Transport:
    """One factory for every driver: a ``supabase://<root-name>`` spec builds
    the cloud driver (credentials from ``<home>/supabase.env``, R23);
    anything else is a synced-folder path. Callers keep passing whatever the
    remembered config holds — the scheme decides.

    A cloud transport is wrapped in a warm read MIRROR (``CachingTransport``):
    doc metadata is bulk-loaded once and refreshed in the background, so the
    hot GUI read paths never pay the per-op network RTT (see cache.py). A
    local folder needs no cache — every read is already ~free — so it is
    returned bare, and the well-exercised folder read/write behaviour is left
    untouched."""
    if isinstance(spec, Transport):
        return spec
    text = str(spec)
    if text.startswith("supabase://"):
        from .supabase import SupabaseTransport

        inner = SupabaseTransport(text[len("supabase://"):], home=home)
        snapshot_path = None
        if offline_cache:
            from ..core.config import DEFAULT_HOME

            digest = hashlib.sha256(inner.cache_key.encode("utf-8")).hexdigest()[:24]
            snapshot_path = (home or DEFAULT_HOME) / "cache" / f"cloud-{digest}.json"
        return CachingTransport(
            inner,
            snapshot_path=snapshot_path,
            nonblocking_cold=offline_cache,
        )
    return FolderTransport(spec)
