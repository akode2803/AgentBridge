from types import SimpleNamespace

from agentbridge.core import launcher


def test_project_python_is_platform_aware(tmp_path):
    win = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    posix = tmp_path / ".venv" / "bin" / "python3"
    win.parent.mkdir(parents=True)
    posix.parent.mkdir(parents=True)
    win.touch()
    posix.touch()

    assert launcher.find_project_python(tmp_path, platform="win32") == win
    assert launcher.find_project_python(tmp_path, platform="darwin") == posix


def test_project_python_falls_back_to_current_interpreter(tmp_path):
    fallback = tmp_path / "fallback-python"
    assert launcher.find_project_python(
        tmp_path, platform="darwin", fallback=str(fallback)) == fallback


def test_launch_module_uses_repo_venv_and_log(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    log_path = tmp_path / "home" / "launcher.log"
    seen = {}

    def fake_popen(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    child = launcher.launch_module(
        tmp_path, "agentbridge.gui", ["--no-browser"], log_path=log_path)

    assert child.pid == 123
    assert seen["command"] == [str(python), "-m", "agentbridge.gui",
                               "--no-browser"]
    assert seen["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert seen["kwargs"]["start_new_session"] is True
    assert "launching agentbridge.gui" in log_path.read_text(encoding="utf-8")


def test_run_launcher_records_spawn_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        launcher, "launch_module",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")))

    assert launcher.run_launcher(tmp_path, "agentbridge.gui") is False
    text = (tmp_path / ".agentbridge" / "launcher.log").read_text("utf-8")
    assert "OSError: spawn failed" in text
