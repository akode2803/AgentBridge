"""Fair bounded background count jobs; the GUI independently finalizes handout."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace

from .page_operation import PageOperation, PageOperationLimits
from .source_schedule import SourceSchedule
from .unread_counts import (MAX_JOBS, RAW_LIMIT, VISIBLE_LIMIT, UnreadSession,
                            accumulate)

DISPATCH_SECONDS = 0.25
WORK_LIMITS = PageOperationLimits(max_rounds=1, max_bytes=16 * 1024 * 1024,
                                 max_steps=2048, max_crypto=2048)


@dataclass(frozen=True)
class UnreadStep:
    status: str
    chat_id: str | None = None
    raw_examined: int = 0
    visible_examined: int = 0
    reason: str = ''


@dataclass
class _Job:
    session: UnreadSession
    order: int
    due: float
    progress: object = None
    failures: int = 0


class UnreadRuntime:
    def __init__(self, mesh, *, clock=time.monotonic):
        self.mesh = mesh
        self._owners = mesh.tx, mesh.store, mesh.messaging, mesh.keys
        self._identity = mesh.user, mesh.messaging.user, mesh.messaging.machine
        self._clock = clock
        self._lock = threading.RLock()
        self._step_lock = threading.Lock()
        self._jobs = {}
        self._session = None
        self._session_floor = 0
        self._selected = None
        self._selected_runs = self._order = 0
        self._next_dispatch = 0.0
        self._closed = False
        self._stop = threading.Event()
        self._thread = None

    def _now(self):
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError('invalid unread monotonic clock')
        return float(now)

    def _session_ok(self, session):
        return (type(session) is UnreadSession and session.viewer == self.mesh.user
                and self._owners == (self.mesh.tx, self.mesh.store,
                                     self.mesh.messaging, self.mesh.keys)
                and self._identity == (self.mesh.user, self.mesh.messaging.user,
                                       self.mesh.messaging.machine))

    def request(self, chat, session, *, selected=False):
        chat = SourceSchedule._chat(chat)
        if not self._session_ok(session) or type(selected) is not bool:
            raise ValueError('foreign unread job')
        with self._lock:
            if self._closed:
                return False
            if self._session is not None and (session.app_identity != self._session.app_identity
                    or session.generation < self._session_floor):
                return False
            if self._session != session:
                # An old concurrent request cannot erase a newer GUI session.
                if (self._session is not None and session.app_identity == self._session.app_identity
                        and session.generation < self._session.generation):
                    return False
                self._jobs.clear()
                self._session = session
                self._session_floor = session.generation
                self._selected = None
                self._selected_runs = 0
            if selected:
                self._selected = chat
            if chat not in self._jobs:
                if len(self._jobs) >= MAX_JOBS:
                    return False
                self._order += 1
                self._jobs[chat] = _Job(session, self._order, self._now())
            return True

    def candidate(self, chat, session):
        with self._lock:
            if self._closed or not self._session_ok(session) or session != self._session:
                return None
            job = self._jobs.get(chat)
            value = job.progress if job is not None and job.session == session else None
            return value if value is not None and value.complete else None

    def invalidate(self, chat, session):
        with self._lock:
            job = self._jobs.get(chat)
            if job is not None and job.session == session:
                # Replace, rather than mutate, to fence any in-flight result.
                self._jobs[chat] = _Job(session, job.order, self._now())

    def accept(self, candidate, validated_ns):
        """Advance only scalar clock evidence after a GUI finalization.

        The GUI calls this while holding its session/lock handout gate, after
        canonical validation. It supplies no continuing permission, and a
        concurrently replaced/stopped job cannot authorize the exact fields.
        """
        with self._lock:
            job = self._jobs.get(candidate.chat_id)
            if (self._closed or not self._session_ok(candidate.session)
                    or job is None or job.progress is not candidate
                    or not candidate.complete or job.session != self._session
                    or type(validated_ns) is not int
                    or not candidate.evidence.validated_ns <= validated_ns <= 2**63 - 1
                    or candidate.evidence.deadline_ns is not None
                    and validated_ns >= candidate.evidence.deadline_ns):
                return False
            job.progress = replace(candidate, evidence=replace(candidate.evidence,
                                                               validated_ns=validated_ns))
            return True

    def clear(self, *, session=None):
        if session is not None and not self._session_ok(session):
            raise ValueError('foreign unread session fence')
        with self._lock:
            self._jobs.clear()
            self._selected = None
            self._selected_runs = 0
            if session is not None:
                if self._session is not None and self._session.app_identity != session.app_identity:
                    raise ValueError('foreign unread app fence')
                self._session = session
                self._session_floor = max(self._session_floor, session.generation)
            elif self._session is not None:
                self._session_floor = max(self._session_floor, self._session.generation) + 1
            # Retain the app/session floor, including across an empty queue.

    def clear_selection(self):
        with self._lock:
            self._selected = None
            self._selected_runs = 0

    def step(self):
        if not self._step_lock.acquire(blocking=False):
            return UnreadStep('busy')
        try:
            return self._step()
        finally:
            self._step_lock.release()

    def _step(self):
        with self._lock:
            now = self._now()
            if self._closed or not self._session_ok(self._session) or now < self._next_dispatch:
                return UnreadStep('idle')
            due = [(job.order, chat, job) for chat, job in self._jobs.items()
                   if job.due <= now]
            if not due:
                return UnreadStep('idle')
            ordinary = [item for item in due if item[1] != self._selected]
            selected = next((item for item in due if item[1] == self._selected), None)
            choice = (selected if selected is not None and (self._selected_runs < 2 or not ordinary)
                      else min(ordinary or due))
            _, chat, job = choice
            self._selected_runs = self._selected_runs + 1 if chat == self._selected else 0
            self._order += 1
            job.order = self._order
            self._next_dispatch = now + DISPATCH_SECONDS
        progress, result = self._run_quantum(chat, job)
        with self._lock:
            if self._closed or self._jobs.get(chat) is not job:
                return UnreadStep('stale', chat)
            if progress is not None:
                job.progress = progress
                job.failures = 0
                job.due = math.inf if progress.complete else self._now() + DISPATCH_SECONDS
            else:
                if result.status in ('reset_required', 'forbidden'):
                    job.progress = None
                job.failures = min(job.failures + 1, 5)
                job.due = self._now() + min(8.0, 0.5 * 2**(job.failures - 1))
        if progress is not None and progress.complete:
            events = getattr(self.mesh.local_inputs, 'read_events', None)
            if events is not None:
                events.changed('sidebar', chat)
        return result

    def _run_quantum(self, chat, job):
        runtime, prior = self.mesh.local_inputs, job.progress
        try:
            reader, receipt, index = runtime.inputs(chat)
            operation = PageOperation(self.mesh, chat, source_reader=reader,
                before=prior.before if prior is not None else None,
                expected_position=prior.evidence.position if prior is not None else None,
                unread_candidate=prior, summary_only=True,
                limit=VISIBLE_LIMIT, scan_budget=RAW_LIMIT, limits=WORK_LIMITS)
            work = operation.prepare(receipt, receipt, index)
            if work.status == 'prepared':
                final = work.prepared.finalize()
                if final.status == 'page':
                    candidate = accumulate(work.prepared, final.result, job.session, prior)
                    return candidate, UnreadStep('complete' if candidate.complete else 'scanning',
                        chat, final.result.page.raw_examined, len(final.result.page.messages))
                work = final
            if work.status == 'work':
                runtime.request_page(chat, index=index,
                    proofs=work.work if work.reason == 'overlay_proofs' else ())
            if (work.status in ('restart', 'reset_required')
                    or work.reason.endswith('_changed')
                    or work.reason in ('clock_rollback', 'clock_expired')):
                return None, UnreadStep('reset_required', chat, reason=work.reason)
            return None, UnreadStep('forbidden' if work.status == 'forbidden' else 'pending',
                                    chat, reason=work.reason)
        except Exception as exc:
            # Storage/key/pin failures never publish a count or kill the worker.
            from .page_operation import _failure
            failed = _failure(exc)
            changed = (failed.status in ('restart', 'reset_required')
                       or failed.reason.endswith('_changed')
                       or failed.reason in ('clock_rollback', 'clock_expired'))
            return None, UnreadStep('reset_required' if changed else 'pending',
                                   chat, reason=failed.reason)

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('unread runtime is closed')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='unread-counts', daemon=True)
                self._thread.start()

    def _run(self):
        while not self._stop.wait(DISPATCH_SECONDS):
            self.step()

    def stop(self):
        with self._lock:
            self._closed = True
            self._jobs.clear()
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError('unread work has not stopped; Store must remain open')
        if not self._step_lock.acquire(timeout=5):
            raise RuntimeError('unread work has not stopped; Store must remain open')
        self._step_lock.release()
