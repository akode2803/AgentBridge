"""Update checks (V45) — the About page's "Check for updates" + auto-check.

The source of truth is the GitHub releases feed (the packaging session will
publish real artifacts there); until releases exist the check degrades to an
honest "couldn't check" / "up to date". Stdlib urllib only — no tokens, no
third-party clients; a private repo simply reports unreachable.
"""

from __future__ import annotations

import json
import urllib.request

from .. import __version__
from .routing import authed

__all__ = ["GET", "POST", "ver_tuple"]

RELEASES_LATEST = ("https://api.github.com/repos/"
                   "DAA-Aryan-Kumar/AgentBridge/releases/latest")


def ver_tuple(v: str) -> tuple[int, ...]:
    """Dotted version -> comparable int tuple; tolerant of a v prefix and
    trailing junk ("v0.25.1-beta" -> (0, 25, 1))."""
    out = []
    for part in (v or "").strip().lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def fetch_latest(url: str = RELEASES_LATEST, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"AgentBridge/{__version__}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


@authed
def update_check(app, req, mesh) -> dict:
    """Compare the running version against the newest GitHub release. The
    client renders three honest states: up to date / update available (with
    the download URL) / couldn't check."""
    try:
        rel = fetch_latest()
    except Exception:  # noqa: BLE001 — offline/private/rate-limited: honest miss
        return {"ok": False, "current": __version__,
                "note": "Couldn't reach the update service"}
    latest = str(rel.get("tag_name") or rel.get("name") or "").strip()
    assets = rel.get("assets") or []
    url = (assets[0].get("browser_download_url")
           if assets and isinstance(assets[0], dict) else "") \
        or str(rel.get("html_url") or "")
    return {
        "ok": True, "current": __version__, "latest": latest,
        "newer": ver_tuple(latest) > ver_tuple(__version__),
        "url": url,
    }


GET = {"/api/update_check": update_check}
POST: dict = {}
