from types import SimpleNamespace

from agentbridge.gui import desktop


def _chrome_only(path):
    return path.name == "Google Chrome.app" and path.parent.name == "Applications"


def test_macos_launch_focuses_existing_app_window(monkeypatch):
    focused = []
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.Path, "exists", _chrome_only)
    monkeypatch.setattr(
        desktop, "_focus_macos_window",
        lambda app, url: focused.append((app, url)) or True,
    )
    monkeypatch.setattr(
        desktop.subprocess, "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")),
    )

    desktop.launch_window("http://127.0.0.1:7787/")

    assert focused == [("Google Chrome", "http://127.0.0.1:7787/")]


def test_macos_launch_opens_first_window_when_none_exists(monkeypatch):
    spawned = []
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.Path, "exists", _chrome_only)
    monkeypatch.setattr(desktop, "_focus_macos_window", lambda *a: False)
    monkeypatch.setattr(
        desktop.subprocess, "Popen",
        lambda command, **kwargs: spawned.append((command, kwargs)),
    )

    desktop.launch_window("http://127.0.0.1:7787/")

    assert spawned[0][0] == [
        "open", "-na", "Google Chrome", "--args",
        "--app=http://127.0.0.1:7787/", "--window-size=1240,860",
    ]


def test_macos_focus_targets_origin_across_hash_routes(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="focused\n")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    assert desktop._focus_macos_window(
        "Google Chrome", "http://127.0.0.1:7787/#/settings/connection")
    script = seen["command"][2]
    assert 'tell application "Google Chrome"' in script
    assert 'starts with "http://127.0.0.1:7787/"' in script
