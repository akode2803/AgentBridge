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
    blocked: int = 0
    rerun_at: float | None = None
    blocked_until: float = 0.0
    hot_until: float = 0.0


class SourceSchedule:
    def __init__(self, *, background_s=4.0, capacity=2048):
        if type(capacity) is not int or not 1 <= capacity <= 2048:
            raise ValueError('invalid ingestion queue capacity')
        if type(background_s) not in (int, float) or not math.isfinite(background_s) or not 0.35 <= background_s <= 300:
            raise ValueError('invalid ingestion background cadence')
        self.background = float(background_s)
        self.capacity = capacity
        self._lock = threading.Lock()
        self._states = {}
        self._selected = None
        self._lease_until = self._hot_until = 0.0
        self._blocked_until = 0.0
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
            if changed_route:
                # Explicit foreground demand may interrupt a long background
                # ambiguity backoff once. Subsequent lease renewals cannot;
                # another blocked result installs the selected 350 ms floor.
                state.blocked_until = 0.0
            hot = activity or changed_route
            if hot:
                state.hot_until = now + 4.0
                state.idle = 0
                if chat == self._selected:
                    self._hot_until = state.hot_until
            # Pure lease renewal should not force a fresh scan. A hint uses
            # activity=True, regardless of whether this is the selected chat.
            if hot:
                if self._running is not None and self._running.chat_id == chat:
                    state.rerun_at = min(state.rerun_at or now + 0.05, now + 0.05)
                else:
                    state.due = max(state.blocked_until, min(state.due, now + 0.05))
            return True

    def clear_selection(self):
        with self._lock:
            state = self._states.get(self._selected)
            if state is not None:
                state.hot_until = 0.0
            self._selected = None
            self._lease_until = self._hot_until = 0.0

    def take_due(self, *, now):
        now = self._time(now)
        with self._lock:
            if self._running is not None or now < self._blocked_until:
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

    def finish(self, job, *, now, changed=False, success=True, blocked=False):
        now = self._time(now)
        if (type(changed) is not bool or type(success) is not bool
                or type(blocked) is not bool or (blocked and success)):
            raise ValueError('invalid ingestion outcome')
        with self._lock:
            if job is not self._running or job is None:
                raise ValueError('stale ingestion completion')
            state = self._states[job.chat_id]
            selected = job.chat_id == self._selected and now < self._lease_until
            hot = now < state.hot_until
            if blocked:
                state.blocked = min(8, state.blocked + 1)
            else:
                state.blocked = 0
                state.failures = 0 if success else min(8, state.failures + 1)
            # ``idle`` counts successful observations that found no source
            # change. Failed or blocked work has its own bounded retry policy;
            # treating it as proof that a source is quiet would compound two
            # unrelated backoffs and delay recovery after a transient error.
            if changed or (success and hot):
                state.idle = 0
            elif success:
                state.idle = min(8, state.idle + 1)
            if changed and selected:
                self._hot_until = now + 4.0
            if blocked:
                # Durable overlapping writes are not IO failures. Retry the
                # gate promptly for a selected lease, without admitting data
                # or letting repeated activity hints turn it into a hot loop.
                # A selected chat stays responsive because completion of the
                # exact durable mutation may make its next canonical attempt
                # readable.  Inactive rooms cannot repair an ambiguous intent
                # by rescanning the same local source, so back them off to the
                # same finite safety ceiling as other quiet background work.
                # A new route selection still pulls that room forward through
                # ``request(..., selected=True)`` without clearing the fence.
                delay = (0.35 if selected else
                         min(300.0, self.background * 2 ** (state.blocked - 1)))
                # One pending mutation can overlap many chat sources (account
                # and lifecycle inputs are shared).  Without a process-level
                # floor the scheduler immediately walks every other due room,
                # producing a retry herd against the same durable fence.  This
                # floor coalesces that burst; after it expires the selected
                # room wins normal priority if it is active.
                self._blocked_until = max(self._blocked_until, now + 0.35)
            elif not success:
                # Hints can request one rerun; repeated failures cannot create
                # an uncontrolled hot polling loop without new signals.
                delay = min(60.0, self.background * 2 ** (state.failures - 1))
            elif selected:
                delay = 0.35 if now < self._hot_until else min(self.background, 0.35 * 2 ** state.idle)
            elif hot:
                # A scoped off-room hint can precede the corresponding mirror
                # refresh. Keep only that named source warm long enough for the
                # persisted mirror wake to catch up; unrelated rooms retain
                # their adaptive background cadence.
                delay = 0.35
            else:
                # Background discovery used to reread every known chat every
                # four seconds forever.  A large cached account therefore kept
                # the single ingestion owner and SQLite writer busy even when
                # Realtime reported no activity.  Keep the first confirmation
                # at the configured cadence, then back quiet sources off to a
                # five-minute safety ceiling.  A real change resets ``idle``;
                # a hint still pulls the selected chat forward independently.
                delay = min(300.0, self.background * 2 ** max(0, state.idle - 1))
            state.blocked_until = now + delay if blocked else 0.0
            state.due = now + delay
            if state.rerun_at is not None:
                state.due = max(state.blocked_until, min(state.due, max(now, state.rerun_at)))
            state.rerun_at = None
            self._running = None

    def wait_s(self, *, now, maximum):
        now = self._time(now)
        maximum = self._time(maximum)
        with self._lock:
            if self._running is not None or not self._states:
                return maximum
            due = max(self._blocked_until, min(s.due for s in self._states.values()))
            return min(maximum, max(0.0, due - now))
