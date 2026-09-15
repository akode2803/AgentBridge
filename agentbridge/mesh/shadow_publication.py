"""One-shot publication of a bounded process-mirror snapshot.

Receipts describe a historical diagnostic cut.  They do not establish remote
freshness, access authority, or cache-admission authority.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from ..store import shadow_slot
from ..store.db import Store
from ..store.shadow_slot import ShadowPosition, ShadowSnapshot, ShadowSource
from ..transport.base import Transport
from ..transport.mirror_observation import (
    MAX_MIRROR_INTEGER,
    MirrorCaptureUnavailable,
    MirrorObservation,
)

_DEFAULT_BYTES = 64 * 1024 * 1024
_PHASES = {"capture", "preflight", "acquire", "publish"}
_REASONS = {
    "unsupported",
    "cold",
    "invalid_identity",
    "invalid_payload",
    "budget_exceeded",
    "revision_exhausted",
    "mutation_interrupted",
    "slot_limit_exceeded",
    "acquire_conflict",
    "publish_conflict",
    "sqlite_unavailable",
}
_SQLITE_BUSY = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


@dataclass(frozen=True)
class PublishedShadow:
    position: ShadowPosition
    revision: int
    provider_cursor: int
    provenance: str

    def __post_init__(self) -> None:
        if type(self.position) is not ShadowPosition or not self.position.initialized:
            raise ValueError("published receipt requires an initialized exact position")
        for value, label in (
            (self.revision, "revision"),
            (self.provider_cursor, "provider_cursor"),
        ):
            if type(value) is not int or not 0 <= value <= MAX_MIRROR_INTEGER:
                raise ValueError(f"{label} must be a nonnegative bounded integer")
        if type(self.provenance) is not str or self.provenance not in (
            "bootstrap_unverified", "provider_observed"
        ):
            raise ValueError("unknown mirror provenance")


@dataclass(frozen=True)
class ShadowPublicationUnavailable:
    reason: str
    phase: str
    acquired_position: ShadowPosition | None = None

    def __post_init__(self) -> None:
        if type(self.reason) is not str or self.reason not in _REASONS:
            raise ValueError("unknown shadow publication unavailable reason")
        if type(self.phase) is not str or self.phase not in _PHASES:
            raise ValueError("unknown shadow publication phase")
        if self.acquired_position is not None:
            if type(self.acquired_position) is not ShadowPosition:
                raise ValueError("acquired position must be exact ShadowPosition")
            if self.phase != "publish":
                raise ValueError("acquired position is only valid during publication")


def publish_shadow_once(
    transport: Transport,
    store: Store,
    expected_slot: ShadowPosition,
    *,
    max_documents: int = 100_000,
    max_chat_ids: int = 100_000,
    max_bytes: int = _DEFAULT_BYTES,
) -> PublishedShadow | ShadowPublicationUnavailable:
    """Capture and publish exactly one historical mirror cut, without retry."""
    if not isinstance(transport, Transport):
        raise ValueError("transport must implement Transport")
    if not isinstance(store, Store):
        raise ValueError("store must be a Store")

    pinned_transport = transport
    pinned_store = store
    database_path = pinned_store.path
    shadow_slot.validate_shadow_budgets(
        max_documents=max_documents,
        max_chat_ids=max_chat_ids,
        max_bytes=max_bytes,
    )
    wanted = shadow_slot.validate_shadow_position(expected_slot, database_path)

    mirror = pinned_transport.capture_mirror(
        max_documents=max_documents,
        max_chat_ids=max_chat_ids,
        max_bytes=max_bytes,
    )
    if isinstance(mirror, MirrorCaptureUnavailable):
        return ShadowPublicationUnavailable(mirror.reason, "capture")
    if type(mirror) is not MirrorObservation:
        raise RuntimeError("transport returned an invalid mirror capture")

    snapshot = ShadowSnapshot(
        source=ShadowSource(
            mirror.root_identity,
            mirror.cache_identity,
            mirror.instance_nonce,
        ),
        revision=mirror.revision,
        provider_cursor=mirror.provider_cursor,
        provenance=mirror.provenance,
        chat_ids=mirror.chat_ids,
        records=tuple((record.path, record.payload_json) for record in mirror.records),
    )
    try:
        shadow_slot.validate_shadow_snapshot(
            snapshot,
            max_documents=max_documents,
            max_chat_ids=max_chat_ids,
            max_bytes=max_bytes,
        )
    except OverflowError:
        return ShadowPublicationUnavailable("slot_limit_exceeded", "preflight")

    try:
        acquired = pinned_store.acquire_shadow(
            wanted, uuid.uuid4().hex, snapshot.source
        )
    except shadow_slot.ShadowConflict:
        return ShadowPublicationUnavailable("acquire_conflict", "acquire")
    except OverflowError:
        return ShadowPublicationUnavailable("slot_limit_exceeded", "acquire")
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return ShadowPublicationUnavailable("sqlite_unavailable", "acquire")
        raise

    try:
        committed = pinned_store.publish_shadow(
            acquired,
            snapshot,
            max_documents=max_documents,
            max_chat_ids=max_chat_ids,
            max_bytes=max_bytes,
        )
    except shadow_slot.ShadowConflict:
        return ShadowPublicationUnavailable(
            "publish_conflict", "publish", acquired
        )
    except OverflowError:
        return ShadowPublicationUnavailable(
            "slot_limit_exceeded", "publish", acquired
        )
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return ShadowPublicationUnavailable(
                "sqlite_unavailable", "publish", acquired
            )
        raise

    return PublishedShadow(
        committed, snapshot.revision, snapshot.provider_cursor, snapshot.provenance
    )


def retire_published_shadow(store: Store, receipt: PublishedShadow) -> ShadowPosition:
    """Retire only the exact position carried by a successful receipt."""
    if not isinstance(store, Store):
        raise ValueError("store must be a Store")
    if type(receipt) is not PublishedShadow:
        raise ValueError("receipt must be exact PublishedShadow")
    position = shadow_slot.validate_shadow_position(
        receipt.position, store.path, owned=True
    )
    if not position.initialized:
        raise ValueError("published receipt position must be initialized")
    return store.retire_shadow(position)


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return type(code) is int and (code & 0xFF) in _SQLITE_BUSY


__all__ = [
    "PublishedShadow",
    "ShadowPublicationUnavailable",
    "publish_shadow_once",
    "retire_published_shadow",
]
