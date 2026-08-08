"""The conversation manager — every message reaches the agent ENRICHED.

A delivery bundles what a considerate colleague would know before answering:
the transcript (read through the membership-filtered read model, so edits are
applied and tombstones stay blank), the active pins, the roster with each
member's reply behaviour, and — for each triggering sender — their display
name, kind, CURRENT status (someone who went dnd since asking may prefer not
to be pinged back loudly) and online/last-seen, all matrix-gated exactly as
the agent is allowed to see them.

A ``Delivery`` is pure data; every word built FROM it (the prompt, the
context file, the feed lines) belongs to the R17 prompt manager (prompt.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..core.models import ChatKind, Message, UserKind
from ..mesh.service import Mesh
from .queue import WorkGroup
from .settings import HarnessSettings
from .triggers import RULE_DESC

__all__ = ["ConversationManager", "Delivery", "TriggerContext"]


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
    # older messages the retrieval index judged relevant (R21) — filled by
    # the responder when the agent has an index; rendered before the tail
    recalled: list[Message] = field(default_factory=list)
    # V54 (parity c): the chat facts a human sees in the info pane
    created_by: str = ""
    created_at: str = ""
    permissions: dict = field(default_factory=dict)   # groups only
    # V87/V107 self-awareness: the agent's own recent run outcomes in THIS
    # chat (from its run history — what it did, and whether its last run was
    # stopped/interrupted before the reply posted) and its pending wake-ups
    # for this chat. Other chats contribute only a COUNT — a run's context
    # must never carry another chat's content (visibility = membership).
    recent_runs: list[dict] = field(default_factory=list)
    timers: list[dict] = field(default_factory=list)
    other_timers: int = 0
    # V88: how late a wake-up fires past its scheduled time (0 = on time) —
    # a harness that was offline fires the backlog late, and the prompt
    # warns the agent to re-check relevance instead of acting on stale plans
    late_s: float = 0.0
    run_id: str = ""              # bound by Runner to the live RunFeed id
    invocation: Any = None        # adapter-owned immutable resolved invocation
    harness_settings: Any = None  # settings snapshot paired with invocation


class ConversationManager:
    def __init__(self, mesh: Mesh, timers=None) -> None:
        self.mesh = mesh
        self.agent = mesh.user
        self.timers = timers   # the runner's TimerService (None in bare tests)

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

        # V54: chat genesis — the first created event in the fold (a human
        # reads this in the info-pane footer)
        genesis = next((m for m in transcript
                        if (m.event or {}).get("type") == "created"), None)
        recent_runs = self._recent_runs(chat_id)
        timers, other_timers = self._timers(chat_id)
        return Delivery(
            agent=self.agent,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_kind=snap.kind.value,
            kind=group.kind,
            rule=settings.rule_for(chat_id, dm=snap.kind is ChatKind.DM),
            roster=self._roster(snap),
            pins=pins,
            transcript=transcript,
            triggers=triggers,
            note=group.items[0].note if group.kind == "timer" else "",
            late_s=(max(0.0, (time.time_ns() - int(group.items[0].ns)) / 1e9)
                    if group.kind == "timer" else 0.0),
            created_by=genesis.from_ if genesis else "",
            created_at=genesis.ts if genesis else "",
            permissions=({k: getattr(v, "value", v)
                          for k, v in snap.permissions.__dict__.items()}
                         if snap.kind is ChatKind.GROUP else {}),
            recent_runs=recent_runs,
            timers=timers,
            other_timers=other_timers,
        )

    # ------------------------------------------------------------- helpers
    def _recent_runs(self, chat_id: str) -> list[dict]:
        """This chat's tail of the agent's own run history (V87/V107) —
        the same ``status/<agent>_runs.json`` the owner reads in Settings,
        filtered to THIS chat so no other chat's activity rides along."""
        try:
            doc = self.mesh.tx.get_doc(
                f"status/{self.agent}_runs.json", default={}) or {}
            runs = doc.get("runs") if isinstance(doc, dict) else None
            return [r for r in (runs or []) if isinstance(r, dict)
                    and r.get("chat_id") == chat_id][-5:]
        except Exception:  # noqa: BLE001 — self-awareness never blocks a run
            return []

    def _timers(self, chat_id: str) -> tuple[list[dict], int]:
        """This chat's pending wake-ups (full, with ids — cancel_timer takes
        them) + a bare COUNT of those scheduled elsewhere."""
        if self.timers is None:
            return [], 0
        try:
            mine, elsewhere = [], 0
            for t in self.timers.snapshot():
                if t.get("chat_id") == chat_id:
                    mine.append(t)
                else:
                    elsewhere += 1
            return mine, elsewhere
        except Exception:  # noqa: BLE001
            return [], 0

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
                # V54: admins are visible to every member in the GUI roster —
                # the agent should know who can act on the group too
                role = getattr(snap.members.get(name), "role", None)
                desc = "admin" if getattr(role, "value", role) == "admin" \
                    else "member"
            else:
                rule = HarnessSettings.from_account(acc).rule_for(
                    snap.id, dm=snap.kind is ChatKind.DM)
                desc = RULE_DESC.get(rule, rule)
            out.append({"name": name, "display": acc.display or name,
                        "kind": acc.kind.value, "desc": desc,
                        "you": name == self.agent})
        return out
