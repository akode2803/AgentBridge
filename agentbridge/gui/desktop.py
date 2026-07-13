"""OS-integration helpers (ported from the v1 server): every platform-
specific call lives here so feature code stays portable.

pythonw quirks (learned the hard way in v1): without CREATE_NO_WINDOW every
subprocess flashes a console; without stdin redirected they can fail
outright ("the handle is invalid") — pythonw has no std handles.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

__all__ = ["SUBPROC", "open_path", "pick_folder", "launch_window",
           "sync_client_running"]

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
SUBPROC = {"stdin": subprocess.DEVNULL, "creationflags": NO_WINDOW}


def _find_edge() -> Path | None:
    import os

    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.is_file():
                return p
    return None


def launch_window(url: str) -> None:
    """Chromeless app window: Edge on Windows, Edge/Chrome on macOS, default
    browser elsewhere (ported from the v1 launcher)."""
    if sys.platform == "win32":
        edge = _find_edge()
        if edge:
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=1240,860"],
                             **SUBPROC)
            return
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome"):
            if Path(f"/Applications/{app}.app").exists():
                subprocess.Popen(["open", "-na", app, "--args",
                                  f"--app={url}", "--window-size=1240,860"])
                return
    import webbrowser

    webbrowser.open(url)


def open_path(path: Path | str) -> None:
    """Open a file or folder with the OS default handler."""
    if sys.platform == "win32":
        import os

        os.startfile(str(path))  # noqa: S606 — local desktop app by design
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# the process probe shells out (~1s), so the answer is cached for a minute —
# /api/state polls every few seconds (ported from the v1 server)
_sync_cache = {"ts": 0.0, "running": None}


def sync_client_running() -> bool | None:
    """Is the folder-sync client (OneDrive today; anything later) alive?
    None = unknown. Only meaningful for folder mesh roots."""
    now = time.time()
    if now - _sync_cache["ts"] > 60 or _sync_cache["running"] is None:
        _sync_cache.update(ts=now, running=_probe_sync_client())
    return _sync_cache["running"]


def _probe_sync_client() -> bool | None:
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq OneDrive.exe"],
                capture_output=True, text=True, timeout=15, **SUBPROC).stdout
            return "OneDrive.exe" in out
        except Exception:  # noqa: BLE001 — a broken probe is just "unknown"
            return None
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["pgrep", "-x", "OneDrive"],
                               capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return None
    return None


def pick_folder(timeout: float = 600) -> str:
    """Native folder picker via a tkinter subprocess (blocks until closed).
    Returns '' when cancelled or unavailable."""
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory() or '')"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout, **SUBPROC,
        )
        return r.stdout.strip()
    except Exception:  # noqa: BLE001 — headless box: picker just unavailable
        return ""
