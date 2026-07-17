"""Small, dependency-free helpers for the repository's double-click launchers.

The entry scripts may be started by Windows PythonW, macOS Python Launcher, or
a desktop file association with a sparse environment. They must select the
project venv themselves and leave a useful log when spawning fails.
"""

from __future__ import annotations

import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .spawn import windowless_kwargs

__all__ = ["find_project_python", "launch_module", "run_launcher"]


def find_project_python(repo: Path, *, platform: str | None = None,
                        fallback: str | None = None) -> Path:
    """Return the first usable venv interpreter for the target platform."""
    platform = platform or sys.platform
    candidates = (
        [repo / ".venv" / "Scripts" / "pythonw.exe",
         repo / ".venv" / "Scripts" / "python.exe"]
        if platform == "win32" else
        [repo / ".venv" / "bin" / "python3",
         repo / ".venv" / "bin" / "python"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(fallback or sys.executable)


def launch_module(repo: Path, module: str, args: list[str] | None = None,
                  *, log_path: Path | None = None):
    """Spawn one project module detached from a stdio-less desktop parent."""
    repo = Path(repo).resolve()
    log_path = log_path or (Path.home() / ".agentbridge" / "launcher.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    python = find_project_python(repo)
    command = [str(python), "-m", module, *(args or [])]
    log = log_path.open("a", encoding="utf-8")
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.write(f"[{stamp}] launching {module} with {python}\n")
        log.flush()
        kwargs = windowless_kwargs()
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            command, cwd=str(repo), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )
    finally:
        log.close()


def run_launcher(repo: Path, module: str, args: list[str] | None = None) -> bool:
    """Desktop entrypoint wrapper: never lose a startup exception silently."""
    log_path = Path.home() / ".agentbridge" / "launcher.log"
    try:
        launch_module(repo, module, args, log_path=log_path)
        return True
    except Exception:  # noqa: BLE001 - this is the final desktop crash boundary
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(traceback.format_exc())
        return False
