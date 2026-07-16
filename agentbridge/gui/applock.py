"""V111 app lock — WhatsApp's screen lock, for the desktop app window.

An optional LOCAL passphrase gate over the machine-trust model: R75's
password-free session restore means anyone at the Windows profile has the
signed-in app, by design — the lock adds a second, purely local layer for
the walk-past case. It does NOT replace the account password and never
touches the mesh: what's stored is a scrypt VERIFIER (salt + hash) in the
local home dir, the locked flag is per-process, and agents keep running
while locked.

The lock covers the API, not just the window (the backlog's rail — a
UI-only cover would be cosmetic): ``routing.authed`` refuses every data
endpoint and ``app.py`` refuses new SSE streams while locked. Wrong
attempts back off (free tries, then exponential to a cap) so the localhost
API can't be brute-forced faster than the lock screen.

Recovery is honest about the boundary: the signed-in account's password
also unlocks (it already grants a full sign-out/sign-in swap, so accepting
it here loses nothing) — and someone with filesystem access can always
delete this file, which is exactly the machine-trust line the lock sits on.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

from ..core.config import atomic_write_json, read_json

__all__ = ["AppLock"]

_FILE = "applock.json"
_FREE_TRIES = 3          # misses before the backoff starts
_MAX_COOLDOWN_S = 30.0


def _hash(passphrase: str, salt: bytes) -> bytes:
    # stdlib scrypt (the GUI layer stays stdlib-only); parameters mirror
    # accounts.py's password hashing
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                          n=2 ** 14, r=8, p=1, dklen=32)


class AppLock:
    """One per GuiApp. ``enabled`` = the verifier file exists; ``locked``
    starts True at boot whenever enabled (launch always asks)."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / _FILE
        self._mx = threading.Lock()
        self._fails = 0
        self._cooldown_until = 0.0
        self.locked = self.enabled

    @property
    def enabled(self) -> bool:
        return self.path.is_file()

    def _doc(self) -> dict:
        return read_json(self.path, default=None) or {}

    @property
    def autolock_min(self) -> int:
        """Idle minutes before the client locks itself; 0 = manual only."""
        try:
            return max(0, int(self._doc().get("autolock_min", 0)))
        except (TypeError, ValueError):
            return 0

    def status(self) -> dict:
        return {"enabled": self.enabled, "locked": self.locked,
                "autolock_min": self.autolock_min}

    # ------------------------------------------------------------ verifying
    def retry_in(self) -> float:
        """Seconds of cooldown still in force (0 = attempts are open)."""
        return max(0.0, self._cooldown_until - time.monotonic())

    def verify(self, passphrase: str) -> bool:
        doc = self._doc()
        try:
            salt = base64.b64decode(doc["salt"])
            want = base64.b64decode(doc["hash"])
        except (KeyError, TypeError, ValueError):
            return False
        return secrets.compare_digest(_hash(passphrase or "", salt), want)

    def note_failure(self) -> float:
        """Record a wrong attempt; returns the cooldown now in force."""
        with self._mx:
            self._fails += 1
            extra = self._fails - _FREE_TRIES
            if extra < 0:
                return 0.0
            wait = min(float(2 ** extra), _MAX_COOLDOWN_S)
            self._cooldown_until = time.monotonic() + wait
            return wait

    def note_success(self) -> None:
        with self._mx:
            self._fails = 0
            self._cooldown_until = 0.0

    # -------------------------------------------------------------- actions
    def lock(self) -> None:
        if self.enabled:
            self.locked = True

    def unlock(self) -> None:
        self.locked = False

    def configure(self, passphrase: str, autolock_min: int = 0) -> None:
        salt = os.urandom(16)
        atomic_write_json(self.path, {
            "algo": "scrypt",
            "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(_hash(passphrase, salt)).decode(),
            "autolock_min": max(0, int(autolock_min)),
        })

    def set_autolock(self, autolock_min: int) -> None:
        doc = self._doc()
        if doc:
            doc["autolock_min"] = max(0, int(autolock_min))
            atomic_write_json(self.path, doc)

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
        self.locked = False
