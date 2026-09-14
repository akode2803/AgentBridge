"""Bounded diagnostic overlap of one process mirror and one SQLite read cut.

The result proves only that the two local observations coexisted at SQLite's
first read while the cooperating mirror remained unchanged.  It is not current
at return, transport freshness, access authority, or cache-admission evidence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..store.chat_inputs import LocalChatInputs
from ..store.db import Store
from ..transport.base import Transport
from ..transport.mirror_observation import (
    MAX_MIRROR_INTEGER,
    MirrorCaptureUnavailable,
    MirrorExpectedPosition,
    MirrorObservation,
    MirrorPositionValidation,
)

_DEFAULT_BYTES = 64 * 1024 * 1024
_UNAVAILABLE_REASONS = {
    "unsupported",
    "cold",
    "invalid_identity",
    "invalid_payload",
    "budget_exceeded",
    "revision_exhausted",
    "mutation_interrupted",
    "sqlite_unavailable",
    "mirror_changed",
}
_SQLITE_BUSY = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


@dataclass(frozen=True)
class LocalOverlapObservation:
    mirror: MirrorObservation
    local: LocalChatInputs
    attempts: int
    serialized_bytes: int
    local_overlap: bool = True


@dataclass(frozen=True)
class LocalOverlapUnavailable:
    reason: str
    attempts: int

    def __post_init__(self) -> None:
        if self.reason not in _UNAVAILABLE_REASONS:
            raise ValueError("unknown local overlap unavailable reason")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 2:
            raise ValueError("local overlap attempts must be one or two")


def capture_local_overlap(
    transport: Transport,
    store: Store,
    chat_id: str,
    *,
    max_documents: int = 100_000,
    max_chat_ids: int = 100_000,
    max_messages: int = 100_000,
    max_logs: int = 10_000,
    max_bytes: int = _DEFAULT_BYTES,
) -> LocalOverlapObservation | LocalOverlapUnavailable:
    """Capture at most two attempts; only a changed mirror causes a retry."""
    if not isinstance(transport, Transport):
        raise ValueError("transport must implement Transport")
    if not isinstance(store, Store):
        raise ValueError("store must be a Store")
    if type(chat_id) is not str or not chat_id:
        raise ValueError("chat_id must be a nonempty string")
    for label, value in (
        ("max_documents", max_documents),
        ("max_chat_ids", max_chat_ids),
        ("max_messages", max_messages),
        ("max_logs", max_logs),
        ("max_bytes", max_bytes),
    ):
        if type(value) is not int or not 0 <= value <= MAX_MIRROR_INTEGER:
            raise ValueError(f"{label} must be a nonnegative bounded integer")
    database_path = str(store.path)
    static_local_bytes = _utf8_size(database_path, chat_id)

    for attempt in (1, 2):
        mirror = transport.capture_mirror(
            max_documents=max_documents,
            max_chat_ids=max_chat_ids,
            max_bytes=max_bytes,
        )
        if isinstance(mirror, MirrorCaptureUnavailable):
            return LocalOverlapUnavailable(mirror.reason, attempt)
        if type(mirror) is not MirrorObservation:
            raise RuntimeError("transport returned an invalid mirror capture")
        mirror_bytes = _mirror_bytes(mirror)
        if mirror_bytes + static_local_bytes > max_bytes:
            return LocalOverlapUnavailable("budget_exceeded", attempt)
        remaining = max_bytes - mirror_bytes - static_local_bytes
        expected = MirrorExpectedPosition.from_observation(mirror)
        try:
            local = store.capture_chat_inputs(
                chat_id,
                document_paths=("sync/log_cursor",),
                max_messages=max_messages,
                max_logs=max_logs,
                max_bytes=remaining,
            )
        except OverflowError:
            return LocalOverlapUnavailable("budget_exceeded", attempt)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if type(code) is int and (code & 0xFF) in _SQLITE_BUSY:
                return LocalOverlapUnavailable("sqlite_unavailable", attempt)
            raise
        if (type(local) is not LocalChatInputs or local.chat_id != chat_id
                or local.database_path != database_path):
            raise RuntimeError("Store returned different local input identity")
        total_bytes = mirror_bytes + _local_bytes(local)
        if total_bytes > max_bytes:
            return LocalOverlapUnavailable("budget_exceeded", attempt)
        validation = transport.validate_mirror_position(expected)
        if type(validation) is not MirrorPositionValidation:
            raise RuntimeError("transport returned invalid position validation")
        if validation.status == "matched":
            return LocalOverlapObservation(mirror, local, attempt, total_bytes)
        if validation.status == "unavailable":
            return LocalOverlapUnavailable(validation.reason or "invalid_payload", attempt)
        if validation.status != "changed":
            raise RuntimeError("transport returned unknown position validation")
        if attempt == 2:
            return LocalOverlapUnavailable("mirror_changed", attempt)
        # Release the first attempt's potentially large buffers before retrying.
        del local, mirror
    raise AssertionError("unreachable")


def _mirror_bytes(observation: MirrorObservation) -> int:
    return _utf8_size(
        observation.instance_nonce,
        observation.root_identity,
        observation.cache_identity,
        *observation.chat_ids,
        *(item for record in observation.records
          for item in (record.path, record.payload_json)),
    )


def _local_bytes(observation: LocalChatInputs) -> int:
    return _utf8_size(
        observation.database_path,
        observation.incarnation,
        observation.chat_id,
        *observation.message_json,
        *(name for name, _offset in observation.offsets),
        *(item for name, payload in observation.document_json
          for item in ((name,) if payload is None else (name, payload))),
    )


def _utf8_size(*values: str) -> int:
    try:
        return sum(len(value.encode("utf-8")) for value in values)
    except UnicodeEncodeError as exc:
        raise ValueError("observation identity is not valid UTF-8") from exc


__all__ = [
    "LocalOverlapObservation",
    "LocalOverlapUnavailable",
    "capture_local_overlap",
]
