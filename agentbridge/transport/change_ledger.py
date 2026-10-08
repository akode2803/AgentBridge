"""Transport-neutral contracts for durable Realtime change replay.

Ledger positions are delivery evidence only.  They never cache or establish
membership, trust, keys, lifecycle state, or visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

MAX_LEDGER_INTEGER = 2**63 - 1
MAX_LEDGER_PAGE_SIZE = 1_000
MAX_STREAM_ID_BYTES = 1_024
_STREAM_KINDS = {"root", "chat"}
_DOMAINS = {"docs", "logs", "visibility"}


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    minimum = 1 if positive else 0
    if value < minimum or value > MAX_LEDGER_INTEGER:
        raise ValueError(f"{name} is outside the supported range")
    return value


@dataclass(frozen=True)
class ChangeLedgerCapability:
    schema_version: int
    max_page_size: int = MAX_LEDGER_PAGE_SIZE

    def __post_init__(self) -> None:
        _integer(self.schema_version, "schema_version", positive=True)
        size = _integer(self.max_page_size, "max_page_size", positive=True)
        if size > MAX_LEDGER_PAGE_SIZE:
            raise ValueError("max_page_size exceeds the client safety limit")


@dataclass(frozen=True)
class ChangeLedgerEpoch:
    epoch: str
    minimum_cursor: int
    schema_version: int

    def __post_init__(self) -> None:
        if type(self.epoch) is not str:
            raise ValueError("epoch must be a canonical UUID")
        try:
            canonical = str(UUID(self.epoch))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("epoch must be a canonical UUID") from exc
        if canonical != self.epoch:
            raise ValueError("epoch must be a canonical UUID")
        _integer(self.minimum_cursor, "minimum_cursor")
        _integer(self.schema_version, "schema_version", positive=True)


@dataclass(frozen=True)
class ChangeLedgerEvent:
    event_id: int
    stream_kind: str
    stream_id: str
    domain: str
    doc_head: int | None = None
    log_head: int | None = None

    def __post_init__(self) -> None:
        _integer(self.event_id, "event_id", positive=True)
        if type(self.stream_kind) is not str or self.stream_kind not in _STREAM_KINDS:
            raise ValueError("unknown ledger stream kind")
        if type(self.stream_id) is not str \
                or len(self.stream_id.encode("utf-8")) > MAX_STREAM_ID_BYTES:
            raise ValueError("invalid ledger stream identity")
        if self.stream_kind == "root" and self.stream_id:
            raise ValueError("root ledger events cannot name a chat")
        if self.stream_kind == "chat" and not self.stream_id:
            raise ValueError("chat ledger events require a stream identity")
        if type(self.domain) is not str or self.domain not in _DOMAINS:
            raise ValueError("unknown ledger event domain")
        if self.doc_head is not None:
            _integer(self.doc_head, "doc_head")
        if self.log_head is not None:
            _integer(self.log_head, "log_head")
        if self.domain == "docs":
            if self.doc_head is None or self.log_head is not None:
                raise ValueError("document events require only a document head")
        elif self.domain == "logs":
            if self.log_head is None or self.doc_head is not None \
                    or self.stream_kind != "chat":
                raise ValueError("log events require only a chat log head")
        elif self.doc_head is not None or self.log_head is not None:
            raise ValueError("visibility events cannot carry data heads")


@dataclass(frozen=True)
class ChangeLedgerPage:
    after_cursor: int
    events: tuple[ChangeLedgerEvent, ...]
    has_more: bool

    def __post_init__(self) -> None:
        _integer(self.after_cursor, "after_cursor")
        if type(self.events) is not tuple \
                or any(type(event) is not ChangeLedgerEvent for event in self.events):
            raise ValueError("ledger events must be an immutable tuple")
        if len(self.events) > MAX_LEDGER_PAGE_SIZE:
            raise ValueError("ledger page exceeds the client safety limit")
        if type(self.has_more) is not bool:
            raise ValueError("has_more must be a boolean")
        previous = self.after_cursor
        for event in self.events:
            if event.event_id <= previous:
                raise ValueError("ledger events must be strictly ordered after the cursor")
            previous = event.event_id
        if self.has_more and not self.events:
            raise ValueError("an empty ledger page cannot promise more rows")

    @property
    def cursor(self) -> int:
        return self.events[-1].event_id if self.events else self.after_cursor
