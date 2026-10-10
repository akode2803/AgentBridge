"""Agent timers — a run can schedule its own wake-up ("the target is dnd,
try again at 15:00"). Durable in the agent's store; due timers surface as
queue items so they ride the same dispatch pipeline (pause, rate cap,
answered-guard) as messages. The owner sees every pending timer through the
harness status doc (runner.py mirrors it) — nothing fires invisibly.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from ..core.timekit import new_id, utcnow_iso
from ..store.db import Store

__all__ = ["TimerService", "parse_at", "when_local", "parse_repeat",
           "next_occurrence", "repeat_label"]

TIMERS_DOC = "harness/timers"
MAX_TIMERS = 50  # per agent — a runaway scheduler can't amass an army
# V55: the note is the agent's brief to its future self — room for a real
# task description, not just a nudge (was 280)
NOTE_CHARS = 2000


def when_local(at_ns: int) -> str:
    """A fire time as unambiguous local wall clock (V74: state the zone —
    this machine's timezone may differ from the member being helped)."""
    dt = datetime.fromtimestamp(int(at_ns) / 1e9).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M %Z (UTC%z)").strip()


def parse_at(spec: str, *, now_s: float | None = None) -> int | None:
    """A human-shaped absolute time -> at_ns, or None when unparseable.
    'HH:MM' = the NEXT occurrence of that local wall-clock time;
    'YYYY-MM-DD HH:MM' = that local datetime; full ISO (with an offset)
    is honored as given. The harness runs on the owner's machine, so
    local time IS the owner's time (V55)."""
    s = str(spec or "").strip()
    if not s:
        return None
    base = datetime.fromtimestamp(
        now_s if now_s is not None else time.time()).astimezone()
    try:
        if len(s) <= 5 and ":" in s and "-" not in s:      # HH:MM
            hh, mm = s.split(":", 1)
            t = base.replace(hour=int(hh), minute=int(mm),
                             second=0, microsecond=0)
            if t <= base:
                t += timedelta(days=1)                      # rolls to tomorrow
            return int(t.timestamp() * 1e9)
        t = datetime.fromisoformat(s)                       # date-time / ISO
        if t.tzinfo is None:
            t = t.replace(tzinfo=base.tzinfo)
        return int(t.timestamp() * 1e9)
    except (ValueError, OverflowError):
        return None


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_repeat(spec) -> dict | None:
    """V88: a recurrence out of a tool-friendly STRING (or an already-shaped
    dict) — 'daily', 'weekly:mon,wed' (or numbers, Mon=0), 'monthly:15'.
    Invalid → None (a bad recurrence never breaks scheduling; the timer just
    fires once)."""
    if isinstance(spec, dict):
        return spec if spec.get("kind") in ("daily", "weekly", "monthly") else None
    s = str(spec or "").strip().lower()
    if not s:
        return None
    kind, _, arg = s.partition(":")
    if kind == "daily":
        return {"kind": "daily"}
    if kind == "weekly":
        days = []
        for part in (p.strip() for p in arg.split(",") if p.strip()):
            if part[:3] in _DAYS:
                days.append(_DAYS.index(part[:3]))
            elif part.isdigit() and int(part) <= 6:
                days.append(int(part))
        days = sorted(set(days))
        return {"kind": "weekly", "days": days} if days else None
    if kind == "monthly":
        if arg.isdigit() and 1 <= int(arg) <= 31:
            return {"kind": "monthly", "day": int(arg)}
        return None
    return None


def next_occurrence(at_ns: int, repeat: dict, *,
                    now_ns: int | None = None) -> int | None:
    """The occurrence AFTER ``at_ns`` that is also in the future — anchored
    to the schedule (same local wall-clock time; no drift from a late fire),
    and skipping past occurrences in one go when the harness was offline for
    days. None = the recurrence is unreadable."""
    repeat = parse_repeat(repeat)
    if not repeat:
        return None
    now = now_ns if now_ns is not None else time.time_ns()
    t = datetime.fromtimestamp(int(at_ns) / 1e9).astimezone()
    for _ in range(500):  # bounded: > a year of daily catch-up
        kind = repeat["kind"]
        if kind == "daily":
            t = t + timedelta(days=1)
        elif kind == "weekly":
            days = repeat.get("days") or [t.weekday()]
            for ahead in range(1, 8):
                cand = t + timedelta(days=ahead)
                if cand.weekday() in days:
                    t = cand
                    break
        else:  # monthly
            day = int(repeat.get("day") or t.day)
            year, month = t.year + (t.month // 12), (t.month % 12) + 1
            # clamp to the target month's length (Jan 31 → Feb 28)
            for d in range(day, 27, -1):
                try:
                    t = t.replace(year=year, month=month, day=d)
                    break
                except ValueError:
                    continue
        ns = int(t.timestamp() * 1e9)
        if ns > now:
            return ns
    return None


def repeat_label(repeat: dict | None) -> str:
    """'repeats daily' / 'repeats weekly on Mon, Wed' / 'repeats monthly on
    day 15' — one wording for chips, context, and confirmations."""
    repeat = parse_repeat(repeat)
    if not repeat:
        return ""
    if repeat["kind"] == "daily":
        return "repeats daily"
    if repeat["kind"] == "weekly":
        names = ", ".join(_DAYS[d].capitalize()
                          for d in repeat.get("days") or [])
        return f"repeats weekly on {names}" if names else "repeats weekly"
    return f"repeats monthly on day {repeat.get('day')}"


class TimerService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = threading.RLock()

    def _all(self) -> dict[str, dict]:
        return self.store.cached_doc(TIMERS_DOC, default={}) or {}

    def set(self, chat_id: str, at_ns: int, note: str,
            repeat: dict | str | None = None) -> str | None:
        """Schedule a wake-up; returns its id (None when the cap is hit).
        ``repeat`` (V88) makes it recurring — reschedule happens at the
        FIRE-side pop, so a dismissal cancels the whole series."""
        with self._lock:
            timers = self._all()
            if len(timers) >= MAX_TIMERS:
                return None
            tid = new_id("t")
            entry = {
                "id": tid, "chat_id": chat_id, "at_ns": int(at_ns),
                "note": (note or "")[:NOTE_CHARS], "created": utcnow_iso(),
            }
            rep = parse_repeat(repeat)
            if rep:
                entry["repeat"] = rep
            timers[tid] = entry
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
                           str(spec.get("note") or ""),
                           repeat=spec.get("repeat"))
            if tid:
                out.append(tid)
        return out

    def due(self) -> list[dict]:
        now = time.time_ns()
        return sorted(
            (t for t in self._all().values() if int(t.get("at_ns", 0)) <= now),
            key=lambda t: t.get("at_ns", 0),
        )

    def next_future_ns(self, *, now_ns: int | None = None) -> int | None:
        """Earliest timer still in the future; already-due work was just scanned."""
        now = time.time_ns() if now_ns is None else int(now_ns)
        future = []
        for timer in self._all().values():
            try:
                deadline = int(timer.get("at_ns", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if deadline > now:
                future.append(deadline)
        return min(future, default=None)

    def pop(self, timer_id: str, *, reschedule: bool = False) -> dict | None:
        """Remove a timer (it fired, or the owner cancelled it). V88: the
        FIRE paths pass ``reschedule=True`` so a recurring timer re-arms for
        its next occurrence (same id — the owner's chip stays one thing);
        the cancel/dismiss paths keep the default and end the series."""
        with self._lock:
            timers = self._all()
            t = timers.pop(timer_id, None)
            if t is not None:
                if reschedule and t.get("repeat"):
                    nxt = next_occurrence(int(t.get("at_ns", 0)),
                                          t.get("repeat"))
                    if nxt:
                        timers[timer_id] = {**t, "at_ns": nxt}
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
