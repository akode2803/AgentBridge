r"""Supabase membership tooling (R84, trust model v2.2 — account creation
IS membership).

No secret is ever transferred and nobody approves anything. A member's
credential is BORN on their own machine — self-signup with the PUBLISHABLE
key (public by design; the generated password goes straight into the local
``supabase.env`` and never exists anywhere else) — and the same act
SELF-CLAIMS their username in ``ab_members`` (first come first served, the
app directory's own rule). The mesh is as private as its bootstrap config:
URL + publishable key + root name — possession of the bootstrap is the
invite, like a group link.

Run from the repo root with the project's OWN venv Python — bare
``python`` is the hermes venv, which has no ``supabase`` package (and from
a subdirectory can't import ``agentbridge`` either):

    new member's machine:
        .\.venv\Scripts\python.exe -m agentbridge.transport.supabase_admin join <username>

    the owner (service key, kept OFFLINE), rarely:
        ...supabase_admin seed <username> [--install|--out FILE]
                                          mint a credential for a machine
                                          that can't run join itself
        ...supabase_admin revoke <username>   eviction (row + auth user)

``join`` is also the primitive the app's account-creation flow calls when
the setup overhaul lands (V-2026-07-16: "create a user key during the
account creation") — creating an app account on a supabase mesh provisions
the Supabase identity in the same breath.

One-time dashboard prerequisites for ``join``: email signup enabled, email
confirmations OFF (the addresses are synthetic; an auth user with no
``ab_members`` row can see and touch nothing).

A sealed root refuses direct ``ab_members`` writes, so ``join`` and ``seed``
claim there through the typed, receipt-bearing RPC (``member_claim.py``);
an unsealed root keeps the legacy insert. Eviction under a seal needs a typed
operator flow that does not exist yet, so ``revoke`` refuses it unchanged.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from ..core.config import DEFAULT_HOME, load_app_config, validate_root_spec
from ..core.errors import ConfigError
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


_SEALED = "legacy provider writes are sealed"


def _sealed_claim(env: dict[str, str], email: str, password: str, root: str,
                  username: str, uid: str, *, rerun: str) -> str:
    """Claim as this member through the typed RPC when the root is sealed.

    Returns ``unsealed`` when the root still takes the legacy insert. Every
    failure names ``rerun``, which replays the same operation.
    """
    from .member_claim import MemberClaimError, claim_member, open_member_rpc

    # Never let a service key stand in for the member: the claim is the
    # member's own act, and typed RPCs refuse a service session anyway.
    member_env = {k: v for k, v in env.items() if k != "SUPABASE_SECRET_KEY"}
    member_env.update(SUPABASE_MEMBER_EMAIL=email, SUPABASE_MEMBER_PASSWORD=password)
    try:
        rpc = open_member_rpc(member_env, root, str(uid))
    except Exception:  # noqa: BLE001 - never echo provider/auth details
        raise SystemExit(f"could not open @{username}'s session for the "
                         f"claim — {rerun}") from None
    try:
        return claim_member(rpc, username)
    except MemberClaimError as exc:
        if exc.reason == "taken":
            raise SystemExit(str(exc)) from None
        raise SystemExit(f"claim did not finish ({exc}) — {rerun}") from None


# ------------------------------------------------------------ the commands
def join_mesh(env: dict[str, str], username: str, root: str,
              env_path: Path) -> None:
    """The whole join, on the new member's machine: self-signup (the
    password is generated HERE, written into supabase.env, never shown)
    then self-claim the username. IDEMPOTENT — rerunning after any
    failure resumes where it stopped (the first live run died between
    sign-up and claim because the SQL wasn't pasted yet, orphaning an
    auth user whose password was already lost; never again):

    1. an existing LOCAL credential is reused (no second auth user);
    2. the credential is installed BEFORE the claim, so a failed claim
       never strands a working sign-in;
    3. a claim that already exists as OURS is success, not an error.
    """
    client = _client(env, admin=False)
    email = _member_email(username, root)

    # 1. a working session — reuse the local credential if we have one
    session = None
    password = env.get("SUPABASE_MEMBER_PASSWORD", "")
    if env.get("SUPABASE_MEMBER_EMAIL", "").lower() == email.lower() and password:
        try:
            session = client.auth.sign_in_with_password(
                {"email": email, "password": password})
        except Exception:  # noqa: BLE001 — stale local creds; sign up fresh
            session = None
    if session is None:
        password = secrets.token_urlsafe(24)
        res = client.auth.sign_up({"email": email, "password": password})
        if getattr(res, "user", None) is None:
            raise SystemExit(
                "sign-up refused — is email signup enabled (and "
                "confirmation OFF) in the project's Auth settings?")
        if getattr(res, "session", None) is None:
            # an existing email returns a userless-session stub
            # (anti-enumeration) — or confirmations are still ON
            raise SystemExit(
                f"{email} already has an auth user this machine holds no "
                f"password for (a half-finished join, or confirmations "
                f"were ON). Ask the owner to run: supabase_admin revoke "
                f"{username} — then rerun join.")
        session = res

    # 2. the credential works — install it NOW (a failed claim below must
    #    never strand a working sign-in with a lost password again)
    _write_env_lines(env_path, email, password)

    # 3. claim the username (the PK arbitrates; ours-already = done). A
    #    sealed root takes only the typed claim, keyed to replay exactly.
    uid = session.user.id
    if _sealed_claim(env, email, password, root, username, uid,
                     rerun="your credential is installed; rerun join to "
                           "resume") == "unsealed":
        _legacy_claim(client, root, username, uid)
    print(f"@{username} joined {root}; credential installed into {env_path}")
    print("restart the app — the Connection panel should say "
          f"'Member ({username})'")


def _legacy_claim(client, root: str, username: str, uid: str) -> None:
    """The unsealed self-claim; an existing row of ours is success.

    returning="minimal" is LOAD-BEARING: the default representation return
    must pass the SELECT policy too, and its membership lookup cannot see the
    row being born in this same statement (42501 on a perfectly valid claim —
    the second live failure).
    """
    try:
        client.table("ab_members").insert({
            "root": root, "username": username, "uid": uid,
        }, returning="minimal").execute()
    except Exception as e:  # noqa: BLE001 — classify, always with the creds safe
        msg = str(e)
        mine = []
        try:
            mine = (client.table("ab_members").select("uid")
                    .eq("root", root).eq("username", username)
                    .execute().data or [])
        except Exception:  # noqa: BLE001
            pass
        if mine and str(mine[0].get("uid")) == str(uid):
            pass  # already claimed by us — a rerun, not a conflict
        elif "does not exist" in msg or "42P01" in msg:
            raise SystemExit(
                "the ab_members table is missing — paste "
                "docs/supabase_schema.sql first, then rerun join "
                "(your credential is installed and will be reused)")
        elif "duplicate" in msg.lower() or "23505" in msg:
            raise SystemExit(f"@{username} is already claimed on {root} "
                             f"by someone else")
        elif _SEALED in msg:
            # The root was sealed after the typed probe read it as unsealed.
            raise SystemExit("this root was just sealed — your credential is "
                             "installed; rerun join to claim the typed way")
        else:
            raise SystemExit(
                f"claim failed ({msg[:120]}) — your credential is "
                f"installed; rerun join after fixing the cause")


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
    # A sealed root takes only the member's own typed claim, made here by
    # signing in as the member with the publishable key.
    if env.get("SUPABASE_PUBLISHABLE_KEY") and _sealed_claim(
            env, email, password, root, username, uid,
            rerun="rerun seed to resume") != "unsealed":
        return email, password
    try:
        client.table("ab_members").upsert({
            "root": root, "username": username, "uid": uid,
        }).execute()
    except Exception as e:  # noqa: BLE001 - classify only the seal
        if _SEALED in str(e):
            raise SystemExit("this root is sealed: seed claims as the member "
                             "itself, which needs SUPABASE_PUBLISHABLE_KEY in "
                             "supabase.env — add it, then rerun seed") from None
        raise
    return email, password


def revoke(env: dict[str, str], username: str, root: str) -> bool:
    """Owner-only: eviction — the members row AND the auth user. The
    evictee's chats stay closed either way (meta ACLs + E2EE epochs)."""
    client = _client(env, admin=True)
    try:
        client.table("ab_members").delete().eq("root", root) \
            .eq("username", username).execute()
    except Exception as e:  # noqa: BLE001 - classify only the seal
        if _SEALED in str(e):
            # Nothing changed: the row delete is the first step.
            raise SystemExit("this root is sealed: eviction needs the typed "
                             "operator flow, which is not built yet") from None
        raise
    existing = _find_user(client.auth.admin, _member_email(username, root))
    if existing is None:
        return False
    client.auth.admin.delete_user(existing.id)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-supabase-admin")
    ap.add_argument("--root", default="", help="Supabase mesh root (supabase://NAME)")
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
    spec = args.root or load_app_config(home).get("mesh_root")
    if not spec:
        ap.error("no --root given and none remembered in config.json")
    try:
        root = validate_root_spec(spec)[len("supabase://"):]
    except ConfigError as exc:
        ap.error(str(exc))
    env = load_supabase_env(home)

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
