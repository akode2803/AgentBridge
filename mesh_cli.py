#!/usr/bin/env python3
"""AgentBridge mesh CLI — chat from a terminal, a script, or a Claude session.

The GUI is for humans; this is the power-user and automation surface. It
speaks straight to the mesh data layer (mesh.py), so everything it does is
identical to the app: same chats, same audit trail, same rules.

Identity: --as <username> (or MESH_USER env var). This is cooperative trust,
like every write to the shared folder — the folder ACL is the real boundary.
Humans' password auth gates the GUI login, not this CLI.

Examples:
    python mesh_cli.py chats --as aryan
    python mesh_cli.py read "MMM Analysis" --as aryan --tail 30
    python mesh_cli.py post "MMM Analysis" "@coco validate the joins" --as aryan
    python mesh_cli.py post mmm-analysis-32b414 "see attached" --attach out.csv --as claude
    python mesh_cli.py create "Order Fulfilment" --members claude,coco --as aryan
    python mesh_cli.py users

The shared folder is read from %USERPROFILE%\\.agentbridge\\config.json
(shared_dir), or pass --shared explicitly.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mesh import Mesh, MeshError, read_json  # noqa: E402


def say(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(str(msg).encode(enc, "replace").decode(enc))


def find_shared(explicit):
    if explicit:
        return Path(explicit)
    cfg = read_json(Path.home() / ".agentbridge" / "config.json") or {}
    if cfg.get("shared_dir"):
        return Path(cfg["shared_dir"])
    raise SystemExit("No shared folder: pass --shared or configure the bridge "
                     "(~/.agentbridge/config.json needs shared_dir).")


def resolve_chat(m, ref):
    """Accept a chat id, an exact name, or an unambiguous name prefix."""
    if m.get_chat(ref):
        return ref
    matches = []
    for d in sorted(m.chats_dir.iterdir()) if m.chats_dir.is_dir() else []:
        meta = read_json(d / "meta.json")
        if not meta:
            continue
        name = (meta.get("name") or "").lower()
        if name == ref.lower():
            return meta["id"]
        if name.startswith(ref.lower()) or meta["id"].startswith(ref):
            matches.append(meta)
    if len(matches) == 1:
        return matches[0]["id"]
    if not matches:
        raise SystemExit(f"No chat matching '{ref}'. Try: mesh_cli.py chats")
    names = ", ".join(f"{c['name']} ({c['id']})" for c in matches)
    raise SystemExit(f"'{ref}' is ambiguous: {names}")


def cmd_users(m, args):
    for u in m.users().values():
        if u["kind"] == "agent":
            owners = ", ".join(u.get("owners") or [])
            rule = (u.get("settings") or {}).get("default_rule", "tagged")
            say(f"@{u['username']:<12} agent   {u.get('display', ''):<12} "
                f"owners: {owners:<20} default rule: {rule}")
        else:
            say(f"@{u['username']:<12} human   {u.get('display', '')}")


def cmd_chats(m, args):
    for meta in m.chats_for(args.as_user, include_archived=args.archived):
        last = meta.get("last")
        tail = (f" — {last['from']}: {last['body'][:60]}" if last else "")
        arch = " [archived]" if meta.get("archived") else ""
        unread = m.unread_count(meta["id"], args.as_user)
        badge = f" ({unread} unread)" if unread else ""
        say(f"{meta['id']:<32} {meta['name']}{arch}{badge}{tail}")


def cmd_read(m, args):
    chat_id = resolve_chat(m, args.chat)
    for msg in m.messages(chat_id, tail=args.tail):
        files = ("  [files: " + ", ".join(f["name"] for f in msg["files"]) + "]"
                 if msg.get("files") else "")
        say(f"--- @{msg['from']} ({msg.get('kind', '?')})  {msg.get('ts', '')}")
        say((msg.get("body") or "") + files)
        say("")
    if args.mark:
        m.mark_read(chat_id, args.as_user)


def cmd_post(m, args):
    chat_id = resolve_chat(m, args.chat)
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8-sig")
    msg = m.post(chat_id, args.as_user, body or "", attachments=args.attach)
    m.mark_read(chat_id, args.as_user)
    say(f"[posted] {msg['id']} to {chat_id}"
        + (f" tags={msg['tags']}" if msg["tags"] else ""))


def cmd_create(m, args):
    members = [x.strip() for x in (args.members or "").split(",") if x.strip()]
    meta = m.create_chat(args.name, args.as_user, members=members)
    say(f"[created] {meta['id']}  '{meta['name']}'  members: "
        + ", ".join(meta["members"]))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--shared", default=None,
                        help="shared folder (default: from ~/.agentbridge/config.json)")
    common.add_argument("--as", dest="as_user",
                        default=os.environ.get("MESH_USER"),
                        help="act as this username (or MESH_USER env var)")
    ap = argparse.ArgumentParser(
        prog="mesh_cli.py", parents=[common],
        description="AgentBridge mesh — terminal client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("users", help="list humans and agents", parents=[common])

    p = sub.add_parser("chats", help="list chats visible to --as", parents=[common])
    p.add_argument("--archived", action="store_true", help="include archived")

    p = sub.add_parser("read", help="print a chat's transcript", parents=[common])
    p.add_argument("chat", help="chat id, name, or unambiguous prefix")
    p.add_argument("--tail", type=int, default=20)
    p.add_argument("--mark", action="store_true", help="update your read cursor")

    p = sub.add_parser("post", help="post a message (and files) to a chat",
                       parents=[common])
    p.add_argument("chat", help="chat id, name, or unambiguous prefix")
    p.add_argument("body", nargs="?", default=None)
    p.add_argument("--body-file", default=None, help="read the body from a file")
    p.add_argument("--attach", action="append", default=[],
                   help="attach a file (repeatable)")

    p = sub.add_parser("create", help="create a chat", parents=[common])
    p.add_argument("name")
    p.add_argument("--members", default="",
                   help="comma-separated usernames to add")

    args = ap.parse_args()
    m = Mesh(find_shared(args.shared))
    if not m.exists():
        raise SystemExit("No mesh at that shared folder — start it from the "
                         "app's Chats page first.")
    if args.cmd != "users" and not args.as_user:
        raise SystemExit("Say who you are: --as <username> (or set MESH_USER).")
    if args.cmd != "users" and not m.get_user(args.as_user):
        raise SystemExit(f"Unknown user @{args.as_user}. See: mesh_cli.py users")

    try:
        {"users": cmd_users, "chats": cmd_chats, "read": cmd_read,
         "post": cmd_post, "create": cmd_create}[args.cmd](m, args)
    except MeshError as e:
        raise SystemExit(f"[mesh] {e}")


if __name__ == "__main__":
    main()
