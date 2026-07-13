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

import time

from .. import crypto
from ..core.errors import NotAMember, PermissionDenied, ValidationError
from ..core.models import BodyRecord, ChatKind, ChatSnapshot, Envelope, Message, MsgKind
from ..core.timekit import new_id, next_ns, utcnow_iso
from ..store.db import Store
from ..transport.base import Transport
from . import authz
from .events import pin_signing_bytes, reaction_signing_bytes, \
    redaction_signing_bytes, signing_bytes, state_signing_bytes
from .overlays import ChatOverlays, UserState, fold_reactions, reaction_map
from .paths import P
from .readmodel import build_messages, parse_tags, unread_info
from .sealer import E2EESealer, Sealer

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
        privacy=None,  # PrivacyService, wired by the Mesh facade (avoids a cycle)
        event_signer=lambda data: "",  # (bytes)->sig; facade wires the identity
        directory=None,  # Directory — resolves sign_pub for redaction verify (R25)
    ) -> None:
        self.tx = tx
        self.store = store
        self.sealer = sealer
        self.user = user
        self.machine = machine
        self._notify_outbox = notify_outbox
        self.privacy = privacy
        self._sign_event = event_signer
        self.directory = directory

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
        snap = self._require_member(chat_id)
        if not authz.can_send(snap, self.user):
            raise PermissionDenied("sending messages is restricted in this chat")
        # R6/R7: a block kills the EXISTING DM too (WhatsApp), and so does the
        # peer's account deletion — common groups stay unaffected either way.
        # The reason never reveals which it was.
        if snap.kind is ChatKind.DM and self.privacy is not None:
            other = next((m for m in snap.members if m != self.user), None)
            if other:
                peer = self.privacy.directory.get(other)
                if (peer is None or not peer.active
                        or self.privacy.blocked_between(self.user, other)):
                    raise PermissionDenied(f"@{other} is not available")
        if not (body or "").strip() and not files:
            raise ValidationError("empty message")
        record = BodyRecord(
            body=body,
            tags=tags if tags is not None else parse_tags(body),
            reply_to=reply_to,
            files=files or [],
            fwd=fwd,
        )
        # id/ns are minted FIRST so the sealer can bind them (replay-proofing)
        ns = next_ns()
        env_id = new_id("m", ns)
        env = Envelope(
            id=env_id, ns=ns, ts=utcnow_iso(), from_=self.user,
            kind=MsgKind.MESSAGE, **self.sealer.seal(chat_id, env_id, ns, record),
        )
        self.commit_envelope(chat_id, env)
        return env

    # ------------------------------------------------------------ info events
    def build_event(self, chat_id: str, event: dict) -> Envelope:
        """An INFO envelope — plaintext, it IS the chat-state log (FORMAT2).
        Signed with the author's identity key (R13.5) so the fold can reject
        a forged event attributed to someone else; unsigned only when this
        identity has no key yet (migrated/pre-upgrade)."""
        env = Envelope(
            id=new_id("m"), ns=next_ns(), ts=utcnow_iso(), from_=self.user,
            kind=MsgKind.INFO, event=event,
        )
        env.sig = self._sign_event(signing_bytes(chat_id, env.to_dict()))
        return env

    def post_event(self, chat_id: str, event: dict) -> Envelope:
        self._require_member(chat_id)
        env = self.build_event(chat_id, event)
        self.commit_envelope(chat_id, env)
        return env

    def commit_envelope(self, chat_id: str, env: Envelope) -> None:
        """Optimistic local cache + durable outbox commit (the send guarantee)."""
        payload = env.to_dict()
        self.store.upsert_messages(chat_id, [payload])
        self.store.outbox_add(
            OUTBOX_APPEND, f"{chat_id}|{P.log_name(self.user, self.machine)}", payload
        )
        self._notify_outbox()

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
        edit_ns = next_ns()  # minted first: the seal binds (msg_id, edit_ns)
        sealed = self.sealer.seal(
            chat_id, msg_id, edit_ns,
            BodyRecord(body=new_body, tags=parse_tags(new_body)),
        )
        ChatOverlays(self.tx, chat_id).put_edit(msg_id, sealed, by=self.user, ns=edit_ns)

    def redact(self, chat_id: str, msg_ids: list[str]) -> None:
        """Delete-for-everyone: SENDER-only, tombstoned in place (v1 rule).
        The tombstone is Ed25519-SIGNED by the sender (R25) so a folder writer
        can't forge a redaction of someone else's message — the read model
        verifies the signature before honoring it."""
        self._require_member(chat_id)
        ov = ChatOverlays(self.tx, chat_id)
        for msg_id in msg_ids:
            original = self._cached(chat_id, msg_id)
            if original is None:
                raise ValidationError(f"unknown message {msg_id}")
            if original.get("from") != self.user:
                raise PermissionDenied("only the sender may delete for everyone")
            red_ns = next_ns()  # minted first: the signature binds it
            sig = self._sign_event(
                redaction_signing_bytes(chat_id, msg_id, self.user, red_ns))
            ov.put_redaction(msg_id, by=self.user, sig=sig, ns=red_ns)

    def react(self, chat_id: str, msg_id: str, emoji: str | None) -> None:
        self._require_member(chat_id)
        ChatOverlays(self.tx, chat_id).set_reaction(
            self.user, msg_id, emoji, signer=self._sign_event)

    def pin(self, chat_id: str, msg_id: str, hours: float | None = None) -> None:
        """Pin, optionally expiring (v1 UX: 24h/7d/forever). Expiry is LAZY —
        readers just stop seeing it; nobody writes a cleanup. The pin doc is
        signed by the pinner (R31) — the ns and expiry are minted first so the
        signature binds them."""
        self._require_member(chat_id)
        pin_ns = next_ns()
        until_ns = pin_ns + int(hours * 3600 * 1e9) if hours else 0
        sig = self._sign_event(
            pin_signing_bytes(chat_id, msg_id, self.user, pin_ns, until_ns))
        ChatOverlays(self.tx, chat_id).put_pin(
            msg_id, by=self.user, ns=pin_ns, until_ns=until_ns, sig=sig)

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
        snap = self._require_member(chat_id)
        # history-on-join policy: unless the group shares history, a member
        # sees only messages from their (latest) join onward; info pills stay
        history_from = 0
        if snap.kind is ChatKind.GROUP and not snap.permissions.send_history:
            history_from = snap.members[self.user].joined_ns
        ov = ChatOverlays(self.tx, chat_id)
        return build_messages(
            chat_id,
            self.user,
            self.store.messages(chat_id),
            self.sealer,
            edits=ov.edits(),
            redactions=ov.redactions(),
            reactions=self._verified_reactions(chat_id, snap, ov),
            state=self._state(chat_id).get(),
            history_from_ns=history_from,
            tenure=snap.tenure,
            verify_redaction=self._redaction_verifier(chat_id),
        )

    def _crypto_boundary(self) -> bool:
        """True when this mesh has a real crypto boundary to enforce overlay
        signatures against (E2EE + a directory to resolve keys). A plaintext/
        dev mesh keeps the presence-based overlay semantics."""
        return isinstance(self.sealer, E2EESealer) and self.directory is not None

    def _ever_member(self, snap: ChatSnapshot, user: str) -> bool:
        """Current member, or was one (tenure) — a departed member's
        reactions/pins on old messages stay, a never-member's never count."""
        return snap.is_member(user) or user in snap.tenure

    def _verified_reactions(
        self, chat_id: str, snap: ChatSnapshot, ov: ChatOverlays,
    ) -> dict[str, dict[str, list[str]]]:
        """Reactions folded from per-user files that VERIFY (R31): the file
        must be signed by its owner over the full mapping, and the owner must
        be (or have been) a member. Unsigned/forged files are ignored —
        fail-safe, like redactions (harden_startup re-signs local legacy
        files so real reactions survive the tightening)."""
        if not self._crypto_boundary():
            return ov.reactions()
        verified: dict[str, dict[str, str]] = {}
        for user, doc in ov.reaction_docs().items():
            sig = doc.get("sig") or ""
            pub = self.directory.sign_pub(user)
            if not sig or not pub or not self._ever_member(snap, user):
                continue
            mapping = reaction_map(doc)
            data = reaction_signing_bytes(
                chat_id, user, int(doc.get("ns", 0)), mapping)
            if crypto.verify(pub, sig, data):
                verified[user] = mapping
        return fold_reactions(verified)

    def _redaction_verifier(self, chat_id: str):
        """A callable ``(msg_id, redaction_doc, original_sender) -> bool`` for
        the read model, or None for a plaintext/dev mesh (no crypto boundary,
        so redactions stay presence-based there). A redaction counts only when
        it is SIGNED by the message's original sender (delete-for-everyone is
        sender-only) — a forged/unsigned overlay dropped on the shared folder
        is ignored (R25)."""
        if not self._crypto_boundary():
            return None

        def ok(msg_id: str, red: dict, original_from: str) -> bool:
            by = red.get("by")
            if not by or by != original_from:
                return False  # only the original sender may delete for everyone
            pub = self.directory.sign_pub(by)
            sig = red.get("sig") or ""
            if not pub or not sig:
                return False
            return crypto.verify(
                pub, sig,
                redaction_signing_bytes(chat_id, msg_id, by, int(red.get("ns", 0))),
            )

        return ok

    def unread(self, chat_id: str) -> dict:
        return unread_info(
            self.messages_for(chat_id), self.user, self._state(chat_id).get()
        )

    def chat_overview(self, chat_id: str) -> dict:
        """One-pass sidebar payload: last visible message + unread info + my
        per-chat flags. Folds the chat once (vs unread()+tail separately)."""
        msgs = self.messages_for(chat_id)
        state = self._state(chat_id).get()
        last = next(
            (m for m in reversed(msgs) if m.kind is MsgKind.MESSAGE), None
        )
        return {
            "last": last,
            **unread_info(msgs, self.user, state),
            "archived": bool(state.get("archived")),
            "pinned": bool(state.get("pinned")),
            "mute": state.get("mute", False),
            # delete-for-me of the WHOLE chat (hidden from my list, undoable)
            "deleted": bool(state.get("deleted")),
        }

    def my_state(self, chat_id: str) -> dict:
        """My sanitized per-chat state for the transcript view (starred ids +
        read cursor + flags) — never the raw overlay document."""
        self._require_member(chat_id)
        state = self._state(chat_id).get()
        return {
            "starred": list(state.get("starred", [])),
            "read_ns": int(state.get("read_ns", 0)),
            "archived": bool(state.get("archived")),
            "pinned": bool(state.get("pinned")),
            "forced_unread": bool(state.get("forced_unread")),
            "mute": state.get("mute", False),
        }

    def pins(self, chat_id: str) -> dict[str, dict]:
        snap = self._require_member(chat_id)
        now = time.time_ns()
        live = {
            mid: doc
            for mid, doc in ChatOverlays(self.tx, chat_id).pins().items()
            if not doc.get("until_ns") or int(doc["until_ns"]) > now
        }
        if not self._crypto_boundary():
            return live
        # R31: honor only pins signed by their pinner (a member); a dropped-in
        # doc or a tampered expiry doesn't verify and is ignored
        out: dict[str, dict] = {}
        for mid, doc in live.items():
            by = doc.get("by") or ""
            pub = self.directory.sign_pub(by)
            sig = doc.get("sig") or ""
            if not pub or not sig or not self._ever_member(snap, by):
                continue
            data = pin_signing_bytes(
                chat_id, mid, by, int(doc.get("ns", 0)),
                int(doc.get("until_ns", 0)))
            if crypto.verify(pub, sig, data):
                out[mid] = doc
        return out

    def starred(self, chat_id: str) -> list[Message]:
        """Starred = ids resolved LIVE (v2 change: no snapshots — a redacted
        message reads as its tombstone here too, never its old body)."""
        ids = set(self._state(chat_id).starred())
        return [m for m in self.messages_for(chat_id) if m.id in ids]

    # ---------------------------------------------------------------- helpers
    def state_of(self, chat_id: str, user: str) -> UserState:
        """A per-user state accessor with verification wired (R31.5): reads
        honor only docs signed by their owner. Only the service's OWN identity
        gets the signer — nobody writes another user's state through here."""
        return UserState(
            self.tx, chat_id, user,
            signer=self._sign_event if user == self.user else None,
            verifier=self._state_verifier(chat_id, user),
        )

    def _state_verifier(self, chat_id: str, user: str):
        """``(state_doc) -> bool``, or None without a crypto boundary. Every
        verified reader — the owner's own view, receipts' cursors, the
        notifier's mute check — treats a doc that fails this as absent."""
        if not self._crypto_boundary():
            return None

        def ok(doc: dict) -> bool:
            if not doc:
                return True  # absent state is a valid (empty) state
            pub = self.directory.sign_pub(user)
            sig = doc.get("sig") or ""
            fields = UserState.signed_fields(doc)
            return bool(pub) and bool(sig) and crypto.verify(
                pub, sig,
                state_signing_bytes(chat_id, user, int(doc.get("ns", 0)), fields),
            )

        return ok

    def _state(self, chat_id: str) -> UserState:
        return self.state_of(chat_id, self.user)

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
