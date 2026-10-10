"""Bounded exact-source reads for the inactive local-node collector.

The provider applies current authority on every call.  These values carry raw
delivery inputs only; a missing row, cursor or payload never grants or revokes
membership, trust, keys, lifecycle state or visibility.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import TransportError
from .change_ledger import MAX_LEDGER_INTEGER, MAX_LEDGER_PAGE_SIZE

MAX_SOURCE_BATCH_PATHS = 128
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_KEY_BYTES = 4_096
MAX_SOURCE_STREAM_BYTES = 1_024
SOURCE_ROW_WIRE_OVERHEAD = 256


class ScopedSourceOverflow(TransportError):
    """A valid exact-source result does not fit in one provider response."""


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= MAX_LEDGER_INTEGER:
        raise ValueError(f"invalid {name}")
    return value


def _text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"invalid {name}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"invalid {name}") from None
    if size > maximum:
        raise ValueError(f"invalid {name}")
    return value


def validate_source_budget(value: object) -> int:
    budget = _integer(value, "source byte budget", positive=True)
    if budget > MAX_SOURCE_BYTES:
        raise ValueError("source byte budget exceeds supported maximum")
    return budget


def validate_source_cursor(value: object, name: str) -> int:
    return _integer(value, name)


def validate_source_paths(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not 1 <= len(value) <= MAX_SOURCE_BATCH_PATHS:
        raise ValueError("invalid source document path batch")
    paths = tuple(_text(path, "source document path",
                        maximum=MAX_SOURCE_KEY_BYTES) for path in value)
    if len(set(paths)) != len(paths):
        raise ValueError("source document paths must be unique")
    for path in paths:
        parts = path.split("/")
        if (path.startswith("/") or path.endswith("/") or "\\" in path
                or len(parts) > 32
                or any(not part or part in (".", "..") for part in parts)):
            raise ValueError("invalid source document path")
    return paths


@dataclass(frozen=True)
class ScopedDocumentRow:
    path: str
    seq: int
    deleted: bool
    payload: bytes | None

    def __post_init__(self) -> None:
        validate_source_paths((self.path,))
        _integer(self.seq, "source document sequence", positive=True)
        if type(self.deleted) is not bool or self.deleted != (self.payload is None):
            raise ValueError("invalid source document deletion state")
        if self.payload is not None and type(self.payload) is not bytes:
            raise ValueError("invalid source document payload")


@dataclass(frozen=True)
class ScopedDocumentBatch:
    requested_paths: tuple[str, ...]
    rows: tuple[ScopedDocumentRow, ...]

    def __post_init__(self) -> None:
        requested = validate_source_paths(self.requested_paths)
        if (type(self.rows) is not tuple
                or any(type(row) is not ScopedDocumentRow for row in self.rows)):
            raise ValueError("invalid source document batch")
        positions = {path: index for index, path in enumerate(requested)}
        paths = tuple(row.path for row in self.rows)
        if (len(set(paths)) != len(paths)
                or any(path not in positions for path in paths)
                or tuple(positions[path] for path in paths)
                != tuple(sorted(positions[path] for path in paths))):
            raise ValueError("source document rows do not match request order")

    @property
    def observed_paths(self) -> frozenset[str]:
        return frozenset(row.path for row in self.rows)


@dataclass(frozen=True)
class ScopedLogRow:
    row_id: int
    payload: bytes

    def __post_init__(self) -> None:
        _integer(self.row_id, "source log row", positive=True)
        if type(self.payload) is not bytes:
            raise ValueError("invalid source log payload")


@dataclass(frozen=True)
class ScopedLogPage:
    after_cursor: int
    through_cursor: int
    rows: tuple[ScopedLogRow, ...]
    has_more: bool

    def __post_init__(self) -> None:
        after = _integer(self.after_cursor, "source log cursor")
        through = _integer(self.through_cursor, "source log cut")
        if after > through:
            raise ValueError("source log cursor exceeds its cut")
        if (type(self.rows) is not tuple
                or len(self.rows) > MAX_LEDGER_PAGE_SIZE
                or any(type(row) is not ScopedLogRow for row in self.rows)
                or type(self.has_more) is not bool):
            raise ValueError("invalid source log page")
        ids = tuple(row.row_id for row in self.rows)
        if (ids != tuple(sorted(ids)) or len(set(ids)) != len(ids)
                or any(row_id <= after or row_id > through for row_id in ids)):
            raise ValueError("source log rows are outside their bounded keyset")
        if self.has_more and not self.rows:
            raise ValueError("empty source log page cannot promise more")

    @property
    def cursor(self) -> int:
        return self.rows[-1].row_id if self.rows else self.after_cursor


def validate_source_stream(value: object, name: str) -> str:
    return _text(value, name, maximum=MAX_SOURCE_STREAM_BYTES)


def validate_source_key(value: object, name: str) -> str:
    return _text(value, name, maximum=MAX_SOURCE_KEY_BYTES)


__all__ = [
    "MAX_SOURCE_BATCH_PATHS", "MAX_SOURCE_BYTES", "ScopedDocumentBatch",
    "ScopedDocumentRow", "ScopedLogPage", "ScopedLogRow", "ScopedSourceOverflow",
    "SOURCE_ROW_WIRE_OVERHEAD",
    "validate_source_budget", "validate_source_cursor", "validate_source_key",
    "validate_source_paths", "validate_source_stream",
]
