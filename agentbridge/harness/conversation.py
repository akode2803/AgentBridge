"""The conversation manager — every message reaches the agent ENRICHED.

A delivery bundles what a considerate colleague would know before answering:
the transcript (read through the membership-filtered read model, so edits are
applied and tombstones stay blank), the active pins, the roster with each
member's reply behaviour, and — for each triggering sender — their display
name, kind, CURRENT status (someone who went dnd since asking may prefer not
to be pinged back loudly) and online/last-seen, all matrix-gated exactly as
the agent is allowed to see them.

``render()`` is a deliberately plain, factual text form for R15's seam; the
R17 prompt manager owns real prompt assembly (persona, etiquette, JSON-driven
wording) and replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import ChatKind, Message, MsgKind, UserKind
from ..mesh.service import Mesh
from .queue import WorkGroup
from .settings import HarnessSettings
from .triggers import RULE_DESC

__all__ = ["ConversationManager", "Delivery", "TriggerContext"]

TRANSCRIPT_TAIL = 30


@dataclass
class TriggerContext:
    message: Message
    reason: str
    sender: str = ""
    sender_display: str = ""
    sender_kind: str = ""
    sender_status: dict | None = None    # {state, text} when visible to me
    sender_presence: dict | None = None  # {online, last_seen} when visible


@dataclass
class Delivery:
    agent: str
    chat_id: str
    chat_name: str
    chat_kind: str
    kind: str                     # "message" | "timer"
    rule: str
    roster: list[dict] = field(default_factory=list)
    pins: list[dict] = field(default_factory=list)
    transcript: list[Message] = field(default_factory=list)
    triggers: list[TriggerContext] = field(default_factory=list)
    note: str = ""                # timer note

    def render(self) -> str:
        """Plain factual rendering (R17 replaces this with real prompts)."""
        lines = [f"Chat: {self.chat_name} ({self.chat_kind})"]
        lines.append("Members: " + "; ".join(
            f"@{r['name']}{' (you)' if r.get('you') else ''}"
            f" — {r.get('desc', '')}" for r in self.roster))
        if self.kind == "timer":
            lines.append(f"Scheduled wake-up: {self.note}")
        for t in self.triggers:
            bits = [f"Trigger ({t.reason}): @{t.sender}"]
            if t.sender_status and t.sender_status.get("state") not in (None, "available"):
                bits.append(f"status={t.sender_status['state']}")
            if t.sender_presence is not None:
                bits.append("online" if t.sender_presence.get("online")
                            else f"last seen {t.sender_presence.get('last_seen') or 'unknown'}")
            lines.append(" ".join(bits))
        for p in self.pins:
            body = (p.get("body") or "").replace("\n", " ")[:160]
            lines.append(f"[PINNED by @{p.get('by')}] {body}")
        for m in self.transcript[-TRANSCRIPT_TAIL:]:
            lines.append(_render_msg(m, self.agent))
        return "\n".join(lines)


def _render_msg(m: Message, agent: str) -> str:
    if m.kind is MsgKind.INFO:
        ev = m.event or {}
        return f"[{m.ts}] · {ev.get('type', 'event')}"
    if m.deleted:
        return f"[{m.ts}] · a message was deleted"
    who = f"@{m.from_}" + (" (you)" if m.from_ == agent else "")
    rt = m.reply_to or {}
    rline = ""
    if rt.get("from"):
        excerpt = (rt.get("body") or "").replace("\n", " ")[:120]
        who_r = ("their own message" if rt["from"] == m.from_ else f"@{rt['from']}")
        rline = f' [replying to {who_r}: "{excerpt}"]'
    fwd = m.fwd or {}
    fline = f" [forwarded from @{fwd['from']}]" if fwd.get("from") else ""
    names = ", ".join(f.get("name", "") for f in (m.files or []))
    files = f"  [files: {names}]" if names else ""
    edited = " (edited)" if m.edited else ""
    return f"[{m.ts}] {who}:{fline}{rline}{edited} {m.body}{files}"


class ConversationManager:
    def __init__(self, mesh: Mesh) -> None:
        self.mesh = mesh
        self.agent = mesh.user

    def build(
        self,
        group: WorkGroup,
        transcript: list[Message],
        settings: HarnessSettings,
    ) -> Delivery:
        chat_id = group.chat_id
        snap = self.mesh.snapshot(chat_id)
        chat_name = snap.name
        if snap.kind is ChatKind.DM:
            other = next((m for m in snap.members if m != self.agent), "")
            chat_name = f"direct chat with @{other}"

        by_id = {m.id: m for m in transcript}
        triggers = []
        for item in sorted(group.items, key=lambda i: i.ns):
            if item.kind != "message":
                continue
            msg = by_id.get(item.msg_id)
            if msg is None:
                continue
            triggers.append(self._trigger_context(msg, item.reason))

        pins = []
        try:
            for mid, doc in self.mesh.pins(chat_id).items():
                body = next((m.body for m in transcript if m.id == mid), "")
                pins.append({"id": mid, "by": doc.get("by"), "body": body})
        except Exception:  # noqa: BLE001 — pins are garnish, never a blocker
            pass

        return Delivery(
            agent=self.agent,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_kind=snap.kind.value,
            kind=group.kind,
            rule=settings.rule_for(chat_id),
            roster=self._roster(snap),
            pins=pins,
            transcript=transcript,
            triggers=triggers,
            note=group.items[0].note if group.kind == "timer" else "",
        )

    # ------------------------------------------------------------- helpers
    def _trigger_context(self, msg: Message, reason: str) -> TriggerContext:
        sender = msg.from_
        acc = self.mesh.directory.get(sender)
        status = presence = None
        try:
            profile = self.mesh.privacy.visible_profile(sender, viewer=self.agent)
            status = profile.get("status")
            presence = self.mesh.presence.visible_presence(sender, viewer=self.agent)
            if presence.get("online") is None and presence.get("last_seen") is None:
                presence = None
        except Exception:  # noqa: BLE001 — enrichment must never block a run
            pass
        return TriggerContext(
            message=msg,
            reason=reason,
            sender=sender,
            sender_display=(acc.display if acc else sender),
            sender_kind=(acc.kind.value if acc else "agent"),
            sender_status=status,
            sender_presence=presence,
        )

    def _roster(self, snap) -> list[dict]:
        out = []
        for name in snap.members:
            acc = self.mesh.directory.get(name)
            if acc is None:
                continue
            if name == self.agent:
                desc = "you"
            elif acc.kind is UserKind.HUMAN:
                desc = "member"
            else:
                rule = HarnessSettings.from_account(acc).rule_for(snap.id)
                desc = RULE_DESC.get(rule, rule)
            out.append({"name": name, "display": acc.display or name,
                        "kind": acc.kind.value, "desc": desc,
                        "you": name == self.agent})
        return out
