"""Commit-fenced provider observations for the inactive local-node collector.

These values identify raw source work that must be reconciled.  They never
carry or cache membership, visibility, trust, key, or lifecycle authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .change_ledger import (
    MAX_LEDGER_INTEGER,
    MAX_LEDGER_PAGE_SIZE,
    ChangeLedgerEvent,
)

MAX_SOURCE_KEY_BYTES = 4_096


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= MAX_LEDGER_INTEGER:
        raise ValueError(f"invalid {name}")
    return value


def _uuid(value: object) -> str:
    if type(value) is not str:
        raise ValueError("invalid source ledger epoch")
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid source ledger epoch") from exc
    if canonical != value:
        raise ValueError("invalid source ledger epoch")
    return value


def _key(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("invalid source ledger key")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("invalid source ledger key") from None
    if size > MAX_SOURCE_KEY_BYTES:
        raise ValueError("invalid source ledger key")
    return value


@dataclass(frozen=True)
class SourceLedgerFence:
    epoch: str
    minimum_cursor: int
    cursor: int
    schema_version: int

    def __post_init__(self) -> None:
        _uuid(self.epoch)
        minimum = _integer(self.minimum_cursor, "source ledger minimum cursor")
        cursor = _integer(self.cursor, "source ledger cursor")
        if minimum > cursor:
            raise ValueError("source ledger minimum exceeds its cursor")
        _integer(self.schema_version, "source ledger schema version", positive=True)


@dataclass(frozen=True)
class SourceLedgerEvent:
    event_id: int
    stream_kind: str
    stream_id: str
    domain: str
    source_key: str | None = None
    doc_head: int | None = None
    log_head: int | None = None

    def __post_init__(self) -> None:
        ChangeLedgerEvent(
            self.event_id, self.stream_kind, self.stream_id, self.domain,
            self.doc_head, self.log_head,
        )
        if self.domain in ("docs", "logs"):
            # Events written before the exact-identity capability can become
            # visible after the fence (for example after a room rejoin).  An
            # explicit SQL NULL is conservative recovery work for the event's
            # root/chat scope; it must never be acknowledged as an exact key.
            if self.source_key is not None:
                _key(self.source_key)
        elif self.source_key is not None:
            raise ValueError("visibility events cannot carry a source key")

    @property
    def requires_scope_recovery(self) -> bool:
        """Whether the containing root/chat scope must be reconciled."""
        return self.source_key is None


@dataclass(frozen=True)
class SourceLedgerPage:
    after_cursor: int
    events: tuple[SourceLedgerEvent, ...]
    has_more: bool

    def __post_init__(self) -> None:
        after = _integer(self.after_cursor, "source ledger cursor")
        if (type(self.events) is not tuple
                or len(self.events) > MAX_LEDGER_PAGE_SIZE
                or any(type(event) is not SourceLedgerEvent for event in self.events)
                or type(self.has_more) is not bool):
            raise ValueError("invalid source ledger page")
        previous = after
        for event in self.events:
            if event.event_id <= previous:
                raise ValueError("source ledger events are not strictly ordered")
            previous = event.event_id
        if self.has_more and not self.events:
            raise ValueError("empty source ledger page cannot promise more")

    @property
    def cursor(self) -> int:
        return self.events[-1].event_id if self.events else self.after_cursor


__all__ = ["SourceLedgerEvent", "SourceLedgerFence", "SourceLedgerPage"]
