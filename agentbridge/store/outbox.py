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
import time
from typing import Any, Callable

from ..core.errors import ValidationError
from ..core.latency import sink_for_store
from ..core import delivery_trace
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
        typed_owner: Any | None = None,
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
        # Linearizes background-dispatch admission with stop(). A dispatch
        # admitted before stop is in flight and may finish; once stop wins this
        # gate, no later claimed item can begin a transport mutation.
        self._dispatch_gate = threading.RLock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._typed_owner = None
        self.latency = sink_for_store(store)
        if typed_owner is not None:
            self.bind_typed_owner(typed_owner)

    def bind_typed_owner(self, owner: Any) -> None:
        """One-way opt-in after transport/journal ownership is established."""
        if (owner is None or not callable(getattr(owner, 'dispatch', None))
                or not callable(getattr(owner, 'release', None))):
            raise TypeError('invalid typed outbox owner')
        with self._dispatch_gate:
            if self._typed_owner is not None:
                raise ValueError('typed outbox owner is already bound')
            self._typed_owner = owner

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._dispatch_gate:
            self._stop_requested.clear()
            self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ab-outbox")
        self._thread.start()

    def request_stop(self) -> None:
        """Prevent another background dispatch without claiming full stop."""
        self._stop_requested.set()
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        # The early hint keeps a completed handler from admitting another item
        # while stop waits for the shared gate. _stop itself is the linearized
        # boundary: when the gate is busy, a handler has already started.
        self.request_stop()
        acquired = self._dispatch_gate.acquire(timeout=timeout)
        if not acquired:
            # A handler may not have reached its transport call yet, so a
            # timeout cannot declare the stop boundary complete. Keep the
            # request hint set, leave _stop unset and make Mesh.close retain
            # this owner for an exact later teardown retry.
            raise TimeoutError('outbox dispatch did not stop')
        try:
            self._stop.set()
        finally:
            self._dispatch_gate.release()
        # request_stop() may have woken a worker that then re-entered its wait
        # before this linearized boundary was established. Wake again so the
        # completed stop is always observed promptly.
        self._wake.set()
        if self._thread:
            self._thread.join(max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise TimeoutError('outbox worker did not stop')

    def notify(self) -> None:
        """Wake the worker immediately (call right after outbox_add)."""
        self._wake.set()

    # ------------------------------------------------------------------ work
    def _run(self) -> None:
        while not self._stop.is_set() and not self._stop_requested.is_set():
            self._wake.wait(self.poll_s)
            self._wake.clear()
            if self._stop.is_set() or self._stop_requested.is_set():
                break
            try:
                self._flush_once(stop=self._stop)
            except Exception:  # noqa: BLE001 — the loop must survive anything
                pass

    def flush_once(self) -> int:
        """Claim and dispatch every currently-due item. Returns how many were
        completed. Deterministic — tests and synchronous callers use this."""
        return self._flush_once()

    @delivery_trace.observed('outbox_batch')
    def _flush_once(self, *, stop: threading.Event | None = None) -> int:
        """Flush due work, honoring revocation only for the background owner.

        Explicit synchronous callers may still use ``flush_once`` after
        stopping the worker. The background path releases every unprocessed
        exact lease when stopped, so it never strands a claimed batch or starts
        a later transport mutation.
        """
        done = 0
        if stop is not None and stop.is_set():
            return done
        for item in self.store.outbox_prune_dead():
            self._hook(self.prune_hooks, item)
        # A Store-sent tombstone is the durable consumer settlement. It must
        # release every linked journal leg before deletion or spool cleanup.
        sent = self.store.outbox_sent_items()
        if sent and self._typed_owner is None:
            raise RuntimeError('typed outbox owner is unavailable')
        for item in sent:
            if stop is not None and (stop.is_set() or self._stop_requested.is_set()):
                return done
            with self._dispatch_gate:
                if stop is not None and (stop.is_set() or self._stop_requested.is_set()):
                    return done
                done += self._finalize_typed(item)
        while True:
            if stop is not None and stop.is_set():
                return done
            items = self.store.outbox_claim_due(lease_s=self.lease_s)
            if not items:
                return done
            for index, item in enumerate(items):
                result = self._dispatch(item, stop=stop)
                if result is None:
                    for pending in items[index:]:
                        self.store.outbox_release_claim(pending)
                    return done
                done += result

    def _dispatch(self, item: OutboxItem, *, stop: threading.Event | None = None) -> int | None:
        if stop is not None:
            # Keep the shared gate through the handler call. stop() either sets
            # the boundary first (and this lease is released) or observes an
            # already-started handler; a transport handler cannot begin after
            # the stop boundary.
            with self._dispatch_gate:
                if stop.is_set() or self._stop_requested.is_set():
                    return None
                return self._dispatch(item)
        with self._dispatch_gate:
            if self._typed_owner is not None:
                return self._dispatch_typed(item)
            return self._dispatch_legacy(item)

    def _dispatch_typed(self, item: OutboxItem) -> int:
        source = item.payload if isinstance(item.payload, dict) else {}
        payload = source.get('envelope') \
            if isinstance(source.get('envelope'), dict) else source
        trace_ref = str(payload.get('id') or '') if isinstance(payload, dict) else ''
        started = delivery_trace.queue_clock()
        try:
            self._typed_owner.dispatch(item)
        except Exception as exc:  # noqa: BLE001 — durable unknowns never go dead
            delivery_trace.emit('transport_append', message=trace_ref,
                                status='error', duration_ms=delivery_trace.elapsed_ms(started))
            delay = min(self.base_delay * (2 ** min(item.attempts, 16)), self.max_delay)
            delay *= 1 + random.uniform(0, 0.1)
            self.store.outbox_retry_claim(item, f'{type(exc).__name__}: {exc}', delay)
            delivery_trace.emit('outbox_retry', message=trace_ref,
                                status='error', retry_ms=delay * 1000)
            return 0
        delivery_trace.emit('transport_append', message=trace_ref,
                            outcome='completed', duration_ms=delivery_trace.elapsed_ms(started))
        if not self.store.outbox_mark_sent(item):
            # Another lease or a sent tombstone won. Its owner will resume the
            # exact linked ticket; this stale worker must not publish success.
            return 0
        settled = OutboxItem(item.seq, item.kind, item.target, item.payload,
                             item.attempts, 0, item.payload_json, 'sent')
        return self._finalize_typed(settled)

    def _finalize_typed(self, item: OutboxItem) -> int:
        try:
            self._typed_owner.release(item)
            hook = self.success_hooks.get(item.kind)
            if hook is not None:
                hook(item.target, item.payload)
            if not self.store.outbox_finalize_sent(item):
                return 0
        except Exception:  # noqa: BLE001 — sent tombstone retries on next flush
            return 0
        return 1

    def _dispatch_legacy(self, item: OutboxItem) -> int:
        handler = self.handlers.get(item.kind)
        if handler is None:
            self.store.outbox_dead(item.seq, f"no handler for kind {item.kind!r}")
            try:
                payload = item.payload if isinstance(item.payload, dict) else {}
                envelope = payload.get("envelope")
                payload = envelope if isinstance(envelope, dict) else payload
                message = payload.get("id")
                delivery_trace.emit('outbox_dead',
                                    message=message if isinstance(message, str) else '',
                                    status='error')
            except Exception:  # noqa: BLE001 — tracing never changes a dead outcome
                pass
            return 0
        payload = item.payload.get("envelope") \
            if isinstance(item.payload.get("envelope"), dict) else item.payload
        trace_ref = str(payload.get("id") or "") if isinstance(payload, dict) else ""
        if trace_ref:
            self.latency.observe(
                "outbox_attempt", trace_ref, lane="local",
                outcome=f"attempt-{item.attempts + 1}")
        started = delivery_trace.queue_clock()
        try:
            handler(item.target, item.payload)
        except ValidationError as e:
            delivery_trace.emit("transport_append", message=trace_ref, status="error",
                                duration_ms=delivery_trace.elapsed_ms(started))
            # structurally unprocessable — retrying can never help
            self.store.outbox_dead(item.seq, f"{type(e).__name__}: {e}")
            delivery_trace.emit('outbox_dead', message=trace_ref, status='error')
            self._hook(self.dead_hooks, item)
            return 0
        except Exception as e:  # noqa: BLE001 — transient: retry forever
            delivery_trace.emit("transport_append", message=trace_ref, status="error",
                                duration_ms=delivery_trace.elapsed_ms(started))
            # Cap the exponent BEFORE converting to float. A poison row that
            # retried 1024 times used to raise OverflowError here and abort the
            # whole claimed batch, starving every newer unrelated message.
            delay = min(
                self.base_delay * (2 ** min(item.attempts, 16)),
                self.max_delay,
            )
            delay *= 1 + random.uniform(0, 0.1)  # jitter
            self.store.outbox_retry(item.seq, f"{type(e).__name__}: {e}", delay)
            delivery_trace.emit("outbox_retry", message=trace_ref, status="error", retry_ms=delay*1000)
            return 0
        delivery_trace.emit("transport_append", message=trace_ref, outcome="completed",
                            duration_ms=delivery_trace.elapsed_ms(started))
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
