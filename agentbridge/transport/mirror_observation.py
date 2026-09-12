"""Bounded immutable observations of one process-local transport mirror.

An observation is diagnostic input for later publication work.  It is not
transport freshness, authorization, a remote snapshot, or cache-admission
authority.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

MAX_MIRROR_INTEGER = 2**63 - 1
MAX_DOCUMENT_PATH_BYTES = 4096
MAX_IDENTITY_BYTES = 4096
_REASONS = {
    "unsupported",
    "cold",
    "invalid_identity",
    "invalid_payload",
    "budget_exceeded",
    "revision_exhausted",
    "mutation_interrupted",
}


@dataclass(frozen=True)
class MirrorDocumentRecord:
    path: str
    payload_json: str

    def decoded(self) -> Any:
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class MirrorObservation:
    instance_nonce: str
    revision: int
    root_identity: str
    cache_identity: str
    provider_cursor: int
    provenance: str
    chat_ids: tuple[str, ...]
    records: tuple[MirrorDocumentRecord, ...]

    def documents(self) -> dict[str, Any]:
        return {record.path: record.decoded() for record in self.records}


@dataclass(frozen=True)
class MirrorCaptureUnavailable:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in _REASONS:
            raise ValueError("unknown mirror capture unavailable reason")


def validate_capture_budget(value: int, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_MIRROR_INTEGER:
        raise ValueError(f"{label} must be a nonnegative bounded integer")
    return value


def validate_identity(value: Any) -> str | None:
    if type(value) is not str or not value or "\x00" in value:
        return None
    try:
        if len(value.encode("utf-8")) > MAX_IDENTITY_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    return value


def capture_mirror_locked(
    *,
    warm: bool,
    invalid_reason: str | None,
    instance_nonce: str,
    revision: int,
    root_identity: str | None,
    cache_identity: str | None,
    provider_cursor: int,
    provenance: str,
    chat_ids: list[str],
    documents: dict[str, Any],
    max_documents: int,
    max_chat_ids: int,
    max_bytes: int,
) -> MirrorObservation | MirrorCaptureUnavailable:
    """Serialize one cut while its owner holds the mirror mutex.

    All recursive validation uses exact built-in types.  Consequently this
    function cannot invoke caller-defined container, key, numeric, or string
    hooks while the mutex is held.
    """
    if invalid_reason is not None:
        return MirrorCaptureUnavailable(invalid_reason)
    if root_identity is None or cache_identity is None:
        return MirrorCaptureUnavailable("invalid_identity")
    if not warm:
        return MirrorCaptureUnavailable("cold")
    if type(documents) is not dict or type(chat_ids) is not list:
        return MirrorCaptureUnavailable("invalid_payload")
    if len(documents) > max_documents or len(chat_ids) > max_chat_ids:
        return MirrorCaptureUnavailable("budget_exceeded")
    if (type(revision) is not int or not 0 <= revision <= MAX_MIRROR_INTEGER
            or type(provider_cursor) is not int
            or not 0 <= provider_cursor <= MAX_MIRROR_INTEGER
            or type(provenance) is not str
            or (provenance != "bootstrap_unverified"
                and provenance != "provider_observed")):
        return MirrorCaptureUnavailable("invalid_payload")

    try:
        used = sum(len(value.encode("utf-8")) for value in (
            instance_nonce, root_identity, cache_identity,
        ))
    except UnicodeEncodeError:
        return MirrorCaptureUnavailable("invalid_identity")
    if used > max_bytes:
        return MirrorCaptureUnavailable("budget_exceeded")
    normalized_ids: list[str] = []
    for chat_id in chat_ids:
        if type(chat_id) is not str or not chat_id or "\x00" in chat_id:
            return MirrorCaptureUnavailable("invalid_payload")
        try:
            used += len(chat_id.encode("utf-8"))
        except UnicodeEncodeError:
            return MirrorCaptureUnavailable("invalid_payload")
        if used > max_bytes:
            return MirrorCaptureUnavailable("budget_exceeded")
        normalized_ids.append(chat_id)

    rows: list[MirrorDocumentRecord] = []
    for path, value in documents.items():
        try:
            valid = _valid_path(path) and _valid_json(value, set())
        except (RecursionError, UnicodeEncodeError):
            valid = False
        if not valid:
            return MirrorCaptureUnavailable("invalid_payload")
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            return MirrorCaptureUnavailable("invalid_payload")
        try:
            used += len(path.encode("utf-8")) + len(payload.encode("utf-8"))
        except UnicodeEncodeError:
            return MirrorCaptureUnavailable("invalid_payload")
        if used > max_bytes:
            return MirrorCaptureUnavailable("budget_exceeded")
        rows.append(MirrorDocumentRecord(path, payload))

    return MirrorObservation(
        instance_nonce=instance_nonce,
        revision=revision,
        root_identity=root_identity,
        cache_identity=cache_identity,
        provider_cursor=provider_cursor,
        provenance=provenance,
        chat_ids=tuple(sorted(normalized_ids)),
        records=tuple(sorted(rows, key=lambda row: row.path)),
    )


def _valid_path(path: Any) -> bool:
    if type(path) is not str or not path or "\x00" in path:
        return False
    if path.startswith("/") or "\\" in path or ":" in path:
        return False
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    return len(path.encode("utf-8")) <= MAX_DOCUMENT_PATH_BYTES


def _valid_json(value: Any, active: set[int]) -> bool:
    kind = type(value)
    if value is None or kind is str or kind is bool:
        return True
    if kind is int:
        return True
    if kind is float:
        return math.isfinite(value)
    if kind is not dict and kind is not list:
        return False
    marker = id(value)
    if marker in active:
        return False
    active.add(marker)
    try:
        if kind is dict:
            return all(type(key) is str and _valid_json(item, active)
                       for key, item in value.items())
        return all(_valid_json(item, active) for item in value)
    finally:
        active.remove(marker)


__all__ = [
    "MAX_MIRROR_INTEGER",
    "MirrorCaptureUnavailable",
    "MirrorDocumentRecord",
    "MirrorObservation",
    "capture_mirror_locked",
    "validate_capture_budget",
    "validate_identity",
]
