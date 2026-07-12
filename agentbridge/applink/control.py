"""Control lane (R11) — machine-to-machine request/reply RPC over the same
transport. General substrate; setup-assist is its first consumer.

Wire model:
  control/<recipient_machine>/<msg_id>.json   one immutable doc per message
Each machine polls its OWN directory, skips ids it has already handled (a
local cursor set in the Store), and dispatches to a handler keyed by ``kind``.
Replies are just messages sent back to the origin machine with ``reply_to``
set — so a reply lands in the requester's inbox and its poll picks it up.

Messages are operational metadata (config proposals, version pings), never
chat content — plaintext by design, and the receiving side ALWAYS reviews a
proposal before acting on it (see setup_assist). A best-effort ``gc`` drops
expired docs; delivery is at-least-once, so handlers must be idempotent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..core.timekit import new_id, next_ns, utcnow_iso
from ..store.db import Store
from ..transport.base import Transport

__all__ = ["ControlMessage", "ControlLane"]

_SEEN_SCOPE = "control_seen"
_DEFAULT_TTL_S = 7 * 24 * 3600.0


@dataclass
class ControlMessage:
    id: str
    kind: str
    from_machine: str
    from_user: str
    to_machine: str
    payload: dict
    ns: int
    reply_to: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ControlMessage":
        return cls(
            id=d.get("id", ""), kind=d.get("kind", ""),
            from_machine=d.get("from_machine", ""), from_user=d.get("from_user", ""),
            to_machine=d.get("to_machine", ""), payload=d.get("payload") or {},
            ns=int(d.get("ns", 0)), reply_to=d.get("reply_to", ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "from_machine": self.from_machine,
            "from_user": self.from_user, "to_machine": self.to_machine,
            "payload": self.payload, "ns": self.ns, "reply_to": self.reply_to,
            "at": utcnow_iso(),
        }


class ControlLane:
    def __init__(self, tx: Transport, store: Store, machine: str, user: str = "") -> None:
        self.tx = tx
        self.store = store
        self.machine = machine
        self.user = user
        self._handlers: dict[str, Callable[[ControlMessage], dict | None]] = {}

    def _inbox(self, machine: str) -> str:
        return f"control/{machine}"

    # -------------------------------------------------------------- sending
    def send(self, to_machine: str, kind: str, payload: dict, *, reply_to: str = "") -> str:
        ns = next_ns()
        msg = ControlMessage(
            id=new_id("ctl", ns), kind=kind, from_machine=self.machine,
            from_user=self.user, to_machine=to_machine, payload=payload,
            ns=ns, reply_to=reply_to,
        )
        self.tx.put_doc(f"{self._inbox(to_machine)}/{msg.id}.json", msg.to_dict())
        return msg.id

    # ------------------------------------------------------------ receiving
    def register(self, kind: str, handler: Callable[[ControlMessage], dict | None]) -> None:
        """A handler may return a dict to auto-reply, or None for no reply."""
        self._handlers[kind] = handler

    def _seen(self, msg_id: str) -> bool:
        return self.store.get_cursor(_SEEN_SCOPE, msg_id) != 0

    def _mark_seen(self, msg_id: str) -> None:
        self.store.set_cursor(_SEEN_SCOPE, msg_id, next_ns())

    def inbox(self) -> list[ControlMessage]:
        out = []
        for path in self.tx.list_docs(self._inbox(self.machine)):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and doc.get("id"):
                out.append(ControlMessage.from_dict(doc))
        return sorted(out, key=lambda m: m.ns)

    def poll(self) -> list[ControlMessage]:
        """Dispatch every unseen inbox message to its handler (idempotent:
        handled ids are cursor-tracked locally). Returns the messages handled
        this pass. A handler's dict return is sent back as a reply."""
        handled = []
        for msg in self.inbox():
            if self._seen(msg.id):
                continue
            self._mark_seen(msg.id)
            handler = self._handlers.get(msg.kind)
            if handler is None:
                continue
            try:
                reply = handler(msg)
            except Exception:  # noqa: BLE001 — one bad handler can't stall the lane
                reply = None
            if reply is not None and not msg.reply_to:  # never reply to a reply
                self.send(msg.from_machine, msg.kind, reply, reply_to=msg.id)
            handled.append(msg)
        return handled

    # ------------------------------------------------------------- cleanup
    def gc(self, ttl_s: float = _DEFAULT_TTL_S) -> int:
        """Best-effort removal of expired control docs across all inboxes
        (operational ephemera; any machine may sweep them)."""
        floor = time.time_ns() - int(ttl_s * 1e9)
        removed = 0
        for path in self.tx.list_docs("control"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and int(doc.get("ns", 0)) < floor:
                self.tx.delete_doc(path)
                removed += 1
        return removed
