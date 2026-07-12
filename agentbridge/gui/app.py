"""The HTTP server: routes -> endpoint modules, static frontend, SSE.

stdlib ThreadingHTTPServer on 127.0.0.1 — same footing as the v1 server the
Edge app window already fronts, so R14's cutover is a launcher flip. Route
tables are plain dicts contributed by the api_* modules; binary responses
use routing.Response; ``/api/mesh/events`` is the one streaming route.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..core.config import load_app_config, save_app_config

from . import (
    api_agents,
    api_auth,
    api_chats,
    api_files,
    api_membership,
    api_messages,
    api_profile,
)
from .context import GuiApp
from .routing import Request, Response, dispatch
from .sse import stream

__all__ = ["GuiServer", "make_server", "main"]

log = logging.getLogger("agentbridge.gui")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".woff2": "font/woff2",
}

GET_ROUTES: dict = {}
POST_ROUTES: dict = {}
RAW_ROUTES: dict = {}
for mod in (api_auth, api_chats, api_messages, api_membership,
            api_profile, api_agents, api_files):
    GET_ROUTES.update(mod.GET)
    POST_ROUTES.update(mod.POST)
    RAW_ROUTES.update(getattr(mod, "RAW_POST", {}))

MAX_BODY = 64 * 1024 * 1024  # JSON bodies and raw uploads alike


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> GuiApp:
        return self.server.gui  # type: ignore[attr-defined]

    # --------------------------------------------------------------- verbs
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/mesh/events":
            self._sse()
            return
        handler = GET_ROUTES.get(path)
        if handler is not None:
            params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            req = Request(method="GET", path=path, params=params)
            self._reply(dispatch(handler, self.app, req))
            return
        if path.startswith("/api/"):
            self._json({"error": f"unknown endpoint {path}"}, status=404)
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/shutdown":
            self._json({"ok": True})
            import threading

            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            raw = self.rfile.read(length) if length else b""
        except (ValueError, OSError):
            self._json({"error": "unreadable body"}, status=400)
            return
        raw_handler = RAW_ROUTES.get(path)
        if raw_handler is not None:
            params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            req = Request(method="POST", path=path, params=params)
            self._reply(dispatch(raw_handler, self.app, req, raw))
            return
        handler = POST_ROUTES.get(path)
        if handler is None:
            self._json({"error": f"unknown endpoint {path}"}, status=404)
            return
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}
        except ValueError:
            self._json({"error": "malformed JSON body"}, status=400)
            return
        req = Request(method="POST", path=path, data=data)
        self._reply(dispatch(handler, self.app, req))

    # ------------------------------------------------------------- replies
    def _reply(self, out) -> None:
        if isinstance(out, Response):
            self.send_response(out.status)
            self.send_header("Content-Type", out.ctype)
            self.send_header("Content-Length", str(len(out.body)))
            for k, v in out.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out.body)
            return
        if out is None:
            out = {"error": "internal error"}
        self._json(out)

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------- static
    def _static(self, path: str) -> None:
        base = self.app.static_dir.resolve()
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        try:
            target = (base / rel).resolve()
            target.relative_to(base)  # traversal guard
        except (OSError, ValueError):
            self._json({"error": "not found"}, status=404)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._json({"error": "not found"}, status=404)
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # a local desktop app updates in place — never let the embedded browser
        # serve a stale module after the app files change (no bandwidth cost on
        # 127.0.0.1). ETag lets an unchanged file still 304 on reload.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- SSE
    def _sse(self) -> None:
        app = self.app
        sub = app.subscribe()
        if sub is None:
            self._json({"error": "Sign in first"}, status=401)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in stream(app, sub, app.sse_ping_s):
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass  # client went away — routine
        finally:
            app.release(sub)

    # HTTP/1.1 + Connection: close needs the socket actually closed
    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        log.debug("%s %s", self.address_string(), fmt % args)


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], app: GuiApp) -> None:
        super().__init__(addr, Handler)
        self.gui = app

    def handle_error(self, request, client_address) -> None:
        # a browser dropping a keep-alive/SSE socket is routine, not an error;
        # endpoint exceptions never reach here (routing.dispatch catches them)
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            log.debug("client %s dropped: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


def make_server(app: GuiApp, port: int = 0, host: str = "127.0.0.1") -> GuiServer:
    return GuiServer((host, port), app)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-gui",
                                 description="AgentBridge GUI server (v2)")
    ap.add_argument("--root", default="",
                    help="mesh root (the synced folder); remembered after the "
                         "first run, so a bare launch reuses it")
    ap.add_argument("--home", default="", help="local home dir (default: ~/.agentbridge)")
    ap.add_argument("--port", type=int, default=7787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--machine", default="")
    ap.add_argument("--no-encrypt", action="store_true",
                    help="plaintext sealer (tests/dev only)")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve only; don't open the app window")
    ap.add_argument("--static", default="", help="frontend dir override")
    args = ap.parse_args(argv)

    try:
        from gui import __version__ as app_version  # canonical until R26
    except ImportError:
        app_version = "dev"

    home = Path(args.home) if args.home else None
    # root: CLI wins and is REMEMBERED (merged into config, never clobbering
    # other keys); a bare launch reuses the saved one — the R14 cutover flip
    cfg = load_app_config(home)
    if args.root:
        root = Path(args.root)
        save_app_config({**cfg, "mesh_root": str(root)}, home)
    elif cfg.get("mesh_root"):
        root = Path(cfg["mesh_root"])
    else:
        ap.error("no --root given and none remembered in config.json")

    app = GuiApp(
        root,
        home=home,
        machine=args.machine,
        encrypt=not args.no_encrypt,
        static_dir=Path(args.static) if args.static else None,
        app_version=app_version,
    )
    app.restore()
    server = make_server(app, args.port, args.host)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"AgentBridge GUI (v2) on {url}  root={root}")
    if not args.no_browser:
        from .desktop import launch_window

        launch_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
