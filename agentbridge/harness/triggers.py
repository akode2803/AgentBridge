"""Trigger predicates — pure functions deciding what deserves a run.

Ported v1 semantics, upgraded:
- rules all / tagged / humans; ``@all`` (the everyone-mention) tags every
  member; a reply to one of this agent's messages counts as tagging it.
- an agent never triggers on itself, on info events, or on tombstones.
- messages from before the agent's (latest) join never trigger it — being
  re-added must not replay old mentions (v1 lesson, kept).
- a HUMAN editing an already-seen message into a mention re-triggers ONE
  reply. v2 keys the answered-guard on ``(msg_id, edit_ns)``, so a handled
  edit can never replay — v1's per-process baseline dance is gone.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Message, MsgKind, UserKind

__all__ = ["Candidate", "should_reply", "extract", "RULE_DESC"]

RULE_DESC = {
    "all": "an agent that replies to every message",
    "tagged": "an agent that replies only when tagged",
    "humans": "an agent that replies only to people",
}


@dataclass
class Candidate:
    """One potential run: a message (at a specific edit revision)."""

    message: Message
    edit_ns: int      # 0 = the original text; else the edit revision handled
    trigger_ns: int   # freshness ordinal (the edit time for edit-triggers)
    reason: str       # tagged | reply | rule-all | rule-humans | edit

    @property
    def key(self) -> str:
        return f"{self.message.id}@{self.edit_ns}"


def should_reply(
    rule: str, msg: Message, agent: str, kinds: dict[str, UserKind | None]
) -> str | None:
    """The reason this message triggers ``agent`` under ``rule``, or None."""
    if msg.from_ == agent or msg.kind is not MsgKind.MESSAGE or msg.deleted:
        return None
    tags = msg.tags or []
    if agent in tags or "all" in tags:
        return "tagged"
    if (msg.reply_to or {}).get("from") == agent:
        return "reply"  # replying to the agent counts as tagging it
    if rule == "all":
        return "rule-all"
    if rule == "humans" and kinds.get(msg.from_) is UserKind.HUMAN:
        return "rule-humans"
    return None


def extract(
    msgs: list[Message],
    agent: str,
    rule: str,
    kinds: dict[str, UserKind | None],
    *,
    joined_ns: int = 0,
    after_ns: int = 0,
    after_edit_ns: int = 0,
) -> tuple[list[Candidate], int, int]:
    """Candidates newer than the scan cursors. Returns
    ``(candidates, max_ns_seen, max_edit_ns_seen)`` so the caller can advance
    its cursors regardless of how many candidates qualified (the rule decides
    whether we ANSWER, not whether we re-scan — v1 principle, kept)."""
    out: list[Candidate] = []
    max_ns = after_ns
    max_edit = after_edit_ns
    for m in msgs:
        max_ns = max(max_ns, m.ns)
        edit_ns = int((m.edited or {}).get("ns", 0))
        max_edit = max(max_edit, edit_ns)
        if m.ns < joined_ns:
            continue  # from before I (last) joined — never replayed
        is_new = m.ns > after_ns
        # v2 edits are author-only, so "a human's edit" = a human's message
        is_fresh_edit = (
            edit_ns > after_edit_ns
            and not is_new
            and kinds.get(m.from_) is UserKind.HUMAN
        )
        if not (is_new or is_fresh_edit):
            continue
        reason = should_reply(rule, m, agent, kinds)
        if reason is None:
            continue
        out.append(
            Candidate(
                message=m,
                edit_ns=edit_ns,
                trigger_ns=edit_ns if (is_fresh_edit and not is_new) else m.ns,
                reason="edit" if (is_fresh_edit and not is_new) else reason,
            )
        )
    return out, max_ns, max_edit
