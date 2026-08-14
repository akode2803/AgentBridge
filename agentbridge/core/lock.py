"""Single-instance file lock (leaf layer — no internal deps).

An OS advisory lock on a file: whoever holds it is "the one instance"; a second
acquirer fails fast. The kernel frees the lock the instant the holder dies, so
there's no stale-PID cleanup and a crash never wedges the next launch.

Used by the GUI to stop a second server (a double-clicked ``AgentBridge.pyw``
beside the supervised fleet) from co-binding the app port — on Windows
``SO_REUSEADDR`` lets two sockets share a port silently, so the bind alone is
not a guard. ``harness/runner.py`` carries its own equivalent for the per-agent
run lock; this is the shared home for the same idea and new callers should use
it (the harness copy can migrate here later).
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

__all__ = ["SingleInstance", "locked_pid"]


def _try_lock(fh, *, platform: str | None = None) -> bool | None:
    """Return True if acquired, False if held, or None on lock failure."""
    platform = platform or os.name
    try:
        if platform == "nt":
            import msvcrt

            # Byte zero preserves compatibility with already-running releases.
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fh.seek(0)
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) \
                or getattr(exc, "winerror", None) in (33, 36):
            return False
        return None


def locked_pid(path: Path | str) -> int | None:
    """Return the PID written by a currently held lock, never a stale file.

    This is an ownership hint, not sufficient process identity on its own;
    callers that signal it must also validate the expected command.
    """
    try:
        fh = open(Path(path), "a+")
    except OSError:
        return None
    try:
        state = _try_lock(fh)
        if state is not False:
            return None
        # The owner writes immediately after taking the lock. Read only after
        # observing contention so a previous owner's stale PID is not sampled
        # before the current owner has had the chance to replace it.
        try:
            if os.name == "nt":
                # Windows denies reads overlapping another handle's locked
                # byte, so PID metadata lives beside the compatibility lock.
                raw = Path(str(path) + ".pid").read_text("ascii").strip()
            else:
                fh.seek(0)
                raw = fh.read().strip()
        except OSError:
            return None
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None
    finally:
        try:
            fh.close()
        except OSError:
            pass


class SingleInstance:
    """Advisory instance lock at ``path``. ``acquire()`` returns True when
    this process took it, False when another live process holds it. The lock
    is held until ``release()`` (or process exit)."""

    def __init__(self, path: Path | str, *, fail_open: bool = True) -> None:
        self.path = Path(path)
        self.fail_open = fail_open
        self._fh = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except OSError:
            # Existing GUI/per-agent callers retain the historical availability
            # fallback. Fleet masters opt into strict duplicate prevention.
            return self.fail_open
        state = _try_lock(fh)
        if state is not True:
            fh.close()
            return False if state is False else self.fail_open
        self._fh = fh
        tmp_path = None
        try:
            if os.name == "nt":
                pid_path = Path(str(self.path) + ".pid")
                tmp_path = Path(str(pid_path) + f".tmp-{os.getpid()}")
                tmp_path.write_text(str(os.getpid()), encoding="ascii")
                os.replace(tmp_path, pid_path)
            else:
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()))
                fh.flush()
        except OSError:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if not self.fail_open:
                self.release()
                return False
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self) -> "SingleInstance":
        self.acquired = self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
