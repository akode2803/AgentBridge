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

Assembly is fixed (persona → roster → task → capabilities → etiquette →
silence) so an overlay can reword blocks but not reorder the rails; the
silence block always carries the REAL sentinel (``responder.SILENCE``),
injected here — an edited pack can never desync the prompt from the parser.
A bad template (stray ``{``) degrades to its raw text: wording must never
break a run.

The transcript rendering (``context_text``) is factual machine formatting —
its headers are pack keys, the message lines are code. ``step_line`` words
the run feed's activity lines from the pack's ``activity`` map, replacing
R15's raw tool noise ("Running Grep: …" → "Searching for …").
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.config import DEFAULT_HOME
from ..core.models import Message, MsgKind
from .conversation import Delivery
from .responder import SILENCE

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
        else:
            parts.append(self.text("task_message", context_file=context_file))
        parts.append(self.text("capabilities", outbox=outbox))
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
        if delivery.kind == "timer":
            lines.append(self.text("context_wakeup", note=delivery.note))
        for t in delivery.triggers:
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
            lines.append(self.text("context_pinned", by=p.get("by"), body=body))
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
        template = acts.get(name.lower()) or acts.get("_fallback") or ""
        try:
            line = template.format(name=name, detail=detail)
        except (KeyError, IndexError, ValueError):
            line = template
        # a detail-less fill leaves a dangling phrase — trim it
        return line.replace("  ", " ").strip().rstrip(":,-") or None


def _short_detail(detail: str) -> str:
    """Paths shrink to their basename; everything else is trimmed."""
    d = " ".join(str(detail or "").split())
    if ("/" in d or "\\" in d) and " " not in d:
        d = d.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return d[:60]


def render_message(m: Message, agent: str) -> str:
    """One transcript line — factual, code-owned (moved from conversation.py)."""
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
        who_r = ("their own message" if rt["from"] == m.from_
                 else f"@{rt['from']}")
        rline = f' [replying to {who_r}: "{excerpt}"]'
    fwd = m.fwd or {}
    fline = f" [forwarded from @{fwd['from']}]" if fwd.get("from") else ""
    names = ", ".join(f.get("name", "") for f in (m.files or []))
    files = f"  [files: {names}]" if names else ""
    edited = " (edited)" if m.edited else ""
    return f"[{m.ts}] {who}:{fline}{rline}{edited} {m.body}{files}"


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
