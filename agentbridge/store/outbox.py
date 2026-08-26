"""Outbox flush worker — the "no message ever lost" half of R3.

Callers enqueue work in the Store's outbox table (crash-safe: committed before
any send attempt), then ``notify()`` the worker. The worker claims due items
under a lease and dispatches them to registered handlers (kind -> callable).
Transient failures retry forever with capped exponential backoff (WhatsApp's
clock-icon semantics); only structurally unprocessable items go dead.
"""

from __future__ import annotations

import random
import threading
from typing import Any, Callable

from ..core.errors import ValidationError
from ..core.latency import sink_for_store
from .db import OutboxItem, Store

__all__ = ["OutboxWorker"]

Handler = Callable[[str, dict[str, Any]], None]  # (target, payload) -> None
Hook = Callable[[str, dict[str, Any]], None]


class OutboxWorker:
    def __init__(
        self,
        store: Store,
        handlers: dict[str, Handler],
        *,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        poll_s: float = 5.0,
        lease_s: float = 120.0,
        success_hooks: dict[str, Hook] | None = None,
        dead_hooks: dict[str, Hook] | None = None,
        prune_hooks: dict[str, Hook] | None = None,
    ) -> None:
        self.store = store
        self.handlers = handlers
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.poll_s = poll_s
        self.lease_s = lease_s
        self.success_hooks = success_hooks or {}
        self.dead_hooks = dead_hooks or {}
        self.prune_hooks = prune_hooks or {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latency = sink_for_store(store)

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ab-outbox")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def notify(self) -> None:
        """Wake the worker immediately (call right after outbox_add)."""
        self._wake.set()

    # ------------------------------------------------------------------ work
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.poll_s)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.flush_once()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                pass

    def flush_once(self) -> int:
        """Claim and dispatch every currently-due item. Returns how many were
        completed. Deterministic — tests and synchronous callers use this."""
        done = 0
        for item in self.store.outbox_prune_dead():
            self._hook(self.prune_hooks, item)
        while True:
            items = self.store.outbox_claim_due(lease_s=self.lease_s)
            if not items:
                return done
            for item in items:
                done += self._dispatch(item)

    def _dispatch(self, item: OutboxItem) -> int:
        handler = self.handlers.get(item.kind)
        if handler is None:
            self.store.outbox_dead(item.seq, f"no handler for kind {item.kind!r}")
            return 0
        payload = item.payload.get("envelope") \
            if isinstance(item.payload.get("envelope"), dict) else item.payload
        trace_ref = str(payload.get("id") or "") if isinstance(payload, dict) else ""
        if trace_ref:
            self.latency.observe(
                "outbox_attempt", trace_ref, lane="local",
                outcome=f"attempt-{item.attempts + 1}")
        try:
            handler(item.target, item.payload)
        except ValidationError as e:
            # structurally unprocessable — retrying can never help
            self.store.outbox_dead(item.seq, f"{type(e).__name__}: {e}")
            self._hook(self.dead_hooks, item)
            return 0
        except Exception as e:  # noqa: BLE001 — transient: retry forever
            delay = min(self.base_delay * (2**item.attempts), self.max_delay)
            delay *= 1 + random.uniform(0, 0.1)  # jitter
            self.store.outbox_retry(item.seq, f"{type(e).__name__}: {e}", delay)
            return 0
        self.store.outbox_done(item.seq)
        if trace_ref:
            self.latency.observe(
                "append_ack_observed", trace_ref, lane="local",
                outcome="transport-returned")
        self._hook(self.success_hooks, item)
        return 1

    @staticmethod
    def _hook(hooks: dict[str, Hook], item: OutboxItem) -> None:
        hook = hooks.get(item.kind)
        if hook is None:
            return
        try:
            hook(item.target, item.payload)
        except Exception:  # noqa: BLE001 — cleanup never changes send outcome
            pass
