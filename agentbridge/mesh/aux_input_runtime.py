"""One reusable background owner for independent status and room runtime raw sources.

Requests are coalesced hints. Only the serialized worker performs collection;
foreground inputs capture an admitted immutable source and bounded raw docs.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from ..store import document_observation, local_source, raw_publication, staged_source
from ..store import source_selectors, lifecycle_inputs
from ..store.source_publication import SourcePublisher
from ..transport.local_mutations import LocalMutationTransport
from ..transport.raw_documents import RawCollectionUnavailable, collect_document_batches
from .local_aux_source import LocalAuxSource


MAX_JOBS = 132  # 128 room companions plus four root scopes.


class AuxInputRuntime:
    def __init__(self, transport, store, *, clock=time.monotonic):
        if type(transport) is not LocalMutationTransport or not callable(clock):
            raise ValueError('aux ingestion requires mutation-owned transport and clock')
        self.transport, self.store, self.clock = transport, store, clock
        self.coordinator = transport._coordinator
        staged_source.initialize(store)
        source_selectors.initialize(store)
        self.coordinator.register_store(store)
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._jobs = OrderedDict()
        self._serial = 0
        self._running = None
        self._closed = False

    def reader(self, scope, chat_id=''):
        return LocalAuxSource(self.coordinator, self.store, scope, chat_id)

    def inputs(self, scope, chat_id='', *, max_documents=10_000, max_bytes=8 * 1024 * 1024):
        reader = self.reader(scope, chat_id)
        receipt = reader.capture()
        return reader, receipt, reader.capture_documents(
            receipt, max_documents=max_documents, max_bytes=max_bytes)

    def health(self, scope, chat_id=''):
        return local_source.health(self.store, self.reader(scope, chat_id).definition.source)

    def request(self, scope, chat_id=''):
        reader = self.reader(scope, chat_id)
        key = (reader.scope, reader.chat_id)
        with self._lock:
            if self._closed:
                return False
            now = self.clock()
            self._serial += 1
            existing = self._jobs.get(key)
            if existing is None:
                if len(self._jobs) >= MAX_JOBS:
                    victim = next((old for old in self._jobs
                                   if old[0] == 'runtime' and old != self._running), None)
                    if victim is None:
                        return False
                    del self._jobs[victim]
                self._jobs[key] = (now + 0.05, 0, self._serial)
            else:
                due, failures, _serial = existing
                self._jobs[key] = (min(due, now + 0.5) if failures == 0 else due,
                                   failures, self._serial)
            self._jobs.move_to_end(key)
            return True

    def _check_open(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('aux ingestion closed')

    def ingest(self, scope, chat_id=''):
        with self._worker_lock:
            return self._ingest(self.reader(scope, chat_id))

    def _ingest(self, reader):
        self._check_open()
        publisher = SourcePublisher(self.coordinator, self.store, reader.definition)
        captured = publisher.capture()
        expected = stage = None
        try:
            if reader.scope == 'identities':
                # Chatless controls must not depend on a room preparation job.
                # Index work belongs to this serialized background owner.
                lifecycle_inputs.prepare(self.store._conn())
            with self.coordinator.publication_gate(self.store, reader.definition):
                expected = local_source.claim_collection(self.store, captured.source)
            stage = staged_source.begin_raw(self.store, reader.definition.source, expected=expected)
            prefixes = tuple(selector.value + '/' for selector in reader.definition.selectors)

            def append(batch):
                self._check_open()
                for path in batch:
                    document_observation._validate_document_path(path)
                    if not any(path.startswith(prefix) for prefix in prefixes) or len(path.split('/')) > 32:
                        raise ValueError('document outside auxiliary source scope')
                staged_source.append_raw(self.store, stage, batch)

            collect_document_batches(self.transport._transport, reader.definition, consume=append)
            self._check_open()
            staged_source.finish_raw(self.store, stage)
            equality = raw_publication.identical(self.store, expected, stage)
            with self.coordinator.publication_gate(self.store, reader.definition):
                admitted = raw_publication.admit(self.store, expected, stage,
                                                  observed_ns=time.time_ns(), comparison=equality)
            if captured.source.raw.source_id != admitted.raw.source_id:
                staged_source.retire_generation(self.store, captured.source.raw.source_id)
            return admitted
        except Exception as exc:
            budget = isinstance(exc, OverflowError) or (
                isinstance(exc, RawCollectionUnavailable) and exc.args and exc.args[0] in (
                    'document_budget', 'byte_budget', 'path_budget', 'document_byte_budget'))
            if expected is not None:
                with self.coordinator.publication_gate(self.store, reader.definition):
                    local_source.record_failure(self.store, reader.definition.source,
                        reason='budget' if budget else 'unavailable', expected=expected)
            raise
        finally:
            if stage is not None:
                staged_source.abort(self.store, stage)
                staged_source.cleanup(self.store, max_rows=128)

    def run_due(self):
        if not self._worker_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if self._closed or not self._jobs:
                    return False
                now = self.clock()
                due = [(when, key) for key, (when, _failures, _serial) in self._jobs.items()
                       if when <= now]
                if not due:
                    return False
                _when, key = min(due)
                _started_serial = self._jobs[key][2]
                self._running = key
            success = False
            try:
                self._ingest(self.reader(*key))
                success = True
            except Exception:
                pass  # health is recorded by the admission owner; retry boundedly.
            with self._lock:
                if not self._closed and key in self._jobs:
                    requested_due, failures, current_serial = self._jobs[key]
                    failures = 0 if success else min(8, failures + 1)
                    delay = 4.0 if success else min(60.0, 4.0 * 2 ** (failures - 1))
                    next_due = self.clock() + delay
                    if success and current_serial != _started_serial:
                        next_due = min(next_due, max(self.clock() + 0.35, requested_due))
                    self._jobs[key] = (next_due, failures, current_serial)
                    self._jobs.move_to_end(key)
            return True
        finally:
            with self._lock:
                self._running = None
            self._worker_lock.release()

    def stop(self):
        with self._lock:
            self._closed = True
            self._jobs.clear()
