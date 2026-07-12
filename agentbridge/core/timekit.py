"""Time and ordering primitives.

THE rule (FORMAT2 tenet 2, learned the hard way in v1): `ns` — a per-process
strictly-monotonic nanosecond ordinal — is the ONLY thing cursors, sorts, and
comparisons may use. `ts` (ISO seconds) is display-only; two messages in one
second tie on `ts`, and a strict `>` against a tied cursor skips one forever.
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timezone

__all__ = ["utcnow", "utcnow_iso", "next_ns", "new_id"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Second-resolution ISO timestamp — DISPLAY ONLY, never for ordering."""
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class _NsClock:
    """time.time_ns() with a strictly-monotonic per-process guard.

    The guard is per-process only (as in v1): cross-process ordering relies on
    ns being wall-clock-anchored, which is fine for the mesh's use (cursors are
    per-reader; ties across processes are broken deterministically by sender
    name at fold time, never trusted for causality).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0

    def next(self) -> int:
        with self._lock:
            now = time.time_ns()
            if now <= self._last:
                now = self._last + 1
            self._last = now
            return now


_clock = _NsClock()


def next_ns() -> int:
    """A strictly-increasing nanosecond ordinal (thread-safe, per-process)."""
    return _clock.next()


def new_id(prefix: str, ns: int | None = None) -> str:
    """Opaque unique id, e.g. message ids: ``m-<ns>-<4 hex bytes>``."""
    n = next_ns() if ns is None else ns
    return f"{prefix}-{n}-{secrets.token_hex(4)}"
