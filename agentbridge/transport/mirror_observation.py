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
    "changed",
}
_POSITION_STATUSES = {"matched", "changed", "unavailable"}


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


@dataclass(frozen=True)
class MirrorExpectedPosition:
    """Exact identity of one captured process-mirror cut.

    This is concurrency evidence for the originating transport instance.  It
    does not order independent mirrors or authorize reads.
    """

    root_identity: str
    cache_identity: str
    instance_nonce: str
    revision: int

    def __post_init__(self) -> None:
        validated_position_fields(self)

    @classmethod
    def from_observation(cls, observation: MirrorObservation) -> MirrorExpectedPosition:
        if type(observation) is not MirrorObservation:
            raise ValueError("expected a mirror observation")
        return cls(
            observation.root_identity,
            observation.cache_identity,
            observation.instance_nonce,
            observation.revision,
        )


@dataclass(frozen=True)
class MirrorPositionValidation:
    status: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _POSITION_STATUSES:
            raise ValueError("unknown mirror position validation status")
        if self.status == "unavailable":
            if self.reason not in _REASONS:
                raise ValueError("unavailable validation requires a known reason")
        elif self.reason is not None:
            raise ValueError("matched or changed validation cannot have a reason")


@dataclass(frozen=True)
class MirrorSelectionRequest:
    expected: MirrorExpectedPosition | None
    exact_paths: tuple[str, ...]
    complete_prefixes: tuple[str, ...]
    max_records: int
    max_bytes: int
    max_examined_paths: int = 100_000


@dataclass(frozen=True)
class MirrorSelectedPrefix:
    prefix: str
    records: tuple[MirrorDocumentRecord, ...]


@dataclass(frozen=True)
class MirrorSelection:
    position: MirrorExpectedPosition
    provenance: str
    exact_records: tuple[tuple[str, str | None], ...]
    complete_prefixes: tuple[MirrorSelectedPrefix, ...]
    serialized_bytes: int


def validated_selection_request(value: MirrorSelectionRequest) -> MirrorSelectionRequest:
    """Detach and validate a bounded selection request before owner locking."""
    if type(value) is not MirrorSelectionRequest:
        raise ValueError("expected MirrorSelectionRequest")
    expected, paths, prefixes, records, byte_limit, examined = (
        value.expected, value.exact_paths, value.complete_prefixes,
        value.max_records, value.max_bytes, value.max_examined_paths,
    )
    if expected is not None:
        expected = MirrorExpectedPosition(*validated_position_fields(expected))
    if type(paths) is not tuple or type(prefixes) is not tuple:
        raise ValueError("mirror selectors must be exact tuples")
    if len(paths) > 128 or len(prefixes) > 8:
        raise ValueError("too many mirror selectors")
    records = validate_capture_budget(records, "max_records")
    byte_limit = validate_capture_budget(byte_limit, "max_bytes")
    examined = validate_capture_budget(examined, "max_examined_paths")
    if examined > 100_000:
        raise ValueError("max_examined_paths exceeds supported limit")
    copied_paths = tuple(paths)
    copied_prefixes = tuple(prefixes)
    if any(not _valid_path(item) for item in copied_paths) \
            or any(not _valid_prefix(item) for item in copied_prefixes):
        raise ValueError("invalid mirror selector")
    if len(set(copied_paths)) != len(copied_paths) \
            or len(set(copied_prefixes)) != len(copied_prefixes):
        raise ValueError("duplicate mirror selector")
    ordered = sorted(copied_prefixes)
    if any(right.startswith(left) for left, right in zip(ordered, ordered[1:])):
        raise ValueError("overlapping mirror prefixes")
    if any(path.startswith(prefix) for path in copied_paths for prefix in copied_prefixes):
        raise ValueError("exact mirror selector overlaps prefix")
    selectors = copied_paths + copied_prefixes
    selector_bytes = sum(len(item.encode("utf-8")) for item in selectors)
    if selector_bytes > 64 * 1024:
        raise ValueError("mirror selectors exceed byte limit")
    return MirrorSelectionRequest(
        expected, copied_paths, copied_prefixes, records, byte_limit, examined,
    )


def capture_mirror_selection_locked(*, request: MirrorSelectionRequest, warm: bool,
                                    invalid_reason: str | None, instance_nonce: str,
                                    revision: int, root_identity: str | None,
                                    cache_identity: str | None, provenance: str,
                                    documents: dict[str, Any]):
    """Capture a validated selective cut while the owner holds its mutex."""
    if invalid_reason is not None:
        return MirrorCaptureUnavailable(invalid_reason)
    if not warm:
        return MirrorCaptureUnavailable("cold")
    if root_identity is None or cache_identity is None:
        return MirrorCaptureUnavailable("invalid_identity")
    if type(documents) is not dict or type(provenance) is not str \
            or provenance not in {"bootstrap_unverified", "provider_observed"}:
        return MirrorCaptureUnavailable("invalid_payload")
    try:
        position = MirrorExpectedPosition(
            root_identity, cache_identity, instance_nonce, revision,
        )
    except ValueError:
        return MirrorCaptureUnavailable("invalid_identity")
    if request.expected is not None and request.expected != position:
        return MirrorCaptureUnavailable("changed")
    if request.complete_prefixes and len(documents) > request.max_examined_paths:
        return MirrorCaptureUnavailable("budget_exceeded")
    try:
        used = 16 + sum(8 + len(item.encode("utf-8")) for item in (
            root_identity, cache_identity, instance_nonce, provenance,
        ))
        used += sum(8 + len(item.encode("utf-8"))
                    for item in request.exact_paths + request.complete_prefixes)
    except UnicodeEncodeError:
        return MirrorCaptureUnavailable("invalid_identity")
    exact_paths = sorted(request.exact_paths)
    prefix_paths = {prefix: [] for prefix in sorted(request.complete_prefixes)}
    try:
        if len(exact_paths) > request.max_records:
            return MirrorCaptureUnavailable("budget_exceeded")
        used += sum(9 + len(path.encode("utf-8")) for path in exact_paths)
        used += sum(8 + len(prefix.encode("utf-8")) for prefix in prefix_paths)
        if used > request.max_bytes:
            return MirrorCaptureUnavailable("budget_exceeded")
        count = len(exact_paths)
        if prefix_paths:
            for path in documents:
                if type(path) is not str or not _valid_path(path):
                    return MirrorCaptureUnavailable("invalid_payload")
                for prefix, matches in prefix_paths.items():
                    if path.startswith(prefix):
                        matches.append(path)
                        count += 1
                        used += 8 + len(path.encode("utf-8"))
                        if count > request.max_records or used > request.max_bytes:
                            return MirrorCaptureUnavailable("budget_exceeded")
        exact = []
        for path in exact_paths:
            payload = _serialized_document(path, documents[path]) if path in documents else None
            if payload is not None:
                used += len(payload.encode("utf-8"))
                if used > request.max_bytes:
                    return MirrorCaptureUnavailable("budget_exceeded")
            exact.append((path, payload))
        prefixes = []
        for prefix, matches in prefix_paths.items():
            rows = []
            for path in sorted(matches):
                payload = _serialized_document(path, documents[path])
                used += len(payload.encode("utf-8"))
                if used > request.max_bytes:
                    return MirrorCaptureUnavailable("budget_exceeded")
                rows.append(MirrorDocumentRecord(path, payload))
            prefixes.append(MirrorSelectedPrefix(prefix, tuple(rows)))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError, MemoryError):
        return MirrorCaptureUnavailable("invalid_payload")
    return MirrorSelection(position, provenance, tuple(exact), tuple(prefixes), used)


def _serialized_document(path: str, value: Any) -> str:
    if not _valid_path(path) or not _valid_json(value, set()):
        raise ValueError("invalid mirrored document")
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    )


def validate_capture_budget(value: int, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_MIRROR_INTEGER:
        raise ValueError(f"{label} must be a nonnegative bounded integer")
    return value


def validate_identity(value: Any) -> str | None:
    if type(value) is not str or not value or "\x00" in value:
        return None
    if len(value) > MAX_IDENTITY_BYTES:
        return None
    try:
        if len(value.encode("utf-8")) > MAX_IDENTITY_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    return value


def transport_identities(transport):
    """Capture built-in folder Path roots without coercing arbitrary objects.

    Other drivers retain the exact-string identity contract. The same conversion
    is used at cache construction and when checking its pinned inner owner.
    """
    from pathlib import Path
    from .folder import FolderTransport

    root = getattr(transport, 'root', None)
    cache = getattr(transport, 'cache_key', None)
    if type(transport) is FolderTransport and type(root) is type(Path()):
        root = str(root)
    return validate_identity(root), validate_identity(cache)


def validated_position_fields(
    expected: MirrorExpectedPosition,
) -> tuple[str, str, str, int]:
    """Copy and validate token fields before an owner acquires its mutex.

    Frozen dataclasses are not a trust boundary: callers can still replace their
    dictionary fields. Return validated built-ins so subsequent token mutation
    cannot introduce callbacks into the owner's locked comparison.
    """
    if type(expected) is not MirrorExpectedPosition:
        raise ValueError("expected a mirror position")
    root, cache, nonce, revision = (
        expected.root_identity, expected.cache_identity,
        expected.instance_nonce, expected.revision,
    )
    if (validate_identity(root) is None or validate_identity(cache) is None
            or validate_identity(nonce) is None or type(revision) is not int
            or not 0 <= revision <= MAX_MIRROR_INTEGER):
        raise ValueError("invalid expected mirror position")
    return root, cache, nonce, revision


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
    if len(path) > MAX_DOCUMENT_PATH_BYTES:
        return False
    if path.startswith("/") or "\\" in path or ":" in path:
        return False
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    try:
        return len(path.encode("utf-8")) <= MAX_DOCUMENT_PATH_BYTES
    except UnicodeEncodeError:
        return False


def _valid_prefix(prefix: Any) -> bool:
    if type(prefix) is not str or not prefix or "\x00" in prefix \
            or not prefix.endswith("/") or len(prefix) > MAX_DOCUMENT_PATH_BYTES:
        return False
    return _valid_path(prefix[:-1]) \
        and len(prefix.encode("utf-8")) <= MAX_DOCUMENT_PATH_BYTES


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
    "MirrorExpectedPosition",
    "MirrorObservation",
    "MirrorPositionValidation",
    "MirrorSelectedPrefix",
    "MirrorSelection",
    "MirrorSelectionRequest",
    "capture_mirror_locked",
    "capture_mirror_selection_locked",
    "validate_capture_budget",
    "validate_identity",
    "transport_identities",
    "validated_position_fields",
    "validated_selection_request",
]
