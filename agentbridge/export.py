"""Plain-text chat export.

Reads through the READ MODEL as one member — membership, overlays (edits
applied, tombstones blank), and E2EE (this machine's keystore unseals) all
hold, so what exports is exactly what that member sees in the app.

Usage:
    python -m agentbridge.export --user aryan [--root ...] [--out DIR]
                                 [--chat ID ...] [--legacy-only]

``--legacy-only`` selects migrated v1 chats (ids without the ``-g`` genesis
marker) — the set slated for removal once their transcripts are on file.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .core.config import load_app_config
from .core.models import ChatKind, Message, MsgKind
from .mesh.events import is_legacy_chat_id
from .mesh.service import Mesh

__all__ = ["export_chat", "render_line", "main"]


def render_line(m: Message) -> str:
    if m.kind is MsgKind.INFO:
        ev = m.event or {}
        detail = ev.get("name") or ev.get("who") or ""
        return f"[{m.ts}] · {ev.get('type', 'event')}" + (f" {detail}" if detail else "")
    if m.deleted:
        return f"[{m.ts}] · a message was deleted"
    rt = m.reply_to or {}
    reply = ""
    if rt.get("from"):
        excerpt = (rt.get("body") or "").replace("\n", " ")[:80]
        reply = f' [replying to @{rt["from"]}: "{excerpt}"]'
    fwd = (m.fwd or {}).get("from")
    fline = f" [forwarded from @{fwd}]" if fwd else ""
    files = ", ".join(f.get("name", "") for f in (m.files or []))
    ftail = f"  [files: {files}]" if files else ""
    edited = " (edited)" if m.edited else ""
    body = (m.body or "").replace("\r\n", "\n")
    return f"[{m.ts}] @{m.from_}:{fline}{reply}{edited} {body}{ftail}"


def export_chat(mesh: Mesh, chat_id: str, out_dir: Path) -> Path:
    snap = mesh.snapshot(chat_id)
    msgs = mesh.messages_for(chat_id)
    name = snap.name or chat_id
    if snap.kind is ChatKind.DM:
        other = next((m for m in snap.members if m != mesh.user), "")
        name = f"DM with @{other}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "chat"
    out = out_dir / f"{slug}-{chat_id}.txt"
    lines = [
        f"# {name}",
        f"# chat: {chat_id} ({snap.kind.value})",
        f"# members: {', '.join('@' + m for m in sorted(snap.members))}",
        f"# exported by @{mesh.user}; {len(msgs)} entries",
        "",
        *(render_line(m) for m in msgs),
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agentbridge-export",
        description="Export chat transcripts to plain text (one member's view)")
    ap.add_argument("--user", required=True, help="export as this member")
    ap.add_argument("--root", default="", help="mesh root (default: remembered)")
    ap.add_argument("--home", default="",
                    help="local home holding this member's keys")
    ap.add_argument("--machine", default="export")
    ap.add_argument("--out", default="chat-exports")
    ap.add_argument("--chat", action="append", default=[],
                    help="specific chat id (repeatable; default: all mine)")
    ap.add_argument("--legacy-only", action="store_true",
                    help="only migrated v1 chats (no -g genesis marker)")
    args = ap.parse_args(argv)

    home = Path(args.home) if args.home else None
    root = args.root or load_app_config(home).get("mesh_root")
    if not root:
        ap.error("no --root given and none remembered in config.json")

    mesh = Mesh(Path(root), args.user, args.machine, encrypt=True,
                home=home) if home else Mesh(Path(root), args.user, args.machine,
                                             encrypt=True)
    try:
        mesh.sync.sync_once()
        chats = args.chat or [s.id for s in mesh.chats_for()]
        if args.legacy_only:
            chats = [c for c in chats if is_legacy_chat_id(c)]
        if not chats:
            print("nothing to export")
            return 0
        out_dir = Path(args.out)
        for chat_id in chats:
            path = export_chat(mesh, chat_id, out_dir)
            print(f"exported {chat_id} -> {path}")
        return 0
    finally:
        mesh.close()


if __name__ == "__main__":
    sys.exit(main())
