"""Versioned and bounded wire values for the inactive local node."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..core.config import validate_root_spec

PROTOCOL_VERSION = 1
MAX_ENDPOINT_BYTES = 2048
MAX_IDENTITY_PART_BYTES = 1024
MAX_STATUS_BYTES = 16 * 1024
HEALTH_VALUES = frozenset({
    "inactive", "starting", "catching_up", "ready", "degraded",
    "pending", "unavailable",
})


class ProtocolError(ValueError):
    """A local request or response does not match the bounded protocol."""


def _identity_part(value: Any, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ProtocolError(f"invalid {name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ProtocolError(f"invalid {name}") from None
    if len(encoded) > MAX_IDENTITY_PART_BYTES or any(
            (char.isspace() and char != " ")
            or unicodedata.category(char).startswith("C") for char in value):
        raise ProtocolError(f"invalid {name}")
    return value


def canonical_endpoint(value: Any) -> str:
    """Return a credential-free canonical HTTP(S) provider endpoint."""
    if type(value) is not str or not value or value.strip() != value:
        raise ProtocolError("invalid provider endpoint")
    try:
        if len(value.encode("utf-8")) > MAX_ENDPOINT_BYTES:
            raise ProtocolError("invalid provider endpoint")
    except UnicodeEncodeError:
        raise ProtocolError("invalid provider endpoint") from None
    if any(char.isspace() or unicodedata.category(char).startswith("C")
           for char in value):
        raise ProtocolError("invalid provider endpoint")
    parts = urlsplit(value)
    if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ProtocolError("invalid provider endpoint")
    try:
        port = parts.port
    except ValueError:
        raise ProtocolError("invalid provider endpoint") from None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = (parts.scheme.lower() == "https" and port == 443) or (
        parts.scheme.lower() == "http" and port == 80)
    netloc = host if port is None or default else f"{host}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


@dataclass(frozen=True)
class ReplicaIdentity:
    """Identity of one provider observation boundary, never an authority proof."""

    provider_endpoint: str
    root: str
    principal: str
    machine: str

    def __post_init__(self) -> None:
        try:
            root = validate_root_spec(self.root)
        except Exception:
            raise ProtocolError("invalid root") from None
        object.__setattr__(self, "provider_endpoint", canonical_endpoint(
            self.provider_endpoint))
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "principal", _identity_part(
            self.principal, "principal"))
        object.__setattr__(self, "machine", _identity_part(
            self.machine, "machine"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_endpoint": self.provider_endpoint,
            "root": self.root,
            "principal": self.principal,
            "machine": self.machine,
        }

    @property
    def digest(self) -> str:
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NodeStatus:
    protocol_version: int
    node_epoch: str
    database_incarnation: str
    identity_digest: str
    admitted_generation: int
    health: str
    started_ns: int
    last_attempt_ns: int
    last_success_ns: int
    pending_mutations: int

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def parse_status(value: Any, *, expected_identity: ReplicaIdentity,
                 expected_incarnation: str | None = None) -> NodeStatus:
    """Strictly validate a status response against the caller's binding."""
    if type(value) is not dict or set(value) != set(NodeStatus.__dataclass_fields__):
        raise ProtocolError("invalid node status")
    integer_fields = (
        "protocol_version", "admitted_generation", "started_ns",
        "last_attempt_ns", "last_success_ns", "pending_mutations",
    )
    if any(type(value[name]) is not int or value[name] < 0
           for name in integer_fields):
        raise ProtocolError("invalid node status")
    for name in ("node_epoch", "database_incarnation", "identity_digest", "health"):
        if type(value[name]) is not str or not value[name] or len(value[name]) > 128:
            raise ProtocolError("invalid node status")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported node protocol")
    if value["health"] not in HEALTH_VALUES:
        raise ProtocolError("invalid node status")
    if value["identity_digest"] != expected_identity.digest:
        raise ProtocolError("wrong node identity")
    if (expected_incarnation is not None
            and value["database_incarnation"] != expected_incarnation):
        raise ProtocolError("wrong database incarnation")
    return NodeStatus(**value)


def encode_json(value: dict[str, Any], *, max_bytes: int = MAX_STATUS_BYTES) -> bytes:
    if type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024:
        raise ProtocolError("invalid node response budget")
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    if len(raw) > max_bytes:
        raise ProtocolError("node response exceeds budget")
    return raw
