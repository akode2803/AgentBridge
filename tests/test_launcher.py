import sys
from types import SimpleNamespace

from agentbridge.core import launcher
from agentbridge.core.lock import SingleInstance, locked_pid


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


def test_locked_pid_requires_a_live_advisory_owner(tmp_path):
    path = tmp_path / "fleet.lock"
    path.write_text("999999", encoding="ascii")
    assert locked_pid(path) is None

    lock = SingleInstance(path)
    assert lock.acquire() is True
    try:
        assert locked_pid(path) == __import__("os").getpid()
    finally:
        lock.release()
    assert locked_pid(path) is None


def test_strict_single_instance_fails_closed_when_lock_cannot_open(
        tmp_path, monkeypatch):
    import builtins

    monkeypatch.setattr(
        builtins, "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    assert SingleInstance(
        tmp_path / "fleet.lock", fail_open=False).acquire() is False


def test_strict_single_instance_releases_when_pid_publish_fails(
        tmp_path, monkeypatch):
    import builtins
    from agentbridge.core import lock as lock_module

    class File:
        closed = False

        def seek(self, _position):
            pass

        def truncate(self):
            pass

        def write(self, _value):
            raise OSError("publication failed")

        def close(self):
            self.closed = True

    file = File()
    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: file)
    monkeypatch.setattr(lock_module, "_try_lock", lambda _fh: True)
    assert SingleInstance(
        tmp_path / "fleet.lock", fail_open=False).acquire() is False
    assert file.closed is True


def test_windows_lock_preserves_legacy_byte_zero(monkeypatch):
    from agentbridge.core import lock as lock_module

    positions = []

    class File:
        def seek(self, position):
            self.position = position

        def fileno(self):
            return 5

    fake_msvcrt = SimpleNamespace(
        LK_NBLCK=1,
        locking=lambda _fd, _mode, _length: positions.append(file.position),
    )
    file = File()
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    assert lock_module._try_lock(file, platform="nt") is True
    assert positions == [0]


def test_lock_attempt_distinguishes_contention_from_io_failure(monkeypatch):
    from agentbridge.core import lock as lock_module

    class File:
        def seek(self, _position):
            pass

        def fileno(self):
            return 5

    def denied(_fd, _mode, _length):
        raise OSError(13, "held")

    fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=denied)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    assert lock_module._try_lock(File(), platform="nt") is False

    def broken(_fd, _mode, _length):
        raise OSError(5, "device error")

    fake_msvcrt.locking = broken
    assert lock_module._try_lock(File(), platform="nt") is None


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
