"""V45 + V51: the update channels — git first, then releases, then the R11
peer hint, then an honest miss. The git tests run against a REAL scratch
origin/clone pair (no network); endpoint tests stub the git channel out so
the suite never fetches the actual repo.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agentbridge.gui import api_updates

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed")


class _App:
    mesh = object()   # non-None satisfies @authed


def _no_git(monkeypatch):
    monkeypatch.setattr(api_updates, "git_check", lambda *a, **k: None)


def test_ver_tuple_numeric():
    vt = api_updates.ver_tuple
    assert vt("v0.24.132") == (0, 24, 132)
    assert vt("0.25.1-beta") == (0, 25, 1)
    assert vt("1.2") < vt("1.10")            # numeric, not lexicographic
    assert vt("") == ()


def test_release_channel_and_honest_miss(monkeypatch):
    """No git → releases; releases offline → honest ok:False (no peers)."""
    _no_git(monkeypatch)

    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(api_updates, "fetch_latest", boom)
    r = api_updates.update_check(_App(), None)
    assert r["ok"] is False and "current" in r

    monkeypatch.setattr(api_updates, "fetch_latest", lambda *a, **k: {
        "tag_name": "v99.0.0", "html_url": "https://x/rel",
        "assets": [{"browser_download_url": "https://x/dl.exe"}]})
    r = api_updates.update_check(_App(), None)
    assert r["ok"] and r["newer"] and r["url"] == "https://x/dl.exe"
    assert r["latest"] == "v99.0.0" and r["channel"] == "release"
    assert r["can_apply"] is False           # releases download, never apply


def test_peer_hint_is_detection_only(monkeypatch):
    """git + releases unreachable → the R11 machine registry's version
    advert answers, and it can never apply (applink update.py's rail)."""
    _no_git(monkeypatch)
    monkeypatch.setattr(api_updates, "release_check", lambda *a, **k: None)

    class Reg:
        def peers(self):
            return [{"machine": "avd", "app_version": "99.0.0"},
                    {"machine": "old", "app_version": "0.1.0"}]

    class Link:
        registry = Reg()

        def announce(self, caps):
            return {}

    class MeshStub:
        applink = Link()

    class App2:
        mesh = MeshStub()

    r = api_updates.update_check(App2(), None)
    assert r["ok"] and r["channel"] == "peer" and r["newer"]
    assert r["latest"] == "99.0.0" and "avd" in r["note"]
    assert r["can_apply"] is False and not r["url"]

    # nobody newer → the peer channel stays silent → honest miss
    Reg.peers = lambda self: [{"machine": "old", "app_version": "0.1.0"}]
    r = api_updates.update_check(App2(), None)
    assert r["ok"] is False


# ------------------------------------------------------------- git channel
def _run(*args: str, cwd=None) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert p.returncode == 0, f"{args}: {p.stderr}"
    return p.stdout.strip()


@pytest.fixture
def gitworld(tmp_path, monkeypatch):
    """A real bare origin whose main carries __version__ 99.1.0, plus an
    'install' clone one commit behind (at 99.0.0). repo_root() is pointed
    at the install so the module under test sees it as its own checkout."""
    origin = tmp_path / "origin.git"
    _run("git", "init", "--bare", str(origin))
    _run("git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")

    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    seed = tmp_path / "seed"
    _run("git", "clone", str(origin), str(seed))
    _run("git", "-C", str(seed), "checkout", "-B", "main")
    pkg = seed / "agentbridge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "99.0.0"\n',
                                     encoding="utf-8")
    _run("git", "-C", str(seed), *ident, "add", "-A")
    _run("git", "-C", str(seed), *ident, "commit", "-m", "v99.0.0")
    _run("git", "-C", str(seed), "push", "-u", "origin", "main")

    install = tmp_path / "install"
    _run("git", "clone", str(origin), str(install))

    (pkg / "__init__.py").write_text('__version__ = "99.1.0"\n',
                                     encoding="utf-8")
    _run("git", "-C", str(seed), *ident, "add", "-A")
    _run("git", "-C", str(seed), *ident, "commit", "-m", "v99.1.0")
    _run("git", "-C", str(seed), "push", "origin", "main")

    monkeypatch.setattr(api_updates, "repo_root", lambda: install)
    return install


@requires_git
def test_git_check_detects_and_apply_updates(gitworld):
    r = api_updates.git_check()
    assert r is not None and r["channel"] == "git"
    assert r["latest"] == "99.1.0" and r["newer"] and r["can_apply"]

    resp = api_updates.update_apply(_App(), None)
    assert resp["ok"] and resp["updated"] and resp["version"] == "99.1.0"
    assert "restart" in resp["note"].lower()
    head = (gitworld / "agentbridge" / "__init__.py").read_text("utf-8")
    assert "99.1.0" in head                  # the ff-merge really landed


@requires_git
def test_git_apply_refuses_dirty_tree(gitworld):
    (gitworld / "agentbridge" / "__init__.py").write_text(
        '__version__ = "99.0.0"  # local edit\n', encoding="utf-8")
    r = api_updates.git_check()
    assert r["newer"] and r["can_apply"] is False
    assert "local changes" in r["note"]
    resp = api_updates.update_apply(_App(), None)
    assert resp["ok"] is False and "local changes" in resp["note"]


@requires_git
def test_git_apply_ignores_untracked_files(gitworld):
    """V123: a stray untracked file blocked "Update now" forever ("local
    changes on this machine") — but an ff-merge never touches untracked
    files. The dirty rail now counts tracked modifications only."""
    (gitworld / "Detailed prompt.txt").write_text("scratch", encoding="utf-8")
    r = api_updates.git_check()
    assert r["newer"] and r["can_apply"] is True and not r["note"]
    resp = api_updates.update_apply(_App(), None)
    assert resp["ok"] and resp["updated"] and resp["version"] == "99.1.0"
    assert (gitworld / "Detailed prompt.txt").read_text("utf-8") == "scratch"


@requires_git
def test_git_apply_untracked_name_collision_fails_honestly(gitworld):
    """The one case where an untracked file DOES matter: the incoming tree
    creates the same path. Git refuses the merge itself, the endpoint
    reports the failure, and the local file survives untouched."""
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    seed = gitworld.parent / "seed"
    (seed / "NEW.txt").write_text("from origin", encoding="utf-8")
    _run("git", "-C", str(seed), *ident, "add", "-A")
    _run("git", "-C", str(seed), *ident, "commit", "-m", "adds NEW.txt")
    _run("git", "-C", str(seed), "push", "origin", "main")
    (gitworld / "NEW.txt").write_text("local, different", encoding="utf-8")
    resp = api_updates.update_apply(_App(), None)
    assert resp["ok"] is False and "update failed" in resp["note"]
    assert (gitworld / "NEW.txt").read_text("utf-8") == "local, different"


@requires_git
def test_git_apply_refuses_non_default_branch(gitworld):
    _run("git", "-C", str(gitworld), "checkout", "-b", "feature")
    r = api_updates.git_check()
    assert r["newer"] and r["can_apply"] is False and "branch" in r["note"]
    resp = api_updates.update_apply(_App(), None)
    assert resp["ok"] is False


# ------------------------------------------------------- restarter (V113)
def test_restarter_scope_home_parsing():
    from agentbridge.gui.restarter import _canonical_home, _scope_home

    assert _scope_home([]) == ""
    assert _scope_home(["--no-browser"]) == ""
    assert _scope_home(["--home", r"C:\t\ab66\h1", "--port", "7788"]) \
        == _canonical_home(r"C:\t\ab66\h1")
    assert _scope_home([r"--home=C:\t\ab66\h1"]) \
        == _canonical_home(r"C:\t\ab66\h1")
    assert _scope_home([
        "--home", "old", "--home=new",
    ]) == _canonical_home("new")
    assert _scope_home(["--home"]) == ""          # dangling flag: no scope


def test_restarter_scope_home_canonicalizes_symlinks(tmp_path):
    from agentbridge.gui.restarter import _has_scope, _scope_home

    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    assert _scope_home(["--home", str(alias)]) == str(actual.resolve())
    assert _has_scope(
        f"python -m agentbridge.harness --all --home {alias}",
        str(actual),
    )


def test_restarter_fleet_scoping(monkeypatch):
    """The main app never touches a rig (--home in cmdline); a rig restart
    touches ONLY processes naming its own home."""
    from agentbridge.gui import restarter

    procs = [
        (1, r"pythonw.exe -m agentbridge.gui --no-browser"),
        (2, r"pythonw.exe -m agentbridge.harness --all"),
        (3, r"python.exe -m agentbridge.gui --home C:\t\ab66\h1 --port 7788"),
        (4, r"python.exe -m agentbridge.harness scout --home C:\t\ab78\h1"),
        (5, r"python.exe -m agentbridge.gui.restarter --gui-pid 1"),
        (6, r"python.exe -m hermes_cli.main gateway run"),
        (7, r'''python.exe -c "print('-m agentbridge.gui')"'''),
        (8, r"python.exe -m agentbridge.guix --home C:\t\ab66\h1"),
        (9, r"/bin/sh -c python -m agentbridge.gui"),
        (10, r"python.exe -m agentbridge.gui --home C:\t\ab66\h1 "
             r"--home C:\t\ab78\h1"),
        (11, r"python.exe -m agentbridge.harness --all --home="),
        (12, r"python.exe -m another_tool -m agentbridge.harness --all"),
    ]
    monkeypatch.setattr(restarter, "_list_python_procs", lambda: procs)
    monkeypatch.setattr(restarter, "locked_pid", lambda _path: None)
    assert [p for p, _ in restarter._fleet_procs()] == [1, 2, 11]
    assert [p for p, _ in restarter._fleet_procs(r"C:\t\ab66\h1")] == [3]
    assert [p for p, _ in restarter._fleet_procs(r"C:\t\ab78\h1")] == [4, 10]


def test_restarter_requires_command_validation_for_held_master(monkeypatch,
                                                                tmp_path):
    from agentbridge.gui import restarter

    monkeypatch.setattr(restarter, "DEFAULT_HOME", tmp_path)
    monkeypatch.setattr(restarter, "locked_pid", lambda _path: 41)
    monkeypatch.setattr(restarter, "_list_python_procs", lambda: [
        (41, "python -m unrelated --all"),
        (42, "python -m agentbridge.harness --all"),
    ])
    assert [pid for pid, _ in restarter._fleet_procs()] == [42]


def test_restarter_process_scan_failure_is_not_an_empty_success(monkeypatch):
    from types import SimpleNamespace
    from agentbridge.gui import restarter

    monkeypatch.setattr(
        restarter.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="operation not permitted"),
    )
    monkeypatch.setattr(restarter, "_log", lambda _message: None)
    assert restarter._fleet_scan() == ([], False)


def test_restarter_endpoint_and_readiness(monkeypatch, tmp_path):
    from agentbridge.gui import restarter

    assert restarter._gui_endpoint([]) == ("127.0.0.1", 7787)
    assert restarter._gui_endpoint(
        ["--host=localhost", "--port", "8899"],
    ) == ("localhost", 8899)
    assert restarter._gui_endpoint(
        ["--host", "0.0.0.0", "--port=7788"],
    ) == ("127.0.0.1", 7788)
    assert restarter._gui_endpoint(
        ["--host=::", "--port", "7789"],
    ) == ("::1", 7789)

    class Proc:
        pid = 73

        def poll(self):
            return None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"instance_id":"new-generation","server_pid":73}'

    proc = Proc()
    monkeypatch.setattr(
        restarter.urllib.request, "urlopen",
        lambda *_args, **_kw: Response())
    assert restarter._wait_gui_ready(
        proc, "127.0.0.1", 7787, "old-generation", 0.5)
    monkeypatch.setattr(restarter, "locked_pid", lambda _path: 74)
    monkeypatch.setattr(
        restarter, "_command_for_pid",
        lambda _pid: "python -m agentbridge.harness --all")
    monkeypatch.setattr(
        restarter, "_is_descendant_or_self",
        lambda pid, ancestor: (pid, ancestor) == (74, 73))
    assert restarter._wait_harness_ready(
        proc, tmp_path / "harness-all.lock", timeout_s=0.1)


def test_restarter_spawn_captures_output_and_detaches(monkeypatch, tmp_path):
    from agentbridge.gui import restarter

    seen = {}

    class Proc:
        pid = 88

    def fake_popen(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return Proc()

    monkeypatch.setattr(restarter.sys, "platform", "darwin")
    monkeypatch.setattr(restarter.subprocess, "Popen", fake_popen)
    output = tmp_path / "launcher.log"
    proc = restarter._spawn(["python", "-m", "agentbridge.gui"],
                            str(tmp_path), output)
    assert proc.pid == 88
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stderr"] is restarter.subprocess.STDOUT
    assert seen["kwargs"]["stdout"].name == str(output)


def test_restarter_spawn_survives_unavailable_launch_log(monkeypatch,
                                                          tmp_path):
    from agentbridge.gui import restarter

    seen = {}

    class Proc:
        pid = 89

    monkeypatch.setattr(restarter.sys, "platform", "darwin")
    monkeypatch.setattr(
        restarter.Path, "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    monkeypatch.setattr(restarter, "_log", lambda _message: None)
    monkeypatch.setattr(
        restarter.subprocess, "Popen",
        lambda command, **kwargs: (seen.update(command=command, kwargs=kwargs)
                                   or Proc()),
    )
    proc = restarter._spawn(
        ["python", "-m", "agentbridge.gui"], str(tmp_path),
        tmp_path / "launcher.log",
    )
    assert proc.pid == 89
    assert seen["kwargs"]["stdout"] is restarter.subprocess.DEVNULL


def test_restart_endpoint_detaches_posix_helper(monkeypatch):
    import sys
    import threading

    seen = {}

    class Server:
        def shutdown(self):
            pass

    class App:
        mesh = object()
        server = Server()
        instance_id = "old-generation"

    class Timer:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(threading, "Timer", Timer)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda command, **kwargs: seen.update(command=command, kwargs=kwargs),
    )
    result = api_updates.app_restart(App(), None)
    assert result["ok"] is True
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["command"][1:3] == ["-m", "agentbridge.gui.restarter"]
    old_id_index = seen["command"].index("--old-instance-id")
    assert seen["command"][old_id_index + 1] == "old-generation"


def test_restart_endpoint_pins_ephemeral_port_to_actual_server():
    server = type("Server", (), {"server_address": ("127.0.0.1", 43210)})()
    assert api_updates._restart_gui_args(
        ["--port", "0", "--host", "0.0.0.0"], server,
    ) == ["--host", "0.0.0.0", "--port", "43210"]
    assert api_updates._restart_gui_args(
        ["--port=0", "--no-browser"], server,
    ) == ["--no-browser", "--port", "43210"]


def test_restarter_reused_pid_is_not_signalled_and_original_counts_gone(
        monkeypatch):
    from agentbridge.gui import restarter

    calls = []
    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(restarter, "_command_for_pid", lambda _pid: "changed")
    monkeypatch.setattr(restarter.os, "kill", lambda *_args: calls.append(_args))
    assert restarter._terminate_validated(
        45, "python -m agentbridge.gui") is True
    assert calls == []


def test_restarter_treats_already_reaped_fleet_member_as_terminated(
        monkeypatch):
    from agentbridge.gui import restarter

    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        restarter, "_command_for_pid",
        lambda _pid: (_ for _ in ()).throw(AssertionError("must not rescan")),
    )
    assert restarter._terminate_validated(
        45, "python -m agentbridge.harness codex") is True


def test_restarter_treats_pid_absent_from_trusted_scan_as_terminated(
        monkeypatch):
    from agentbridge.gui import restarter

    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(restarter, "_command_for_pid", lambda _pid: "")
    assert restarter._terminate_validated(
        45, "python -m agentbridge.harness codex") is True


def test_restarter_wait_treats_zombie_absent_from_scan_as_gone(monkeypatch):
    from agentbridge.gui import restarter

    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(restarter, "_command_for_pid", lambda _pid: "")
    assert restarter._wait_gone(
        45, 0.1, "python -m agentbridge.harness codex") is True


def test_restarter_never_treats_unknown_scan_as_terminated(monkeypatch):
    from agentbridge.gui import restarter

    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(restarter, "_command_for_pid", lambda _pid: None)
    assert restarter._terminate_validated(
        45, "python -m agentbridge.harness codex") is False


def test_restarter_posix_master_termination_uses_owned_process_group(
        monkeypatch):
    from agentbridge.gui import restarter

    command = "python3 -m agentbridge.harness --all"
    seen = []
    monkeypatch.setattr(restarter.sys, "platform", "darwin")
    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(restarter, "_command_for_pid", lambda _pid: command)
    monkeypatch.setattr(restarter.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        restarter.os, "killpg", lambda pid, sig: seen.append((pid, sig)))
    assert restarter._terminate_validated(46, command) is True
    assert seen == [(46, restarter.signal.SIGTERM)]


def test_restarter_windows_harness_termination_uses_task_tree(monkeypatch):
    from types import SimpleNamespace
    from agentbridge.gui import restarter

    command = "python.exe -m agentbridge.harness --all"
    seen = {}
    monkeypatch.setattr(restarter.sys, "platform", "win32")
    monkeypatch.setattr(restarter, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(restarter, "_command_for_pid", lambda _pid: command)
    monkeypatch.setattr(restarter, "_hidden_startupinfo", lambda: None)
    monkeypatch.setattr(
        restarter.subprocess, "run",
        lambda args, **kwargs: (seen.update(args=args, kwargs=kwargs)
                                or SimpleNamespace(
                                    returncode=0, stdout="", stderr="")),
    )
    assert restarter._terminate_validated(47, command) is True
    assert seen["args"] == ["taskkill", "/PID", "47", "/T", "/F"]


def test_restarter_unknown_scan_never_spawns_duplicate_harness(monkeypatch,
                                                               tmp_path):
    from agentbridge.gui import restarter

    commands = []

    class Proc:
        pid = 91

        def poll(self):
            return None

    monkeypatch.setattr(restarter, "_wait_gone", lambda *_args: True)
    monkeypatch.setattr(restarter, "_fleet_scan", lambda _scope: ([], False))
    monkeypatch.setattr(restarter, "_pick_exe", lambda _cwd, fallback: fallback)
    monkeypatch.setattr(
        restarter, "_spawn",
        lambda command, _cwd, _output: (commands.append(command) or Proc()),
    )
    monkeypatch.setattr(restarter, "_wait_gui_ready", lambda *_args: True)
    monkeypatch.setattr(restarter, "_log", lambda _message: None)

    result = restarter.main([
        "--gui-pid", "90", "--exe", "python", "--cwd", str(tmp_path),
    ])
    assert result == 1
    assert len(commands) == 1
    assert commands[0][1:3] == ["-m", "agentbridge.gui"]


def test_restarter_gui_spawn_failure_still_recovers_harness(monkeypatch,
                                                             tmp_path):
    from agentbridge.gui import restarter

    commands = []

    class Proc:
        pid = 92

        def poll(self):
            return None

    def spawn(command, _cwd, _output):
        commands.append(command)
        if command[2] == "agentbridge.gui":
            raise OSError("gui spawn failed")
        return Proc()

    monkeypatch.setattr(restarter, "_wait_gone", lambda *_args: True)
    monkeypatch.setattr(restarter, "_fleet_scan", lambda _scope: ([], True))
    monkeypatch.setattr(restarter, "_pick_exe", lambda _cwd, fallback: fallback)
    monkeypatch.setattr(restarter, "_spawn", spawn)
    monkeypatch.setattr(restarter, "_wait_harness_ready", lambda *_args: True)
    monkeypatch.setattr(restarter, "_log", lambda _message: None)

    result = restarter.main([
        "--gui-pid", "90", "--exe", "python", "--cwd", str(tmp_path),
    ])
    assert result == 1
    assert [command[2] for command in commands] == [
        "agentbridge.gui", "agentbridge.gui", "agentbridge.harness",
    ]
