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

import os
from pathlib import Path

__all__ = ["SingleInstance"]


class SingleInstance:
    """Advisory whole-file lock at ``path``. ``acquire()`` returns True when
    this process took it, False when another live process holds it. The lock
    is held until ``release()`` (or process exit)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except OSError:
            return True  # can't create a lock file (odd FS) — don't block boot
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False  # another live instance holds it
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        self._fh = fh
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
