"""The detached restart helper (V113) — ``python -m agentbridge.gui.restarter``.

Spawned by ``/api/app_restart`` right before the GUI server shuts itself
down. It outlives its parent on purpose (detached process group): waits for
the old GUI to exit, clears any leftover fleet processes, and relaunches
the GUI (and the harness, if one was running) with the same interpreter.

Scope guard: the restart only touches its OWN instance's processes. The
main fleet runs ``-m agentbridge.gui``/``-m agentbridge.harness`` on the
remembered defaults (no ``--home``), while dev rigs and tests always pass
``--home <dir>`` — so a main-app restart skips anything with ``--home``,
and a rig restart (its args carry ``--home``) touches ONLY processes
naming that same home, never the real fleet. A scoped (rig) restart also
skips the ``harness --all`` relaunch: rigs run per-agent harnesses their
own scripts own.

The relaunched GUI always gets ``--no-browser``: the Edge app window
outlives the server and reconnects on its own — spawning a second window
here would double it.

Process enumeration shells out to PowerShell (an OS facility, not a
runtime dependency); non-Windows falls back to ``ps``. Everything is
best-effort: worst case the helper relaunches beside a process that
refused to die, and the port lock (R45) resolves the race.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

__all__ = ["main"]

_FLEET_MARKS = ("-m agentbridge.gui", "-m agentbridge.harness")


def _list_python_procs() -> list[tuple[int, str]]:
    """[(pid, command line)] for python processes, best-effort."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\""
                 " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                capture_output=True, text=True, timeout=30).stdout
        else:
            out = subprocess.run(["ps", "-eo", "pid=,args="],
                                 capture_output=True, text=True,
                                 timeout=30).stdout
        procs = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_s, _, cmd = line.partition("\t" if "\t" in line else " ")
            try:
                procs.append((int(pid_s), cmd.strip()))
            except ValueError:
                continue
        return procs
    except Exception:  # noqa: BLE001 — enumeration is best-effort
        return []


def _fleet_procs(scope_home: str = "") -> list[tuple[int, str]]:
    me = os.getpid()
    out = []
    for pid, cmd in _list_python_procs():
        if pid == me or "restarter" in cmd:
            continue
        if not any(m in cmd for m in _FLEET_MARKS):
            continue
        if scope_home:
            if scope_home not in cmd:
                continue          # a rig restart touches only its own home
        elif "--home" in cmd:
            continue              # the main app never touches a rig
        out.append((pid, cmd))
    return out


def _scope_home(gui_args: list[str]) -> str:
    """The ``--home`` value in the instance's own args, if any."""
    for i, a in enumerate(gui_args):
        if a == "--home" and i + 1 < len(gui_args):
            return gui_args[i + 1]
        if a.startswith("--home="):
            return a.split("=", 1)[1]
    return ""


def _wait_gone(pid: int, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if all(p != pid for p, _ in _list_python_procs()):
            return
        time.sleep(0.5)


def _spawn(cmd: list[str], cwd: str) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(cmd, cwd=cwd or None, creationflags=flags,
                     close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-restarter")
    ap.add_argument("--gui-pid", type=int, required=True)
    ap.add_argument("--exe", required=True, help="interpreter to relaunch with")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--gui-args", default="[]",
                    help="JSON list: the old GUI's own argv[1:]")
    args = ap.parse_args(argv)

    try:
        gui_args = [str(a) for a in json.loads(args.gui_args)]
    except ValueError:
        gui_args = []
    scope = _scope_home(gui_args)

    # 1. let the old GUI finish its response and exit on its own
    _wait_gone(args.gui_pid, 20.0)

    # 2. clear what's left of THIS instance's fleet (the harness tree, a
    #    wedged GUI); remember whether a harness was part of it
    had_harness = False
    for pid, cmd in _fleet_procs(scope):
        had_harness = had_harness or "agentbridge.harness" in cmd
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _wait_gone(args.gui_pid, 5.0)
    time.sleep(1.0)  # let killed processes release their locks/ports

    # 3. relaunch: the GUI first (same args, window suppressed), then the
    #    harness if one had been running (main app only — see module doc)
    if "--no-browser" not in gui_args:
        gui_args.append("--no-browser")
    _spawn([args.exe, "-m", "agentbridge.gui", *gui_args], args.cwd)
    if had_harness and not scope:
        time.sleep(2.0)
        _spawn([args.exe, "-m", "agentbridge.harness", "--all"], args.cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
