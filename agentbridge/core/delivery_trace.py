"""Best-effort local delivery hooks; no data/SQL or authority decisions."""
from __future__ import annotations

import functools
import secrets
import threading
import time
import weakref
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

_recorder = None
_context = ContextVar('delivery_context', default=None)
_ownership = ContextVar('delivery_ownership', default=None)
_buffer = ContextVar('delivery_db_buffer', default=None)
_holders = {}
_lock = threading.Lock()
_recorder_lock = threading.RLock()
MAX_HOLDERS = 128
MAX_DEFERRED = 128


class _Deferred(list):
    dropped = 0
PHASES = frozenset({
    'request_started', 'request_finished', 'origin_minted', 'local_commit',
    'outbox_attempt', 'append_ack_observed', 'local_append_completed',
    'local_snapshot_admitted',
    'outbox_retry', 'outbox_dead',
    'sync_observed', 'sse_frame', 'transport_read', 'transport_append', 'ingestion',
    'outbox_batch', 'store_send_commit', 'page_prepare', 'page_finalize',
    'source_ingestion', 'source_reconciliation', 'ingestion_queued',
    'ingestion_claimed',
    'preparation', 'preparation_queued', 'preparation_claimed',
    'db_acquire', 'db_body', 'db_commit', 'db_rollback', 'db_close',
    'browser_request_started', 'browser_response', 'browser_request_failed',
    'canonical_dom', 'native_ack', 'retry_scheduled', 'refresh_queued',
    'refresh_started', 'refresh_finished', 'send_reconciled', 'abandoned', 'shutdown',
})


def install(recorder):
    global _recorder
    with _recorder_lock:
        _recorder = weakref.ref(recorder)


@contextmanager
def recorder_fence(sink, generation):
    """Serialize completion admission with owner replacement and opt-out.

    Callers may enqueue bounded sanitized metadata here, never do disk/provider
    work. Install takes only the owner lock; setting changes take the sink lock.
    """
    with _recorder_lock:
        with sink._lock:
            yield recorder() is sink and sink.generation == generation


def recorder():
    try:
        value = _recorder() if _recorder is not None else None
        return value if value is not None and value.enabled and not value._closed else None
    except Exception:
        return None


def _owned(sink):
    ownership = _ownership.get()
    return (ownership is None or
            (ownership[0]() is sink and ownership[1] == sink.generation))


def _reset_ownership(token):
    if token is not None:
        try:
            _ownership.reset(token)
        except Exception:
            try:
                _ownership.set(None)
            except Exception:
                pass


@contextmanager
def _owned_scope(sink, generation):
    # Keep identity outside event metadata, and retain any enclosing ownership.
    token = None
    try:
        if _ownership.get() is None:
            token = _ownership.set((weakref.ref(sink), generation))
        yield
    finally:
        _reset_ownership(token)


def elapsed_ms(started):
    try:
        return (time.perf_counter()-started)*1000 if started is not None else None
    except Exception:
        return None


def queue_clock():
    try:
        return time.perf_counter() if recorder() is not None else None
    except Exception:
        return None


def reference(value):
    try:
        sink = recorder()
        return sink.chat_ref(value) if sink is not None else None
    except Exception:
        return None


def sampling_reference():
    """Return an opaque per-attempt sampling key when diagnostics are active."""
    try:
        return secrets.token_hex(8) if recorder() is not None else None
    except Exception:
        return None


def emit(phase, *, message='', chat='', status='ok', duration_ms=None, **fields):
    """No exceptions or disk I/O while inside an observed transaction scope."""
    try:
        sink = recorder()
        if sink is None or phase not in PHASES or not _owned(sink):
            return
        context = _context.get() or {}
        if context.get('_generation', sink.generation) != sink.generation:
            return
        row = {'event': 'delivery', 'phase': phase, 'status': status,
               'monotonic_ms': time.perf_counter() * 1000, **context, **fields}
        if message:
            row['trace_ref'] = sink.chat_ref(message)
        if chat:
            row['chat_ref'] = sink.chat_ref(chat)
        if duration_ms is not None:
            row['duration_ms'] = duration_ms
        # Fixed schema is applied before any buffering; no raw arguments retained.
        row = sink._sanitize(row)
        if row is None:
            return
        buffer = _buffer.get()
        if buffer is not None:
            if len(buffer) < MAX_DEFERRED:
                buffer.append(row)
            else:
                buffer.dropped = min(1_000_000,buffer.dropped+1)
        else:
            sink.flight_record(row)
    except Exception:
        pass


@contextmanager
def request_context(request_ref=None, request_seq=None):
    token = None
    sink = None
    generation = None
    try:
        sink = recorder()
        if sink is not None:
            generation = sink.generation
            token = _context.set({'request_ref': request_ref, 'request_seq': request_seq,
                                  '_generation': generation})
    except Exception:
        pass
    try:
        if sink is not None:
            with _owned_scope(sink, generation):
                yield
        else:
            yield
    finally:
        if token is not None:
            try:
                _context.reset(token)
            except Exception:
                _context.set(None)


def observed(phase, *, chat_arg=None):
    """Decorate existing work without changing its return/exception contract."""
    def wrap(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            sink = recorder()
            if sink is None or not _owned(sink):
                return function(*args, **kwargs)
            generation = sink.generation
            started = queue_clock()
            chat = args[chat_arg] if chat_arg is not None and len(args) > chat_arg else ''
            failed = False
            error_type = None
            with _owned_scope(sink, generation):
                try:
                    return function(*args, **kwargs)
                except BaseException as exc:
                    error_type = type(exc).__name__
                    failed = True
                    raise
                finally:
                    if recorder() is sink and sink.generation == generation:
                        emit(phase, chat=chat, status='error' if failed else 'ok', error_type=error_type,
                             duration_ms=elapsed_ms(started))
        return call
    return wrap


class _Acquire:
    def __init__(self, transaction):
        self.tx = transaction
        self.started = None
        self.owner = None

    def __enter__(self):
        try:
            if not self.tx.active():
                return
            self.started = time.perf_counter()
            with _lock:
                owners = [v for k, v in _holders.items() if k[0] == self.tx.database_ref]
                self.owner = owners[0] if owners else None
        except Exception:
            pass

    def __exit__(self, kind, value, tb):
        try:
            if not self.tx.active():
                return False
            if self.started is not None:
                fields = {'database_ref': self.tx.database_ref, 'db_kind': self.tx.kind,
                          'holder_count': 0, 'holder_coverage': 'acquisition_start_only'}
                if self.owner is not None:
                    fields.update(holder_ref=self.owner[0],
                                  holder_age_ms=max(0, (self.started-self.owner[1])*1000),
                                  holder_count=1)
                emit('db_acquire', status='error' if kind else 'ok',
                     duration_ms=(time.perf_counter()-self.started)*1000, **fields)
            if kind is None:
                self.tx.acquired = time.perf_counter()
                with _lock:
                    if len(_holders) < MAX_HOLDERS:
                        _holders[(self.tx.database_ref, self.tx.owner_ref)] = (self.tx.owner_ref, self.tx.acquired)
                    else:
                        buffer = _buffer.get()
                        if buffer is not None:
                            buffer.dropped = min(1_000_000,buffer.dropped+1)
        except Exception:
            pass
        return False


class _Finish:
    def __init__(self, tx, phase):
        self.tx, self.phase = tx, phase
        self.started = None

    def __enter__(self):
        try:
            if not self.tx.active():
                return
            self.started = time.perf_counter()
            if self.tx.acquired is not None:
                emit('db_body', database_ref=self.tx.database_ref, db_kind=self.tx.kind,
                     holder_ref=self.tx.owner_ref,
                     duration_ms=(self.started-self.tx.acquired)*1000)
        except Exception:
            pass

    def __exit__(self, kind, value, tb):
        try:
            if not self.tx.active():
                return False
            emit(self.phase, database_ref=self.tx.database_ref, db_kind=self.tx.kind,
                 holder_ref=self.tx.owner_ref, status='error' if kind else 'ok',
                 duration_ms=(time.perf_counter()-self.started)*1000 if self.started else None)
            if kind is None:
                self.tx.release()
        except Exception:
            pass
        return False


class Transaction:
    def __init__(self, kind, path):
        self.kind = kind
        self.database_ref = reference(str(path))
        self.owner_ref = secrets.token_hex(8)
        self.acquired = None
        self.token = None
        self.owner_token = None
        self.rows = None
        sink = recorder()
        self.sink_ref = weakref.ref(sink) if sink is not None else lambda: None
        self.generation = sink.generation if sink is not None else None

    def active(self):
        sink = recorder()
        return (sink is not None and sink is self.sink_ref()
                and sink.generation == self.generation and _owned(sink))

    def __enter__(self):
        try:
            if _ownership.get() is None:
                self.owner_token = _ownership.set((self.sink_ref, self.generation))
            if _buffer.get() is None:
                self.rows = _Deferred()
                self.token = _buffer.set(self.rows)
        except Exception:
            pass
        return self

    def acquiring(self):
        return _Acquire(self)

    def finishing(self, phase):
        return _Finish(self, phase)

    def release(self):
        try:
            with _lock:
                _holders.pop((self.database_ref, self.owner_ref), None)
            self.acquired = None
        except Exception:
            pass

    def __exit__(self, kind, value, tb):
        self.release()
        try:
            if self.token is not None:
                _buffer.reset(self.token)
                sink = recorder()
                if self.active():
                    if self.rows.dropped:
                        sink.note_drop("context", self.rows.dropped)
                    for row in self.rows:
                        sink.flight_record(row)
        except Exception:
            pass
        finally:
            _reset_ownership(self.owner_token)
        return False


class _DisabledTransaction:
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def acquiring(self):
        return nullcontext()
    def finishing(self, _):
        return nullcontext()

_disabled = _DisabledTransaction()


def transaction(kind, path):
    try:
        sink = recorder()
        return Transaction(kind, path) if sink is not None and _owned(sink) else _disabled
    except Exception:
        return _disabled
