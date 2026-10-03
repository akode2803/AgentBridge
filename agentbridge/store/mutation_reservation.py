"""One-use in-process ownership of a not-yet-attempted durable mutation.

Not an authority token or a crash-recovery interface. Only a successful canonical
finalization may request reservation; provider work begins after that cut commits.
"""
from __future__ import annotations

import threading

from . import source_selectors as scopes


class FinalizationMutation:
    def __init__(self, coordinator, changes):
        self.coordinator = coordinator
        self.changes = scopes.selectors(changes, limit=8)
        self.accepted = False
        self._entered = False
        self._intent = None
        self._state = 'new'
        self._lock = threading.Lock()

    def _enter(self, coordinator):
        with self._lock:
            if self.coordinator is not coordinator or self._entered:
                raise ValueError('foreign or reused finalization mutation')
            self._entered = True

    def _committed(self, intent):
        with self._lock:
            self._intent = intent
            self._state = 'reserved'

    def abort_unstarted(self):
        """Release only this process's proven unattempted write; never readiness."""
        with self._lock:
            if self._state != 'reserved':
                raise ValueError('mutation is not unstarted')
            # A failure leaves the intent durable and this ticket unusable.
            self._state = 'aborting'
            self.coordinator._finish(self._intent)
            self._state = 'aborted'

    def execute(self, operation):
        """Call provider once outside finalization locks, retaining ambiguous failures."""
        with self._lock:
            if self._state != 'reserved':
                raise ValueError('mutation is not reserved')
            self._state = 'attempted'
        result = operation()
        self.coordinator.complete(self._intent)
        with self._lock:
            self._state = 'completed'
        return result
