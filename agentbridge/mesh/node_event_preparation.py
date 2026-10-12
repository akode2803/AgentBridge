"""Retained, bounded admitted-event work; no cached authority or payloads.

Ordinary messages use fresh canonical projection. Signed native human reaction
breadcrumbs may map to identity-only semantic wakes after fresh canonical
projection; every other info row remains a content-free invalidation. This
owner does not activate the node runtime.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from ..store import node_event_journal
from . import page_operation
from .node_reactions import ReactionProjectionOperation
from .page_operation import ExactMessageProjectionOperation, PageOperation
from .paths import P

RETRY_S = 0.35
MAX_RETAINED_CHATS = 16


@dataclass(frozen=True)
class NodeEventPreparationStep:
    status: str
    chat_id: str = ''
    records: int = 0
    gap: bool = False
    messages: tuple = field(default=(), repr=False)
    reactions: tuple = field(default=(), repr=False)


@dataclass
class _Job:
    identities: tuple
    operation: PageOperation | None = None
    next_due: float = 0.0
    exhausted_attempts: int = 0


@dataclass
class _InfoJob(_Job):
    pass


@dataclass
class _InfoSweep:
    after_sequence: int
    through_sequence: int


class NodeMessageEventPreparation:
    """One current capture/prepare/finalize quantum per call, with rotation.

    Retained jobs contain identities and work counters only. A computed page
    never survives a work, restart or unavailable result. Exhausting a retry
    ledger does not prove the event malformed and cannot retire its identity.
    """

    def __init__(self, mesh, *, clock: Callable[[], float] = time.monotonic):
        from .service import Mesh
        from .local_input_runtime import LocalInputRuntime
        if (type(mesh) is not Mesh or type(mesh.local_inputs) is not LocalInputRuntime
                or mesh.local_inputs.store is not mesh.store
                or mesh.local_inputs.transport is not mesh.tx or not callable(clock)):
            raise ValueError('invalid retained node event owner')
        self.mesh, self.owner, self.clock = mesh, mesh.local_inputs, clock
        self._store, self._transport = mesh.store, mesh.tx
        self._viewer, self._machine = mesh.user, mesh.messaging.machine
        self._cursor = None
        self._info_cursor = None
        self._unknown_cursor = None
        self._prefer_unknown = True
        self._prefer_info = False
        self._jobs = OrderedDict()
        self._info_jobs = OrderedDict()
        self._info_sweeps = OrderedDict()
        self._lock = threading.RLock()

    def _bound(self):
        return (self.mesh.local_inputs is self.owner and not self.owner._closed
                and self.mesh.store is self._store and self.mesh.tx is self._transport
                and (self.mesh.messaging.user, self.mesh.messaging.machine)
                == (self._viewer, self._machine))

    def run_one(self) -> NodeEventPreparationStep:
        try:
            return self._run_one()
        except page_operation._ERRORS:
            # Peeking or terminal CAS retirement can fail before preparation.
            # Report unavailable; never erase work or kill the ingestion owner.
            return NodeEventPreparationStep('unavailable', self._cursor or '')

    def _run_one(self) -> NodeEventPreparationStep:
        with self._lock:
            if not self._bound():
                return NodeEventPreparationStep('closed')
            if self._prefer_unknown:
                self._prefer_unknown = False
                unknown = node_event_journal.peek_event_identities(self._store,
                    after_chat=self._unknown_cursor, kind='unknown', limit=1)
                if unknown:
                    chat = unknown[0].chat_id
                    self._unknown_cursor = chat
                    retired = node_event_journal.retire_event_identities_as_gap(
                        self._store, unknown, require_unknown=True)
                    return NodeEventPreparationStep('consumed' if retired else 'restart',
                        chat, 1 if retired else 0, retired)
            attempted_info = self._prefer_info
            if attempted_info:
                self._prefer_info = False
                result = self._consume_info()
                if result is not None:
                    return result
            self._prefer_unknown = True
            self._prefer_info = True
            identities = node_event_journal.peek_event_identities(
                self._store, after_chat=self._cursor, kind='message')
            if not identities:
                self._jobs.clear()
                if not attempted_info:
                    result = self._consume_info()
                    if result is not None:
                        self._prefer_info = False
                        return result
                return NodeEventPreparationStep('idle')
            chat = identities[0].chat_id
            self._cursor = chat
            job = self._jobs.get(chat)
            # Compare only the retained prefix: a split excludes later rows,
            # but an ABA/replaced identity creates a fresh request ledger.
            if job is None or identities[:len(job.identities)] != job.identities:
                job = _Job(identities)
                self._jobs[chat] = job
            self._jobs.move_to_end(chat)
            while len(self._jobs) > MAX_RETAINED_CHATS:
                self._jobs.popitem(last=False)
            now = self.clock()
            if now < job.next_due:
                return NodeEventPreparationStep('waiting', chat)
            try:
                reader, receipt, index = self.owner.inputs(chat)
                if job.operation is None:
                    job.operation = ExactMessageProjectionOperation(
                        self.mesh, chat, tuple(i.message_id for i in job.identities),
                        source_reader=reader, event_identities=job.identities)
                work = job.operation.prepare(receipt, receipt, index)
                if work.status == 'prepared':
                    work = work.prepared.finalize()
            except page_operation._ERRORS as exc:
                work = page_operation._failure(exc)
            if work.status == 'page' or (work.status == 'forbidden' and work.result is not None
                                         and work.result.event_consumption is not None):
                consumption = work.result.event_consumption
                if consumption is None or not consumption.consumed:
                    raise RuntimeError('canonical event consumption missing')
                self._jobs.pop(chat, None)
                return NodeEventPreparationStep('consumed', chat,
                    len(job.identities), consumption.gap,
                    () if consumption.gap or work.result.page is None else work.result.page.messages)
            if work.reason == 'event_id_not_projectable':
                if len(job.identities) > 1:
                    return self._split(job, chat)
                retired = node_event_journal.retire_event_identities_as_gap(
                    self._store, job.identities)
                self._jobs.pop(chat, None)
                return NodeEventPreparationStep('consumed' if retired else 'restart',
                    chat, 1 if retired else 0, retired)
            # A combined capture ceiling may be splittable. A single capture
            # failure is retained: authority/parent bytes, rather than the row
            # alone, may dominate. Cumulative operation budgets also retain.
            if work.reason in {'budget_exhausted', 'operation_parent_budget'} \
                    and len(job.identities) > 1:
                return self._split(job, chat)
            if work.reason == 'budget_exhausted' and len(job.identities) == 1:
                if node_event_journal.retire_event_identities_as_gap(
                        self._store, job.identities, require_oversize=True,
                        state_paths=(P.state(chat, self._viewer),)):
                    self._jobs.pop(chat, None)
                    return NodeEventPreparationStep('consumed', chat, 1, True)
            if work.reason in {
                    'operation_round_budget', 'operation_byte_budget',
                    'operation_step_budget', 'operation_crypto_budget',
                    'operation_epoch_budget'}:
                # A request ledger is finite; keep the wake but begin another
                # request only after increasing bounded cooldown. Recovery may
                # make that next attempt cheaper. Never infer terminal input.
                job.operation = None
                job.exhausted_attempts = min(8, job.exhausted_attempts + 1)
                job.next_due = now + min(60.0, RETRY_S * 2**job.exhausted_attempts)
                return NodeEventPreparationStep('pending', chat)
            if work.status == 'work' and work.reason == 'overlay_proofs':
                self.owner.request_page(chat, index=index, proofs=work.work)
            else:
                self.owner.request(chat, activity=True)
                self.owner.request_page(chat)
            job.next_due = now + RETRY_S
            return NodeEventPreparationStep('pending', chat)

    def _consume_info(self):
        available = node_event_journal.peek_event_identities(
            self._store, after_chat=self._info_cursor, kind='info', limit=1)
        if not available:
            self._info_jobs.clear()
            self._info_sweeps.clear()
            return None
        chat = available[0].chat_id
        self._info_cursor = chat
        identity = self._next_info_identity(chat)
        if identity is None:
            return NodeEventPreparationStep('restart', chat)
        identities = (identity,)
        job_key = (chat, identity.sequence)
        job = self._info_jobs.get(job_key)
        if job is None or job.identities != identities:
            job = _InfoJob(identities)
            self._info_jobs[job_key] = job
        self._info_jobs.move_to_end(job_key)
        while len(self._info_jobs) > node_event_journal.MAX_PENDING_EVENTS:
            self._info_jobs.popitem(last=False)
        now = self.clock()
        if now < job.next_due:
            return NodeEventPreparationStep('waiting', chat)
        try:
            reader, receipt, index = self.owner.inputs(chat)
            if job.operation is None:
                job.operation = ReactionProjectionOperation(
                    self.mesh, chat, tuple(i.message_id for i in job.identities),
                    source_reader=reader, event_identities=job.identities,
                )
            work = job.operation.prepare(receipt, receipt, index)
            if work.status == 'prepared':
                work = work.prepared.finalize()
        except page_operation._ERRORS as exc:
            work = page_operation._failure(exc)
        if work.status == 'page' or (work.status == 'forbidden' and work.result is not None
                                     and work.result.event_consumption is not None):
            consumption = work.result.event_consumption
            if consumption is None or not consumption.consumed:
                raise RuntimeError('reaction event consumption missing')
            reactions = (() if consumption.gap or work.status != 'page' else
                         ReactionProjectionOperation.events(work, self._viewer))
            self._info_jobs.pop(job_key, None)
            return NodeEventPreparationStep(
                'consumed', chat, len(job.identities), consumption.gap,
                reactions=reactions,
            )
        if work.reason == 'event_id_not_projectable':
            retired = node_event_journal.retire_event_identities_as_gap(
                self._store, job.identities)
            self._info_jobs.pop(job_key, None)
            return NodeEventPreparationStep(
                'consumed' if retired else 'restart', chat,
                1 if retired else 0, retired,
            )
        if work.reason == 'budget_exhausted' and len(job.identities) == 1:
            if node_event_journal.retire_event_identities_as_gap(
                    self._store, job.identities, require_oversize=True,
                    state_paths=(P.state(chat, self._viewer),)):
                self._info_jobs.pop(job_key, None)
                return NodeEventPreparationStep('consumed', chat, 1, True)
        if work.reason in {
                'operation_round_budget', 'operation_byte_budget',
                'operation_step_budget', 'operation_crypto_budget',
                'operation_epoch_budget', 'operation_proof_budget'}:
            job.operation = None
            job.exhausted_attempts = min(8, job.exhausted_attempts + 1)
            job.next_due = now + min(60.0, RETRY_S * 2**job.exhausted_attempts)
            return NodeEventPreparationStep('pending', chat)
        if work.status == 'work' and work.reason == 'overlay_proofs':
            self.owner.request_page(chat, index=index, proofs=work.work)
        else:
            self.owner.request(chat, activity=True)
            self.owner.request_page(chat)
        job.next_due = now + RETRY_S
        return NodeEventPreparationStep('pending', chat)

    def _next_info_identity(self, chat):
        sweep = self._info_sweeps.get(chat)
        if sweep is None:
            high_water = node_event_journal.event_identity_high_water(
                self._store, chat, kind='info')
            if high_water is None:
                return None
            sweep = _InfoSweep(0, high_water)
            self._info_sweeps[chat] = sweep
        identities = node_event_journal.peek_chat_event_identities(
            self._store, chat, after_sequence=sweep.after_sequence,
            through_sequence=sweep.through_sequence, kind='info', limit=1,
        )
        if not identities:
            high_water = node_event_journal.event_identity_high_water(
                self._store, chat, kind='info')
            if high_water is None:
                self._info_sweeps.pop(chat, None)
                return None
            # Finish the admitted sweep before observing a later tail. This
            # guarantees retained earlier identities another bounded turn
            # even if new info rows arrive continuously.
            sweep = _InfoSweep(0, high_water)
            self._info_sweeps[chat] = sweep
            identities = node_event_journal.peek_chat_event_identities(
                self._store, chat, after_sequence=0,
                through_sequence=high_water, kind='info', limit=1,
            )
            if not identities:
                self._info_sweeps.pop(chat, None)
                return None
        identity = identities[0]
        sweep.after_sequence = identity.sequence
        self._info_sweeps.move_to_end(chat)
        while len(self._info_sweeps) > node_event_journal.MAX_PENDING_EVENTS:
            self._info_sweeps.popitem(last=False)
        return identity

    @staticmethod
    def _split(job, chat):
        job.identities = job.identities[:max(1, len(job.identities) // 2)]
        job.operation = None
        job.next_due = 0.0
        return NodeEventPreparationStep('split', chat)


__all__ = ['NodeMessageEventPreparation', 'NodeEventPreparationStep']
