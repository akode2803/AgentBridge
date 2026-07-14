"""R1 spike: supabase-py import + surface check (no account yet — D2).

Confirms the client and realtime channel APIs exist in the Python client; the
live connectivity test waits for the R23 round once Aryan opens the account.
"""

import sys


def main() -> None:
    import supabase
    from supabase import create_client  # noqa: F401

    import realtime  # noqa: F401  (import IS the check: realtime-py present)
    ver = getattr(supabase, "__version__", "unknown")

    # surface check: the client class exposes channel/realtime plumbing
    from supabase import Client
    surface = [a for a in ("channel", "realtime", "table", "storage", "auth")
               if hasattr(Client, a) or a in getattr(Client, "__annotations__", {})]

    print(f"OK smoke_supabase: supabase {ver} imports; realtime module present; "
          f"client surface: {surface}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_supabase: {type(e).__name__}: {e}")
        sys.exit(1)
