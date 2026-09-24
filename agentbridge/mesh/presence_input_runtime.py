"""Serialized background presence ingestion; no request-path provider reads."""
from __future__ import annotations

import threading
import time

from ..store import document_observation, local_source, presence_index
from ..store import presence_publication, source_selectors, staged_source
from ..store.source_publication import SourcePublisher
from ..transport.local_mutations import LocalMutationTransport
from ..transport.raw_documents import RawCollectionUnavailable, collect_document_batches
from .local_presence_source import LocalPresenceSource


class PresenceInputRuntime:
    def __init__(self, transport, store, *, clock=time.monotonic):
        if type(transport) is not LocalMutationTransport:
            raise ValueError('presence ingestion requires a mutation-owned transport')
        if not callable(clock):
            raise ValueError('invalid presence clock')
        self.transport, self.store, self.clock = transport, store, clock
        self.coordinator = transport._coordinator
        staged_source.initialize(store)
        source_selectors.initialize(store)
        presence_index.initialize(store)
        self.coordinator.register_store(store)
        self.reader = LocalPresenceSource(self.coordinator, store)
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._needed = False
        self._closed = False
        self._next_due = 0.0
        self._failures = 0

    def request(self):
        """A bounded hint; neither provider work nor a membership verdict."""
        with self._lock:
            if self._closed:
                return False
            self._needed = True
            return True

    def health(self):
        return local_source.health(self.store, self.reader.definition.source)

    def inputs(self, members):
        receipt = self.reader.capture()
        return self.reader, receipt, self.reader.capture_members(receipt, members)

    def _check_open(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('presence ingestion is closed')

    def _ingest_once(self):
        self._check_open()
        reader = self.reader
        publisher = SourcePublisher(self.coordinator, self.store, reader.definition)
        expected = stage = None
        captured = publisher.capture()
        try:
            with self.coordinator.publication_gate(self.store, reader.definition):
                expected = local_source.claim_collection(self.store, captured.source)
            stage = staged_source.begin_raw(self.store, reader.definition.source, expected=expected)

            def append(batch):
                self._check_open()
                for path in batch:
                    document_observation._validate_document_path(path)
                    if not path.startswith('presence/') or len(path.split('/')) > 32:
                        raise ValueError('document outside presence source scope')
                staged_source.append_raw(self.store, stage, batch)

            collect_document_batches(self.transport._transport, reader.definition, consume=append)
            self._check_open()
            staged_source.finish_raw(self.store, stage)
            presence_index.build(self.store, stage, expected=expected, check_open=self._check_open)
            self._check_open()
            with self.coordinator.publication_gate(self.store, reader.definition):
                admitted = presence_publication.admit(
                    self.store, expected, stage, observed_ns=time.time_ns())
            if captured.source.raw.source_id != admitted.raw.source_id:
                staged_source.retire_generation(self.store, captured.source.raw.source_id)
            return admitted
        except Exception as exc:
            budget = isinstance(exc, OverflowError) or (
                isinstance(exc, RawCollectionUnavailable) and exc.args and
                exc.args[0] in ('document_budget', 'byte_budget', 'path_budget',
                                'document_byte_budget'))
            if expected is not None:
                with self.coordinator.publication_gate(self.store, reader.definition):
                    local_source.record_failure(self.store, reader.definition.source,
                                                reason='budget' if budget else 'unavailable',
                                                expected=expected)
            raise
        finally:
            if stage is not None:
                staged_source.abort(self.store, stage)
                staged_source.cleanup(self.store, max_rows=128)

    def ingest(self):
        """One complete background attempt; direct callers share the worker lock."""
        with self._worker_lock:
            return self._ingest_once()

    def run_due(self):
        """Rate-limit requested scans to four seconds; retry failures with backoff."""
        if not self._worker_lock.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            with self._lock:
                if self._closed or not self._needed or now < self._next_due:
                    return False
                self._needed = False
            try:
                self._ingest_once()
            except Exception:
                with self._lock:
                    self._failures = min(8, self._failures + 1)
                    self._next_due = self.clock() + min(60.0, 4.0 * 2 ** (self._failures - 1))
                    if not self._closed:
                        self._needed = True
            else:
                with self._lock:
                    self._failures = 0
                    self._next_due = self.clock() + 4.0
            return True
        finally:
            self._worker_lock.release()

    def stop(self):
        """Caller owns the serialized worker lifetime and Store shutdown."""
        with self._lock:
            self._closed = True
            self._needed = False
