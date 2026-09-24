"""Opt-in background local raw-input ingestion, never a permission cache.

Logs retain their independent verified ingestion path. A successful document
publication is neither remote completeness nor an atomic transport snapshot.
Foreground inputs only read admitted SQLite state and never repair it.
"""
from __future__ import annotations

import threading
import time

from ..store import local_source, overlay_index, source_selectors, staged_source, staged_publication, document_observation
from ..store.source_publication import SourcePublisher
from ..transport.local_mutations import LocalMutationTransport
from ..transport.raw_documents import collect_document_batches, RawCollectionUnavailable
from .local_page_source import LocalPageSource
from .source_schedule import SourceSchedule


class LocalInputRuntime:
    def __init__(self, transport, store):
        if type(transport) is not LocalMutationTransport:
            raise ValueError('local ingestion requires a mutation-owned transport')
        self.transport, self.store = transport, store
        self.coordinator = transport._coordinator
        local_source.initialize(store)
        source_selectors.initialize(store)
        staged_source.initialize(store)
        self.coordinator.register_store(store)
        self.schedule = SourceSchedule()
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._selected = None
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._page_preparation = None
        self._discovery = None
        self._discovery_due = 0.0
        self.presence = None
        self.auxiliary = None

    def bind_page_owner(self, mesh):
        from .page_preparation import PagePreparation
        from .source_discovery import SourceDiscovery
        from .presence_input_runtime import PresenceInputRuntime
        from .aux_input_runtime import AuxInputRuntime
        if mesh.store is not self.store or mesh.tx is not self.transport:
            raise ValueError('foreign page preparation owner')
        with self._lock:
            if self._closed or self._page_preparation is not None:
                raise RuntimeError('page preparation already bound or closed')
            self._page_preparation = PagePreparation(mesh)
            self._discovery = SourceDiscovery(self.store)
            self.presence = PresenceInputRuntime(self.transport, self.store)
            self.auxiliary = AuxInputRuntime(self.transport, self.store)

    def request_page(self, chat, *, index=None, proofs=()):
        with self._lock:
            if self._closed or self._page_preparation is None:
                return False
            return self._page_preparation.request(chat, index=index, proofs=proofs)

    def prepare_one(self):
        # Serialized with collection and stop. A stale proof request cannot
        # change source readiness; the next page request recaptures its inputs.
        with self._worker_lock:
            if self._closed or self._page_preparation is None:
                return False
            try:
                return self._page_preparation.run_one()
            except Exception:
                return False

    def preparation_health(self, chat):
        with self._lock:
            return self._page_preparation.health(chat) if self._page_preparation is not None else None

    def discover(self):
        if self._discovery is None or self._closed:
            return
        now = time.monotonic()
        if now < self._discovery_due:
            return
        self._discovery_due = now + 4.0
        for chat in self._discovery.next_batch(max_rooms=32):
            self.schedule.discover(chat, now=now)

    def reader(self, chat):
        return LocalPageSource(self.coordinator, self.store, chat)

    def request(self, chat, *, selected=False, activity=False):
        # Validate through the canonical path selector before queue admission.
        chat = self.reader(chat).chat
        with self._lock:
            if self._closed:
                return False
            accepted = self.schedule.request(chat, now=time.monotonic(),
                                             selected=selected, activity=activity)
            if accepted and selected:
                self._selected = chat
            return accepted

    def clear_selection(self):
        with self._lock:
            self._selected = None
            self.schedule.clear_selection()

    def hint(self):
        # Hints contain no authority, changed paths or provider payloads.
        with self._lock:
            selected = self._selected
        if selected is not None:
            self.request(selected, activity=True)
            if self.auxiliary is not None:
                self.auxiliary.request('status')
                self.auxiliary.request('runtime', selected)

    def health(self, chat):
        reader = self.reader(chat)
        return local_source.health(self.store, reader.definition.source)

    @staticmethod
    def _index(reader, receipt):
        with reader._read(receipt) as (conn, receipt):
            overlay_index._schema(conn)
            row = conn.execute('SELECT build,schema FROM overlay_index_ready WHERE source=? '
                               "AND typeof(build)='text' AND length(CAST(build AS BLOB))=32 "
                               "AND typeof(schema)='integer'",
                               (receipt.source.raw.source_id,)).fetchone()
            if row is None:
                raise overlay_index.OverlayIndexUnavailable('index_pending')
            index = overlay_index.OverlayIndexPosition(receipt.source.raw, reader.chat, *row)
            index = overlay_index._wanted(index, reader.store.path)
            overlay_index._ready(conn, reader.store.path, index)
            return index

    def inputs(self, chat):
        """Foreground: no collection, registration, index rebuild or authority."""
        reader = self.reader(chat)
        receipt = reader.capture()
        index = self._index(reader, receipt)
        with reader.finalization(receipt) as conn:
            overlay_index._ready(conn, self.store.path, index)
        return reader, receipt, index

    def ingest(self, chat):
        """One complete bounded background attempt; serialize this owner only."""
        with self._worker_lock:
            if self._closed:
                raise RuntimeError('local input runtime is closed')
            reader = self.reader(chat)
            publisher = SourcePublisher(self.coordinator, self.store, reader.definition)
            expected = None
            stage = None
            try:
                captured = publisher.capture()
                # Build an invisible candidate while the latest admitted raw
                # snapshot remains readable. The final transaction CAS checks
                # this exact owner position; local writes still retire it
                # durably before attempting any external mutation.
                with self.coordinator.publication_gate(self.store, reader.definition):
                    expected = local_source.claim_collection(self.store, captured.source)
                stage = staged_source.begin(self.store, reader.definition.source, reader.chat, expected=expected)
                exact = {s.value for s in reader.definition.selectors if s.kind == 'doc_exact'}
                prefixes = {s.value for s in reader.definition.selectors if s.kind == 'doc_prefix'}

                def append(batch):
                    if self._stop.is_set() or self._closed:
                        raise RuntimeError('local input ingestion stopped')
                    for path in batch:
                        document_observation._validate_document_path(path)
                        if len(path.split('/')) > 32:
                            raise ValueError('source document depth')
                        if path not in exact and not any(path == p or path.startswith(p + '/') for p in prefixes):
                            raise ValueError('document outside declared source scope')
                    staged_source.append(self.store, stage, batch)

                collect_document_batches(self.transport._transport, reader.definition, consume=append)
                staged_source.finish(self.store, stage)
                reuse = None
                comparison = staged_publication.identical(self.store, expected, stage)
                if comparison:
                    # Retain unchanged raw/index identity after full comparison.
                    conn = document_observation._open_reader(self.store.path)
                    try:
                        conn.execute('BEGIN')
                        row = conn.execute('SELECT build,schema FROM overlay_index_ready WHERE source=?',
                                           (expected.raw.source_id,)).fetchone()
                        if row is not None:
                            reuse = overlay_index.OverlayIndexPosition(expected.raw, reader.chat, *row)
                            reuse = overlay_index._wanted(reuse, self.store.path)
                            overlay_index._ready(conn, self.store.path, reuse)
                    finally:
                        conn.close()
                with self.coordinator.publication_gate(self.store, reader.definition):
                    published, index = staged_publication.admit(self.store, expected, stage,
                        observed_ns=time.time_ns(), reuse=reuse, comparison=comparison)
                # Keep failure handling bound to the pre-admission claim.
                # Post-commit cleanup cannot retire the newly admitted winner.
                if captured.source.raw.source_id != published.raw.source_id:
                    staged_source.retire_generation(self.store, captured.source.raw.source_id)
                receipt = reader.capture()
                if receipt.source != published:
                    raise local_source.SourceChanged('ingestion_superseded')
                with reader.finalization(receipt) as conn:
                    overlay_index._ready(conn, self.store.path, index)
                return captured.source.raw != published.raw
            except Exception as exc:
                budget = isinstance(exc, OverflowError) or (
                    isinstance(exc, RawCollectionUnavailable) and exc.args and
                    exc.args[0] in ('document_budget', 'byte_budget', 'path_budget', 'document_byte_budget'))
                if expected is not None:
                    with self.coordinator.publication_gate(self.store, reader.definition):
                        local_source.record_failure(self.store, reader.definition.source,
                            reason='budget' if budget else 'unavailable', expected=expected)
                raise
            finally:
                if stage is not None:
                    # abort never retires a mapped generation. Partial/unused
                    # candidates become reclaimable in bounded cleanup steps.
                    staged_source.abort(self.store, stage)
                    staged_source.cleanup(self.store, max_rows=128)

    def run_due(self):
        job = self.schedule.take_due(now=time.monotonic())
        if job is None:
            return False
        changed, success = False, False
        try:
            changed = self.ingest(job.chat_id)
            success = True
        except Exception:
            pass  # health recorded; scheduler backs off without discarding intent
        finally:
            self.schedule.finish(job, now=time.monotonic(), changed=changed, success=success)
        return True

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('local input runtime is closed')
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='local-input-ingestion', daemon=True)
            self._thread.start()

    def _run(self):
        watcher = None
        try:
            try:
                watcher = self.transport.watch()
            except Exception:
                pass  # hints are optional; finite fallback polling remains
            while not self._stop.is_set():
                try:
                    self.discover()
                except Exception:
                    pass  # index preparation may still be pending
                prepared = self.prepare_one()
                ingested = self.run_due()
                with self._worker_lock:
                    presence_work = self.presence.run_due() if self.presence is not None and not self._closed else False
                    aux_work = self.auxiliary.run_due() if self.auxiliary is not None and not self._closed else False
                if ingested or presence_work or aux_work:
                    continue
                # Reclaim old generations between scheduled scans in bounded
                # transactions. Selected-room work is reconsidered every chunk.
                try:
                    with self._worker_lock:
                        reclaimed = staged_source.cleanup(self.store, max_rows=128)
                except Exception:
                    # Cleanup cannot restore readiness or waive capacity. Keep
                    # ingestion/health polling alive if reclamation fails.
                    reclaimed = 0
                if reclaimed or prepared:
                    continue
                delay = self.schedule.wait_s(now=time.monotonic(), maximum=0.35)
                if watcher is None:
                    self._stop.wait(delay)
                    continue
                try:
                    hinted = watcher.wait(delay)
                except Exception:
                    try:
                        watcher.close()
                    except Exception:
                        pass
                    watcher = None
                    continue
                if hinted:
                    self.hint()
        finally:
            if watcher is not None:
                watcher.close()

    def stop(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            if self._page_preparation is not None:
                self._page_preparation.close()
            if self.presence is not None:
                self.presence.stop()
            if self.auxiliary is not None:
                self.auxiliary.stop()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError('local input ingestion has not stopped; Store must remain open')
        # Direct off-path callers also own work through this lock. Never close
        # SQLite while either the background worker or a manual ingest uses it.
        if not self._worker_lock.acquire(timeout=5):
            raise RuntimeError('local input ingestion has not stopped; Store must remain open')
        self._worker_lock.release()
