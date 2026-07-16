"""Privacy & account-permission layer (R6) — the dedicated module gating who
sees what and who may reach whom. Symmetric for members and agents; an
agent's settings are managed by its responsible member.

Audience semantics (docs/FORMAT2.md + REWRITE_PLAN D13):
- PROFILE surfaces (about/status/last-seen/online): ``agents`` also admits
  humans who own an agent — hiding a surface from an agent's owner is fake
  security, their agent can relay it.
- The MESSAGING / ADD-TO-GROUP gates are strict: ``agents`` means agents
  only, no owner ride-along (explicit product decision), and these two gates
  are PUBLIC so an agent can check before reaching out instead of being
  silently blocked. ONE bond overrides them (V103): an agent and its OWN
  responsible member always connect — the owner's own tool reaching them (or
  they it) is never an "outsider", so "who can message me = nobody" gates
  strangers, not your own Claude. Block still wins in both directions.

Blocking is WhatsApp-shaped: it kills DMs in both directions (new AND
existing), never leaks "you are blocked" as a reason, and leaves common
groups untouched.
"""

from __future__ import annotations

from ..core.errors import PermissionDenied, ValidationError
from ..core.models import Account, Audience, ChatSnapshot, UserKind
from ..transport.base import Transport
from .directory import Directory
from .paths import P

__all__ = ["PrivacyService"]

_PROFILE_FIELDS = ("last_seen", "online", "photo", "about", "status")
_GATE_FIELDS = ("messaging", "add_to_group")
_BOOL_FIELDS = ("read_receipts", "view_read_receipts")


class PrivacyService:
    def __init__(self, tx: Transport, directory: Directory, user: str) -> None:
        self.tx = tx
        self.directory = directory
        self.user = user

    # ------------------------------------------------------------- write side
    def set_privacy(self, changes: dict, *, agent: str | None = None) -> Account:
        """Update my own privacy — or an owned agent's (``agent=``)."""
        target = self._writable_target(agent)
        allowed = set(_PROFILE_FIELDS) | set(_GATE_FIELDS) | set(_BOOL_FIELDS)
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"unknown privacy field(s): {sorted(unknown)}")
        for field_name, value in changes.items():
            if field_name in _BOOL_FIELDS:
                if not isinstance(value, bool):
                    raise ValidationError(f"{field_name} must be true/false")
                continue
            try:
                aud = Audience(value)
            except ValueError:
                raise ValidationError(f"invalid audience {value!r} for {field_name}") from None
            if field_name == "photo" and aud not in (Audience.EVERYONE, Audience.NOBODY):
                raise ValidationError("photo visibility is everyone or nobody")
        return self._patch(target, lambda doc: doc.setdefault("privacy", {}).update(changes))

    def set_agent_rules(self, agent: str, changes: dict) -> Account:
        """Owner-set OUTBOUND rules: who may this agent message / add."""
        self._require_owned_agent(agent)
        unknown = set(changes) - set(_GATE_FIELDS)
        if unknown:
            raise ValidationError(f"unknown agent rule(s): {sorted(unknown)}")
        for field_name, value in changes.items():
            try:
                Audience(value)
            except ValueError:
                raise ValidationError(f"invalid audience {value!r} for {field_name}") from None
        return self._patch(agent, lambda doc: doc.setdefault("agent_rules", {}).update(changes))

    def block(self, name: str, *, agent: str | None = None) -> Account:
        target = self._writable_target(agent)
        if name == target:
            raise ValidationError("cannot block yourself")
        if not self.directory.exists(name):
            raise ValidationError(f"unknown user @{name}")

        def apply(doc: dict) -> None:
            blocked = doc.setdefault("blocked", [])
            if name not in blocked:
                blocked.append(name)

        return self._patch(target, apply)

    def unblock(self, name: str, *, agent: str | None = None) -> Account:
        target = self._writable_target(agent)
        return self._patch(
            target,
            lambda doc: doc.update(blocked=[b for b in doc.get("blocked", []) if b != name]),
        )

    # ------------------------------------------------------------- evaluation
    def blocked_between(self, a: str, b: str) -> bool:
        """True if either party blocked the other (hard, both directions)."""
        aa, bb = self.directory.get(a), self.directory.get(b)
        return bool((aa and b in aa.blocked) or (bb and a in bb.blocked))

    def can_message(self, sender: str, recipient: str) -> tuple[bool, str]:
        """The PUBLIC messaging gate + block check. The reason string is safe
        to show the sender (never reveals a block)."""
        r = self.directory.get(recipient)
        if r is None or not r.active:
            return False, f"@{recipient} is not available"
        if self.blocked_between(sender, recipient):
            return False, f"@{recipient} is not available"  # blocks never leak
        # V103: an owner and their own agent always connect — the audience
        # gates (recipient's inbound AND a sender-agent's outbound rule)
        # govern OUTSIDERS opening a conversation, never the owner's own tool.
        # Placed after the block check so an explicit block still overrides.
        if self._agent_owner_pair(sender, recipient):
            return True, ""
        if not self._gate_allows(r.privacy.messaging, gatekeeper=recipient, other=sender):
            return False, (f"@{recipient} accepts messages from "
                           f"{r.privacy.messaging.value} only")
        s = self.directory.get(sender)
        if s and s.kind is UserKind.AGENT:
            rule = s.rules().messaging
            if not self._gate_allows(rule, gatekeeper=sender, other=recipient):
                return False, (f"@{sender}'s responsible member allows it to "
                               f"message {rule.value} only")
        return True, ""

    def can_add_to_group(self, actor: str, target: str) -> tuple[bool, str]:
        """The PUBLIC add-to-group gate (applies to pulled-in owners too)."""
        t = self.directory.get(target)
        if t is None or not t.active:
            return False, f"@{target} is not available"
        if self.blocked_between(actor, target):
            return False, f"@{target} is not available"
        # V103: the owner↔agent bond overrides the gate here too — an agent's
        # auto_dm room PULLS its owner in, so a strict add_to_group on the
        # owner must not stop their own agent from starting that chat.
        if self._agent_owner_pair(actor, target):
            return True, ""
        if not self._gate_allows(t.privacy.add_to_group, gatekeeper=target, other=actor):
            return False, (f"@{target} can be added to groups by "
                           f"{t.privacy.add_to_group.value} only")
        a = self.directory.get(actor)
        if a and a.kind is UserKind.AGENT:
            rule = a.rules().add_to_group
            if not self._gate_allows(rule, gatekeeper=actor, other=target):
                return False, (f"@{actor}'s responsible member allows it to "
                               f"add {rule.value} only")
        return True, ""

    def public_gates(self, name: str) -> dict:
        """The two BY-DESIGN-public settings, readable by anyone — an agent
        checks these before reaching out."""
        acc = self.directory.get(name)
        if acc is None:
            return {}
        return {
            "messaging": acc.privacy.messaging.value,
            "add_to_group": acc.privacy.add_to_group.value,
        }

    def profile_allows(self, field_name: str, owner: str, viewer: str) -> bool:
        """May ``viewer`` see ``owner``'s profile surface (about/status/photo/
        last_seen/online)? Self and the agent's responsible member always may."""
        if viewer == owner:
            return True
        acc = self.directory.get(owner)
        if acc is None:
            return False
        if acc.kind is UserKind.AGENT and self.directory.owner_of(owner) == viewer:
            return True  # the responsible member manages these very settings
        aud = getattr(acc.privacy, field_name)
        if aud is Audience.EVERYONE:
            return True
        if aud is Audience.NOBODY:
            return False
        if aud is Audience.MEMBERS:
            return self.shares_chat(owner, viewer)
        # AGENTS: agents, plus humans who own one (their agent could relay)
        vk = self.directory.kind(viewer)
        return vk is UserKind.AGENT or (vk is UserKind.HUMAN and self._owns_any_agent(viewer))

    def visible_profile(self, target: str, viewer: str | None = None) -> dict:
        """The membership-of-information projection every connector serves
        instead of raw account docs."""
        viewer = viewer or self.user
        acc = self.directory.get(target)
        if acc is None:
            return {}
        out: dict = {
            "name": acc.name,
            "kind": acc.kind.value,
            "display": acc.display,
            "active": acc.active,
            **self.public_gates(target),
        }
        if acc.kind is UserKind.AGENT and acc.agent:
            out["owner"] = acc.agent.owner      # always visible (product rule)
            out["machine"] = acc.agent.machine
        if self.profile_allows("about", target, viewer):
            out["about"] = acc.about
        if self.profile_allows("status", target, viewer):
            out["status"] = {"state": acc.status.state, "text": acc.status.text}
        out["photo_visible"] = self.profile_allows("photo", target, viewer)
        out["may_see_last_seen"] = self.profile_allows("last_seen", target, viewer)
        out["may_see_online"] = self.profile_allows("online", target, viewer)
        return out

    def may_see_receipts_of(self, reader: str, viewer: str | None = None) -> bool:
        """Read-receipt visibility (R8 consumes): the viewer must have
        view_read_receipts ON and the reader must emit them."""
        viewer = viewer or self.user
        v, r = self.directory.get(viewer), self.directory.get(reader)
        return bool(
            v and v.privacy.view_read_receipts and r and r.privacy.read_receipts
        )

    def shares_chat(self, a: str, b: str) -> bool:
        """D13 'members' audience: a and b sit in at least one chat together."""
        for chat_id in self.tx.list_chat_ids():
            doc = self.tx.get_doc(P.meta(chat_id))
            if isinstance(doc, dict):
                snap = ChatSnapshot.from_dict(doc)
                if snap.is_member(a) and snap.is_member(b):
                    return True
        return False

    # ---------------------------------------------------------------- helpers
    def _agent_owner_pair(self, a: str, b: str) -> bool:
        """True if one of ``{a, b}`` is an agent and the other is its
        responsible member (V103). The owner↔agent bond always connects for
        the messaging + add-to-group gates: the owner's own agent is not an
        outsider knocking, and neither is the owner to their agent. Block is
        checked before this everywhere it's used, so an explicit block still
        wins. (Profile surfaces have their own owner ride-along in
        ``profile_allows``; this is only the strict gates.)"""
        for agent, other in ((a, b), (b, a)):
            if (self.directory.kind(agent) is UserKind.AGENT
                    and self.directory.owner_of(agent) == other):
                return True
        return False

    def _gate_allows(self, aud: Audience, *, gatekeeper: str, other: str) -> bool:
        """STRICT gate audience (messaging / add-to-group): no owner
        ride-along on 'agents' (explicit product decision)."""
        if aud is Audience.EVERYONE:
            return True
        if aud is Audience.NOBODY:
            return False
        if aud is Audience.MEMBERS:
            return self.shares_chat(gatekeeper, other)
        return self.directory.kind(other) is UserKind.AGENT  # AGENTS, strictly

    def _owns_any_agent(self, human: str) -> bool:
        for path in self.tx.list_docs("users"):
            doc = self.tx.get_doc(path)
            if isinstance(doc, dict) and (doc.get("agent") or {}).get("owner") == human:
                return True
        return False

    def _writable_target(self, agent: str | None) -> str:
        if agent is None:
            # D19: agents never self-manage privacy/blocks — owner-only
            if self.directory.kind(self.user) is UserKind.AGENT:
                raise PermissionDenied(
                    "an agent's privacy settings are managed by its responsible member"
                )
            return self.user
        self._require_owned_agent(agent)
        return agent

    def _require_owned_agent(self, agent: str) -> None:
        if self.directory.kind(agent) is not UserKind.AGENT:
            raise ValidationError(f"@{agent} is not an agent")
        if self.directory.owner_of(agent) != self.user:
            raise PermissionDenied(
                f"only @{agent}'s responsible member can change its settings"
            )

    def _patch(self, name: str, apply) -> Account:
        return self.directory.patch(name, apply)
