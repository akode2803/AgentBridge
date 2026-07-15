"""Supabase membership tooling (R84, trust model v2.2 — account creation
IS membership).

No secret is ever transferred and nobody approves anything. A member's
credential is BORN on their own machine — self-signup with the PUBLISHABLE
key (public by design; the generated password goes straight into the local
``supabase.env`` and never exists anywhere else) — and the same act
SELF-CLAIMS their username in ``ab_members`` (first come first served, the
app directory's own rule). The mesh is as private as its bootstrap config:
URL + publishable key + root name — possession of the bootstrap is the
invite, like a group link.

    new member's machine:
        python -m agentbridge.transport.supabase_admin join <username>

    the owner (service key, kept OFFLINE), rarely:
        seed <username> [--install|--out FILE]   mint a credential for a
                                                 machine that can't run join
        revoke <username>                        eviction (row + auth user)

``join`` is also the primitive the app's account-creation flow calls when
the setup overhaul lands (V-2026-07-16: "create a user key during the
account creation") — creating an app account on a supabase mesh provisions
the Supabase identity in the same breath.

One-time dashboard prerequisites for ``join``: email signup enabled, email
confirmations OFF (the addresses are synthetic; an auth user with no
``ab_members`` row can see and touch nothing).
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from .supabase import ENV_FILE, load_supabase_env

__all__ = ["main", "join_mesh"]


def _member_email(username: str, root: str) -> str:
    return f"{username}@{root}.agentbridge.local"


def _client(env: dict[str, str], *, admin: bool):
    from supabase import create_client

    url = env.get("SUPABASE_URL", "")
    key = env.get("SUPABASE_SECRET_KEY" if admin
                  else "SUPABASE_PUBLISHABLE_KEY", "")
    if not url or not key:
        need = "SECRET" if admin else "PUBLISHABLE"
        raise SystemExit(f"needs SUPABASE_URL and SUPABASE_{need}_KEY "
                         f"in supabase.env")
    return create_client(url, key)


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


# ------------------------------------------------------------ the commands
def join_mesh(env: dict[str, str], username: str, root: str,
              env_path: Path) -> None:
    """The whole join, on the new member's machine: self-signup (the
    password is generated HERE, written into supabase.env, never shown)
    then self-claim the username. Fails cleanly if the name is taken (the
    PK arbitrates — same first-come rule as the app directory)."""
    client = _client(env, admin=False)
    email = _member_email(username, root)
    password = secrets.token_urlsafe(24)
    res = client.auth.sign_up({"email": email, "password": password})
    user = getattr(res, "user", None)
    if user is None:
        raise SystemExit("sign-up refused — is email signup enabled (and "
                         "confirmation OFF) in the project's Auth settings?")
    try:
        client.table("ab_members").insert({
            "root": root, "username": username, "uid": user.id,
        }).execute()
    except Exception as e:  # noqa: BLE001 — surface the taken-name case
        if "duplicate" in str(e).lower() or "23505" in str(e):
            raise SystemExit(f"@{username} is already claimed on {root}")
        raise
    _write_env_lines(env_path, email, password)
    print(f"@{username} joined {root}; credential installed into {env_path}")
    print("restart the app — the Connection panel should say "
          f"'Member ({username})'")


def seed(env: dict[str, str], username: str, root: str) -> tuple[str, str]:
    """Owner-only (service key): mint the auth user AND the members row for
    a machine that can't run ``join`` itself. Re-seeding rotates the
    password."""
    client = _client(env, admin=True)
    email = _member_email(username, root)
    password = secrets.token_urlsafe(24)
    existing = _find_user(client.auth.admin, email)
    if existing is not None:
        client.auth.admin.update_user_by_id(existing.id, {
            "password": password, "email_confirm": True})
        uid = existing.id
    else:
        res = client.auth.admin.create_user({
            "email": email, "password": password, "email_confirm": True})
        uid = res.user.id
    client.table("ab_members").upsert({
        "root": root, "username": username, "uid": uid,
    }).execute()
    return email, password


def revoke(env: dict[str, str], username: str, root: str) -> bool:
    """Owner-only: eviction — the members row AND the auth user. The
    evictee's chats stay closed either way (meta ACLs + E2EE epochs)."""
    client = _client(env, admin=True)
    client.table("ab_members").delete().eq("root", root) \
        .eq("username", username).execute()
    existing = _find_user(client.auth.admin, _member_email(username, root))
    if existing is None:
        return False
    client.auth.admin.delete_user(existing.id)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-supabase-admin")
    ap.add_argument("--root", default="")
    ap.add_argument("--home", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_join = sub.add_parser("join", help="this machine joins the mesh as <username>")
    p_join.add_argument("username")
    p_seed = sub.add_parser("seed", help="owner: mint a member credential (service key)")
    p_seed.add_argument("username")
    p_seed.add_argument("--install", action="store_true",
                        help="write the credential into THIS machine's supabase.env")
    p_seed.add_argument("--out", default="", help="write the env lines to a file")
    p_rev = sub.add_parser("revoke", help="owner: evict a member (service key)")
    p_rev.add_argument("username")
    args = ap.parse_args(argv)

    home = Path(args.home) if args.home else None
    env = load_supabase_env(home)
    root = args.root
    if not root:
        from ..core.config import load_app_config

        spec = str(load_app_config(home).get("mesh_root") or "")
        root = spec.split("://", 1)[1].strip("/ ") if "://" in spec else ""
    if not root:
        ap.error("no --root given and none remembered in config.json")
    from ..core.config import DEFAULT_HOME

    env_path = (home or DEFAULT_HOME) / ENV_FILE
    name = args.username.strip().lower()

    if args.cmd == "join":
        join_mesh(env, name, root, env_path)
    elif args.cmd == "seed":
        email, password = seed(env, name, root)
        if args.install:
            _write_env_lines(env_path, email, password)
            print(f"member credential for @{name} installed into {env_path}")
        elif args.out:
            _write_env_lines(Path(args.out), email, password)
            print(f"member credential for @{name} written to {args.out}")
        else:
            print(f"# add to <home>/supabase.env on @{name}'s machine")
            print(f"SUPABASE_MEMBER_EMAIL={email}")
            print(f"SUPABASE_MEMBER_PASSWORD={password}")
    elif args.cmd == "revoke":
        gone = revoke(env, name, root)
        print(f"@{name}: " + ("revoked" if gone else "members row cleared; "
                              "no auth user found"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
