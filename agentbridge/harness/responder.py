"""The invocation seam. The harness core never runs a model itself — it hands
a ``Delivery`` to a ``Responder`` and gets a ``Reply`` back. R16's model
registry provides real Responders (subprocess CLIs today, APIs later, one
contract per D8); tests and smoke runs inject scripted ones.

``clean_reply`` is the v1 output hygiene, ported: the NO_REPLY sentinel at
either end and leading narration paragraphs (R17 replaces the sentinel with
an unmistakable marker and moves reply-vs-silence into the prompt manager).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from .conversation import Delivery

__all__ = ["Reply", "Responder", "clean_reply", "NO_REPLY"]

NO_REPLY = "NO_REPLY"

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


class Responder(Protocol):
    def respond(self, delivery: "Delivery") -> Reply: ...


def clean_reply(text: str) -> tuple[str, bool]:
    """Returns ``(body, no_reply)``. Sentinel handling: leading NO_REPLY with
    content after it means "changed its mind, post the rest"; NO_REPLY as the
    final line means silence regardless of preceding narration."""
    s = (text or "").strip().strip("`'\"").strip()
    if not s:
        return "", False
    if s.upper().startswith(NO_REPLY):
        s = s[len(NO_REPLY):].strip("`'\"").strip()
        if not s:
            return "", True
    lines = s.splitlines()
    if lines and lines[-1].strip().strip("`'\".").upper() == NO_REPLY:
        return "", True
    paras = re.split(r"\n\s*\n", s)
    while len(paras) > 1 and _NARRATION_RE.match(paras[0].strip()):
        paras.pop(0)
    return "\n\n".join(paras).strip(), False
