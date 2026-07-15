"""Update checks (V45 + V51) — three channels, first conclusive answer wins.

R58 probed GitHub releases only; the repo is private with no releases, so
every check honestly-but-uselessly reported unreachable (V51). Today's
installs are git checkouts carrying the machine's own credentials, so GIT is
the primary channel: fetch origin, read the default branch's ``__version__``,
compare — and "Update now" applies it under hard rails (default branch only,
clean tree, fast-forward only) with an honest restart note. GitHub releases
stay for the packaged future. The R11 machine registry's version adverts are
the app-to-app fallback — detection only, NEVER an install source
(applink/update.py's rail: a peer may prompt you to go look, it can neither
choose the artifact nor apply it).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .. import __version__
from .routing import authed

__all__ = ["GET", "POST", "ver_tuple", "git_check", "peer_check", "repo_root"]

RELEASES_LATEST = ("https://api.github.com/repos/"
                   "DAA-Aryan-Kumar/AgentBridge/releases/latest")
GIT_TIMEOUT_S = 12.0

_VER_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')


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


# ---------------------------------------------------------------- git channel
def repo_root() -> Path | None:
    """The checkout this process runs from (…/agentbridge/gui/api_updates.py
    → two parents up), or None when packaged without the source layout."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "agentbridge" / "__init__.py").exists() else None


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    """Run git non-interactively; (-1, "") on ANY environment failure so a
    missing/hung git degrades to the next channel instead of a 500. The
    prompt kill matters: a credential prompt would hang the request thread."""
    if not shutil.which("git"):
        return -1, ""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, (p.stdout or "").strip()
    except Exception:  # noqa: BLE001 — timeout / odd cwd / broken install
        return -1, ""


def _default_branch(root: Path) -> str:
    rc, out = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return out.split("/", 1)[1] if rc == 0 and "/" in out else "main"


def _apply_blocker(root: Path, branch: str) -> str:
    """Why "Update now" must refuse on this checkout ("" = safe): the rails
    are default-branch-only, clean tree, and fast-forward reachable — an
    update never merges, rebases, or discards anything."""
    rc, cur = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or cur != branch:
        return (f"this install runs branch {cur or '?'} — "
                "update it manually (git pull)")
    rc, dirty = _git(root, "status", "--porcelain")
    if rc != 0 or dirty:
        return "local changes on this machine — update manually (git pull)"
    rc, _ = _git(root, "merge-base", "--is-ancestor", "HEAD",
                 f"origin/{branch}")
    if rc != 0:
        return "local commits diverge from the update — update manually"
    return ""


def git_check(*, fetch: bool = True) -> dict | None:
    """The git channel. None = not applicable here (packaged, git missing,
    no origin, fetch refused) — fall through to the next channel."""
    root = repo_root()
    if root is None:
        return None
    rc, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return None
    branch = _default_branch(root)
    if fetch:
        rc, _ = _git(root, "fetch", "--quiet", "origin", branch)
        if rc != 0:
            return None  # offline / no credentials — not conclusive
    rc, blob = _git(root, "show", f"origin/{branch}:agentbridge/__init__.py")
    m = _VER_RE.search(blob) if rc == 0 else None
    if not m:
        return None
    latest = m.group(1)
    newer = ver_tuple(latest) > ver_tuple(__version__)
    out = {"ok": True, "channel": "git", "current": __version__,
           "latest": latest, "newer": newer, "url": "",
           "can_apply": False, "note": ""}
    if newer:
        why = _apply_blocker(root, branch)
        out["can_apply"] = not why
        out["note"] = why
    return out


# ------------------------------------------------------------ release channel
def fetch_latest(url: str = RELEASES_LATEST, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"AgentBridge/{__version__}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def release_check() -> dict | None:
    """GitHub releases — the packaged future's channel. None until the
    packaging session publishes something (private repo / no releases)."""
    try:
        rel = fetch_latest()
    except Exception:  # noqa: BLE001 — offline/private/rate-limited
        return None
    latest = str(rel.get("tag_name") or rel.get("name") or "").strip()
    if not latest:
        return None
    assets = rel.get("assets") or []
    url = (assets[0].get("browser_download_url")
           if assets and isinstance(assets[0], dict) else "") \
        or str(rel.get("html_url") or "")
    return {"ok": True, "channel": "release", "current": __version__,
            "latest": latest,
            "newer": ver_tuple(latest) > ver_tuple(__version__),
            "url": url, "can_apply": False, "note": ""}


# --------------------------------------------------------------- peer channel
def peer_check(mesh) -> dict | None:
    """R11's app-to-app lane: every machine announces its version in the
    machine registry. Strictly a hint to go look — never an install source."""
    try:
        best, latest, machine = ver_tuple(__version__), "", ""
        for peer in mesh.applink.registry.peers():
            pv = str(peer.get("app_version") or "")
            if ver_tuple(pv) > best:
                best, latest = ver_tuple(pv), pv
                machine = str(peer.get("machine") or "another machine")
        if not latest:
            return None
        return {"ok": True, "channel": "peer", "current": __version__,
                "latest": latest, "newer": True, "url": "",
                "can_apply": False,
                "note": (f"Version {latest} is running on {machine} — "
                         "update this machine to match")}
    except Exception:  # noqa: BLE001 — registry trouble = no hint
        return None


# ------------------------------------------------------------------ endpoints
@authed
def update_check(app, req, mesh) -> dict:
    """git → releases → peer hint → honest miss. The first channel that can
    actually SPEAK for this install answers; checking never installs."""
    try:  # keep this machine's own advert fresh — peers read it (V51)
        mesh.applink.announce(["gui"])
    except Exception:  # noqa: BLE001 — the advert is garnish
        pass
    for probe in (git_check, release_check):
        r = probe()
        if r is not None:
            return r
    r = peer_check(mesh)
    if r is not None:
        return r
    return {"ok": False, "current": __version__,
            "note": "Couldn't check for updates — no update source reachable"}


@authed
def janitor_sweep(app, req, mesh) -> dict:
    """Run the V63 storage janitor once: reclaim the attachments of
    verified delete-for-everyone'd messages (past the 7-day undo grace)
    and purge groups whose deletion folded terminal. Idempotent."""
    from ..mesh.janitor import Janitor

    out = Janitor(mesh).sweep()
    return {"ok": True, **out}


@authed
def app_restart(app, req, mesh) -> dict:
    """V113: restart the whole app — this GUI server and the agent harness.
    A detached helper (gui/restarter.py) outlives us: it waits for this
    process to exit, clears the rest of the fleet (rigs with --home are
    never touched), and relaunches both with the same interpreter. The
    session restores itself (R75: restore is password-free by design);
    the app window reconnects to the new server on its own."""
    import json as _json
    import subprocess
    import sys
    import threading

    server = getattr(app, "server", None)
    if server is None:
        return {"ok": False, "note": "no live server to restart (test rig?)"}
    gui_args = [str(a) for a in sys.argv[1:]]
    cmd = [sys.executable, "-m", "agentbridge.gui.restarter",
           "--gui-pid", str(os.getpid()),
           "--exe", sys.executable,
           "--cwd", os.getcwd(),
           "--gui-args", _json.dumps(gui_args)]
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    # shut down AFTER this response has flushed to the client
    threading.Timer(0.8, server.shutdown).start()
    return {"ok": True, "note": "Restarting — back in a few seconds"}


@authed
def update_apply(app, req, mesh) -> dict:
    """Apply a git-channel update. Every rail is recomputed server-side —
    the client's earlier check result is never trusted."""
    root = repo_root()
    if root is None:
        return {"ok": False, "note": "this install doesn't update via git"}
    checked = git_check()
    if checked is None:
        return {"ok": False, "note": "couldn't reach the update source"}
    if not checked["newer"]:
        return {"ok": True, "updated": False, "current": __version__,
                "note": "Already up to date"}
    if not checked["can_apply"]:
        return {"ok": False,
                "note": checked["note"] or "can't update this checkout"}
    branch = _default_branch(root)
    rc, _ = _git(root, "merge", "--ff-only", f"origin/{branch}", timeout=30)
    if rc != 0:
        return {"ok": False, "note": "update failed — update manually"}
    try:
        text = (root / "agentbridge" / "__init__.py").read_text(encoding="utf-8")
        m = _VER_RE.search(text)
        ver = m.group(1) if m else checked["latest"]
    except Exception:  # noqa: BLE001 — the merge landed; version is cosmetic
        ver = checked["latest"]
    return {"ok": True, "updated": True, "version": ver,
            "note": f"Updated to {ver} — restart AgentBridge to finish"}


GET = {"/api/update_check": update_check}
POST = {"/api/update_apply": update_apply,
        "/api/app_restart": app_restart,
        "/api/mesh/janitor": janitor_sweep}
