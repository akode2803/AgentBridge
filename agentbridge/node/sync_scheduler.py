"""Inactive, explicit control plane for one local-node provider owner.

The scheduler owns no thread and subscribes to no transport.  Realtime and
ledger observations are content-free wakes only; each caller-driven ``tick``
performs at most one bounded exact-ledger collection or one bounded recovery
step.  Durable Store state remains the only recovery progress evidence.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .collector import ShadowCollectionResult
from .recovery_runtime import NodeRecoveryRuntimeResult
from .server import LocalNodeServer

FOREGROUND_CONTINUE_NS = 250_000_000
BACKGROUND_CONTINUE_NS = 2_000_000_000
FOREGROUND_FALLBACK_NS = 500_000_000
BACKGROUND_FALLBACK_NS = 4_000_000_000
HEALTHY_SAFETY_POLL_NS = 45_000_000_000
FAILURE_INITIAL_NS = 1_000_000_000
FAILURE_MAX_NS = 60_000_000_000
_MAX_NS = 2**63 - 1
_WAKE_KINDS = frozenset({"realtime", "ledger", "manual"})


def _ns(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_NS:
        raise ValueError(f"invalid {name}")
    return value


@dataclass(frozen=True)
class NodeSyncTickResult:
    state: str
    operation: str
    action: str
    next_due_ns: int
    source_health: str
    admitted_generation: int
    last_success_ns: int

    def __post_init__(self) -> None:
        if self.operation not in ("none", "collector", "recovery"):
            raise ValueError("invalid local node sync operation")
        _ns(self.next_due_ns, "local node sync deadline")


class LocalNodeSyncScheduler:
    """Coalesce content-free wakes and advance one owner operation per tick."""

    def __init__(
        self, server: LocalNodeServer, *, foreground: bool = False,
        realtime_healthy: bool = True,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(server) is not LocalNodeServer or not callable(clock_ns):
            raise TypeError("expected running local node owner and clock")
        if type(foreground) is not bool or type(realtime_healthy) is not bool:
            raise TypeError("invalid local node sync mode")
        self.server = server
        self._clock_ns = clock_ns
        self._state_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._foreground = foreground
        self._realtime_healthy = realtime_healthy
        self._wake_revision = 0
        self._completed_revision = 0
        self._recovery_revision_bound: int | None = None
        self._failure_count = 0
        self._retry_not_before_ns = 0
        status = server.store.status(node_epoch="", started_ns=0)
        self._work_pending = (server.recovery_pending()
                              or status.health != "ready")
        now = self._now()
        self._next_due_ns = (
            now if self._work_pending else self._add(now, self._idle_interval()))

    def _now(self) -> int:
        return _ns(self._clock_ns(), "local node sync clock")

    def _idle_interval(self) -> int:
        if self._realtime_healthy:
            return HEALTHY_SAFETY_POLL_NS
        return (FOREGROUND_FALLBACK_NS if self._foreground
                else BACKGROUND_FALLBACK_NS)

    def _continue_interval(self) -> int:
        return (FOREGROUND_CONTINUE_NS if self._foreground
                else BACKGROUND_CONTINUE_NS)

    @staticmethod
    def _add(now: int, delay: int) -> int:
        return min(_MAX_NS, now + delay)

    def set_mode(self, *, foreground: bool, realtime_healthy: bool,
                 now_ns: int | None = None) -> int:
        """Update scheduling mode without treating connection state as truth."""
        if type(foreground) is not bool or type(realtime_healthy) is not bool:
            raise TypeError("invalid local node sync mode")
        now = self._now() if now_ns is None else _ns(
            now_ns, "local node sync time")
        with self._state_lock:
            old_foreground = self._foreground
            old_realtime = self._realtime_healthy
            self._foreground = foreground
            self._realtime_healthy = realtime_healthy
            if (foreground and not old_foreground) or (
                    not realtime_healthy and old_realtime):
                interval = (self._continue_interval() if self._work_pending
                            else self._idle_interval())
                self._next_due_ns = min(
                    self._next_due_ns,
                    max(self._retry_not_before_ns,
                        self._add(now, interval)))
            return self._next_due_ns

    def wake(self, kind: str, *, now_ns: int | None = None) -> int:
        """Record one content-free hint; the hint grants no read authority."""
        if type(kind) is not str or kind not in _WAKE_KINDS:
            raise ValueError("invalid local node sync wake")
        now = self._now() if now_ns is None else _ns(
            now_ns, "local node sync time")
        return self._wake_at(now)

    def _wake_at(self, now: int) -> int:
        with self._state_lock:
            self._wake_revision += 1
            self._work_pending = True
            self._next_due_ns = min(
                self._next_due_ns, max(now, self._retry_not_before_ns))
            return self._wake_revision

    def request_recovery(self, *, now_ns: int | None = None) -> int:
        """Request full recovery after a caller has only work evidence."""
        now = self._now() if now_ns is None else _ns(
            now_ns, "local node sync time")
        self.server.request_recovery()
        return self._wake_at(now)

    @property
    def next_due_ns(self) -> int:
        with self._state_lock:
            return self._next_due_ns

    def _status_result(self, state: str, operation: str, action: str,
                       due: int) -> NodeSyncTickResult:
        store = self.server.store
        if store is None:
            raise RuntimeError("local node recovery owner is not running")
        status = store.status(node_epoch="", started_ns=0)
        return NodeSyncTickResult(
            state, operation, action, due, status.health,
            status.admitted_generation, status.last_success_ns,
        )

    def _deferred(self, state: str, action: str) -> NodeSyncTickResult:
        with self._state_lock:
            due = self._next_due_ns
        return self._status_result(state, "none", action, due)

    def _schedule_failure(self, now: int) -> int:
        self._failure_count = min(self._failure_count + 1, 31)
        delay = min(
            FAILURE_MAX_NS,
            FAILURE_INITIAL_NS * (2 ** (self._failure_count - 1)),
        )
        self._work_pending = True
        self._retry_not_before_ns = self._add(now, delay)
        self._next_due_ns = self._retry_not_before_ns
        return self._next_due_ns

    def _schedule_progress(self, now: int, *, revision: int,
                           complete: bool) -> int:
        self._failure_count = 0
        self._retry_not_before_ns = 0
        if complete:
            self._completed_revision = max(self._completed_revision, revision)
        process_pending = self._completed_revision < self._wake_revision
        durable_pending = self.server.recovery_pending()
        self._work_pending = process_pending or durable_pending or not complete
        delay = self._continue_interval() if self._work_pending else (
            self._idle_interval())
        self._next_due_ns = self._add(now, delay)
        return self._next_due_ns

    def _after_collector(self, result: ShadowCollectionResult, *, now: int,
                         revision: int) -> tuple[str, str, int]:
        if result.state in ("degraded", "unavailable"):
            return result.state, result.state, self._schedule_failure(now)
        if result.state == "busy":
            self._work_pending = True
            self._next_due_ns = self._add(now, self._continue_interval())
            return "busy", "step-in-progress", self._next_due_ns
        if result.state == "catching_up" and result.recovery_scopes:
            self.server.request_recovery()
            due = self._schedule_progress(
                now, revision=revision, complete=False)
            return "running", "recovery-required", due
        complete = result.state in ("up_to_date", "admitted")
        due = self._schedule_progress(
            now, revision=revision, complete=complete)
        state = "ready" if complete and not self._work_pending else "running"
        return state, result.state, due

    def _after_recovery(self, result: NodeRecoveryRuntimeResult, *, now: int,
                        revision: int) -> tuple[str, str, int]:
        if result.state in ("degraded", "unavailable"):
            return result.state, result.action, self._schedule_failure(now)
        if result.state == "busy":
            self._work_pending = True
            self._next_due_ns = self._add(now, self._continue_interval())
            return "busy", result.action, self._next_due_ns
        if result.action == "started":
            self._recovery_revision_bound = revision
        elif result.action == "cut-changed":
            self._recovery_revision_bound = None
        complete = result.state == "ready"
        completion_revision = revision
        if complete:
            completion_revision = self._recovery_revision_bound or 0
            self._recovery_revision_bound = None
        due = self._schedule_progress(
            now, revision=completion_revision, complete=complete)
        state = "ready" if complete and not self._work_pending else "running"
        return state, result.action, due

    def tick(self, *, now_ns: int | None = None) -> NodeSyncTickResult:
        """Perform at most one bounded provider operation when its deadline is due."""
        now = self._now() if now_ns is None else _ns(
            now_ns, "local node sync time")
        if not self._tick_lock.acquire(blocking=False):
            return self._deferred("busy", "tick-in-progress")
        try:
            with self._state_lock:
                if now < self._next_due_ns:
                    return self._status_result(
                        "deferred", "none", "not-due", self._next_due_ns)
                revision = self._wake_revision

            if self.server.recovery_pending():
                operation = "recovery"
                result = self.server.recover_once(attempted_ns=time.time_ns())
                finished = max(now, self._now())
                with self._state_lock:
                    state, action, due = self._after_recovery(
                        result, now=finished, revision=revision)
            else:
                operation = "collector"
                result = self.server.collect_once()
                finished = max(now, self._now())
                with self._state_lock:
                    state, action, due = self._after_collector(
                        result, now=finished, revision=revision)
            return self._status_result(state, operation, action, due)
        finally:
            self._tick_lock.release()


__all__ = [
    "BACKGROUND_CONTINUE_NS", "BACKGROUND_FALLBACK_NS",
    "FAILURE_INITIAL_NS", "FAILURE_MAX_NS", "FOREGROUND_CONTINUE_NS",
    "FOREGROUND_FALLBACK_NS", "HEALTHY_SAFETY_POLL_NS",
    "LocalNodeSyncScheduler", "NodeSyncTickResult",
]
