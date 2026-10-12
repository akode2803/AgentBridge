"""Retained, bounded ordinary-message work; no cached authority or payloads.

Info identities use a separate lane and remain pending until event-time
acceptance can be established. This owner does not activate the node runtime.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from ..store import node_event_journal
import sqlite3
from . import page_operation
from .page_operation import ExactMessageProjectionOperation

RETRY_S = 0.35
MAX_RETAINED_CHATS = 16


@dataclass(frozen=True)
class NodeEventPreparationStep:
    status: str
    chat_id: str = ''
    records: int = 0
    gap: bool = False
    messages: tuple = field(default=(), repr=False)


@dataclass
class _Job:
    identities: tuple
    operation: ExactMessageProjectionOperation | None = None
    next_due: float = 0.0
    exhausted_attempts: int = 0


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
        self._jobs = OrderedDict()
        self._lock = threading.RLock()

    def _bound(self):
        return (self.mesh.local_inputs is self.owner and not self.owner._closed
                and self.mesh.store is self._store and self.mesh.tx is self._transport
                and (self.mesh.messaging.user, self.mesh.messaging.machine)
                == (self._viewer, self._machine))

    def run_one(self) -> NodeEventPreparationStep:
        try:
            return self._run_one()
        except (sqlite3.Error, OSError, node_event_journal.NodeEventJournalChanged):
            # Peeking or terminal CAS retirement can fail before preparation.
            # Report unavailable; never erase work or kill the ingestion owner.
            return NodeEventPreparationStep('unavailable', self._cursor or '')

    def _run_one(self) -> NodeEventPreparationStep:
        with self._lock:
            if not self._bound():
                return NodeEventPreparationStep('closed')
            identities = node_event_journal.peek_event_identities(
                self._store, after_chat=self._cursor, kind='message')
            if not identities:
                self._jobs.clear()
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
            if work.status == 'page':
                consumption = work.result.event_consumption
                if consumption is None or not consumption.consumed:
                    raise RuntimeError('canonical event consumption missing')
                self._jobs.pop(chat, None)
                return NodeEventPreparationStep('consumed', chat,
                    len(job.identities), consumption.gap,
                    () if consumption.gap else work.result.page.messages)
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
                        self._store, job.identities, require_oversize=True):
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

    @staticmethod
    def _split(job, chat):
        job.identities = job.identities[:max(1, len(job.identities) // 2)]
        job.operation = None
        job.next_due = 0.0
        return NodeEventPreparationStep('split', chat)


__all__ = ['NodeMessageEventPreparation', 'NodeEventPreparationStep']
