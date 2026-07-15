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
def test_git_apply_refuses_non_default_branch(gitworld):
    _run("git", "-C", str(gitworld), "checkout", "-b", "feature")
    r = api_updates.git_check()
    assert r["newer"] and r["can_apply"] is False and "branch" in r["note"]
    resp = api_updates.update_apply(_App(), None)
    assert resp["ok"] is False


# ------------------------------------------------------- restarter (V113)
def test_restarter_scope_home_parsing():
    from agentbridge.gui.restarter import _scope_home

    assert _scope_home([]) == ""
    assert _scope_home(["--no-browser"]) == ""
    assert _scope_home(["--home", r"C:\t\ab66\h1", "--port", "7788"]) \
        == r"C:\t\ab66\h1"
    assert _scope_home([r"--home=C:\t\ab66\h1"]) == r"C:\t\ab66\h1"
    assert _scope_home(["--home"]) == ""          # dangling flag: no scope


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
    ]
    monkeypatch.setattr(restarter, "_list_python_procs", lambda: procs)
    assert [p for p, _ in restarter._fleet_procs()] == [1, 2]
    assert [p for p, _ in restarter._fleet_procs(r"C:\t\ab66\h1")] == [3]
    assert [p for p, _ in restarter._fleet_procs(r"C:\t\ab78\h1")] == [4]
