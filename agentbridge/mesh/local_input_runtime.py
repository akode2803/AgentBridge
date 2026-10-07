"""Opt-in background local raw-input ingestion, never a permission cache.

Logs retain their independent verified ingestion path. A successful document
publication is neither remote completeness nor an atomic transport snapshot.
Foreground inputs only read admitted SQLite state and never repair it.
"""
from __future__ import annotations

from ..core.input_phase_timings import span
from ..core import delivery_trace

import threading
import time
from dataclasses import dataclass

from ..store import local_source, overlay_index, source_selectors, staged_source, staged_publication, document_observation
from ..store.source_publication import SourcePublisher
from ..transport.local_mutations import LocalMutationTransport
from ..transport.raw_documents import collect_document_batches, RawCollectionUnavailable
from .local_page_source import LocalPageSource, LocalSourceReceipt
from .source_schedule import SourceSchedule


@dataclass(frozen=True)
class _LocalAppendAdmission:
    chat: str
    source: local_source.SourcePosition


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
        self.unread = None
        self.read_events = None

    def bind_page_owner(self, mesh):
        from .page_preparation import PagePreparation
        from .source_discovery import SourceDiscovery
        from .presence_input_runtime import PresenceInputRuntime
        from .aux_input_runtime import AuxInputRuntime
        from .unread_runtime import UnreadRuntime
        from .read_events import ReadEvents
        if mesh.store is not self.store or mesh.tx is not self.transport:
            raise ValueError('foreign page preparation owner')
        with self._lock:
            if self._closed or self._page_preparation is not None:
                raise RuntimeError('page preparation already bound or closed')
            self.read_events = ReadEvents(mesh.bus)
            self._page_preparation = PagePreparation(mesh, on_ready=self.read_events.changed)
            self._discovery = SourceDiscovery(self.store)
            self.presence = PresenceInputRuntime(self.transport, self.store,
                on_change=lambda: self.read_events.changed('aux'))
            self.auxiliary = AuxInputRuntime(self.transport, self.store,
                on_change=self._aux_changed)
            self.unread = UnreadRuntime(mesh)

    def _aux_changed(self, scope, chat):
        selected = ('sidebar' if scope == 'users' else 'controls'
                    if scope in ('identities', 'peer') else 'aux')
        self.read_events.changed(selected, chat)

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

    def _prepare_burst(self):
        # Yield to collection after at most four existing bounded quanta.
        # The time budget is cooperative: a single slow quantum completes
        # under prepare_one's existing lock and shutdown contract.
        deadline = time.monotonic() + 0.020
        prepared = False
        for _ in range(4):
            if self._stop.is_set() or time.monotonic() >= deadline:
                break
            if not self.prepare_one():
                break
            prepared = True
        return prepared

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

    @delivery_trace.observed('ingestion_queued', chat_arg=1)
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
                if self._page_preparation is not None:
                    self._page_preparation.select(chat)
            return accepted

    def clear_selection(self):
        with self._lock:
            self._selected = None
            self.schedule.clear_selection()
            if self._page_preparation is not None:
                self._page_preparation.select(None)
            if self.unread is not None:
                self.unread.clear_selection()

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

    def prepare_local_append(self, chat, record):
        """Capture a ready pre-append source for an optional exact fast path."""
        if type(record) is not dict or record.get('kind') != 'message':
            return None
        reader = self.reader(chat)
        with self._lock:
            if self._closed:
                return None
        try:
            with self.coordinator.publication_gate(self.store, reader.definition):
                source = local_source.capture(self.store, reader.definition.source)
                if not source.ready or source.writes_pending:
                    return None
                return _LocalAppendAdmission(reader.chat, source)
        except (local_source.SourceChanged, OSError):
            return None

    def admit_local_append(self, admission, record):
        """Restore readiness only for the exact definitely appended local row."""
        if type(admission) is not _LocalAppendAdmission:
            return False
        reader = self.reader(admission.chat)
        if admission.source.source_id != reader.definition.source:
            raise ValueError('foreign local append admission')
        with self._lock:
            if self._closed:
                return False
        try:
            with self.coordinator.publication_gate(self.store, reader.definition):
                local_source.admit_confirmed_local_message(
                    self.store, admission.source, admission.chat, record,
                )
            delivery_trace.emit(
                'local_snapshot_admitted', chat=admission.chat,
                message=record.get('id', ''), outcome='completed',
            )
            return True
        except (local_source.SourceChanged, OSError, OverflowError, ValueError):
            # The ordinary complete-source ingestion path remains authoritative
            # after any missed CAS, malformed row or unavailable storage.
            return False

    def local_append_settled(self, chat, record):
        """After outbox deletion, prepare terminal facts and reconcile fully."""
        if type(record) is not dict or record.get('kind') != 'message':
            return False
        try:
            chat = self.reader(chat).chat
        except ValueError:
            return False
        with self._lock:
            if self._closed:
                return False
        try:
            self.request_page(chat)
        finally:
            # The exact fast admission is only a latest-successful local
            # snapshot. Keep the complete transport-neutral collector scheduled
            # even if terminal-page preparation itself is temporarily broken.
            self.request(chat, activity=True)
        return True

    def inputs(self, chat):
        """Capture ready source and index at one bounded coordinator/SQLite cut.

        Separate capture/index/finalization transactions can straddle a harmless
        background collection claim or publication and report pending although
        each generation was ready. This is raw input capture, not authority;
        PageOperation still verifies and finalizes its own canonical result.
        """
        reader = self.reader(chat)
        with self.coordinator.finalization_cut(self.store, reader.definition) as (conn, source):
            with span('index_checks'):
                receipt = LocalSourceReceipt(reader.chat, str(self.coordinator.path),
                                             self.coordinator.epoch, source)
                row = conn.execute('SELECT build,schema FROM overlay_index_ready WHERE source=? '
                                   "AND typeof(build)='text' AND length(CAST(build AS BLOB))=32 "
                                   "AND typeof(schema)='integer'",
                                   (source.raw.source_id,)).fetchone()
                if row is None:
                    raise overlay_index.OverlayIndexUnavailable('index_pending')
                index = overlay_index.OverlayIndexPosition(source.raw, reader.chat, *row)
                index = overlay_index._wanted(index, reader.store.path)
                overlay_index._ready(conn, self.store.path, index)
        return reader, receipt, index

    @delivery_trace.observed('source_ingestion', chat_arg=1)
    def ingest(self, chat):
        """One complete bounded background attempt; serialize this owner only."""
        with self._worker_lock:
            if self._closed:
                raise RuntimeError('local input runtime is closed')
            reader = self.reader(chat)
            publisher = SourcePublisher(self.coordinator, self.store, reader.definition)
            expected = None
            stage = None
            profile_started = delivery_trace.queue_clock()
            profile = {} if profile_started is not None else None
            profile_ref = delivery_trace.sampling_reference() if profile is not None else None
            profile_status, profile_error = 'ok', None
            collection_fields = (
                'documents_examined', 'documents_selected',
                'document_bytes', 'document_batches',
            )

            def timed(name, started, *, add=False):
                if profile is None:
                    return
                try:
                    value = delivery_trace.elapsed_ms(started)
                    if value is not None:
                        profile[name] = profile.get(name, 0) + value if add else value
                except Exception:
                    pass  # Profiling cannot replace the ingestion result.

            def merge_collection(stats):
                if profile is None or stats is None:
                    return
                try:
                    for name in collection_fields:
                        value = stats.get(name)
                        if type(value) is int:
                            profile[name] = profile.get(name, 0) + value
                except Exception:
                    pass  # Profiling cannot replace the ingestion result.

            try:
                started = delivery_trace.queue_clock()
                captured = publisher.capture()
                # Build an invisible candidate while the latest admitted raw
                # snapshot remains readable. The final transaction CAS checks
                # this exact owner position; local writes still retire it
                # durably before attempting any external mutation.
                with self.coordinator.publication_gate(self.store, reader.definition):
                    expected = local_source.claim_collection(self.store, captured.source)
                timed('capture_claim_ms', started)

                def collect(consume, stats):
                    def checked(batch):
                        if self._stop.is_set() or self._closed:
                            raise RuntimeError('local input ingestion stopped')
                        consume(batch)

                    collect_document_batches(
                        self.transport._transport, reader.definition,
                        consume=checked, stats=stats,
                    )

                def finalize(published, index):
                    final_started = delivery_trace.queue_clock()
                    try:
                        receipt = reader.capture()
                        if receipt.source != published:
                            raise local_source.SourceChanged('ingestion_superseded')
                        with reader.finalization(receipt) as conn:
                            overlay_index._ready(conn, self.store.path, index)
                    finally:
                        timed('source_finalize_ms', final_started)

                if expected.ready and expected.raw.initialized:
                    comparison_collection = {} if profile is not None else None

                    def compare_collection(consume):
                        collect_started = delivery_trace.queue_clock()
                        try:
                            collect(consume, comparison_collection)
                        finally:
                            timed('collect_ms', collect_started, add=True)
                            merge_collection(comparison_collection)

                    started = delivery_trace.queue_clock()
                    try:
                        equality = staged_publication.identical_admitted(
                            self.store, expected, reader.chat, compare_collection,
                        )
                    finally:
                        timed('compare_ms', started, add=True)
                    if equality:
                        started = delivery_trace.queue_clock()
                        try:
                            with self.coordinator.publication_gate(
                                    self.store, reader.definition):
                                published, index = staged_publication.admit_identical(
                                    self.store, expected, equality,
                                    observed_ns=time.time_ns(),
                                )
                        finally:
                            timed('admit_ms', started)
                        finalize(published, index)
                        return False

                started = delivery_trace.queue_clock()
                stage = staged_source.begin(self.store, reader.definition.source, reader.chat, expected=expected)
                timed('stage_open_ms', started)
                exact = {s.value for s in reader.definition.selectors if s.kind == 'doc_exact'}
                prefixes = {s.value for s in reader.definition.selectors if s.kind == 'doc_prefix'}

                def append(batch):
                    for path in batch:
                        document_observation._validate_document_path(path)
                        if len(path.split('/')) > 32:
                            raise ValueError('source document depth')
                        if path not in exact and not any(path == p or path.startswith(p + '/') for p in prefixes):
                            raise ValueError('document outside declared source scope')
                    write_started = delivery_trace.queue_clock()
                    try:
                        staged_source.append(self.store, stage, batch)
                    finally:
                        timed('stage_write_ms', write_started, add=True)

                collection = {} if profile is not None else None
                started = delivery_trace.queue_clock()
                try:
                    collect(append, collection)
                finally:
                    timed('collect_ms', started, add=True)
                    merge_collection(collection)
                started = delivery_trace.queue_clock()
                staged_source.finish(self.store, stage)
                timed('seal_ms', started)
                reuse = None
                started = delivery_trace.queue_clock()
                comparison = staged_publication.identical(self.store, expected, stage)
                timed('compare_ms', started, add=True)
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
                started = delivery_trace.queue_clock()
                try:
                    with self.coordinator.publication_gate(self.store, reader.definition):
                        published, index = staged_publication.admit(self.store, expected, stage,
                            observed_ns=time.time_ns(), reuse=reuse, comparison=comparison)
                finally:
                    timed('admit_ms', started)
                # Keep failure handling bound to the pre-admission claim.
                # Post-commit cleanup cannot retire the newly admitted winner.
                if captured.source.raw.source_id != published.raw.source_id:
                    staged_source.retire_generation(self.store, captured.source.raw.source_id)
                finalize(published, index)
                changed = captured.source.raw != published.raw
                if changed or not captured.source.ready:
                    # Wake terminal preparation from an admitted source change,
                    # rather than relying on repeated ready inventory polls.
                    self.request_page(chat)
                    if self.read_events is not None:
                        self.read_events.changed('chat', chat)
                return changed
            except Exception as exc:
                profile_status, profile_error = 'error', type(exc).__name__
                budget = isinstance(exc, OverflowError) or (
                    isinstance(exc, RawCollectionUnavailable) and exc.args and
                    exc.args[0] in ('document_budget', 'byte_budget', 'path_budget', 'document_byte_budget'))
                if expected is not None:
                    with self.coordinator.publication_gate(self.store, reader.definition):
                        local_source.record_failure(self.store, reader.definition.source,
                            reason='budget' if budget else 'unavailable', expected=expected)
                if (isinstance(exc, staged_source.StageChanged)
                        and exc.args == ('stage_owner_changed',)):
                    # A local mutation may cross the narrow gap between the
                    # source claim and creation of its invisible stage. That is
                    # a routine lost source CAS, not a staging-format failure.
                    # Keep the mutation's newer position and let the bounded
                    # scheduler or foreground test driver collect it again.
                    raise local_source.SourceChanged(
                        'collection_superseded') from exc
                raise
            finally:
                try:
                    if stage is not None:
                        cleanup_started = delivery_trace.queue_clock()
                        try:
                            # abort never retires a mapped generation. Partial/unused
                            # candidates become reclaimable in bounded cleanup steps.
                            staged_source.abort(self.store, stage)
                            staged_source.cleanup(self.store, max_rows=128)
                        finally:
                            timed('cleanup_ms', cleanup_started)
                except BaseException as exc:
                    profile_status, profile_error = 'error', type(exc).__name__
                    raise
                finally:
                    if profile is not None:
                        delivery_trace.emit(
                            'source_reconciliation', chat=chat,
                            status=profile_status, error_type=profile_error,
                            sample_ref=profile_ref,
                            duration_ms=delivery_trace.elapsed_ms(profile_started),
                            **profile,
                        )

    def run_due(self):
        job = self.schedule.take_due(now=time.monotonic())
        if job is None:
            return False
        delivery_trace.emit('ingestion_claimed', chat=job.chat_id)
        changed, success, blocked = False, False, False
        try:
            changed = self.ingest(job.chat_id)
            success = True
        except local_source.SourceChanged as exc:
            blocked = exc.args == ('source_mutation_pending',)
        except Exception:
            pass  # health recorded; scheduler backs off without discarding intent
        finally:
            self.schedule.finish(job, now=time.monotonic(), changed=changed, success=success, blocked=blocked)
        return True

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('local input runtime is closed')
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            if self.unread is not None:
                self.unread.start()
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
                if self.read_events is not None:
                    self.read_events.tick()
                try:
                    self.discover()
                except Exception:
                    pass  # index preparation may still be pending
                prepared = self._prepare_burst()
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
            if self.read_events is not None:
                self.read_events.close()
            if self._page_preparation is not None:
                self._page_preparation.close()
            if self.presence is not None:
                self.presence.stop()
            if self.auxiliary is not None:
                self.auxiliary.stop()
            thread = self._thread
            unread = self.unread
        # No GUI or runtime scheduling lock is held while joining count work.
        # Its canonical finalizer may need the source owner during shutdown.
        if unread is not None:
            unread.stop()
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError('local input ingestion has not stopped; Store must remain open')
        # Direct off-path callers also own work through this lock. Never close
        # SQLite while either the background worker or a manual ingest uses it.
        if not self._worker_lock.acquire(timeout=5):
            raise RuntimeError('local input ingestion has not stopped; Store must remain open')
        self._worker_lock.release()
