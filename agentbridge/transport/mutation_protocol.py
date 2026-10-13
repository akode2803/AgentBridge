"""Strict read-only wire for the inactive provider mutation contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from .scoped_sources import validate_source_cursor

MUTATION_PROTOCOL_SCHEMA_VERSION = 1
MUTATION_PROTOCOL_STATE_VERSIONS = frozenset((1, 2))
DATABASE_SEAL_SCOPE = "database-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _uuid(value: object, name: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError(f"invalid {name}")
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"invalid {name}") from None
    return value


def _root(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("invalid mutation root")
    try:
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("invalid mutation root")
    except UnicodeError:
        raise ValueError("invalid mutation root") from None
    return value


@dataclass(frozen=True)
class ProviderDatabaseSeal:
    seal_id: str
    plan_digest: str
    sealed_from_epoch: str
    protocol_epoch: str
    source_epoch: str
    minimum_cursor: int
    cursor: int
    seal_scope: str

    def __post_init__(self) -> None:
        _uuid(self.seal_id, "database seal id")
        if (type(self.plan_digest) is not str
                or _DIGEST.fullmatch(self.plan_digest) is None):
            raise ValueError("invalid database seal plan digest")
        _uuid(self.sealed_from_epoch, "database seal prior epoch")
        _uuid(self.protocol_epoch, "database seal protocol epoch")
        if self.sealed_from_epoch == self.protocol_epoch:
            raise ValueError("database seal did not rotate protocol epoch")
        _uuid(self.source_epoch, "database seal source epoch")
        minimum = validate_source_cursor(
            self.minimum_cursor, "database seal minimum cursor")
        current = validate_source_cursor(self.cursor, "database seal cursor")
        if minimum > current:
            raise ValueError("database seal minimum exceeds cursor")
        if self.seal_scope != DATABASE_SEAL_SCOPE:
            raise ValueError("unsupported database seal scope")


@dataclass(frozen=True)
class ProviderMutationProtocolState:
    schema_version: int
    account_id: str
    role: str
    root: str
    protocol_epoch: str
    legacy_writes_enabled: bool
    database_seal: ProviderDatabaseSeal | None
    blob_writes_sealed: bool

    def __post_init__(self) -> None:
        if (type(self.schema_version) is not int
                or self.schema_version not in MUTATION_PROTOCOL_STATE_VERSIONS):
            raise ValueError("unsupported mutation protocol contract")
        _uuid(self.account_id, "mutation protocol account")
        if self.role != "authenticated":
            raise ValueError("unsupported mutation protocol role")
        _root(self.root)
        _uuid(self.protocol_epoch, "mutation protocol epoch")
        invalid_database_seal = (
            self.database_seal is not None
            and type(self.database_seal) is not ProviderDatabaseSeal
        )
        if (type(self.legacy_writes_enabled) is not bool
                or invalid_database_seal
                or type(self.blob_writes_sealed) is not bool
                or self.blob_writes_sealed):
            raise ValueError("invalid mutation protocol state")
        seal_epoch_invalid = False
        if self.database_seal is not None:
            matches_current = (
                self.database_seal.protocol_epoch == self.protocol_epoch)
            seal_epoch_invalid = (
                matches_current if self.schema_version == 2
                else not matches_current
            )
        if ((self.database_seal is None) != self.legacy_writes_enabled
                or self.schema_version == 2 and self.legacy_writes_enabled
                or seal_epoch_invalid):
            raise ValueError("inconsistent mutation protocol seal")


__all__ = [
    "DATABASE_SEAL_SCOPE",
    "MUTATION_PROTOCOL_SCHEMA_VERSION",
    "MUTATION_PROTOCOL_STATE_VERSIONS",
    "ProviderDatabaseSeal",
    "ProviderMutationProtocolState",
]
