"""Supabase member provisioning (R84) — the mesh owner's admin tool.

    python -m agentbridge.transport.supabase_admin provision <username>
        [--root mesh2] [--home DIR] [--install | --out FILE]

Creates (or re-provisions, rotating the password of) one Supabase AUTH user
for a mesh member, with the authorization claims RLS policies trust in
``app_metadata`` — ADMIN-set, unlike ``user_metadata`` which the user can
edit themself and which must therefore never gate anything:

    app_metadata: { "ab_member": "<username>", "ab_roots": ["<root>"] }

Needs the SERVICE key (``supabase.env``) — provisioning is the owner's act.
The output is two env lines for the MEMBER's machine:

    SUPABASE_MEMBER_EMAIL=<username>@<root>.agentbridge.local
    SUPABASE_MEMBER_PASSWORD=<generated>

``--install`` appends them to this machine's own ``supabase.env`` (replacing
existing member lines); ``--out FILE`` writes them to a file instead; the
default prints them. Once a machine carries member credentials (plus the
publishable key) the transport signs in as that member and the SERVICE key
line can be REMOVED from that machine — the whole point of the round.

``revoke <username>`` deletes the auth user: their credential stops working
at the next token refresh (within the hour) and immediately for new
sign-ins. E2EE epoch rotation remains the guarantee for message bodies.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from .supabase import ENV_FILE, load_supabase_env

__all__ = ["main"]


def _admin_client(env: dict[str, str]):
    from supabase import create_client

    url = env.get("SUPABASE_URL", "")
    key = env.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise SystemExit("provisioning needs SUPABASE_URL and "
                         "SUPABASE_SECRET_KEY in supabase.env")
    return create_client(url, key)


def _member_email(username: str, root: str) -> str:
    return f"{username}@{root}.agentbridge.local"


def _find_user(admin, email: str):
    page = 1
    while True:
        users = admin.list_users(page=page, per_page=100)
        if not users:
            return None
        for u in users:
            if (u.email or "").lower() == email.lower():
                return u
        page += 1


def provision(env: dict[str, str], username: str, root: str) -> tuple[str, str]:
    """Create or re-provision (password rotation) one member. Returns
    ``(email, password)`` — the only time the password exists in plaintext."""
    client = _admin_client(env)
    email = _member_email(username, root)
    password = secrets.token_urlsafe(24)
    meta = {"ab_member": username, "ab_roots": [root]}
    existing = _find_user(client.auth.admin, email)
    if existing is not None:
        client.auth.admin.update_user_by_id(existing.id, {
            "password": password, "app_metadata": meta,
            "email_confirm": True,
        })
    else:
        client.auth.admin.create_user({
            "email": email, "password": password,
            "email_confirm": True, "app_metadata": meta,
        })
    return email, password


def revoke(env: dict[str, str], username: str, root: str) -> bool:
    client = _admin_client(env)
    existing = _find_user(client.auth.admin, _member_email(username, root))
    if existing is None:
        return False
    client.auth.admin.delete_user(existing.id)
    return True


def _write_env_lines(path: Path, email: str, password: str) -> None:
    """Append/replace the member lines in an env file, atomically."""
    lines: list[str] = []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if not ln.strip().startswith(("SUPABASE_MEMBER_EMAIL",
                                               "SUPABASE_MEMBER_PASSWORD"))]
    except OSError:
        pass
    lines += [f"SUPABASE_MEMBER_EMAIL={email}",
              f"SUPABASE_MEMBER_PASSWORD={password}"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-supabase-admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("provision", help="create/rotate one member credential")
    p.add_argument("username")
    p.add_argument("--root", default="")
    p.add_argument("--home", default="")
    p.add_argument("--install", action="store_true",
                   help="write the member lines into THIS machine's supabase.env")
    p.add_argument("--out", default="", help="write the env lines to a file")
    r = sub.add_parser("revoke", help="delete one member's auth user")
    r.add_argument("username")
    r.add_argument("--root", default="")
    r.add_argument("--home", default="")
    args = ap.parse_args(argv)

    home = Path(args.home) if args.home else None
    env = load_supabase_env(home)
    root = args.root
    if not root:
        # the remembered mesh root (supabase://<root>) names the default
        from ..core.config import load_app_config

        spec = str(load_app_config(home).get("mesh_root") or "")
        root = spec.split("://", 1)[1].strip("/ ") if "://" in spec else ""
    if not root:
        ap.error("no --root given and none remembered in config.json")

    if args.cmd == "revoke":
        gone = revoke(env, args.username.strip().lower(), root)
        print(f"@{args.username}: " + ("revoked" if gone else "no such member"))
        return 0

    email, password = provision(env, args.username.strip().lower(), root)
    if args.install:
        from ..core.config import DEFAULT_HOME

        path = (home or DEFAULT_HOME) / ENV_FILE
        _write_env_lines(path, email, password)
        print(f"member credential for @{args.username} installed into {path}")
        print("restart the app to sign in as this member; the "
              "SUPABASE_SECRET_KEY line can be removed once verified")
    elif args.out:
        _write_env_lines(Path(args.out), email, password)
        print(f"member credential for @{args.username} written to {args.out}")
    else:
        print(f"# add to <home>/supabase.env on @{args.username}'s machine")
        print(f"SUPABASE_MEMBER_EMAIL={email}")
        print(f"SUPABASE_MEMBER_PASSWORD={password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
