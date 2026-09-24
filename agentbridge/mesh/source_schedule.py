"""Bounded ingestion scheduling hints; no freshness or membership authority.

Pure policy for the existing sync loop. Does not create threads, watchers,
provider calls, or browser timers. Inactive until the local-input owner is wired.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class IngestionJob:
    chat_id: str
    serial: int


@dataclass
class _State:
    due: float
    order: int
    idle: int = 0
    failures: int = 0
    rerun_at: float | None = None


class SourceSchedule:
    def __init__(self, *, background_s=4.0, capacity=128):
        if type(capacity) is not int or not 1 <= capacity <= 128:
            raise ValueError('invalid ingestion queue capacity')
        if type(background_s) not in (int, float) or not math.isfinite(background_s) or not 0.35 <= background_s <= 300:
            raise ValueError('invalid ingestion background cadence')
        self.background = float(background_s)
        self.capacity = capacity
        self._lock = threading.Lock()
        self._states = {}
        self._selected = None
        self._lease_until = self._hot_until = 0.0
        self._serial = self._order = self._selected_runs = 0
        self._running = None

    @staticmethod
    def _time(now):
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError('invalid monotonic time')
        return float(now)

    @staticmethod
    def _chat(chat):
        if type(chat) is not str or not chat or len(chat) > 256 or any(c in chat for c in ('/', '\\', '\x00')):
            raise ValueError('invalid scheduling chat')
        return chat

    def request(self, chat_id, *, now, selected=False, activity=False):
        """Coalesce bursts; renewing the same route alone does not undo backoff.

        False means the bounded background queue is full. Its caller can retain
        one reconcile-needed bit, not accumulate another unbounded queue.
        """
        return self._request(chat_id, now=now, selected=selected, activity=activity)

    def discover(self, chat_id, *, now):
        """Admit a background discovery, rotating an idle slot when full.

        A discovery is only a scheduling hint. The caller must separately
        validate source eligibility before doing any work for this chat.
        """
        return self._request(chat_id, now=now, reconcile=True)

    def _request(self, chat_id, *, now, selected=False, activity=False,
                 reconcile=False):
        chat, now = self._chat(chat_id), self._time(now)
        if type(selected) is not bool or type(activity) is not bool:
            raise ValueError('invalid scheduling flags')
        with self._lock:
            state = self._states.get(chat)
            if state is None:
                if len(self._states) >= self.capacity:
                    if not selected and not reconcile:
                        return False
                    # Selected work or bounded discovery may displace the least
                    # recently requested nonrunning slot. Discovery also keeps
                    # the current selected route. An executing job is retained.
                    victims = [(s.order, c) for c, s in self._states.items()
                               if (self._running is None or c != self._running.chat_id)
                               and (not reconcile or c != self._selected)]
                    if not victims:
                        return False
                    del self._states[min(victims)[1]]
                self._order += 1
                state = self._states[chat] = _State(now + 0.05, self._order)
            self._order += 1
            state.order = self._order
            changed_route = selected and self._selected != chat
            if selected:
                self._selected, self._lease_until = chat, now + 15.0
            hot = activity or changed_route
            if hot and chat == self._selected:
                self._hot_until = now + 4.0
                state.idle = 0
            # Pure lease renewal should not force a fresh scan. A hint uses
            # activity=True, regardless of whether this is the selected chat.
            if hot:
                if self._running is not None and self._running.chat_id == chat:
                    state.rerun_at = min(state.rerun_at or now + 0.05, now + 0.05)
                else:
                    state.due = min(state.due, now + 0.05)
            return True

    def clear_selection(self):
        with self._lock:
            self._selected = None
            self._lease_until = self._hot_until = 0.0

    def take_due(self, *, now):
        now = self._time(now)
        with self._lock:
            if self._running is not None:
                return None
            selected = self._selected if now < self._lease_until else None
            due = [(s.due, s.order, c) for c, s in self._states.items() if s.due <= now]
            if not due:
                return None
            background = [row for row in due if row[2] != selected]
            selected_due = next((row for row in due if row[2] == selected), None)
            # Bound starvation even when selected work takes longer than 350ms.
            chosen = selected_due if selected_due and (self._selected_runs < 2 or not background) else min(background or due)
            chat = chosen[2]
            self._selected_runs = self._selected_runs + 1 if chat == selected else 0
            self._serial += 1
            self._running = IngestionJob(chat, self._serial)
            return self._running

    def finish(self, job, *, now, changed=False, success=True):
        now = self._time(now)
        if type(changed) is not bool or type(success) is not bool:
            raise ValueError('invalid ingestion outcome')
        with self._lock:
            if job is not self._running or job is None:
                raise ValueError('stale ingestion completion')
            state = self._states[job.chat_id]
            selected = job.chat_id == self._selected and now < self._lease_until
            state.failures = 0 if success else min(8, state.failures + 1)
            state.idle = 0 if changed or (selected and now < self._hot_until) else min(8, state.idle + 1)
            if changed and selected:
                self._hot_until = now + 4.0
            if not success:
                # Hints can request one rerun; repeated failures cannot create
                # an uncontrolled hot polling loop without new signals.
                delay = min(60.0, self.background * 2 ** (state.failures - 1))
            elif selected:
                delay = 0.35 if now < self._hot_until else min(self.background, 0.35 * 2 ** state.idle)
            else:
                delay = self.background
            state.due = now + delay
            if state.rerun_at is not None:
                state.due = min(state.due, max(now, state.rerun_at))
            state.rerun_at = None
            self._running = None

    def wait_s(self, *, now, maximum):
        now = self._time(now)
        maximum = self._time(maximum)
        with self._lock:
            if self._running is not None or not self._states:
                return maximum
            return min(maximum, max(0.0, min(s.due for s in self._states.values()) - now))
