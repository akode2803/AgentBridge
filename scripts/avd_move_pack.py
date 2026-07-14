"""Build the transfer pack that moves a co-hosted agent to another machine.

Run on the machine that currently HOLDS the agent's keys (the dev box), with
the repo venv so ``agentbridge`` imports:

    .venv\\Scripts\\python.exe scripts\\avd_move_pack.py coco

It writes a pack folder (default: Desktop\\agentbridge-move-<agent>) holding
everything the destination needs that git cannot carry:

    keys/<agent>.key      the identity bundle, exported PLAIN-base64. The
                          local file is DPAPI-wrapped (R31.5) and unreadable
                          off this machine/OS-user; KeyStore.load() unwraps
                          it here and the destination's KeyStore transparently
                          re-wraps on first load. This is the ONLY copyable
                          form — never copy the dpapi1: file itself.
    supabase.env          the cloud transport credentials (SECRET).
    avd_clean_install.ps1 the destination-side installer (from scripts/).
    README.txt            the runbook + the copy list.

The pack contains SECRETS. Move it over a private channel (RDP clipboard/
drive mapping, your own OneDrive) and delete it on both ends once the install
verifies. Nothing else needs copying: the repo arrives via git clone,
config.json is machine-specific and written fresh, caches/workspaces rebuild,
and the mesh itself lives in Supabase.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbridge import __version__  # noqa: E402
from agentbridge import crypto  # noqa: E402
from agentbridge.core.config import DEFAULT_HOME  # noqa: E402
from agentbridge.mesh.keyring import KeyStore  # noqa: E402

README = """AgentBridge move pack — @{agent}  (built from v{version})
=========================================================

WHAT THIS IS
  Everything the new machine needs that a git clone cannot carry:

    keys/{agent}.key       @{agent}'s identity bundle (plain-base64 export;
                           the destination re-wraps it with DPAPI on first
                           load — the file self-upgrades, nothing to do)
    supabase.env           cloud transport credentials  ** SECRET **
    avd_clean_install.ps1  the installer to run on the new machine

ON THE NEW MACHINE (the AVD)
  1. Copy this whole folder somewhere local (NOT into a synced folder).
  2. Open PowerShell IN this folder and run:
         powershell -ExecutionPolicy Bypass -File .\\avd_clean_install.ps1
     It wipes the v1-era install (old scheduled task, worker processes,
     %USERPROFILE%\\.agentbridge, old clone), clones the current repo,
     installs the runtime with uv, places these files, then walks you
     through signing in once as the owner to adopt @{agent} to the new
     machine, and finally launches + auto-starts the harness.
  3. When it finishes, DELETE this folder (it holds a plain agent key and
     the cloud secrets). Delete the copy on the source machine too.

RULES THE INSTALLER ENFORCES (know them anyway)
  - It never touches OneDrive/SharePoint synced folders: the old v1 mesh
    folder and the mesh2/ folder backup are shared data and a delete there
    syncs to every machine. Local state only.
  - Only the OWNER should ever sign into the GUI on the new machine —
    signing in claims the machine's agents (by design, D19).
  - After adoption, the OLD machine's harness stands down for @{agent} on
    its own (it refuses agents homed elsewhere); restart it when convenient
    to clear the supervisor slot.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="export an agent's key + cloud creds into a transfer pack")
    ap.add_argument("agent", nargs="?", default="coco")
    ap.add_argument("--home", default="", help="local home (default ~/.agentbridge)")
    ap.add_argument("--out", default="", help="pack folder (default Desktop)")
    args = ap.parse_args(argv)

    agent = args.agent.strip().lower()
    home = Path(args.home) if args.home else DEFAULT_HOME
    out = Path(args.out) if args.out else (
        Path.home() / "Desktop" / f"agentbridge-move-{agent}")

    bundle = KeyStore(home).load(agent)
    if bundle is None:
        print(f"ERROR: no unlocked key bundle for @{agent} under {home / 'keys'}"
              f" — this machine does not host it, nothing to export.")
        return 1

    (out / "keys").mkdir(parents=True, exist_ok=True)
    # plain KeyStore format: base64 text, no dpapi1: prefix — the one shape
    # that opens anywhere; the destination re-wraps it on first load
    (out / "keys" / f"{agent}.key").write_text(crypto.b64e(bundle), encoding="utf-8")

    env = home / "supabase.env"
    if env.is_file():
        shutil.copyfile(env, out / "supabase.env")
    else:
        print(f"WARNING: {env} not found — cloud transport creds must be "
              f"placed on the destination by hand.")

    installer = Path(__file__).with_name("avd_clean_install.ps1")
    if installer.is_file():
        shutil.copyfile(installer, out / "avd_clean_install.ps1")
    else:
        print(f"WARNING: {installer} missing — copy it into the pack yourself.")

    (out / "README.txt").write_text(
        README.format(agent=agent, version=__version__), encoding="utf-8")

    print(f"Pack built: {out}")
    print("Contents:")
    for p in sorted(out.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(out)}")
    print("\nThis folder holds a PLAIN agent key + cloud secrets: move it over"
          "\na private channel and delete it (both ends) after the install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
