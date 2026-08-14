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
runtime dependency) with ``CREATE_NO_WINDOW`` — the V119 report was a
console flashing up mid-restart (a detached process has no console, so
its child powershell CREATED one). Non-Windows falls back to ``ps``.

Relaunches use the checkout's OWN venv ``pythonw`` when it exists
(``<cwd>/.venv/Scripts/pythonw.exe``) — the canonical fleet shape —
rather than ``sys.executable``, which inside the uv-shim fleet is the
BARE uv ``python.exe`` and produced a non-canonical process chain
(V119's restart death was in such a chain). Every step appends to
``%TEMP%/agentbridge_restart.log`` so the next failure isn't a black
box. Process ownership is revalidated immediately before termination; an
unknown scan or surviving process fails closed instead of starting a duplicate
harness. Relaunched services must also prove readiness before success.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..core.config import DEFAULT_HOME
from ..core.lock import locked_pid
from ..core.runstate import pid_alive

__all__ = ["main"]

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


class ProcessScanError(RuntimeError):
    pass


def _log(msg: str) -> None:
    try:
        with open(Path(tempfile.gettempdir()) / "agentbridge_restart.log",
                  "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] "
                     f"{msg}\n")
    except OSError:
        pass


def _list_python_procs() -> list[tuple[int, str]]:
    """Return process command lines or raise when enumeration is unknown."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\""
                 " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                capture_output=True, text=True, timeout=30,
                creationflags=_NO_WINDOW)
        else:
            result = subprocess.run(
                ["ps", "-eo", "pid=,args="], capture_output=True,
                text=True, timeout=30)
        if result.returncode:
            detail = " ".join((result.stderr or "").split())[:200]
            raise OSError(
                f"process command exited {result.returncode}: {detail}")
        out = result.stdout
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
    except Exception as exc:  # noqa: BLE001 — enumeration is best-effort
        _log(f"process scan failed: {type(exc).__name__}: {exc}")
        raise ProcessScanError(str(exc)) from exc


def _command_tokens(command: str) -> list[str]:
    try:
        # Tests and migration tooling may inspect a Windows command on POSIX;
        # a backslash-bearing line must not be parsed as POSIX escapes.
        return shlex.split(
            command, posix=sys.platform != "win32" and "\\" not in command)
    except ValueError:
        return []


def _module_kind(command: str) -> str:
    tokens = _command_tokens(command)
    for index, token in enumerate(tokens[1:], start=1):
        if token == "-m":
            if index + 1 < len(tokens) and tokens[index + 1] in (
                    "agentbridge.gui", "agentbridge.harness"):
                return tokens[index + 1].rsplit(".", 1)[1]
            return ""
        if token == "-c" or not token.startswith("-"):
            return ""
    return ""


def _is_python_command(command: str) -> bool:
    tokens = _command_tokens(command)
    if not tokens:
        return False
    name = Path(tokens[0].strip('"\'')).name.lower()
    return re.fullmatch(
        r"python(?:w)?(?:\d+(?:\.\d+)*)?(?:\.exe)?", name,
    ) is not None


def _has_flag(command: str, flag: str) -> bool:
    return any(token == flag or token.startswith(flag + "=")
               for token in _command_tokens(command))


def _canonical_home(value: str) -> str:
    if not value:
        return ""
    return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))


def _last_option(tokens: list[str], flag: str) -> str:
    value = ""
    for index, token in enumerate(tokens):
        if token == flag:
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
        elif token.startswith(flag + "="):
            value = token.split("=", 1)[1]
    return value.strip('"\'')


def _has_scope(command: str, scope_home: str) -> bool:
    return _canonical_home(_last_option(
        _command_tokens(command), "--home")) == _canonical_home(scope_home)


def _command_scope(command: str) -> str:
    return _canonical_home(_last_option(_command_tokens(command), "--home"))


def _fleet_scan(scope_home: str = "") \
        -> tuple[list[tuple[int, str]], bool]:
    me = os.getpid()
    out = []
    try:
        processes = _list_python_procs()
    except ProcessScanError:
        return [], False
    for pid, cmd in processes:
        if pid == me or "restarter" in cmd:
            continue
        if not _is_python_command(cmd) or not _module_kind(cmd):
            continue
        if scope_home:
            if not _has_scope(cmd, scope_home):
                continue          # a rig restart touches only its own home
        elif _command_scope(cmd):
            continue              # the main app never touches a rig
        out.append((pid, cmd))
    # A PID file is trusted only while its advisory lock is held AND the same
    # PID appeared with the exact harness --all command in this process scan.
    # A failed scan therefore cannot turn a stale/reused PID into a signal.
    home = Path(scope_home) if scope_home else DEFAULT_HOME
    master = locked_pid(home / "harness-all.lock")
    trustworthy = True
    if master is not None:
        command = next((cmd for pid, cmd in out if pid == master), "")
        if not (command and _module_kind(command) == "harness"
                and _has_flag(command, "--all")):
            _log(f"held fleet lock PID {master} was not process-validated")
            trustworthy = False
    return out, trustworthy


def _fleet_procs(scope_home: str = "") -> list[tuple[int, str]]:
    """Compatibility/test view; main consumes the trust bit as well."""
    return _fleet_scan(scope_home)[0]


def _scope_home(gui_args: list[str]) -> str:
    """Canonical effective ``--home`` using argparse's last-value rule."""
    return _canonical_home(_last_option(gui_args, "--home"))


def _wait_gone(pid: int, timeout_s: float,
               expected_command: str | None = None) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        if expected_command:
            actual_command = _command_for_pid(pid)
            if actual_command == "" or (
                    actual_command is not None
                    and actual_command != expected_command):
                return True
        time.sleep(0.1)
    if not pid_alive(pid):
        return True
    if expected_command:
        actual_command = _command_for_pid(pid)
        return actual_command == "" or (
            actual_command is not None and actual_command != expected_command)
    return False


def _command_for_pid(pid: int) -> str | None:
    """Return the command, empty if absent, or None when scanning failed."""
    try:
        return next((cmd for current, cmd in _list_python_procs()
                     if current == pid), "")
    except ProcessScanError:
        return None


def _terminate_validated(pid: int, expected_command: str) -> bool:
    """Terminate only while a fresh scan proves the same exact process."""
    # Killing the harness master may already have reaped this member of its
    # process group. That is the desired end state, not failed validation.
    if not pid_alive(pid):
        return True
    actual_command = _command_for_pid(pid)
    if actual_command == "":
        return True
    if actual_command is None or not expected_command:
        _log(f"skip kill {pid}: process identity is unavailable")
        return False
    if actual_command != expected_command:
        _log(f"skip kill {pid}: original process is gone or PID was reused")
        return True
    try:
        if sys.platform == "win32" and _module_kind(expected_command) == "harness":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=30,
                creationflags=_NO_WINDOW, startupinfo=_hidden_startupinfo())
            if result.returncode:
                detail = " ".join((result.stderr or result.stdout).split())[:200]
                _log(f"taskkill {pid} failed rc={result.returncode}: {detail}")
                return False
        elif (_module_kind(expected_command) == "harness"
              and _has_flag(expected_command, "--all")
              and os.getpgid(pid) == pid):
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except OSError as exc:
        _log(f"kill {pid} failed: {type(exc).__name__}: {exc}")
        return False


def _hidden_startupinfo():
    """SW_HIDE startupinfo (V122): creation FLAGS only cover the direct
    child — the uv shim's own console-subsystem grandchild inherits the
    STARTUPINFO show state instead, and a default one popped a visible
    Windows Terminal per fleet spawn (the 'terminal opens' reports).
    This is the programmatic twin of Start-Process -WindowStyle Hidden."""
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def _spawn(cmd: list[str], cwd: str, output_path: Path) -> subprocess.Popen:
    flags = _NO_WINDOW
    if sys.platform == "win32":
        flags |= (subprocess.DETACHED_PROCESS
                  | subprocess.CREATE_NEW_PROCESS_GROUP)
    output = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = output_path.open("a", encoding="utf-8")
        destination = str(output_path)
    except OSError as exc:
        destination = "discarded"
        _log(f"launch log unavailable: {type(exc).__name__}: {exc}")
    _log("spawn: " + " ".join(cmd) + f" output={destination}")
    kwargs = {
        "cwd": cwd or None,
        "close_fds": True,
        "stdout": output or subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs.update(
            creationflags=flags, startupinfo=_hidden_startupinfo())
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kwargs)
    finally:
        if output is not None:
            output.close()


def _gui_endpoint(gui_args: list[str]) -> tuple[str, int]:
    host, port = "127.0.0.1", 7787
    for index, value in enumerate(gui_args):
        if value == "--host" and index + 1 < len(gui_args):
            host = gui_args[index + 1]
        elif value.startswith("--host="):
            host = value.split("=", 1)[1]
        elif value == "--port" and index + 1 < len(gui_args):
            try:
                port = int(gui_args[index + 1])
            except ValueError:
                pass
        elif value.startswith("--port="):
            try:
                port = int(value.split("=", 1)[1])
            except ValueError:
                pass
    if host in ("", "0.0.0.0", "*"):
        host = "127.0.0.1"
    elif host in ("::", "[::]"):
        host = "::1"
    return host, port


def _wait_gui_ready(proc: subprocess.Popen, host: str, port: int,
                    old_instance_id: str, timeout_s: float = 20.0) -> bool:
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{port}/api/state"
    deadline = time.time() + timeout_s
    consecutive = 0
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            instance_id = str(payload.get("instance_id") or "")
            try:
                server_pid = int(payload.get("server_pid") or 0)
            except (TypeError, ValueError):
                server_pid = 0
            if (instance_id and instance_id != old_instance_id
                    and server_pid > 0
                    and _is_descendant_or_self(server_pid, proc.pid)):
                consecutive += 1
                if consecutive >= 2 and proc.poll() is None:
                    return True
            else:
                consecutive = 0
        except (OSError, ValueError, urllib.error.URLError):
            consecutive = 0
        time.sleep(0.1)
    return False


def _parent_pid(pid: int) -> int | None:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter "
                 f"\"ProcessId={pid}\").ParentProcessId"],
                capture_output=True, text=True, timeout=5,
                creationflags=_NO_WINDOW, startupinfo=_hidden_startupinfo())
        else:
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5)
        if result.returncode:
            return None
        parent = int(result.stdout.strip())
        return parent if parent > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _is_descendant_or_self(pid: int, ancestor: int) -> bool:
    seen = set()
    while pid > 0 and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        pid = _parent_pid(pid) or 0
    return False


def _wait_harness_ready(proc: subprocess.Popen, lock_path: Path,
                        scope_home: str = "", timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        owner = locked_pid(lock_path)
        if owner is not None:
            command = _command_for_pid(owner) or ""
            scoped = (_has_scope(command, scope_home) if scope_home
                      else not _command_scope(command))
            if (_is_python_command(command)
                    and _module_kind(command) == "harness"
                    and _has_flag(command, "--all")
                    and scoped
                    and _is_descendant_or_self(owner, proc.pid)):
                return True
        time.sleep(0.1)
    return False


def _stop_child(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _pick_exe(cwd: str, fallback: str) -> str:
    """The checkout's own venv pythonw — the canonical fleet interpreter —
    when it exists; the caller's interpreter otherwise (V119)."""
    if cwd and sys.platform == "win32":
        venv_w = Path(cwd) / ".venv" / "Scripts" / "pythonw.exe"
        if venv_w.is_file():
            return str(venv_w)
    return fallback


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentbridge-restarter")
    ap.add_argument("--gui-pid", type=int, required=True)
    ap.add_argument("--exe", required=True, help="interpreter to relaunch with")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--old-instance-id", default="")
    ap.add_argument("--gui-args", default="[]",
                    help="JSON list: the old GUI's own argv[1:]")
    args = ap.parse_args(argv)

    try:
        gui_args = [str(a) for a in json.loads(args.gui_args)]
    except ValueError:
        gui_args = []
    scope = _scope_home(gui_args)
    exe = _pick_exe(args.cwd, args.exe)
    _log(f"start: gui_pid={args.gui_pid} exe={exe} cwd={args.cwd} "
         f"scope={scope or '(main)'} args={gui_args}")

    # 1. let the old GUI finish its response and exit on its own
    old_gui_command = _command_for_pid(args.gui_pid)
    old_gui_gone = _wait_gone(args.gui_pid, 20.0, old_gui_command)
    if not old_gui_gone:
        if _terminate_validated(args.gui_pid, old_gui_command):
            old_gui_gone = _wait_gone(
                args.gui_pid, 5.0, old_gui_command)
        elif old_gui_command:
            fresh_command = _command_for_pid(args.gui_pid)
            if fresh_command is not None and fresh_command != old_gui_command:
                old_gui_gone = True  # the original exited or its PID was reused
    _log(f"old gui gone={old_gui_gone}")

    # 2. clear what's left of THIS instance's fleet (the harness tree, a
    #    wedged GUI)
    procs, scan_ok = _fleet_scan(scope)
    _log(f"fleet scan: ok={scan_ok} {len(procs)} proc(s)")
    for pid, cmd in procs:
        _log(f"kill {pid}: {cmd[:120]}")
        if not _terminate_validated(pid, cmd):
            scan_ok = False
    fleet_gone = scan_ok
    for pid, command in procs:
        if not _wait_gone(pid, 10.0, command):
            _log(f"process {pid} remained alive after termination")
            fleet_gone = False

    # 3. relaunch: the GUI first (same args, window suppressed), then the
    #    harness. V122: the main app ALWAYS gets its harness back — the old
    #    "only if one was running" rule meant a restart could never
    #    resurrect an already-dead harness, which is exactly when the
    #    button gets pressed (the live fleet ran agentless for 40 minutes
    #    across three restarts). Scoped (rig) restarts still skip it.
    if "--no-browser" not in gui_args:
        gui_args.append("--no-browser")
    home = Path(scope) if scope else DEFAULT_HOME
    output_path = home / "launcher.log"
    host, port = _gui_endpoint(gui_args)
    gui_ok = False
    harness_ok = bool(scope)
    for attempt in (1, 2):
        try:
            gui = _spawn(
                [exe, "-m", "agentbridge.gui", *gui_args],
                args.cwd, output_path,
            )
        except Exception as exc:  # noqa: BLE001 - preserve harness recovery
            _log(f"GUI attempt {attempt} spawn failed: "
                 f"{type(exc).__name__}: {exc}")
            continue
        if _wait_gui_ready(
                gui, host, port, args.old_instance_id):
            gui_ok = True
            _log(f"GUI ready pid={gui.pid} endpoint={host}:{port}")
            break
        _log(f"GUI attempt {attempt} failed pid={gui.pid} "
             f"rc={gui.poll()}")
        _stop_child(gui)
    if not scope:
        if fleet_gone:
            try:
                harness = _spawn(
                    [exe, "-m", "agentbridge.harness", "--all"],
                    args.cwd, output_path,
                )
                harness_ok = _wait_harness_ready(
                    harness, home / "harness-all.lock")
                _log(f"harness ready={harness_ok} pid={harness.pid} "
                     f"rc={harness.poll()}")
            except Exception as exc:  # noqa: BLE001 - diagnostic boundary
                harness_ok = False
                _log(f"harness spawn failed: {type(exc).__name__}: {exc}")
        else:
            harness_ok = False
            _log("harness relaunch skipped: owned legacy fleet survived")
    ok = old_gui_gone and gui_ok and harness_ok
    _log(f"done: ok={ok} gui={gui_ok} harness={harness_ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
