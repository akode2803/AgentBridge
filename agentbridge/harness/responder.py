"""The invocation seam. The harness core never runs a model itself — it hands
a ``Delivery`` to a ``Responder`` and gets a ``Reply`` back. R16's model
registry provides real Responders (subprocess CLIs today, APIs later, one
contract per D8); tests and smoke runs inject scripted ones.

``clean_reply`` is the v1 output hygiene: the silence sentinel at either end
and leading narration paragraphs. R17 made the sentinel unmistakable — the
bare word NO_REPLY could silence an agent that merely *discussed* it — and
moved the reply-vs-silence wording into the prompt manager (prompt.py), which
injects ``SILENCE`` into the prompt so parser and prompt can never disagree.

``split_reply`` (R79, V78) is the multi-message contract: one run may post
several chat messages by placing ``MESSAGE_BREAK`` alone on its own line
between them. Same discipline as the sentinel — the marker is code-owned and
injected into the prompt by prompt.py, it only counts on a line of its own
(discussing it inline never splits), and a missing prompt block degrades to
single-message replies, never to a broken parse. Splitting happens AFTER
``clean_reply`` at the delivery seam (runner ``_deliver_reply``), so the
reply pipeline keeps owning threading, the answered-guard and the rate cap —
the reason a raw ``send`` tool stays out of the bridge (see bridge.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from .conversation import Delivery

__all__ = ["MESSAGE_BREAK", "Reply", "Responder", "RunStopped",
           "clean_reply", "split_reply", "SILENCE"]

OnStep = Callable[[str], None]  # live activity line -> the run feed

SILENCE = "<<<NO-REPLY>>>"
MESSAGE_BREAK = "<<<NEXT-MESSAGE>>>"
MAX_MESSAGE_PARTS = 4  # a turn is a short burst, not a broadcast channel


class RunStopped(RuntimeError):
    """The owner stopped this run mid-flight (R36). A deliberate outcome,
    not a failure: the runner posts no error notice, refunds the rate slot,
    and records the triggers as handled so they never re-fire."""

# leading paragraphs that are narration about the work, not the message —
# smaller models leak these despite the prompt ban (v1: seen live)
_NARRATION_RE = re.compile(
    r"^(wait[,;\s]|now i |i need to |i'll |i will |let me |reading |looking at "
    r"|checking |the latest message|the user |the request |first, i )", re.I)


@dataclass
class Reply:
    body: str = ""
    no_reply: bool = False
    steps: list[dict] = field(default_factory=list)   # [{text, ts}] task log
    timers: list[dict] = field(default_factory=list)  # [{in_s | at_ns, note}]
    files: list[str] = field(default_factory=list)    # local paths (R16 stages)
    # V53: owner-approved leave_chat — DEFERRED so the goodbye posts first;
    # the runner executes it after delivery
    leave_chat: bool = False


class Responder(Protocol):
    def respond(self, delivery: "Delivery",
                on_step: OnStep | None = None) -> Reply: ...


def clean_reply(text: str) -> tuple[str, bool]:
    """Returns ``(body, no_reply)``. Sentinel handling: a leading sentinel
    with content after it means "changed its mind, post the rest"; the
    sentinel as the final line means silence regardless of preceding
    narration. Matched case-insensitively — models half-follow."""
    s = (text or "").strip().strip("`'\"").strip()
    if not s:
        return "", False
    if s.upper().startswith(SILENCE):
        s = s[len(SILENCE):].strip("`'\"").strip()
        if not s:
            return "", True
    lines = s.splitlines()
    if lines and lines[-1].strip().strip("`'\".").upper() == SILENCE:
        return "", True
    paras = re.split(r"\n\s*\n", s)
    while len(paras) > 1 and _NARRATION_RE.match(paras[0].strip()):
        paras.pop(0)
    return "\n\n".join(paras).strip(), False


def split_reply(body: str, max_parts: int = MAX_MESSAGE_PARTS) -> list[str]:
    """The multi-message contract (V78): ``MESSAGE_BREAK`` alone on its own
    line ends one chat message and starts the next. Runs on the already
    ``clean_reply``-ed body. Tolerant the way the sentinel is (case, stray
    quoting); an inline mention mid-line never splits. Empty pieces drop;
    beyond ``max_parts`` the overflow merges into the last message so a
    runaway splitter can't turn one turn into a flood — nothing is lost."""
    parts, cur = [], []
    for line in (body or "").splitlines():
        if line.strip().strip("`'\".").upper() == MESSAGE_BREAK:
            parts.append("\n".join(cur).strip())
            cur = []
        else:
            cur.append(line)
    parts.append("\n".join(cur).strip())
    parts = [p for p in parts if p]
    if len(parts) > max_parts:
        parts[max_parts - 1:] = ["\n\n".join(parts[max_parts - 1:])]
    return parts or [""]
