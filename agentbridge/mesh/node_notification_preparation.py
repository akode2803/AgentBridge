"""Threadless, bounded notification wakes; every attempt reads current authority.

The EventBus is best-effort and this process-local queue is not durable or
exactly-once delivery. Retention contains only identities and operation work
counters, never a page, preview, policy decision, or prepared finalizer.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from . import eventbus, page_operation
from .eventbus import Event
from .node_notifications import MessageNotificationOperation, MessageNotificationWake
from .notify import Notification

MAX_JOBS = 64
MAX_COMPLETED = 128
RETRY_S = 0.35
MAX_COOLDOWN_S = 60.0
_EXHAUSTED = frozenset({
    'operation_round_budget', 'operation_byte_budget', 'operation_step_budget',
    'operation_crypto_budget', 'operation_epoch_budget',
    'operation_proof_budget', 'operation_parent_budget',
})


@dataclass(frozen=True)
class NotificationPreparationStep:
    status: str
    wake: MessageNotificationWake | None = None
    notification: Notification | None = field(default=None, repr=False)
    reason: str = ''


@dataclass
class _Job:
    wake: MessageNotificationWake
    operation: MessageNotificationOperation | None = None
    next_due: float = 0.0
    exhausted_attempts: int = 0


class NodeNotificationPreparation:
    """One fresh exact-ID capture/prepare/finalize quantum per ``run_one``."""

    def __init__(self, mesh, *, app=None, session=None,
                 clock: Callable[[], float] = time.monotonic):
        from .service import Mesh
        from .local_input_runtime import LocalInputRuntime
        if (type(mesh) is not Mesh or type(mesh.local_inputs) is not LocalInputRuntime
                or mesh.local_inputs.store is not mesh.store
                or mesh.local_inputs.transport is not mesh.tx
                or mesh.messaging.store is not mesh.store
                or mesh.messaging.tx is not mesh.tx
                or mesh.messaging.user != mesh.user
                or mesh.messaging.machine != mesh.machine
                or not callable(clock)):
            raise ValueError('invalid notification preparation owner')
        if app is not None or session is not None:
            from ..gui.context import GuiApp, SessionReadToken
            if (type(app) is not GuiApp or type(session) is not SessionReadToken
                    or app.mesh is not mesh or session.mesh is not mesh
                    or not app.validate_session_read(session)):
                raise ValueError('invalid notification session owner')
        self.mesh, self.owner, self.clock = mesh, mesh.local_inputs, clock
        self._store, self._transport = mesh.store, mesh.tx
        self._messaging = mesh.messaging
        self._viewer, self._machine = mesh.user, mesh.machine
        self._app, self._session = app, session
        self._jobs: OrderedDict[tuple[str, str], _Job] = OrderedDict()
        self._completed: OrderedDict[tuple[str, str], MessageNotificationWake] = OrderedDict()
        self._gap = False
        self._closed = False
        self._closing = threading.Event()
        self._lock = threading.RLock()

    def _bound(self) -> bool:
        if (self._closing.is_set() or self.mesh.local_inputs is not self.owner or self.owner._closed
                or self.mesh.store is not self._store or self.mesh.tx is not self._transport
                or self.mesh.messaging is not self._messaging
                or self._messaging.store is not self._store
                or self._messaging.tx is not self._transport
                or (self.mesh.user, self.mesh.machine,
                    self._messaging.user, self._messaging.machine)
                != (self._viewer, self._machine, self._viewer, self._machine)):
            return False
        if self._app is not None:
            return (self._app.mesh is self.mesh
                    and self._app.validate_session_read(self._session)
                    and not self._app.lock.expire_if_idle())
        return True

    def _close_locked(self) -> None:
        self._closing.set()
        self._closed = True
        self._jobs.clear()
        self._completed.clear()

    def close(self) -> None:
        # Cancellation must not join a job waiting for a consumer gate held
        # by this callback. The running quantum observes revocation and clears.
        self._closing.set()
        if self._lock.acquire(blocking=False):
            try:
                self._close_locked()
            finally:
                self._lock.release()

    def enqueue(self, event: Event) -> bool:
        """Admit a strict content-free message wake; duplicates keep their ledger."""
        if type(event) is not Event or event.type != eventbus.MESSAGE:
            return False
        wake = MessageNotificationWake.capture(event)
        key = (wake.chat_id, wake.message_id)
        with self._lock:
            if self._closed or not self._bound():
                self._close_locked()
                return False
            job = self._jobs.get(key)
            if job is not None:
                if job.wake == wake:
                    return False
                self._jobs[key] = _Job(wake)
                return True
            completed = self._completed.get(key)
            if completed == wake:
                return False
            self._completed.pop(key, None)
            if len(self._jobs) >= MAX_JOBS:
                self._jobs.popitem(last=False)
                self._gap = True
            self._jobs[key] = _Job(wake)
            return True

    def take_gap(self) -> bool:
        with self._lock:
            gap, self._gap = self._gap, False
            return gap

    def wait_s(self, maximum: float = RETRY_S) -> float:
        if (type(maximum) not in (int, float) or isinstance(maximum, bool)
                or not math.isfinite(maximum) or not 0 <= maximum <= 300):
            raise ValueError('invalid notification wait')
        with self._lock:
            if self._closed or not self._jobs:
                return float(maximum)
            return min(float(maximum), max(0.0,
                min(job.next_due for job in self._jobs.values()) - self.clock()))

    def run_one(self, *, notifier=None,
                notifier_generation=None) -> NotificationPreparationStep:
        """Select one due wake; hand its freshly finalized note out immediately."""
        if notifier is None and notifier_generation is not None:
            raise ValueError('notification generation requires a notifier')
        with self._lock:
            if self._closed or not self._bound():
                self._close_locked()
                return NotificationPreparationStep('closed')
            if not self._jobs:
                return NotificationPreparationStep('idle')
            now = self.clock()
            selected = None
            for _ in range(len(self._jobs)):
                key, job = self._jobs.popitem(last=False)
                self._jobs[key] = job
                if now >= job.next_due:
                    selected = (key, job)
                    break
            if selected is None:
                return NotificationPreparationStep('waiting')
            key, job = selected
            wake = job.wake
            try:
                reader, receipt, index = self.owner.inputs(wake.chat_id)
                if job.operation is None:
                    job.operation = MessageNotificationOperation(
                        self.mesh, wake, source_reader=reader)
                work = job.operation.prepare(receipt, receipt, index)
                if work.status == 'prepared':
                    kwargs = {'app': self._app, 'session': self._session}
                    if notifier is not None:
                        kwargs['notifier'] = notifier
                        kwargs['notifier_generation'] = notifier_generation
                    work = job.operation.finalize_notification(work.prepared, **kwargs)
            except page_operation._ERRORS as exc:
                work = page_operation._failure(exc)
            if self._closed or not self._bound():
                self._close_locked()
                return NotificationPreparationStep('closed')
            if work.status in {'ready', 'forbidden'}:
                self._complete(key, wake)
                return NotificationPreparationStep(work.status, wake,
                    getattr(work, 'notification', None), work.reason)
            if work.status == 'locked' or work.reason in {
                    'session_changed', 'notification_owner_changed',
                    'notification_consumer_changed', 'identity_changed'}:
                self._close_locked()
                return NotificationPreparationStep('closed', reason=work.reason)
            if work.reason == 'event_id_not_projectable':
                self._jobs.pop(key, None)
                self._gap = True
                return NotificationPreparationStep('dropped', wake, reason=work.reason)
            if work.reason in _EXHAUSTED:
                job.operation = None
                job.exhausted_attempts = min(8, job.exhausted_attempts + 1)
                job.next_due = now + min(MAX_COOLDOWN_S,
                    RETRY_S * 2**job.exhausted_attempts)
                return NotificationPreparationStep('pending', wake, reason=work.reason)
            try:
                if work.status == 'work' and work.reason == 'overlay_proofs':
                    self.owner.request_page(wake.chat_id, index=index, proofs=work.work)
                else:
                    self.owner.request(wake.chat_id, activity=True)
                    self.owner.request_page(wake.chat_id)
            except Exception:
                # Scheduling is best-effort and must not erase this identity.
                pass
            job.next_due = now + RETRY_S
            return NotificationPreparationStep('pending', wake, reason=work.reason)

    def _complete(self, key, wake) -> None:
        self._jobs.pop(key, None)
        self._completed[key] = wake
        self._completed.move_to_end(key)
        while len(self._completed) > MAX_COMPLETED:
            self._completed.popitem(last=False)


__all__ = ['NodeNotificationPreparation', 'NotificationPreparationStep']
