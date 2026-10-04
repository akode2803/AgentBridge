"""Bounded, content-free invalidations for event-first local GUI readers.

These hints never carry a permission result. A consumer must perform a new
canonical read. Deadlines only request a reread; they cannot extend authority.
"""
from __future__ import annotations

import threading
import time
import math

from . import eventbus

MAX_DEADLINES = 128
SCOPES = frozenset(('chat', 'sidebar', 'aux', 'controls', 'global'))


class ReadEvents:
    def __init__(self, bus, *, clock=time.time_ns):
        self.bus, self.clock = bus, clock
        self._lock = threading.Lock()
        self._deadlines = {}
        self._last_now = None
        self._overflow_deadline = None
        self._closed = False

    def changed(self, scope, chat=''):
        if type(scope) is not str or scope not in SCOPES or type(chat) is not str or len(chat) > 256:
            raise ValueError('invalid read invalidation')
        with self._lock:
            if self._closed:
                return
        self.bus.publish(eventbus.Event(eventbus.READ_MODEL, chat,
                                        {'scope': scope}, ns=self.clock()))

    def note_deadline(self, chat, deadline, validated_ns):
        if (type(validated_ns) is not int or not 0 <= validated_ns <= 2**63 - 1
                or deadline is not None and (
                    type(deadline) not in (int, float) or not math.isfinite(deadline)
                    or not validated_ns < deadline <= 2**63 - 1)):
            raise ValueError('invalid canonical recheck deadline')
        if deadline is not None:
            deadline = math.floor(deadline)  # Early invalidation, never a later lease.
        if type(chat) is not str or not chat or len(chat) > 256:
            raise ValueError('invalid deadline chat')
        with self._lock:
            if self._closed:
                return
            self._last_now = max(self._last_now or 0, validated_ns)
            if deadline is None:
                return
            old = self._deadlines.get(chat)
            if old is None and len(self._deadlines) >= MAX_DEADLINES:
                # Foreground room inventories already cap at this size. Do
                # not retain an unbounded second room registry.
                old = self._overflow_deadline
                self._overflow_deadline = deadline if old is None else min(old, deadline)
                return
            self._deadlines[chat] = deadline if old is None else min(old, deadline)

    def tick(self):
        now = self.clock()
        with self._lock:
            if self._closed:
                return
            rollback = self._last_now is not None and now < self._last_now
            overflow_due = (self._overflow_deadline is not None
                            and self._overflow_deadline <= now)
            if rollback or overflow_due:
                self._overflow_deadline = None
            self._last_now = now
            due = tuple(chat for chat, deadline in self._deadlines.items()
                        if rollback or deadline <= now)
            for chat in due:
                del self._deadlines[chat]
        if rollback or overflow_due:
            self.changed('global')
        else:
            for chat in due:
                self.changed('chat', chat)

    def close(self):
        with self._lock:
            self._closed = True
            self._deadlines.clear()
            self._overflow_deadline = None
