"""Diagnostic collection of content-free projection input candidates.

Coverage is deliberately incomplete and cannot authorize cache admission.
Document snapshots are bounded/local on mirror and folder transports, but the
existing membership resolver may read through, update pins, and retain lifecycle
heads while reconciling newer state events. No cached data is served here.
"""

from __future__ import annotations

import calendar
import copy
import time
from dataclasses import dataclass
from typing import Any

from ..core.errors import NotAMember
from .paths import P
from .projection_version import (
    ProjectionComponent, ProjectionInputVersion, ProjectionVersionError,
    component_digest, frontier_digest, projection_binding,
)

__all__ = [
    "ProjectionInputCollection", "ProjectionInputCollector",
    "ProjectionInputError",
]

_ROOM_DOC_LIMIT = 20_000
_GLOBAL_DOC_LIMIT = 10_000
_MESSAGE_LIMIT = 100_000
_COVERAGE_GAPS = (
    "historical_identity_dependencies", "retained_lifecycle_heads",
    "global_privacy_ownership", "future_skew_activation",
    "decrypted_result_cache",
)


class ProjectionInputError(ProjectionVersionError):
    """Local projection sources are cold, oversized, or changed mid-read."""


@dataclass(frozen=True)
class ProjectionInputCollection:
    version: ProjectionInputVersion
    next_observed_boundary_ns: int | None
    reconciling: bool
    runtime_process_supplied: bool
    replication_frontier_supplied: bool

    def candidate_digest(self) -> str:
        return self.version.candidate_digest()

    @property
    def external_inputs_supplied(self) -> bool:
        """Argument presence only, never proof of trust, freshness or coverage."""
        return self.runtime_process_supplied and self.replication_frontier_supplied

    @property
    def coverage_gaps(self) -> tuple[str, ...]:
        return _COVERAGE_GAPS

    def require_cache_ready(self) -> None:
        raise ProjectionInputError(
            "diagnostic collector cannot authorize cache admission: "
            + ", ".join(self.coverage_gaps))


def _subset(docs: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {path: docs[path] for path in sorted(docs) if path.startswith(prefix)}


def _iso_ns(value: Any) -> int:
    try:
        return int(calendar.timegm(
            time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")) * 1_000_000_000)
    except (TypeError, ValueError):
        return 0


def _collect_deadlines(value: Any, out: list[int]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"expires_ns", "until_ns"}:
                try:
                    deadline = int(item or 0)
                except (TypeError, ValueError):
                    deadline = 0
                if deadline > 0:
                    out.append(deadline)
            _collect_deadlines(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_deadlines(item, out)


def _directory_facts(
    user_docs: dict[str, Any],
    lifecycle_docs: dict[str, Any],
    names: list[str],
    viewer: str,
) -> tuple[dict[str, Any], str]:
    """Sanitize one bounded mirror snapshot without directory read-through."""
    facts: dict[str, Any] = {}
    viewer_sign_pub = ""
    for name in names:
        account = user_docs.get(P.user(name))
        if isinstance(account, dict):
            account = copy.deepcopy(account)
            account.pop("auth", None)
            keys = account.get("keys")
            if isinstance(keys, dict):
                account["keys"] = {
                    key: copy.deepcopy(keys[key])
                    for key in ("sign_pub", "agree_pub", "history")
                    if key in keys
                }
                if name == viewer:
                    viewer_sign_pub = str(keys.get("sign_pub") or "")
        else:
            account = None
        lifecycle_prefix = f"lifecycle/{name}/"
        facts[name] = {
            "account": account,
            # Raw envelopes are conservative invalidators. The security fold
            # still decides which signed lifecycle chain is authoritative.
            "lifecycle": _subset(lifecycle_docs, lifecycle_prefix),
        }
    return facts, viewer_sign_pub


def _presence_facts(
    docs: dict[str, Any], members: list[str],
) -> dict[str, Any]:
    prefixes = tuple(f"presence/{name}@" for name in members)
    return {path: docs[path] for path in sorted(docs) if path.startswith(prefixes)}


def _status_facts(
    docs: dict[str, Any], chat_id: str,
) -> dict[str, Any]:
    """Keep only status records whose projection belongs to this room."""
    facts = {}
    for path in sorted(docs):
        doc = docs[path]
        if not isinstance(doc, dict):
            continue
        runs = doc.get("runs")
        selected = [
            copy.deepcopy(run) for run in runs
            if isinstance(run, dict) and run.get("chat_id") == chat_id
        ] if isinstance(runs, list) else []
        if selected:
            facts[path] = {
                **{key: copy.deepcopy(value) for key, value in doc.items()
                   if key != "runs"},
                "runs": selected,
            }
        elif doc.get("chat_id") == chat_id:
            facts[path] = copy.deepcopy(doc)
    return facts


class ProjectionInputCollector:
    """Collect diagnostic snapshots behind the existing membership gate.

    This is not a trusted freshness builder. In particular, membership
    reconciliation retains its existing directory read and local-write behavior.
    """

    def __init__(
        self,
        mesh,
        *,
        server_generation: str,
        runtime_process_facts: Any = None,
        replication_frontier_digest: str = "",
    ) -> None:
        if not server_generation:
            raise ProjectionInputError("server generation is required")
        self.mesh = mesh
        self.server_generation = server_generation
        self.runtime_process_facts = runtime_process_facts
        self.replication_frontier_digest = replication_frontier_digest

    def _mirror_state(self) -> tuple[bool, bool]:
        status = getattr(self.mesh.tx, "mirror_status", None)
        if not callable(status):
            return True, False  # a local folder transport is authoritative here
        current = status()
        warm = bool(isinstance(current, dict) and current.get("warm"))
        state = str(current.get("state") or "loading") if isinstance(current, dict) \
            else "loading"
        return warm, state != "online"

    def _expiry(
        self,
        chat_id: str,
        room_docs: dict[str, Any],
        presence_docs: dict[str, Any],
        status_docs: dict[str, Any],
        *,
        now_ns: int,
    ) -> tuple[str, int | None]:
        deadlines: list[int] = []
        _collect_deadlines(room_docs, deadlines)
        viewer_state = P.state(chat_id, self.mesh.user)
        for path, doc in room_docs.items():
            if not isinstance(doc, dict):
                continue
            if path == viewer_state:
                mute = doc.get("mute")
                if isinstance(mute, int) and not isinstance(mute, bool) and mute > 0:
                    deadlines.append(mute)
        stale_s = float(getattr(self.mesh.presence, "stale_s", 45.0))
        for doc in presence_docs.values():
            if isinstance(doc, dict):
                seen = int(doc.get("last_seen_ns") or 0)
                if seen:
                    deadlines.append(seen + int(stale_s * 1e9))
        for doc in status_docs.values():
            if not isinstance(doc, dict):
                continue
            updated = _iso_ns(doc.get("updated"))
            if updated:
                deadlines.extend(updated + int(seconds * 1e9)
                                 for seconds in (12, 600, 7200))
            for run in doc.get("runs") or ():
                if isinstance(run, dict):
                    run_updated = _iso_ns(run.get("updated"))
                    if run_updated:
                        deadlines.extend(run_updated + int(seconds * 1e9)
                                         for seconds in (600, 7200))
        future = sorted(deadline for deadline in deadlines if deadline > now_ns)
        passed = sum(deadline <= now_ns for deadline in deadlines)
        return component_digest(
            "expiry", {"deadlines": sorted(deadlines), "passed": passed}), \
            (future[0] if future else None)

    def collect(
        self,
        chat_id: str,
        *,
        fold_mode: str = "viewer",
        tail_limit: int | None = 200,
        now_ns: int | None = None,
    ) -> ProjectionInputCollection:
        warm, reconciling = self._mirror_state()
        if not warm:
            raise ProjectionInputError("projection mirror is not warm")
        try:
            before = self.mesh.messaging._require_member(chat_id)
        except NotAMember:
            raise
        try:
            room_docs = self.mesh.tx.cached_docs_bounded(
                f"chats/{chat_id}/", _ROOM_DOC_LIMIT)
            user_docs = self.mesh.tx.cached_docs_bounded("users/", _GLOBAL_DOC_LIMIT)
            lifecycle_docs = self.mesh.tx.cached_docs_bounded(
                "lifecycle/", _GLOBAL_DOC_LIMIT)
            presence_docs = self.mesh.tx.cached_docs_bounded(
                "presence/", _GLOBAL_DOC_LIMIT)
            status_docs = self.mesh.tx.cached_docs_bounded(
                "status/", _GLOBAL_DOC_LIMIT)
        except OverflowError as exc:
            raise ProjectionInputError("projection source exceeds its read budget") from exc
        message_count = self.mesh.store.message_count(chat_id)
        if message_count > _MESSAGE_LIMIT:
            raise ProjectionInputError("projection message source exceeds its read budget")
        messages = self.mesh.store.messages(chat_id, limit=_MESSAGE_LIMIT + 1)
        if len(messages) > _MESSAGE_LIMIT:
            raise ProjectionInputError("projection message source exceeds its read budget")
        if len(messages) != message_count:
            raise ProjectionInputError("projection messages changed during collection")

        state_prefix = P.state_prefix(chat_id)
        runtime_prefix = f"chats/{chat_id}/runtime/"
        pause_prefix = f"chats/{chat_id}/runtime/member-control/pause"
        key_prefix = f"chats/{chat_id}/keys/"
        members = sorted(before.members)
        directory_names = [self.mesh.user, *(
            name for name in members if name != self.mesh.user)]
        directory, viewer_sign_pub = _directory_facts(
            user_docs, lifecycle_docs, directory_names, self.mesh.user)
        presence_docs = _presence_facts(presence_docs, members)
        status_docs = _status_facts(status_docs, chat_id)
        trust = self.mesh.key_pins.projection_facts(members)
        offsets = self.mesh.store.log_offsets(chat_id)
        origins = {
            component_digest("node", {"writer_log": log_name}): offset
            for log_name, offset in offsets.items()
        }
        if not origins:
            origins[component_digest("node", {
                "transport": str(getattr(
                    self.mesh.tx, "cache_key", getattr(self.mesh.tx, "scheme", "local"))),
                "machine": self.mesh.machine,
            })] = 0
        log_frontier = frontier_digest(origins)
        doc_cursor = self.mesh.store.cached_doc("sync/log_cursor", default={})
        now = int(time.time_ns() if now_ns is None else now_ns)
        expiry, valid_until = self._expiry(
            chat_id, room_docs, presence_docs, status_docs, now_ns=now)
        key_docs = _subset(room_docs, key_prefix)
        key_availability = self.mesh.keys.projection_facts(chat_id, key_docs)
        supplied_frontier = self.replication_frontier_digest
        if supplied_frontier:
            ProjectionComponent("replication_frontier", supplied_frontier)
            frontier_input = component_digest(
                "replication_frontier", {
                    "kind": "distributed", "frontier": supplied_frontier,
                })
            frontier_complete = True
        else:
            frontier_input = component_digest(
                "replication_frontier", {
                    "kind": "local_writer_offsets",
                    "logs": log_frontier, "feed_cursor": doc_cursor,
                })
            frontier_complete = False
        process_complete = self.runtime_process_facts is not None

        components = {
            "membership": component_digest("membership", before.to_dict()),
            "messages": component_digest("messages", messages),
            "edits": component_digest(
                "edits", _subset(room_docs, P.edits_prefix(chat_id))),
            "redactions": component_digest(
                "redactions", _subset(room_docs, P.redactions_prefix(chat_id))),
            "reactions": component_digest(
                "reactions", _subset(room_docs, P.reactions_prefix(chat_id))),
            "pins": component_digest(
                "pins", _subset(room_docs, P.pins_prefix(chat_id))),
            "viewer_state": component_digest(
                "viewer_state", room_docs.get(P.state(chat_id, self.mesh.user))),
            "receipts": component_digest(
                "receipts", _subset(room_docs, state_prefix)),
            "directory": component_digest("directory", directory),
            "key_epoch": component_digest(
                "key_epoch", {
                    "docs": key_docs, "available": key_availability,
                }),
            "key_trust": component_digest("key_trust", trust),
            "runtime": component_digest("runtime", {
                "room": _subset(room_docs, runtime_prefix),
                "status": status_docs,
                "process": (self.runtime_process_facts
                            if process_complete else {"unavailable": True}),
            }),
            "presence": component_digest("presence", presence_docs),
            "pause": component_digest(
                "pause", _subset(room_docs, pause_prefix)),
            "replication_frontier": frontier_input,
            "expiry": expiry,
        }
        after = self.mesh.messaging._require_member(chat_id)
        if before.to_dict() != after.to_dict():
            raise ProjectionInputError("membership changed during input collection")
        version = ProjectionInputVersion.build(
            viewer_binding=projection_binding(
                "viewer", f"{self.mesh.user}\0{viewer_sign_pub}"),
            room_binding=projection_binding(
                "room", f"{getattr(self.mesh.tx, 'cache_key', self.mesh.tx.scheme)}\0{chat_id}"),
            server_generation=self.server_generation,
            fold_mode=fold_mode,
            tail_limit=tail_limit,
            components=components,
        )
        version.require_structurally_complete()
        return ProjectionInputCollection(
            version=version, next_observed_boundary_ns=valid_until,
            reconciling=reconciling,
            runtime_process_supplied=process_complete,
            replication_frontier_supplied=frontier_complete,
        )
