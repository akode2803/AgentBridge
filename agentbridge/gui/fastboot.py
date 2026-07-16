"""V126 fast boot: first paint before the heavy imports.

A cold ``python -m agentbridge.gui`` used to spend ~1s importing the
mesh/crypto/cloud stack and only then bind the port and spawn the app
window — a double-clicked AgentBridge.pyw felt dead for seconds. This
module is the thin front door: it parses the CLI, resolves the root,
takes the single-instance lock, BINDS the listening socket (milliseconds
— early requests queue in the OS accept backlog), and spawns the Edge
app window so the browser's own startup overlaps the heavy imports.
Only then does it import ``.app`` and hand the pre-bound socket over.

Everything imported at the top of this module must stay stdlib-cheap —
that's the whole point. ``python check_frontend.py`` doesn't guard this;
the measure is ``import agentbridge.gui.fastboot`` staying ~instant.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

from ..core.config import DEFAULT_HOME, load_app_config, save_app_config

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentbridge-gui",
                                 description="AgentBridge GUI server (v2)")
    ap.add_argument("--root", default="",
                    help="mesh root (the synced folder); remembered after the "
                         "first run, so a bare launch reuses it")
    ap.add_argument("--home", default="",
                    help="local home dir (default: ~/.agentbridge)")
    ap.add_argument("--port", type=int, default=7787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--machine", default="")
    ap.add_argument("--no-encrypt", action="store_true",
                    help="plaintext sealer (tests/dev only)")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve only; don't open the app window")
    ap.add_argument("--static", default="", help="frontend dir override")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    home = Path(args.home) if args.home else None
    # root: CLI wins and is REMEMBERED (merged into config, never clobbering
    # other keys); a bare launch reuses the saved one — the R14 cutover flip
    cfg = load_app_config(home)

    def as_root(text: str):
        # a scheme spec (supabase://…) must stay a STRING — Path() collapses
        # the double slash and mangles it (R23); folder roots stay Paths
        return text if "://" in text else Path(text)

    if args.root:
        root = as_root(args.root)
        save_app_config({**cfg, "mesh_root": str(args.root)}, home)
    elif cfg.get("mesh_root"):
        root = as_root(cfg["mesh_root"])
    else:
        ap.error("no --root given and none remembered in config.json")

    # single-instance guard (R45): a double-clicked AgentBridge.pyw beside
    # the supervised fleet would otherwise co-bind :7787 (Windows
    # SO_REUSEADDR lets two sockets share a port silently) and run a SECOND
    # GUI — the chronic "stray GUI pair". The lock is port-scoped, so a dev
    # rig on another port isn't blocked; an ephemeral port (0) skips it
    # (tests). A loser opens the app window at the running server, exits 0.
    lock = None
    if args.port:
        from ..core.lock import SingleInstance

        lock = SingleInstance((home or DEFAULT_HOME) / f"gui-{args.port}.lock")
        if not lock.acquire():
            running = f"http://{args.host}:{args.port}/"
            print(f"AgentBridge GUI already running on {running} — "
                  "focusing it.")
            if not args.no_browser:
                from .desktop import launch_window

                launch_window(running)
            return 0

    # the fast first paint: bind NOW (early requests wait in the accept
    # backlog) and open the window NOW (Edge's startup runs beside the
    # heavy imports below). SO_REUSEADDR matches GuiServer's
    # allow_reuse_address — same bind semantics as before, just earlier.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
        sock.listen(128)
    except OSError as e:
        sock.close()
        if lock is not None:
            lock.release()
        print(f"can't bind {args.host}:{args.port}: {e}")
        return 1
    host, port = sock.getsockname()[:2]
    if not args.no_browser:
        from .desktop import launch_window

        launch_window(f"http://{host}:{port}/")

    from .app import serve  # the heavy chain — the window is already up

    return serve(root=root, home=home, args=args, lock=lock, sock=sock)
