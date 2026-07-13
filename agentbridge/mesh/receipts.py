"""Receipts (R8) — Sent / Delivered / Read, exactly per the v1 HANDOFF design.

No new write path: Read derives from each member's ``read_ns`` cursor (their
own state overlay), and Delivered derives from presence — *"the recipient's
client has been online since the message was posted, so it fetched it"* —
via the SAME ns-compare (``last_seen_ns >= message.ns``). ns, never ts.

Privacy (R6, HANDOFF's explicit note): Delivered exposes online timing — the
same surface as read receipts — so BOTH tiers are gated by the receipt
toggles: a member with ``read_receipts`` off contributes Sent only, and a
viewer with ``view_read_receipts`` off sees Sent everywhere. A deactivated
account never advances past Sent for free: it has no fresh heartbeat.
"""

from __future__ import annotations

from ..core.errors import ValidationError
from ..core.models import ChatKind, MsgKind, ReceiptState
from .messaging import MessagingService
from .presence import PresenceService
from .privacy import PrivacyService

__all__ = ["ReceiptsService"]

_TIER = {ReceiptState.SENT: 0, ReceiptState.DELIVERED: 1, ReceiptState.READ: 2}


class ReceiptsService:
    def __init__(
        self,
        messaging: MessagingService,
        privacy: PrivacyService,
        presence: PresenceService,
    ) -> None:
        self.messaging = messaging
        self.privacy = privacy
        self.presence = presence
        self.user = messaging.user

    # ----------------------------------------------------------------- core
    def _cursors_of(self, chat_id: str, members: list[str]) -> dict[str, dict]:
        """Each other member's verified receipt cursors (R31.5): a forged or
        unsigned state doc reads as absent, so nobody fabricates a tick from
        raw store access. Delivered is a real per-recipient cursor now (R33),
        with presence as the floor for a member who hasn't written one yet."""
        out: dict[str, dict] = {}
        for m in members:
            st = self.messaging.state_of(chat_id, m)
            out[m] = {
                "read_ns": st.read_ns(), "read_ts": st.read_ts(),
                "delivered_ns": st.delivered_ns(),
                "delivered_ts": st.delivered_ts(),
                "last_seen_ns": self.presence.presence_of(m)["last_seen_ns"],
            }
        return out

    def _member_tier(self, member: str, msg_ns: int, cur: dict) -> ReceiptState:
        if not self.privacy.may_see_receipts_of(member, viewer=self.user):
            return ReceiptState.SENT  # receipt-gated: both tiers hidden
        if cur["read_ns"] >= msg_ns:
            return ReceiptState.READ
        if cur["delivered_ns"] >= msg_ns or cur["last_seen_ns"] >= msg_ns:
            return ReceiptState.DELIVERED
        return ReceiptState.SENT

    def receipts_for(self, chat_id: str) -> dict[str, dict]:
        """{msg_id: {state, read_by, delivered_to, pending, total}} for the
        viewer's OWN messages (the tick column). State = the LOWEST tier any
        other member is at (v1: double-accent only when everyone read)."""
        snap = self.messaging._require_member(chat_id)
        others = [m for m in snap.members if m != self.user]
        cursors = self._cursors_of(chat_id, others)

        out: dict[str, dict] = {}
        for msg in self.messaging.messages_for(chat_id):
            if msg.from_ != self.user or msg.kind is not MsgKind.MESSAGE or msg.deleted:
                continue
            if not others:  # self-chat: your own note is trivially read
                out[msg.id] = {"state": ReceiptState.READ.value, "read_by": [],
                               "delivered_to": [], "pending": [], "total": 0}
                continue
            read_by, delivered_to, pending = [], [], []
            worst = ReceiptState.READ
            for m in others:
                tier = self._member_tier(m, msg.ns, cursors[m])
                if _TIER[tier] < _TIER[worst]:
                    worst = tier
                (read_by if tier is ReceiptState.READ
                 else delivered_to if tier is ReceiptState.DELIVERED
                 else pending).append(m)
            out[msg.id] = {
                "state": worst.value,
                "read_by": sorted(read_by),
                "delivered_to": sorted(delivered_to),
                "pending": sorted(pending),
                "total": len(others),
            }
        return out

    def message_info(self, chat_id: str, msg_id: str) -> dict:
        """The Message-info dialog payload. Mine -> per-member rows carrying
        the Delivered/Read TIMINGS (Q17: the dialog used to show only "Sent");
        someone else's -> just the sent time (agent task steps ride the
        harness, R15). ``mine``/``kind`` let the client branch without guessing."""
        snap = self.messaging._require_member(chat_id)
        msg = next(
            (m for m in self.messaging.messages_for(chat_id) if m.id == msg_id), None
        )
        if msg is None:
            raise ValidationError(f"unknown message {msg_id}")
        mine = msg.from_ == self.user
        base = {"id": msg.id, "from": msg.from_, "ts": msg.ts, "ns": msg.ns,
                "mine": mine, "kind": msg.kind.value,
                "dm": snap.kind is ChatKind.DM}
        if not mine or msg.kind is not MsgKind.MESSAGE or msg.deleted:
            return base
        others = [m for m in snap.members if m != self.user]
        cursors = self._cursors_of(chat_id, others)
        rows = []
        worst = ReceiptState.READ if others else ReceiptState.READ
        for m in others:
            cur = cursors[m]
            tier = self._member_tier(m, msg.ns, cur)
            if _TIER[tier] < _TIER[worst]:
                worst = tier
            rows.append({
                "user": m,
                "tier": tier.value,
                # a timing only shows once the member has actually reached it
                "delivered_ts": cur["delivered_ts"]
                if _TIER[tier] >= _TIER[ReceiptState.DELIVERED] else "",
                "read_ts": cur["read_ts"]
                if tier is ReceiptState.READ else "",
            })
        return {**base, "state": worst.value, "total": len(others),
                "members": rows}
