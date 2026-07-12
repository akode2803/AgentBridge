"""Messaging service — every mutating message operation, membership-gated.

THE INVARIANT (CLAUDE.md): visibility = membership. Every public method —
read OR write — resolves the chat snapshot and refuses non-members
(``NotAMember``). When you add a method here, gate it the same way; the
v0.24.1 lesson was that read gates alone are not the full picture.

Posting is durable: the sealed envelope is cached optimistically (sender sees
it instantly) and committed to the OUTBOX before any transport attempt — the
OutboxWorker flushes it with retry-forever semantics (R3).
"""

from __future__ import annotations

from ..core.errors import NotAMember, PermissionDenied, ValidationError
from ..core.models import BodyRecord, ChatSnapshot, Envelope, Message, MsgKind
from ..core.timekit import new_id, next_ns, utcnow_iso
from ..store.db import Store
from ..transport.base import Transport
from .overlays import ChatOverlays, UserState
from .paths import P
from .readmodel import build_messages, parse_tags, unread_info
from .sealer import Sealer

__all__ = ["MessagingService", "OUTBOX_APPEND"]

OUTBOX_APPEND = "append_log"  # outbox kind; target = "<chat_id>|<log_name>"


class MessagingService:
    def __init__(
        self,
        tx: Transport,
        store: Store,
        sealer: Sealer,
        user: str,
        machine: str,
        *,
        notify_outbox=lambda: None,
    ) -> None:
        self.tx = tx
        self.store = store
        self.sealer = sealer
        self.user = user
        self.machine = machine
        self._notify_outbox = notify_outbox

    # ------------------------------------------------------------- membership
    def snapshot(self, chat_id: str) -> ChatSnapshot:
        doc = self.tx.get_doc(P.meta(chat_id))
        if not isinstance(doc, dict):
            raise NotAMember(f"unknown chat {chat_id}")
        return ChatSnapshot.from_dict(doc)

    def _require_member(self, chat_id: str) -> ChatSnapshot:
        snap = self.snapshot(chat_id)
        if not snap.is_member(self.user):
            raise NotAMember(f"{self.user} is not a member of {chat_id}")
        return snap

    # ------------------------------------------------------------------ write
    def post(
        self,
        chat_id: str,
        body: str,
        *,
        tags: list[str] | None = None,
        reply_to: dict | None = None,
        files: list[dict] | None = None,
        fwd: dict | None = None,
    ) -> Envelope:
        self._require_member(chat_id)
        if not (body or "").strip() and not files:
            raise ValidationError("empty message")
        record = BodyRecord(
            body=body,
            tags=tags if tags is not None else parse_tags(body),
            reply_to=reply_to,
            files=files or [],
            fwd=fwd,
        )
        env = Envelope(
            id=new_id("m"), ns=next_ns(), ts=utcnow_iso(), from_=self.user,
            kind=MsgKind.MESSAGE, **self.sealer.seal(chat_id, record),
        )
        payload = env.to_dict()
        # optimistic local cache first (sender sees it instantly) ...
        self.store.upsert_messages(chat_id, [payload])
        # ... then the durable outbox commit — this is the send guarantee
        self.store.outbox_add(
            OUTBOX_APPEND, f"{chat_id}|{P.log_name(self.user, self.machine)}", payload
        )
        self._notify_outbox()
        return env

    def edit(self, chat_id: str, msg_id: str, new_body: str) -> None:
        self._require_member(chat_id)
        if not (new_body or "").strip():
            raise ValidationError("empty edit")
        original = self._cached(chat_id, msg_id)
        if original is None:
            raise ValidationError(f"unknown message {msg_id}")
        if original.get("from") != self.user:
            raise PermissionDenied("only the author may edit")
        if original.get("kind") != MsgKind.MESSAGE.value:
            raise ValidationError("info events cannot be edited")
        if self.tx.get_doc(P.redaction(chat_id, msg_id)) is not None:
            raise ValidationError("a deleted message cannot be edited")
        sealed = self.sealer.seal(
            chat_id, BodyRecord(body=new_body, tags=parse_tags(new_body))
        )
        ChatOverlays(self.tx, chat_id).put_edit(msg_id, sealed, by=self.user)

    def redact(self, chat_id: str, msg_ids: list[str]) -> None:
        """Delete-for-everyone: SENDER-only, tombstoned in place (v1 rule)."""
        self._require_member(chat_id)
        ov = ChatOverlays(self.tx, chat_id)
        for msg_id in msg_ids:
            original = self._cached(chat_id, msg_id)
            if original is None:
                raise ValidationError(f"unknown message {msg_id}")
            if original.get("from") != self.user:
                raise PermissionDenied("only the sender may delete for everyone")
            ov.put_redaction(msg_id, by=self.user)

    def react(self, chat_id: str, msg_id: str, emoji: str | None) -> None:
        self._require_member(chat_id)
        ChatOverlays(self.tx, chat_id).set_reaction(self.user, msg_id, emoji)

    def pin(self, chat_id: str, msg_id: str) -> None:
        self._require_member(chat_id)
        ChatOverlays(self.tx, chat_id).put_pin(msg_id, by=self.user)

    def unpin(self, chat_id: str, msg_id: str) -> None:
        self._require_member(chat_id)
        ChatOverlays(self.tx, chat_id).remove_pin(msg_id)

    # ------------------------------------------------- per-user state writes
    def star(self, chat_id: str, msg_ids: list[str]) -> None:
        self._require_member(chat_id)
        self._state(chat_id).star(msg_ids)

    def unstar(self, chat_id: str, msg_ids: list[str]) -> None:
        self._require_member(chat_id)
        self._state(chat_id).unstar(msg_ids)

    def hide(self, chat_id: str, msg_ids: list[str]) -> None:
        """Delete-for-me: a private per-user hide (reversible)."""
        self._require_member(chat_id)
        self._state(chat_id).hide(msg_ids)

    def unhide(self, chat_id: str, msg_ids: list[str]) -> None:
        self._require_member(chat_id)
        self._state(chat_id).unhide(msg_ids)

    def clear_chat(self, chat_id: str, *, keep_starred: bool = False) -> None:
        self._require_member(chat_id)
        msgs = self.store.messages(chat_id)
        cut = max((m.get("ns", 0) for m in msgs), default=0)
        if cut:
            self._state(chat_id).clear(cut, keep_starred=keep_starred)

    def mark_read(self, chat_id: str) -> None:
        self._require_member(chat_id)
        msgs = self.store.messages(chat_id)
        latest = max((m.get("ns", 0) for m in msgs), default=0)
        if latest:
            self._state(chat_id).mark_read(latest)

    def set_chat_flag(self, chat_id: str, name: str, value) -> None:
        self._require_member(chat_id)
        self._state(chat_id).set_flag(name, value)

    # ------------------------------------------------------------------- read
    def messages_for(self, chat_id: str) -> list[Message]:
        """THE read choke-point: membership + every overlay applied."""
        self._require_member(chat_id)
        ov = ChatOverlays(self.tx, chat_id)
        return build_messages(
            chat_id,
            self.user,
            self.store.messages(chat_id),
            self.sealer,
            edits=ov.edits(),
            redactions=ov.redactions(),
            reactions=ov.reactions(),
            state=self._state(chat_id).get(),
        )

    def unread(self, chat_id: str) -> dict:
        return unread_info(
            self.messages_for(chat_id), self.user, self._state(chat_id).get()
        )

    def pins(self, chat_id: str) -> dict[str, dict]:
        self._require_member(chat_id)
        return ChatOverlays(self.tx, chat_id).pins()

    def starred(self, chat_id: str) -> list[Message]:
        """Starred = ids resolved LIVE (v2 change: no snapshots — a redacted
        message reads as its tombstone here too, never its old body)."""
        ids = set(self._state(chat_id).starred())
        return [m for m in self.messages_for(chat_id) if m.id in ids]

    # ---------------------------------------------------------------- helpers
    def _state(self, chat_id: str) -> UserState:
        return UserState(self.tx, chat_id, self.user)

    def _cached(self, chat_id: str, msg_id: str) -> dict | None:
        for rec in self.store.messages(chat_id):
            if rec.get("id") == msg_id:
                return rec
        return None

    # ------------------------------------------------------- outbox handler
    def outbox_handlers(self) -> dict:
        def append_log(target: str, payload: dict) -> None:
            chat_id, _, log_name = target.partition("|")
            if not chat_id or not log_name:
                raise ValidationError(f"malformed append target {target!r}")
            self.tx.append_log(chat_id, log_name, payload)

        return {OUTBOX_APPEND: append_log}
