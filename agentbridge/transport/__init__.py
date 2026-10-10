"""Transport layer: the only code that touches bytes-at-rest (FORMAT2)."""

import hashlib
from pathlib import Path

from ..core.config import validate_root_spec
from .base import Transport, Watcher
from .cache import CachingTransport
from .change_ledger import (
    ChangeLedgerCapability,
    ChangeLedgerEpoch,
    ChangeLedgerEvent,
    ChangeLedgerPage,
)
from .source_ledger import SourceLedgerEvent, SourceLedgerFence, SourceLedgerPage
from .scoped_sources import (
    ScopedDocumentBatch,
    ScopedDocumentRow,
    ScopedLogPage,
    ScopedLogRow,
    ScopedSourceOverflow,
)

__all__ = [
    "Transport", "Watcher", "CachingTransport", "make_transport", "validate_root_spec",
    "ChangeLedgerCapability", "ChangeLedgerEpoch", "ChangeLedgerEvent",
    "ChangeLedgerPage",
    "SourceLedgerEvent", "SourceLedgerFence", "SourceLedgerPage",
    "ScopedDocumentBatch", "ScopedDocumentRow", "ScopedLogPage", "ScopedLogRow",
    "ScopedSourceOverflow",
]


def make_transport(spec, home: Path | None = None, *, offline_cache: bool = False) -> Transport:
    """Build a cloud transport with a warm read mirror and optional snapshot.

    Owned Transport instances are accepted for internal composition. Configured
    roots must be valid cloud specifications; local storage remains independent.
    """
    if isinstance(spec, Transport):
        return spec
    text = validate_root_spec(spec)
    from .supabase import SupabaseTransport

    inner = SupabaseTransport(text[len("supabase://"):], home=home)
    snapshot_path = None
    if offline_cache:
        from ..core.config import DEFAULT_HOME

        digest = hashlib.sha256(inner.cache_key.encode("utf-8")).hexdigest()[:24]
        snapshot_path = (home or DEFAULT_HOME) / "cache" / f"cloud-{digest}.json"
    return CachingTransport(
        inner, snapshot_path=snapshot_path, nonblocking_cold=offline_cache,
    )
