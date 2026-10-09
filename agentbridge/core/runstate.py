"""Local harness run-state (V109) — process truth, not message inference.

The GUI used to infer "is @agent's runner alive" from transport docs (run
feeds, ask docs), and a process that died mid-write left ghosts: permission
prompts that lingered after a stop, "running" bubbles for crashed workers.
Aryan's architecture call: the app asks the HARNESS directly.

The channel is a heartbeat file in the shared LOCAL home (never the mesh —
zero cloud traffic, and process state is machine-local by nature):

    <home>/harness/runstate_<agent>.json   {"agent", "pid", "updated"}

The runner rewrites it every loop pass and removes it on clean exit; the
GUI (same machine, same home) reads it and checks BOTH freshness and that
the pid is actually alive — a stale heartbeat with a reused pid still reads
dead. For agents hosted on ANOTHER machine the probe answers None and
callers fall back to doc-age heuristics.

Deliberately NOT the SingleInstance lock: probing an advisory lock means
briefly acquiring it, and a runner booting in that instant fails its own
acquire straight into the supervisor's already-running cooldown.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = ["runstate_path", "write_beat", "clear_beat", "pid_alive",
           "runner_alive", "FRESH_S"]

FRESH_S = 30.0   # a beat older than this reads dead (runner writes ~10s)


def runstate_path(home: Path, agent: str) -> Path:
    return Path(home) / "harness" / f"runstate_{agent}.json"


def write_beat(home: Path, agent: str) -> None:
    """Atomic local write; best-effort — a heartbeat never breaks a runner."""
    try:
        path = runstate_path(home, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "agent": agent, "pid": os.getpid(), "updated": time.time(),
        }), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def clear_beat(home: Path, agent: str) -> None:
    try:
        runstate_path(home, agent).unlink(missing_ok=True)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Is the process alive? NEVER ``os.kill(pid, 0)`` on Windows — any
    non-CTRL signal there is TerminateProcess, so a liveness probe would
    kill the process it probes."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def runner_alive(home: Path, agent: str, *, fresh_s: float = FRESH_S) -> bool:
    """Process truth for THIS machine: fresh heartbeat + live pid."""
    try:
        doc = json.loads(runstate_path(home, agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        if time.time() - float(doc.get("updated") or 0) > fresh_s:
            return False
        return pid_alive(int(doc.get("pid") or 0))
    except (TypeError, ValueError):
        return False
