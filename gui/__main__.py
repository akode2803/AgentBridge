"""Entry point: python -m gui [--port N] [--home DIR] [--no-browser]"""

import argparse
from pathlib import Path

from gui import __version__, server


def main():
    ap = argparse.ArgumentParser(
        prog="python -m gui",
        description="AgentBridge GUI — local web app in an Edge window")
    ap.add_argument("--port", type=int, default=7787)
    ap.add_argument("--home", default=None,
                    help="bridge state dir (default: %%USERPROFILE%%\\.agentbridge)")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve only; do not open an app window")
    args = ap.parse_args()

    if args.home:
        server.HOME = Path(args.home)

    httpd = server.serve(args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"AgentBridge GUI v{__version__} — {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        server.launch_window(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[gui] stopped")


if __name__ == "__main__":
    main()
