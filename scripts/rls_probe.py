"""R84 RLS verification probe (docs/SECURITY_RLS.md §4.2).

    python scripts/rls_probe.py --env <member-env-file> [--home DIR]
        [--expect-chat CHAT_ID] [--expect-no-chat CHAT_ID]

Signs in as ONE member (the env file needs SUPABASE_MEMBER_EMAIL/_PASSWORD;
URL + publishable key come from it too, or from <home>/supabase.env) and
reports what that member can actually see. Run it:

- PRE-paste: everything must be zero (RLS deny-by-default) while the
  service-key fleet keeps working — proves the auth plumbing.
- POST-paste: global lanes readable; --expect-chat rows visible;
  --expect-no-chat rows invisible; a foreign root invisible.

Read-only against real data; creates nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbridge.transport.supabase import load_supabase_env  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, help="member env file")
    ap.add_argument("--home", default="")
    ap.add_argument("--root", default="mesh2")
    ap.add_argument("--expect-chat", default="",
                    help="a chat id this member IS in (post-paste: visible)")
    ap.add_argument("--expect-no-chat", default="",
                    help="a chat id this member is NOT in (never visible)")
    args = ap.parse_args()

    base = load_supabase_env(Path(args.home) if args.home else None)
    member: dict[str, str] = {}
    for line in Path(args.env).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            member[k.strip()] = v.strip()
    env = {**base, **member}

    from supabase import create_client

    sb = create_client(env["SUPABASE_URL"], env["SUPABASE_PUBLISHABLE_KEY"])
    sb.auth.sign_in_with_password({
        "email": env["SUPABASE_MEMBER_EMAIL"],
        "password": env["SUPABASE_MEMBER_PASSWORD"],
    })
    who = env["SUPABASE_MEMBER_EMAIL"].split("@", 1)[0]
    print(f"signed in as {who}")

    def count(q) -> int:
        try:
            return len(q.execute().data or [])
        except Exception as e:  # noqa: BLE001
            print(f"  (query error: {e})")
            return -1

    root = args.root
    checks: list[tuple[str, int, str]] = []
    n = count(sb.table("ab_docs").select("path")
              .eq("root", root).not_.like("path", "chats/%").limit(50))
    checks.append(("global docs (users/status/…)", n, ">0 post-paste, 0 pre"))
    n = count(sb.table("ab_docs").select("path")
              .eq("root", root).like("path", "chats/%").limit(200))
    print(f"chat docs visible: {n} (only chats this member is in, post-paste)")
    if args.expect_chat:
        n = count(sb.table("ab_docs").select("path").eq("root", root)
                  .like("path", f"chats/{args.expect_chat}/%").limit(5))
        checks.append((f"member chat {args.expect_chat}", n, ">0 post-paste"))
        n = count(sb.table("ab_logs").select("id").eq("root", root)
                  .eq("chat_id", args.expect_chat).limit(5))
        checks.append((f"member chat logs {args.expect_chat}", n, ">0 post-paste"))
    if args.expect_no_chat:
        n = count(sb.table("ab_docs").select("path").eq("root", root)
                  .like("path", f"chats/{args.expect_no_chat}/%").limit(5))
        checks.append((f"FOREIGN chat {args.expect_no_chat}", n, "must be 0"))
        n = count(sb.table("ab_logs").select("id").eq("root", root)
                  .eq("chat_id", args.expect_no_chat).limit(5))
        checks.append((f"FOREIGN chat logs {args.expect_no_chat}", n, "must be 0"))
    n = count(sb.table("ab_docs").select("path")
              .eq("root", "some-other-root").limit(5))
    checks.append(("foreign root", n, "must be 0"))

    print()
    for label, got, want in checks:
        print(f"  {label}: {got}   [{want}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
