"""Durable change-ledger recovery for harness runtime discovery.

Realtime is only a wake.  The observer replays provider-committed positions and
advances its local cursor only after document changes have been admitted into
the synchronized mirror.  Authority remains in the current mirror inputs and
the canonical runtime readers; a ledger row never authorizes work.
"""

from __future__ import annotations

import threading
import time

from ..transport.change_ledger import ChangeLedgerEpoch

_STATE_DOC = "sync/harness_change_ledger"
_PAGE_SIZE = 256
_MAX_PAGES_PER_TICK = 4
_HEALTHY_AUDIT_S = 300.0
_RECOVERY_POLL_S = 45.0
_REPROBE_S = 60.0


class HarnessChangeObserver:
    """Replay the transport ledger into one harness's local mirror."""

    def __init__(self, transport, store, wake, *, clock=time.monotonic) -> None:
        self.transport = transport
        self.store = store
        self.wake = wake
        self.clock = clock
        self._lock = threading.Lock()
        self._dirty = True
        self._started = False
        self._epoch: ChangeLedgerEpoch | None = None
        self._cursor = 0
        self._last_check = 0.0
        self._retry_at = 0.0
        self._next_probe = 0.0
        self._unsubscribe = None

    def _signalled(self, _event_id: int) -> None:
        with self._lock:
            self._dirty = True
        self.wake()

    def _load_state(self, epoch: ChangeLedgerEpoch) -> bool:
        state = self.store.cached_doc(_STATE_DOC, default={})
        same = (
            isinstance(state, dict)
            and state.get("epoch") == epoch.epoch
            and type(state.get("cursor")) is int
            and epoch.minimum_cursor <= state["cursor"]
        )
        self._cursor = int(state["cursor"]) if same else epoch.minimum_cursor
        return same

    def _save_state(self) -> None:
        assert self._epoch is not None
        self.store.cache_doc(_STATE_DOC, {
            "v": 1,
            "epoch": self._epoch.epoch,
            "cursor": self._cursor,
        })

    def _start(self) -> bool:
        now = self.clock()
        if self._started:
            return True
        if now < self._next_probe:
            return False
        try:
            capability = self.transport.change_ledger_capability()
            if capability is None:
                raise RuntimeError("change ledger unavailable")
            epoch = self.transport.change_ledger_epoch()
            same = self._load_state(epoch)
            # A replaced/compacted epoch invalidates incremental evidence.  The
            # harness has already completed startup catch-up, but refresh the
            # document mirror once more before adopting the new floor.
            if not same:
                refresh = getattr(self.transport, "refresh", None)
                if not callable(refresh):
                    raise RuntimeError("transport mirror cannot reset")
                refresh()
                self._save_state_for(epoch)
            self._epoch = epoch
            self._unsubscribe = self.transport.subscribe_change_ledger(
                self._signalled,
            )
            self._started = True
            self._dirty = True
            return True
        except Exception:
            self._next_probe = now + _REPROBE_S
            return False

    def _save_state_for(self, epoch: ChangeLedgerEpoch) -> None:
        self.store.cache_doc(_STATE_DOC, {
            "v": 1,
            "epoch": epoch.epoch,
            "cursor": self._cursor,
        })

    def tick(self) -> bool:
        """Replay bounded pages; return whether runtime discovery should run."""
        if not self._start():
            return False
        now = self.clock()
        if now < self._retry_at:
            return False
        status = self.transport.change_ledger_realtime_status()
        with self._lock:
            dirty = self._dirty
            due = now - self._last_check >= (
                _HEALTHY_AUDIT_S if status == "ready" else _RECOVERY_POLL_S
            )
            if not dirty and not due:
                return False
            self._dirty = False
        changed = False
        try:
            for _ in range(_MAX_PAGES_PER_TICK):
                page = self.transport.change_ledger_events(
                    self._cursor, limit=_PAGE_SIZE,
                )
                if page.events:
                    doc_change = any(
                        event.domain in {"docs", "visibility"}
                        for event in page.events
                    )
                    visibility = any(
                        event.domain == "visibility" for event in page.events
                    )
                    if doc_change:
                        refresh = getattr(
                            self.transport, "refresh_observed_changes", None,
                        )
                        if not callable(refresh):
                            raise RuntimeError("transport mirror cannot recover")
                        refresh(visibility=visibility)
                        changed = True
                    self._cursor = page.cursor
                    self._save_state()
                if not page.has_more:
                    break
            else:
                with self._lock:
                    self._dirty = True
                self.wake()
            self._last_check = now
            self._retry_at = 0.0
            return changed
        except Exception:
            # The cursor remains at the last page whose mirror admission and
            # local save both succeeded.  Retry from there on the recovery
            # cadence; stale runtime discovery stays fail-closed where needed.
            with self._lock:
                self._dirty = True
            self._last_check = now
            self._retry_at = now + _RECOVERY_POLL_S
            return False

    def close(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        self._started = False
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                pass
