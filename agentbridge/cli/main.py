"""agentbridge CLI (R12) — one install, two entry points (account-model v2):

  python -m agentbridge.cli mcp   --root PATH --user NAME [--machine M] [--encrypt]
      run the MCP server on stdio for this identity (agents' default mode;
      no password — agents never authenticate, their identity is the machine)

  python -m agentbridge.cli send  --root ... --user ... --password ... CHAT BODY
  python -m agentbridge.cli read  --root ... --user ... --password ... CHAT
  python -m agentbridge.cli chats --root ... --user ... --password ...
      human-mode conveniences: a HUMAN identity must pass the password check
      (CLI auth is humans-only; account CREATION stays GUI-only)

  python -m agentbridge.cli watch --root ... --user ... [--json] [-- CMD ARGS...]
      the M3 notify hook (R42): stream this identity's notifications — one
      line per ping on stdout, and optionally run CMD per ping (argv after
      ``--``; fields arrive as AB_KIND/AB_CHAT/AB_CHAT_NAME/AB_FROM/
      AB_PREVIEW/AB_NS env vars). The R10 rules apply: only chats I'm a
      member of, never my own messages, mute + read-state respected. Agents
      run it bare (their identity is the machine, like mcp mode); a human
      identity passes the password check. The hook is REGISTERED BY RUNNING
      this process — nothing persists a command to auto-run later.

Account-management options (status, handle, privacy, ...) are deliberately
not here — GUI-only, per D19.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys

from ..core.errors import PermissionDenied
from ..core.models import UserKind
from ..mesh.notify import CommandHook, Notification
from ..mesh.service import Mesh


def _mesh(args) -> Mesh:
    return Mesh(
        args.root, args.user, args.machine or platform.node() or "cli",
        encrypt=args.encrypt,
    )


def _watch_line(note: Notification, *, json_mode: bool) -> str:
    """One stdout line per notification — the same field names the
    CommandHook exports (AB_*), so both consumers speak one schema."""
    if json_mode:
        return json.dumps({
            "kind": note.kind, "chat": note.chat_id, "chat_name": note.chat_name,
            "from": note.from_, "preview": note.preview, "ns": note.ns,
        }, ensure_ascii=False)
    where = note.chat_name or note.chat_id
    return f"[{where}] @{note.from_}: {note.preview}"


def _watch(mesh: Mesh, hook_argv: list[str], *, json_mode: bool, poll_s: float) -> int:
    """Blocking notification stream: sinks on the R10 notifier + this
    process's own sync cadence (the notifier only sees what sync ingests).
    Ctrl+C exits. No presence heartbeat — watching isn't being online."""
    def emit(note: Notification) -> None:
        line = _watch_line(note, json_mode=json_mode)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # cp1252 console meets an emoji preview
            print(line.encode("ascii", "replace").decode(), flush=True)

    mesh.notifier.add_sink(emit)
    if hook_argv:
        mesh.notifier.add_sink(CommandHook(hook_argv))
    mesh.start(heartbeat=False)
    try:
        mesh.sync.run(poll_s=poll_s)
    except KeyboardInterrupt:
        pass
    return 0


def _require_human_login(mesh: Mesh, password: str | None) -> None:
    kind = mesh.directory.kind(mesh.user)
    if kind is not UserKind.HUMAN:
        raise PermissionDenied("human commands need a member account (agents use mcp mode)")
    if not password or not mesh.accounts.verify_password(mesh.user, password):
        raise PermissionDenied("sign-in failed: check the username and password")
    if mesh.keystore.load(mesh.user) is None:  # unlock keys on this device
        mesh.accounts.unlock(password)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge")
    ap.add_argument("--root", required=True, help="path to the mesh2 root")
    ap.add_argument("--user", required=True)
    ap.add_argument("--machine", default="")
    ap.add_argument("--encrypt", action="store_true", help="use E2EE sealing")
    ap.add_argument("--password", default=None, help="human commands only")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("mcp", help="run the MCP server on stdio")
    p_send = sub.add_parser("send")
    p_send.add_argument("chat_id")
    p_send.add_argument("body")
    p_read = sub.add_parser("read")
    p_read.add_argument("chat_id")
    p_read.add_argument("--limit", type=int, default=20)
    sub.add_parser("chats")
    p_watch = sub.add_parser("watch", help="stream notifications; run CMD per ping")
    p_watch.add_argument("--json", action="store_true", dest="json_lines",
                         help="one JSON object per line instead of prose")
    p_watch.add_argument("--poll", type=float, default=3.0,
                         help="sync cadence in seconds (default 3)")
    p_watch.add_argument("hook", nargs=argparse.REMAINDER,
                         help="command to run per notification (after --)")

    args = ap.parse_args(argv)
    mesh = _mesh(args)
    try:
        if args.cmd == "mcp":
            from .server import build_mcp

            mesh.start(heartbeat=True)
            build_mcp(mesh).run()  # stdio transport; blocks until the client leaves
            return 0
        if args.cmd == "watch":
            # agents watch bare (mcp-mode policy); humans pass the password
            if mesh.directory.kind(mesh.user) is UserKind.HUMAN:
                _require_human_login(mesh, args.password)
            hook = args.hook[1:] if args.hook[:1] == ["--"] else args.hook
            return _watch(mesh, hook, json_mode=args.json_lines, poll_s=args.poll)

        _require_human_login(mesh, args.password)
        if args.cmd == "chats":
            for snap in mesh.membership.chats_for():
                print(f"{snap.id}\t{snap.kind.value}\t{snap.name}")
        elif args.cmd == "send":
            env = mesh.post(args.chat_id, args.body)
            mesh.outbox.flush_once()
            print(env.id)
        elif args.cmd == "read":
            mesh.sync.sync_once([args.chat_id])
            for m in mesh.messages_for(args.chat_id)[-args.limit:]:
                who = f"@{m.from_}"
                print(f"[{m.ts}] {who}: {m.body}" if not m.event
                      else f"[{m.ts}] * {m.event.get('type')}")
        return 0
    except PermissionDenied as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        mesh.close()


if __name__ == "__main__":
    raise SystemExit(main())
