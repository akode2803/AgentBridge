"""Opt-in background local raw-input ingestion, never a permission cache.

Logs retain their independent verified ingestion path. A successful document
publication is neither remote completeness nor an atomic transport snapshot.
Foreground inputs only read admitted SQLite state and never repair it.
"""
from __future__ import annotations

import threading
import time

from ..store import local_source, overlay_index, source_selectors
from ..store.source_publication import SourcePublisher
from ..transport.local_mutations import LocalMutationTransport
from ..transport.raw_documents import collect_documents
from .local_page_source import LocalPageSource
from .overlay_index import prepare_overlay_index
from .source_schedule import SourceSchedule


class LocalInputRuntime:
    def __init__(self, transport, store):
        if type(transport) is not LocalMutationTransport:
            raise ValueError('local ingestion requires a mutation-owned transport')
        self.transport, self.store = transport, store
        self.coordinator = transport._coordinator
        local_source.initialize(store)
        source_selectors.initialize(store)
        self.coordinator.register_store(store)
        self.schedule = SourceSchedule()
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._selected = None
        self._stop = threading.Event()
        self._thread = None
        self._closed = False

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
            try:
                captured = publisher.capture()
                expected = captured.source
                documents = collect_documents(self.transport._transport, reader.definition)
                published = publisher.publish(captured, documents, observed_ns=time.time_ns())
                expected = published
                receipt = reader.capture()
                if receipt.source != published:
                    raise local_source.SourceChanged('ingestion_superseded')
                try:
                    index = self._index(reader, receipt)
                except overlay_index.OverlayIndexUnavailable:
                    prefix = f'chats/{reader.chat}/overlays/'
                    paths = tuple(sorted(p for p in documents if p.startswith(prefix)))
                    observed = self.store.capture_selected_documents(receipt.source.raw, paths,
                        max_documents=20_000, max_bytes=16 * 1024 * 1024)
                    index = self.store.publish_overlay_index(prepare_overlay_index(observed, reader.chat),
                                                     shared_source=True)
                # Admission/index preparation racing a local writer cannot be
                # reported as current successful work after that invalidation.
                with reader.finalization(receipt) as conn:
                    overlay_index._ready(conn, self.store.path, index)
                return captured.source.raw != receipt.source.raw
            except Exception:
                # Exception text may include paths/provider credentials. Health
                # deliberately records only a stable content-free outcome.
                if expected is not None:
                    with self.coordinator.publication_gate(self.store, reader.definition):
                        local_source.record_failure(self.store, reader.definition.source,
                            reason='unavailable', expected=expected)
                raise

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
                if self.run_due():
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
