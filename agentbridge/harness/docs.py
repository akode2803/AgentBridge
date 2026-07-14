"""Tool documentation (R43, Q7/Q11) — one data file owns every per-tool word.

``prompts/tooldocs.json`` carries, per tool: the ``ask`` verb phrase the
owner's permission popup shows ("wants to write a file", never a raw tool
id), the ``short`` one-liner, and the ``long`` manual entry; plus ``guides``
— conceptual entries (workspace, memory, etiquette, …). The bridge's
``read_docs`` tool serves the catalog and the entries, so the run prompt
stays lean (Q7: documentation is a tool, not inline context) and an agent
can quote its own manual when a member asks what it can do (Q11).

Resolution mirrors the prompt pack: the shipped file, overlaid by
``<home>/prompts/tooldocs.json`` — an owner rewords or extends entries
without touching code.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.config import DEFAULT_HOME
from .prompt import _friendly_tool_name

__all__ = ["ToolDocs"]

PACK_FILE = Path(__file__).resolve().parent / "prompts" / "tooldocs.json"


def _load_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class ToolDocs:
    def __init__(self, data: dict) -> None:
        self.tools: dict[str, dict] = {
            str(k).lower(): v for k, v in (data.get("tools") or {}).items()
            if isinstance(v, dict)
        }
        self.guides: dict[str, dict] = {
            str(k).lower(): v for k, v in (data.get("guides") or {}).items()
            if isinstance(v, dict)
        }

    @classmethod
    def load(cls, home: Path | None = None) -> "ToolDocs":
        """Shipped entries, overlaid per-section by the home file (an
        override replaces a tool's whole entry, never merges inside it —
        partial entries would silently drop the fields they omit)."""
        data = _load_json(PACK_FILE)
        override = _load_json((home or DEFAULT_HOME) / "prompts" / "tooldocs.json")
        for section in ("tools", "guides"):
            merged = dict(data.get(section) or {})
            merged.update(override.get(section) or {})
            data[section] = merged
        return cls(data)

    # -------------------------------------------------------------- ask lane
    def ask_phrase(self, tool: str) -> str:
        """The popup's verb phrase for a tool — 'write a file' — falling back
        to a humanized name ('use search issues (github)') so a raw tool id
        never reaches the owner."""
        entry = self.tools.get(str(tool or "").lower())
        phrase = str((entry or {}).get("ask") or "").strip()
        if phrase:
            return phrase
        return f"use {_friendly_tool_name(tool)}" if tool else ""

    # ------------------------------------------------------------- docs lane
    def catalog(self) -> str:
        """read_docs() — the table of contents: every guide and documented
        tool with its one-liner."""
        lines = ["Your AgentBridge manual. Call read_docs(<name>) for any "
                 "entry below.", "", "Guides:"]
        for name, g in sorted(self.guides.items()):
            lines.append(f"- {name}: {str(g.get('short') or '').strip()}")
        lines.append("")
        lines.append("Tools:")
        for name, t in sorted(self.tools.items()):
            short = str(t.get("short") or "").strip()
            if short:  # inner-CLI tools carry only an ask phrase — not yours
                lines.append(f"- {name}: {short}")
        return "\n".join(lines)

    def topic(self, name: str) -> str:
        """read_docs(name) — the full entry for a guide or tool. Accepts the
        bare name or the mcp-prefixed spelling."""
        key = str(name or "").strip().lower()
        if key.startswith("mcp__"):
            key = key.split("__")[-1]
        entry = self.guides.get(key) or self.tools.get(key)
        if entry:
            body = str(entry.get("long") or entry.get("short") or "").strip()
            if body:
                return body
        near = [k for k in [*self.guides, *self.tools] if key and key in k]
        hint = f" Did you mean: {', '.join(sorted(near))}?" if near else ""
        return (f"No entry named {key!r}.{hint} Call read_docs() with no "
                f"argument for the full list.")
