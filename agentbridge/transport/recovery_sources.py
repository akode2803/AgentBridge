"""Detached, bounded provider observations for inactive whole-root recovery.

Provider rows and cuts are work evidence only; canonical authority is recomputed
from current membership and overlays when the consuming node eventually exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .scoped_sources import (
    MAX_SOURCE_KEY_BYTES,
    MAX_SOURCE_STREAM_BYTES,
    ScopedDocumentRow,
    validate_source_cursor,
    validate_source_key,
    validate_source_stream,
)
from .source_ledger import SourceLedgerEvent

RECOVERY_SCHEMA_VERSION = 1
RECOVERY_INDEX_CONTRACT = "root-manifest-v1"
RECOVERY_SOURCE_SCHEMA_VERSION = 2
MAX_RECOVERY_PAGE_ROWS = 256
RECOVERY_RESPONSE_OVERHEAD = 1024


def _text(value: object, name: str, maximum: int) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"invalid {name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"invalid {name}") from None
    if len(encoded) > maximum:
        raise ValueError(f"invalid {name}")
    return value


def validate_recovery_after(value: object, family: str) -> str | tuple[str, str] | int:
    if family == "events":
        return validate_source_cursor(value, "recovery event cursor")
    if family == "streams":
        if type(value) is not tuple or len(value) != 2:
            raise ValueError("invalid recovery stream keyset")
        chat, log = value
        if chat == log == "":
            return ("", "")
        return (validate_source_stream(chat, "recovery chat"),
                validate_source_key(log, "recovery log"))
    if family in ("documents", "chats"):
        if value == "" and type(value) is str:
            return value
        return (_text(value, "recovery document key", MAX_SOURCE_KEY_BYTES)
                if family == "documents" else
                _text(value, "recovery chat key", MAX_SOURCE_STREAM_BYTES))
    raise ValueError("invalid recovery family")


@dataclass(frozen=True)
class RecoveryCut:
    schema_version: int
    index_contract: str
    source_schema_version: int
    source_epoch: str
    account_id: str
    role: str
    minimum_cursor: int
    cursor: int

    def __post_init__(self) -> None:
        if (type(self.schema_version) is not int
                or self.schema_version != RECOVERY_SCHEMA_VERSION
                or type(self.index_contract) is not str
                or self.index_contract != RECOVERY_INDEX_CONTRACT
                or type(self.source_schema_version) is not int
                or self.source_schema_version != RECOVERY_SOURCE_SCHEMA_VERSION):
            raise ValueError("unsupported recovery source contract")
        try:
            if type(self.source_epoch) is not str or str(UUID(self.source_epoch)) != self.source_epoch:
                raise ValueError("invalid recovery source epoch")
        except (TypeError, AttributeError, ValueError):
            raise ValueError("invalid recovery source epoch") from None
        try:
            if (type(self.account_id) is not str
                    or str(UUID(self.account_id)) != self.account_id):
                raise ValueError("invalid recovery account id")
        except (TypeError, AttributeError, ValueError):
            raise ValueError("invalid recovery account id") from None
        if self.role != "authenticated":
            raise ValueError("unsupported recovery role")
        minimum = validate_source_cursor(self.minimum_cursor, "recovery minimum")
        current = validate_source_cursor(self.cursor, "recovery cursor")
        if minimum > current:
            raise ValueError("recovery minimum exceeds cursor")


@dataclass(frozen=True)
class RecoveryChat:
    chat_id: str

    def __post_init__(self) -> None:
        validate_source_stream(self.chat_id, "recovery chat")


@dataclass(frozen=True)
class RecoveryStream:
    chat_id: str
    log_name: str
    head: int

    def __post_init__(self) -> None:
        validate_source_stream(self.chat_id, "recovery chat")
        validate_source_key(self.log_name, "recovery log")
        if validate_source_cursor(self.head, "recovery log head") < 1:
            raise ValueError("invalid recovery log head")


@dataclass(frozen=True)
class RecoveryPage:
    family: str
    after: str | tuple[str, str] | int
    cut: RecoveryCut
    rows: tuple[ScopedDocumentRow | RecoveryChat | RecoveryStream | SourceLedgerEvent, ...]
    has_more: bool

    def __post_init__(self) -> None:
        after = validate_recovery_after(self.after, self.family)
        if (type(self.cut) is not RecoveryCut or type(self.rows) is not tuple
                or len(self.rows) > MAX_RECOVERY_PAGE_ROWS
                or type(self.has_more) is not bool
                or self.has_more and not self.rows):
            raise ValueError("invalid recovery page")
        if (self.family == "events"
                and not self.cut.minimum_cursor <= after <= self.cut.cursor):
            raise ValueError("recovery event cursor is outside its cut")
        kinds = {"documents": ScopedDocumentRow, "chats": RecoveryChat,
                 "streams": RecoveryStream, "events": SourceLedgerEvent}
        kind = kinds[self.family]
        if any(type(row) is not kind for row in self.rows):
            raise ValueError("invalid recovery page row")
        keys = tuple((row.path if self.family == "documents" else
                      row.chat_id if self.family == "chats" else
                      (row.chat_id, row.log_name) if self.family == "streams" else
                      row.event_id) for row in self.rows)
        if (keys != tuple(sorted(set(keys)))
                or any(key <= after for key in keys)
                or self.family == "events" and any(
                    key > self.cut.cursor for key in keys)):
            raise ValueError("recovery page is outside its keyset")

    @property
    def terminal(self) -> bool:
        return not self.has_more

    @property
    def cursor(self) -> str | tuple[str, str] | int:
        if not self.rows:
            return self.after
        row = self.rows[-1]
        return (row.path if self.family == "documents" else
                row.chat_id if self.family == "chats" else
                (row.chat_id, row.log_name) if self.family == "streams" else
                row.event_id)


__all__ = [
    "MAX_RECOVERY_PAGE_ROWS",
    "RECOVERY_INDEX_CONTRACT",
    "RECOVERY_SCHEMA_VERSION",
    "RECOVERY_SOURCE_SCHEMA_VERSION",
    "RECOVERY_RESPONSE_OVERHEAD",
    "RecoveryChat",
    "RecoveryCut",
    "RecoveryPage",
    "RecoveryStream",
    "validate_recovery_after",
]
