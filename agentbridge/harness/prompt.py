"""The prompt manager (R17) — every word an agent is told is DATA, not code.

The wording lives in a JSON pack (``prompts/default.json``), resolved in
three layers, each overlaying the previous key-by-key (the ``activity`` map
merges one level deep, like every per-key dict in this codebase):

1. the shipped pack (this package's ``prompts/`` dir);
2. ``<home>/prompts/default.json`` — the machine's owner rewords anything
   without touching code;
3. the agent's own ``agent.harness["prompts"]`` dict — per-agent persona or
   etiquette tweaks stay config, never a fork of the runner (one harness,
   all agents).

Assembly is fixed (persona → roster → task → capabilities → multi_message →
etiquette → silence) so an overlay can reword blocks but not reorder the
rails; the silence block always carries the REAL sentinel
(``responder.SILENCE``) and the multi_message block the REAL break marker
(``responder.MESSAGE_BREAK``), injected here — an edited pack can never
desync the prompt from the parser.
A bad template (stray ``{``) degrades to its raw text: wording must never
break a run.

The transcript rendering (``context_text``) is factual machine formatting —
its headers are pack keys, the message lines are code. ``step_line`` words
the run feed's activity lines from the pack's ``activity`` map, replacing
R15's raw tool noise ("Running Grep: …" → "Searching for …").
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..core.config import DEFAULT_HOME
from ..core.models import Message, MsgKind
from ..core.timekit import utcnow_iso
from .conversation import Delivery
from .responder import MESSAGE_BREAK, SILENCE

__all__ = ["PromptManager", "PromptPack", "render_message"]

PACK_DIR = Path(__file__).resolve().parent / "prompts"
TRANSCRIPT_TAIL = 30

# the one rail that survives even a gutted pack: without the silence
# instruction the sentinel parser would eat replies the model never chose
_SILENCE_FALLBACK = (
    "If the new messages need no response from you, answer with exactly "
    "{sentinel} and nothing else, and no message will be posted."
)


def _overlay(base: dict, extra: dict) -> dict:
    """Key-level overlay; dict values merge one level deep."""
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


class PromptPack:
    """One resolved wording pack, bound to an agent's run."""

    def __init__(self, data: dict) -> None:
        self.data = data

    def text(self, key: str, **fill) -> str:
        template = self.data.get(key)
        if not isinstance(template, str) or not template:
            return ""
        try:
            return template.format(**fill)
        except (KeyError, IndexError, ValueError):
            return template  # a broken template never breaks a run

    # ------------------------------------------------------------ the prompt
    def prompt(self, delivery: Delivery, acc, *, context_file, outbox,
               bridge: bool = False) -> str:
        roster = "; ".join(
            f"@{r['name']} ({r.get('desc', '')})" for r in delivery.roster)
        parts = [self.text(
            "persona",
            display=(acc.display if acc else delivery.agent) or delivery.agent,
            agent=delivery.agent, chat_name=delivery.chat_name,
        )]
        about = (acc.about if acc else "") or ""
        if about:
            parts.append(self.text("persona_about", about=about))
        parts.append(self.text("roster", roster=roster))
        if delivery.kind == "timer":
            parts.append(self.text(
                "task_timer", note=(delivery.note or "").replace("'", ""),
                context_file=context_file))
        elif delivery.triggers and all(
                t.reason == "reaction" for t in delivery.triggers):
            # V92: a pure reaction nudge is an FYI, not a request — a pack
            # without the key degrades to the normal message task
            parts.append(self.text("task_reaction", context_file=context_file)
                         or self.text("task_message",
                                      context_file=context_file))
        else:
            parts.append(self.text("task_message", context_file=context_file))
        parts.append(self.text("capabilities", outbox=outbox))
        # V78: the break marker is injected like the sentinel; a pack that
        # drops this block simply degrades to single-message replies
        parts.append(self.text("multi_message", delimiter=MESSAGE_BREAK))
        if bridge:  # only when the run really has the harness channel
            parts.append(self.text("bridge"))
        parts.append(self.text("etiquette"))
        silence = self.text("silence", sentinel=SILENCE) \
            or _SILENCE_FALLBACK.format(sentinel=SILENCE)
        parts.append(silence)
        return " ".join(p for p in parts if p)

    # ----------------------------------------------------------- context.md
    def context_text(self, delivery: Delivery,
                     staged: dict[str, str] | None = None) -> str:
        members = "; ".join(
            f"@{r['name']}{' (you)' if r.get('you') else ''}"
            f" — {r.get('desc', '')}" for r in delivery.roster)
        lines = [
            self.text("context_header", chat_name=delivery.chat_name,
                      chat_kind=delivery.chat_kind),
            self.text("context_members", members=members),
        ]
        # V117: time-grounding, code-built like the trigger lines (never
        # per-prompt copy). The stamp matches the transcript's own [ts]
        # format so the model can compare directly; the caution appears
        # only when the newest message is genuinely old — a wakeup firing
        # late or an agent that was offline otherwise reads a stale
        # transcript as "now" (a "tomorrow" answered with yesterday's
        # tomorrow, per the live report).
        lines.append(f"Current time: {utcnow_iso()}.")
        last_ns = max((m.ns for m in delivery.transcript if m.ns), default=0)
        age_h = (time.time_ns() - last_ns) / 3.6e12 if last_ns else 0.0
        if age_h >= 1.0:
            age = (f"about {age_h / 24:.0f} days" if age_h >= 48
                   else "about an hour" if round(age_h) <= 1
                   else f"about {age_h:.0f} hours")
            lines.append(
                f"Note: the conversation below last moved {age} ago — you "
                "may have been offline. Check anything time-sensitive "
                '(deadlines, "today"/"tomorrow", timers) against the '
                "current time before you act.")
        # V54 (parity c): the chat facts a human reads in the info pane —
        # genesis + the group's permission levels (factual, code-built like
        # the trigger lines)
        if delivery.created_by:
            lines.append(f"Created by @{delivery.created_by}"
                         + (f" on {delivery.created_at}"
                            if delivery.created_at else ""))
        if delivery.chat_kind == "group" and delivery.permissions:
            lines.append("Group permissions: " + ", ".join(
                f"{k}={v}" for k, v in sorted(delivery.permissions.items())))
        # V107: the newest outcome in THIS chat was a run that never posted —
        # say so up top, or the agent re-attempts what its member just refused
        # (a stop means "stop doing that", and until now the agent never knew).
        last = delivery.recent_runs[-1] if delivery.recent_runs else {}
        if last.get("state") in ("stopped", "interrupted"):
            doing = (last.get("doing") or "").strip()
            lines.append(self.text(
                "context_" + last["state"],
                when=last.get("finished", ""),
                doing=f" (while: {doing})" if doing else ""))
        if delivery.kind == "timer":
            lines.append(self.text("context_wakeup", note=delivery.note))
        for t in delivery.triggers:
            if t.reason == "reaction":
                # V92: say WHAT was reacted to — the breadcrumb itself renders
                # as nothing in the transcript. Emoji is member input: cap +
                # single-line it (the V54 anti-smuggling rule).
                ev = t.message.event or {}
                emoji = " ".join(str(ev.get("emoji") or "").split())[:8]
                target = next((m for m in delivery.transcript
                               if m.id == ev.get("msg_id")), None)
                excerpt = (target.body or "").replace("\n", " ")[:120] \
                    if target else ""
                lines.append(
                    f"Trigger (reaction): @{t.sender} reacted {emoji} to "
                    + (f'your message "{excerpt}"' if excerpt
                       else "one of your messages"))
                continue
            bits = [f"Trigger ({t.reason}): @{t.sender}"]
            if t.sender_status and t.sender_status.get("state") not in (
                    None, "available"):
                bits.append(f"status={t.sender_status['state']}")
            if t.sender_presence is not None:
                bits.append("online" if t.sender_presence.get("online")
                            else "last seen "
                            f"{t.sender_presence.get('last_seen') or 'unknown'}")
            lines.append(" ".join(bits))
        for p in delivery.pins:
            body = (p.get("body") or "").replace("\n", " ")[:160]
            # carry the pin's message id (R33) so the agent can actually
            # unpin_message it — a pinned message older than the transcript
            # tail has its id nowhere else in the context
            lines.append(self.text("context_pinned", by=p.get("by"), body=body,
                                   id=p.get("id", "")))
        # V87 short-term memory: the agent's own recent runs HERE (factual,
        # code-built like the trigger lines; the member-facing wording of
        # each note was already written by the feed)
        if delivery.recent_runs:
            lines.append(self.text("context_runs"))
            for r in delivery.recent_runs:
                note = " ".join(str(r.get("note") or "").split())[:160]
                doing = " ".join(str(r.get("doing") or "").split())[:120]
                lines.append(
                    f"- [{r.get('finished', '')}] {r.get('state', '')}: {note}"
                    + (f" (while: {doing})" if doing else ""))
        # V87 timer list: this chat's pending wake-ups, with the ids
        # cancel_timer takes; other chats contribute only a count
        if delivery.timers:
            from .timers import when_local
            lines.append(self.text("context_timers"))
            for t in delivery.timers:
                note = " ".join(str(t.get("note") or "").split())[:160]
                try:
                    fires = when_local(int(t.get("at_ns", 0)))
                except (ValueError, OSError, OverflowError):
                    fires = "unknown"
                lines.append(f"- (id {t.get('id', '')}) fires {fires} — {note}")
        if delivery.other_timers:
            lines.append(self.text("context_timers_other",
                                   n=delivery.other_timers))
        if delivery.recalled:          # retrieval hits from beyond the tail
            lines.append(self.text("context_recall"))
            for m in delivery.recalled:
                lines.append(render_message(m, delivery.agent))
            lines.append(self.text("context_recent"))
        for m in delivery.transcript[-TRANSCRIPT_TAIL:]:
            lines.append(render_message(m, delivery.agent))
        if staged:
            notes = "\n".join(f"- {name} -> read it at {rel}"
                              for name, rel in sorted(staged.items()))
            lines.append("\n" + self.text("context_staged") + "\n" + notes)
        return "\n".join(x for x in lines if x)

    # ------------------------------------------------------------- the feed
    def step_line(self, kind: str, name: str = "", detail: str = "") -> str | None:
        """One clean activity line for the run feed (None = say nothing)."""
        acts = self.data.get("activity") or {}
        if kind == "text":
            if SILENCE in (detail or "").upper():
                return None  # the sentinel is a decision, not an activity
            return detail or None
        if kind in ("init", "result"):
            return acts.get(f"_{kind}") or None
        if kind != "tool":
            return None
        detail = _short_detail(detail)
        # the run's own context/reply files are plumbing — members should read
        # "the conversation", not an internal filename (R36)
        if detail in ("context.md", "reply.md"):
            detail = "the conversation" if detail == "context.md" else "the reply"
        template = acts.get(name.lower()) or ""
        if not template:
            # an unmapped tool must never leak its raw id ("mcp__x__do_thing"):
            # humanize it — "do thing (x)" — before the generic fallback (R36)
            template = acts.get("_fallback") or ""
            name = _friendly_tool_name(name)
        try:
            line = template.format(name=name, detail=detail)
        except (KeyError, IndexError, ValueError):
            line = template
        line = line.replace("  ", " ").strip().rstrip(":,-")
        # a detail-less fill leaves a dangling connector ("Searching for",
        # "Searching the web for") — drop it so the line still reads clean
        if not detail:
            line = re.sub(r"\s+(for|to|the|a|in|on|at|of|from)$", "",
                          line, flags=re.IGNORECASE).strip()
        return line or None


def _friendly_tool_name(name: str) -> str:
    """'mcp__github__search_issues' -> 'search issues (github)';
    'SomeCamelTool' -> 'some camel tool'. Raw tool ids never reach members."""
    n = str(name or "")
    if n.lower().startswith("mcp__"):
        parts = n.split("__", 2)
        server = parts[1] if len(parts) > 1 else ""
        tool = parts[2] if len(parts) > 2 else server
        tool = tool.replace("_", " ").replace("-", " ").strip()
        return f"{tool} ({server})" if server and len(parts) > 2 else tool
    n = re.sub(r"(?<!^)(?=[A-Z])", " ", n).replace("_", " ")
    return " ".join(n.split()).lower() or "a tool"


def _short_detail(detail: str) -> str:
    """Paths shrink to their basename; everything else is trimmed."""
    d = " ".join(str(detail or "").split())
    if ("/" in d or "\\" in d) and " " not in d:
        d = d.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return d[:60]


def _safe_body(body: str) -> str:
    """Neutralize transcript-line injection (R25): a real entry starts at
    column 0 with ``[<ts>] (id …) @who:`` — so a sender embedding newlines in
    their body could otherwise fabricate extra lines, including a forged
    ``(id m-…) @owner: approved …`` that the model reads as an instruction.
    Indenting every continuation line keeps a body's own lines visibly nested
    under its one entry and off column 0. (reply-quotes and pins already strip
    newlines; the body is kept multi-line for code/lists, just indented.)"""
    return (body or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\n    ")


def render_message(m: Message, agent: str) -> str:
    """One transcript line — factual, code-owned (moved from conversation.py).
    The message id rides every line: the chat tools (pin/star/react/forward)
    take ids, and a model can only pass what it can see (R19 — a live probe
    invented an id and got an opaque backend error)."""
    if m.kind is MsgKind.INFO:
        ev = m.event or {}
        if ev.get("type") == "reaction":
            return ""  # V50 breadcrumb — pure notification fuel, not context
        return f"[{m.ts}] · {ev.get('type', 'event')}"
    if m.deleted:
        return f"[{m.ts}] · a message was deleted"
    if m.undecrypted:
        # R66: sealed to a key this device hasn't synced yet — say so rather
        # than showing a blank line the model could misread as an empty ping
        return f"[{m.ts}] · a message from @{m.from_} hasn't synced here yet"
    who = f"@{m.from_}" + (" (you)" if m.from_ == agent else "")
    who = f"(id {m.id}) {who}" if m.id else who
    rt = m.reply_to or {}
    rline = ""
    if rt.get("from"):
        excerpt = (rt.get("body") or "").replace("\n", " ")[:120]
        who_r = ("their own message" if rt["from"] == m.from_
                 else f"@{rt['from']}")
        rline = f' [replying to {who_r}: "{excerpt}"]'
    fwd = m.fwd or {}
    fline = f" [forwarded from @{fwd['from']}]" if fwd.get("from") else ""
    # V54 (parity c): reactions become visible to the agent — one bracketed
    # suffix per message. Emoji strings are member input: cap + single-line
    # them so a hostile "emoji" can't smuggle transcript lines.
    rx = ""
    if m.reactions:
        parts = []
        for emoji, users in sorted(m.reactions.items()):
            e = " ".join(str(emoji).split())[:8]
            parts.append(f"{e} by {', '.join('@' + u for u in sorted(users))}")
        rx = f" [reactions: {'; '.join(parts)}]"
    names = ", ".join(f.get("name", "") for f in (m.files or []))
    files = f"  [files: {names}]" if names else ""
    edited = " (edited)" if m.edited else ""
    return f"[{m.ts}] {who}:{fline}{rline}{edited} {_safe_body(m.body)}{files}{rx}"


class PromptManager:
    """Loads the pack layers once; binds per-agent overrides per run."""

    def __init__(self, home: Path | None = None) -> None:
        base: dict = {}
        for f in (PACK_DIR / "default.json",
                  (home or DEFAULT_HOME) / "prompts" / "default.json"):
            try:
                base = _overlay(base, json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # a bad overlay never blocks the shipped wording
        self.base = base

    def for_agent(self, acc) -> PromptPack:
        overrides = {}
        if acc is not None and acc.agent:
            o = (acc.agent.harness or {}).get("prompts")
            overrides = o if isinstance(o, dict) else {}
        return PromptPack(_overlay(self.base, overrides))
