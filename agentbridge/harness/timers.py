"""Agent timers — a run can schedule its own wake-up ("the target is dnd,
try again at 15:00"). Durable in the agent's store; due timers surface as
queue items so they ride the same dispatch pipeline (pause, rate cap,
answered-guard) as messages. The owner sees every pending timer through the
harness status doc (runner.py mirrors it) — nothing fires invisibly.
"""

from __future__ import annotations

import threading
import time

from ..core.timekit import new_id, utcnow_iso
from ..store.db import Store

__all__ = ["TimerService"]

TIMERS_DOC = "harness/timers"
MAX_TIMERS = 50  # per agent — a runaway scheduler can't amass an army


class TimerService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = threading.RLock()

    def _all(self) -> dict[str, dict]:
        return self.store.cached_doc(TIMERS_DOC, default={}) or {}

    def set(self, chat_id: str, at_ns: int, note: str) -> str | None:
        """Schedule a wake-up; returns its id (None when the cap is hit)."""
        with self._lock:
            timers = self._all()
            if len(timers) >= MAX_TIMERS:
                return None
            tid = new_id("t")
            timers[tid] = {
                "id": tid, "chat_id": chat_id, "at_ns": int(at_ns),
                "note": (note or "")[:280], "created": utcnow_iso(),
            }
            self.store.cache_doc(TIMERS_DOC, timers)
            return tid

    def add_from_reply(self, chat_id: str, specs: list[dict]) -> list[str]:
        """Timers a Reply asked for: ``{"in_s": seconds}`` or ``{"at_ns": ns}``
        plus a ``note``. Malformed specs are ignored, never fatal."""
        out = []
        for spec in specs or []:
            try:
                if spec.get("at_ns"):
                    at_ns = int(spec["at_ns"])
                else:
                    at_ns = time.time_ns() + int(float(spec["in_s"]) * 1e9)
            except (KeyError, TypeError, ValueError):
                continue
            tid = self.set(str(spec.get("chat_id") or chat_id), at_ns,
                           str(spec.get("note") or ""))
            if tid:
                out.append(tid)
        return out

    def due(self) -> list[dict]:
        now = time.time_ns()
        return sorted(
            (t for t in self._all().values() if int(t.get("at_ns", 0)) <= now),
            key=lambda t: t.get("at_ns", 0),
        )

    def pop(self, timer_id: str) -> dict | None:
        """Remove a timer (it fired, or the owner cancelled it)."""
        with self._lock:
            timers = self._all()
            t = timers.pop(timer_id, None)
            if t is not None:
                self.store.cache_doc(TIMERS_DOC, timers)
            return t

    def clear(self) -> int:
        """Cancel every scheduled wake-up (the peer-repair path for a runaway
        scheduler, R22.5). Returns how many were cancelled."""
        with self._lock:
            n = len(self._all())
            self.store.cache_doc(TIMERS_DOC, {})
            return n

    def snapshot(self) -> list[dict]:
        return sorted(self._all().values(), key=lambda t: t.get("at_ns", 0))
