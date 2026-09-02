"""Content-free projection input versions and invalidation ownership.

This module defines candidate equality inputs only. It does not authorize cache
serving: a future trusted builder must establish freshness, then every serve
must recheck current membership and key availability.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "ProjectionComponent", "ProjectionInputVersion", "ProjectionScope",
    "ProjectionVersionError", "REQUIRED_COMPONENTS", "component_digest",
    "frontier_digest", "invalidation_scopes", "projection_binding",
]

SCHEMA_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

REQUIRED_COMPONENTS = frozenset({
    "membership", "messages", "edits", "redactions", "reactions",
    "viewer_state", "receipts", "directory", "key_epoch", "key_trust",
    "runtime", "presence", "pause", "replication_frontier", "expiry",
})


class ProjectionVersionError(ValueError):
    """A projection input or distributed frontier is malformed/incomplete."""


class ProjectionScope(str, Enum):
    SUMMARY = "summary"
    MESSAGES = "messages"
    UNREAD = "unread"
    RECEIPTS = "receipts"
    PINS = "pins"
    RUNTIME = "runtime"
    AUTHORITY = "authority"
    PRESENCE = "presence"
    NOTIFICATIONS = "notifications"
    CONTROLS = "controls"
    ALL = "all"


_MUTATION_SCOPES: dict[str, frozenset[ProjectionScope]] = {
    "message_append": frozenset({
        ProjectionScope.MESSAGES, ProjectionScope.SUMMARY,
        ProjectionScope.UNREAD, ProjectionScope.RECEIPTS,
        ProjectionScope.NOTIFICATIONS,
    }),
    "edit": frozenset({
        ProjectionScope.MESSAGES, ProjectionScope.SUMMARY,
        ProjectionScope.UNREAD,
    }),
    "redaction": frozenset({
        ProjectionScope.MESSAGES, ProjectionScope.SUMMARY,
        ProjectionScope.PINS, ProjectionScope.RECEIPTS,
        ProjectionScope.NOTIFICATIONS,
    }),
    "reaction": frozenset({
        ProjectionScope.MESSAGES, ProjectionScope.NOTIFICATIONS,
    }),
    "viewer_visibility": frozenset({
        ProjectionScope.MESSAGES, ProjectionScope.SUMMARY,
        ProjectionScope.UNREAD, ProjectionScope.PINS,
        ProjectionScope.RECEIPTS,
    }),
    "viewer_flags": frozenset({
        ProjectionScope.SUMMARY, ProjectionScope.UNREAD,
        ProjectionScope.NOTIFICATIONS, ProjectionScope.CONTROLS,
    }),
    "receipt_cursor": frozenset({
        ProjectionScope.UNREAD, ProjectionScope.RECEIPTS,
        ProjectionScope.SUMMARY,
    }),
    "membership": frozenset({ProjectionScope.ALL}),
    "key_state": frozenset({ProjectionScope.ALL}),
    "directory": frozenset({
        ProjectionScope.SUMMARY, ProjectionScope.MESSAGES,
        ProjectionScope.AUTHORITY, ProjectionScope.RECEIPTS,
        ProjectionScope.NOTIFICATIONS,
    }),
    "presence": frozenset({
        ProjectionScope.PRESENCE, ProjectionScope.RECEIPTS,
        ProjectionScope.NOTIFICATIONS,
    }),
    "runtime": frozenset({
        ProjectionScope.RUNTIME, ProjectionScope.AUTHORITY,
        ProjectionScope.SUMMARY,
    }),
    "pin": frozenset({ProjectionScope.PINS, ProjectionScope.MESSAGES}),
    "pause": frozenset({ProjectionScope.CONTROLS, ProjectionScope.RUNTIME}),
}


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProjectionVersionError("projection input must be canonical JSON") from exc


def _digest(domain: str, value: Any) -> str:
    return hashlib.sha256(
        b"agentbridge-projection-v1\0" + domain.encode("ascii") + b"\0"
        + _canonical(value)
    ).hexdigest()


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ProjectionVersionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def projection_binding(kind: str, stable_identity: str) -> str:
    """Return an opaque domain-separated viewer or room binding."""
    if kind not in {"viewer", "room"}:
        raise ProjectionVersionError("projection binding kind must be viewer or room")
    if not isinstance(stable_identity, str) or not stable_identity:
        raise ProjectionVersionError("projection binding identity is required")
    return _digest(f"binding:{kind}", stable_identity)


def component_digest(name: str, value: Any) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ProjectionVersionError("invalid projection component name")
    return _digest(f"component:{name}", value)


def frontier_digest(
    contiguous: Mapping[str, int],
    gaps: Mapping[str, Sequence[tuple[int, int]]] | None = None,
) -> str:
    """Digest a canonical per-origin frontier; no scalar implies completeness."""
    gaps = gaps or {}
    if set(gaps) - set(contiguous):
        raise ProjectionVersionError("frontier gaps require a named origin")
    canonical_frontier = []
    for node_id, sequence in sorted(contiguous.items()):
        _require_digest(node_id, "origin node id")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ProjectionVersionError("frontier sequence must be non-negative")
        ranges = []
        prior_end = sequence
        for item in gaps.get(node_id, ()):
            if (not isinstance(item, (tuple, list)) or len(item) != 2
                    or any(isinstance(v, bool) or not isinstance(v, int) for v in item)):
                raise ProjectionVersionError("frontier gap must be an integer range")
            start, end = item
            if start <= prior_end or end < start:
                raise ProjectionVersionError("frontier gaps must be ordered and beyond sequence")
            ranges.append([start, end])
            prior_end = end
        canonical_frontier.append({
            "origin": node_id, "contiguous": sequence, "gaps": ranges,
        })
    return _digest("replication-frontier", canonical_frontier)


@dataclass(frozen=True, order=True)
class ProjectionComponent:
    name: str
    digest: str

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ProjectionVersionError("invalid projection component name")
        _require_digest(self.digest, f"component {self.name}")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "digest": self.digest}


@dataclass(frozen=True)
class ProjectionInputVersion:
    """Structurally complete candidate inputs, never authorization evidence."""

    viewer_binding: str
    room_binding: str
    server_generation: str
    fold_mode: str
    tail_limit: int | None
    components: tuple[ProjectionComponent, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ProjectionVersionError("unsupported projection version schema")
        _require_digest(self.viewer_binding, "viewer binding")
        _require_digest(self.room_binding, "room binding")
        if (not isinstance(self.server_generation, str)
                or not self.server_generation or len(self.server_generation) > 160):
            raise ProjectionVersionError("server generation is required and bounded")
        if self.fold_mode not in {"viewer", "breadcrumbs"}:
            raise ProjectionVersionError("unsupported projection fold mode")
        if (self.tail_limit is not None
                and (isinstance(self.tail_limit, bool)
                     or not isinstance(self.tail_limit, int)
                     or not 1 <= self.tail_limit <= 10_000)):
            raise ProjectionVersionError("projection tail limit is invalid")
        if tuple(sorted(self.components)) != self.components:
            raise ProjectionVersionError("projection components must be canonical")
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ProjectionVersionError("projection component names must be unique")

    @classmethod
    def build(
        cls,
        *,
        viewer_binding: str,
        room_binding: str,
        server_generation: str,
        fold_mode: str,
        tail_limit: int | None,
        components: Mapping[str, str],
    ) -> "ProjectionInputVersion":
        return cls(
            viewer_binding=viewer_binding,
            room_binding=room_binding,
            server_generation=server_generation,
            fold_mode=fold_mode,
            tail_limit=tail_limit,
            components=tuple(sorted(
                ProjectionComponent(name, digest)
                for name, digest in components.items()
            )),
        )

    @property
    def structurally_complete(self) -> bool:
        return REQUIRED_COMPONENTS <= {component.name for component in self.components}

    def require_structurally_complete(self) -> None:
        missing = sorted(
            REQUIRED_COMPONENTS - {component.name for component in self.components})
        if missing:
            raise ProjectionVersionError(
                f"projection inputs are incomplete: {', '.join(missing)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "viewer_binding": self.viewer_binding,
            "room_binding": self.room_binding,
            "server_generation": self.server_generation,
            "fold_mode": self.fold_mode,
            "tail_limit": self.tail_limit,
            "components": [component.to_dict() for component in self.components],
        }

    def candidate_digest(self) -> str:
        """Equality candidate only; callers cannot infer freshness or access."""
        self.require_structurally_complete()
        return _digest("input-version", self.to_dict())


def invalidation_scopes(mutation: str) -> frozenset[ProjectionScope]:
    """Unknown mutation families fail safe by invalidating the whole room."""
    return _MUTATION_SCOPES.get(str(mutation), frozenset({ProjectionScope.ALL}))
