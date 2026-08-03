"""Authenticated, pairwise-encrypted AppLink request/reply control lane."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

from ..core.timekit import new_id, next_ns
from ..core.jsonkit import canonical_json_bytes
from ..mesh.pairwise import open_pairwise, seal_pairwise
from ..store.db import Store
from ..transport.base import Transport
from .machines import MachineRegistry

__all__ = ["ControlError", "ControlMessage", "ControlLane"]

_SEEN_SCOPE = "applink_control_seen"
_DEFAULT_TTL_S = 7 * 24 * 3600.0
_FUTURE_SKEW_NS = int(5 * 60 * 1e9)
_HEADER_FIELDS = {
    "v", "kind", "id", "ns", "sender", "recipient", "from_machine",
    "to_machine", "expires_ns", "reply_to", "request_digest",
}


class ControlError(ValueError):
    """An AppLink control message is malformed, forged, stale, or misrouted."""


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ControlMessage:
    id: str
    kind: str
    from_machine: str
    from_user: str
    to_machine: str
    to_user: str
    payload: dict
    ns: int
    expires_ns: int
    reply_to: str = ""
    request_digest: str = ""
    envelope_digest: str = ""


class ControlLane:
    def __init__(self, tx: Transport, store: Store, machine: str, *, user: str,
                 directory, keystore, registry: MachineRegistry) -> None:
        self.tx = tx
        self.store = store
        self.machine = machine
        self.user = user
        self.directory = directory
        self.keystore = keystore
        self.registry = registry
        self._mesh = SimpleNamespace(directory=directory, keystore=keystore)
        self._handlers: dict[str, Callable[[ControlMessage], dict | None]] = {}

    @staticmethod
    def _inbox(machine: str, user: str) -> str:
        return f"runtime/applink/{machine}/{user}"

    def send(self, to_machine: str, kind: str, payload: dict, *, to_user: str,
             reply_to: str = "", request_digest: str = "",
             ttl_s: float = _DEFAULT_TTL_S) -> str:
        if not self.registry.has_identity(self.machine, self.user):
            raise ControlError("sender has no active signed machine announcement")
        if not self.registry.has_identity(to_machine, to_user):
            raise ControlError("recipient has no active signed machine announcement")
        ns = next_ns()
        msg_id = new_id("ctl", ns)
        header = {
            "v": 1, "kind": str(kind), "id": msg_id, "ns": ns,
            "sender": self.user, "recipient": to_user,
            "from_machine": self.machine, "to_machine": to_machine,
            "expires_ns": ns + max(1, int(ttl_s * 1e9)),
            "reply_to": str(reply_to), "request_digest": str(request_digest),
        }
        if not header["kind"] or (reply_to and len(request_digest) != 64):
            raise ControlError("invalid control reply binding")
        env = seal_pairwise(
            self._mesh, header=header, sender=self.user,
            recipient=to_user, payload=payload,
        )
        self.tx.create_doc(
            f"{self._inbox(to_machine, to_user)}/{msg_id}.json", env,
        )
        if not reply_to:
            self.store.cache_doc(f"applink/outbound/{msg_id}", {
                "digest": _digest(env), "kind": header["kind"],
                "recipient": to_user, "to_machine": to_machine,
            })
        return msg_id

    def register(self, kind: str,
                 handler: Callable[[ControlMessage], dict | None]) -> None:
        self._handlers[kind] = handler

    def _seen(self, msg_id: str) -> bool:
        return self.store.get_cursor(_SEEN_SCOPE, msg_id) != 0

    def _mark_seen(self, msg_id: str) -> None:
        self.store.set_cursor(_SEEN_SCOPE, msg_id, next_ns())

    def _open(self, path: str, raw: object) -> ControlMessage:
        if not isinstance(raw, dict) or not isinstance(raw.get("header"), dict):
            raise ControlError("invalid control envelope")
        header = raw["header"]
        sender = str(header.get("sender") or "")
        expected = {
            "v": 1, "recipient": self.user, "to_machine": self.machine,
        }
        opened = open_pairwise(
            self._mesh, raw, header_fields=_HEADER_FIELDS, expected=expected,
            sender=sender, recipient=self.user, viewer=self.user,
        )
        h = opened.header
        expected_path = f"{self._inbox(self.machine, self.user)}/{h['id']}.json"
        if path != expected_path or h["from_machine"] == "":
            raise ControlError("control path or sender machine mismatch")
        for name in ("kind", "reply_to", "request_digest"):
            if not isinstance(h[name], str):
                raise ControlError(f"invalid control {name}")
        if not h["kind"] or h["ns"] > time.time_ns() + _FUTURE_SKEW_NS \
                or time.time_ns() >= h["expires_ns"]:
            raise ControlError("control message is expired or future-dated")
        acc = self.directory.get(sender)
        if not acc or not acc.active \
                or not self.registry.has_identity(h["from_machine"], sender):
            raise ControlError("control sender is not active on that machine")
        if h["reply_to"]:
            sent = self.store.cached_doc(
                f"applink/outbound/{h['reply_to']}", default={},
            )
            if (not isinstance(sent, dict) or sent.get("digest") != h["request_digest"]
                    or sent.get("kind") != h["kind"]
                    or sent.get("recipient") != sender
                    or sent.get("to_machine") != h["from_machine"]):
                raise ControlError("control reply does not bind an outbound request")
        elif h["request_digest"]:
            raise ControlError("control request cannot carry a reply digest")
        return ControlMessage(
            id=h["id"], kind=h["kind"], from_machine=h["from_machine"],
            from_user=sender, to_machine=self.machine, to_user=self.user,
            payload=opened.payload, ns=h["ns"], expires_ns=h["expires_ns"],
            reply_to=h["reply_to"], request_digest=h["request_digest"],
            envelope_digest=_digest(raw),
        )

    def inbox(self) -> list[ControlMessage]:
        out = []
        for path in self.tx.list_docs(self._inbox(self.machine, self.user)):
            try:
                out.append(self._open(path, self.tx.get_doc(path)))
            except (ControlError, OSError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda m: (m.ns, m.id))

    def poll(self) -> list[ControlMessage]:
        handled = []
        for msg in self.inbox():
            if self._seen(msg.id):
                continue
            handler = self._handlers.get(msg.kind)
            if handler is None:
                self._mark_seen(msg.id)
                continue
            try:
                reply = handler(msg)
                if reply is not None and not msg.reply_to:
                    self.send(
                        msg.from_machine, msg.kind, reply, to_user=msg.from_user,
                        reply_to=msg.id, request_digest=msg.envelope_digest,
                        ttl_s=max(1.0, (msg.expires_ns - time.time_ns()) / 1e9),
                    )
            except Exception:  # a failed handler remains unseen for retry
                continue
            self._mark_seen(msg.id)
            handled.append(msg)
        return handled

    def gc(self, ttl_s: float = _DEFAULT_TTL_S) -> int:
        floor = time.time_ns() - int(ttl_s * 1e9)
        removed = 0
        prefix = self._inbox(self.machine, self.user)
        for path in self.tx.list_docs(prefix):
            raw = self.tx.get_doc(path)
            header = raw.get("header") if isinstance(raw, dict) else None
            if (isinstance(header, dict) and header.get("recipient") == self.user
                    and header.get("to_machine") == self.machine
                    and isinstance(header.get("ns"), int) and header["ns"] < floor):
                self.tx.delete_doc(path)
                removed += 1
        return removed
