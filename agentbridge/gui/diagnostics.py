"""Opt-in, bounded GUI diagnostics with a fixed privacy-preserving schema."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import threading
import queue
import weakref
from datetime import datetime, timezone
from pathlib import Path

from ..core import input_phase_timings, delivery_trace
from collections import deque

MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 3
_REQUEST_LOCK = threading.Lock()
_request_sequence = 0
MAX_LINE_BYTES = 2048
MAX_CLIENT_EVENTS = 50
MAX_RATE_ROWS = 64
CRITICAL_RESERVE = 16
# Four internal admission booleans, including their fixed JSON field names.
CONTEXT_BOOKKEEPING_BYTES = 96
_TAB = re.compile(r'[0-9a-f]{16}\Z')

EVENTS = frozenset({
    'client_request', 'page_read', 'page_paint', 'page_reconcile', 'transcript_state',
    'realtime', 'client_error', 'route', 'server_request', 'page_stage', 'delivery',
})
ROUTES = frozenset({
    '/api/state', '/api/mesh/state', '/api/mesh/sidebar_refresh', '/api/mesh/chat',
    '/api/mesh/chat_page', '/api/mesh/chat_aux',
    '/api/mesh/chat_summary', '/api/mesh/chat_collection',
    '/api/mesh/read', '/api/mesh/chat_page_read', '/api/mesh/mark_unread', '/api/mesh/asks',
    '/api/mesh/file', '/api/mesh/open_file', '/api/mesh/save',
    '/api/mesh/post', '/api/mesh/sidebar', '/api/mesh/events',
    '/api/mesh/ask', '/api/mesh/runtime', '/api/mesh/updates',
    'chats', 'chat', 'sidebar', 'details', 'media', 'search', 'settings',
    'about', 'home', 'other',
})
STATUSES = frozenset({
    'ready', 'page', 'pending', 'reset_required', 'unavailable',
    'forbidden', 'locked', 'not_found', 'error', 'ok', 'acknowledged', 'bytes',
    'busy', 'stale', 'painted', 'empty', 'loading', 'unsupported',
    'prepared', 'work', 'restart',
    'unknown', 'other',
})
REASONS = frozenset({
    'none', 'unknown', 'other', 'local_inputs_pending', 'local_paging_disabled',
    'empty_raw_transition',
    'overlay_proofs', 'terminal_classification_pending', 'continuation_changed',
    'continuation_expired', 'window_store_changed', 'page_inputs_changed',
    'page_mirror_changed', 'page_progress', 'source_refresh',
    'source_not_ready', 'source_mutation_pending', 'pending_mutation_budget',
    'mutation_scope_quarantined', 'rooms_pending', 'rooms_deferred', 'room_limit',
    'inventory_pending', 'users_pending', 'response_byte_budget',
    'operation_byte_budget', 'operation_step_budget', 'page_fetch_failed',
    'refresh_request_budget', 'page_metadata_invalid', 'session_changed',
    'viewer_not_member', 'app_locked', 'schema_preparation_failed',
    'read_failed', 'network_error', 'timeout', 'abort', 'stale_owner',
    'missing_transcript', 'scroll_restore', 'older_page_pending',
    'page_unavailable', 'page_changed', 'receipt_presence_changed',
    'source_changed', 'collection_superseded', 'comparison_superseded',
    'admission_superseded', 'finalization_superseded',
    'position_changed', 'budget_exhausted',
    'inputs_unavailable', 'key_unavailable',
    'local_inputs_changed', 'source_changed_during_finalization', 'index_pending',
    'mirror_changed', 'mirror_pending', 'unsafe_cached_value',
    'invalid_payload', 'unsupported_transport', 'storage_error',
})
MODES = frozenset({
    'first', 'older', 'refresh', 'aux_controls', 'aux_members',
    'latest', 'none', 'other',
})
OUTCOMES = frozenset({
    'started', 'completed', 'failed', 'aborted', 'retained', 'cleared',
    'rendered', 'skipped', 'retry', 'visible', 'hidden', 'enabled',
    'changed', 'received', 'disconnected', 'unknown', 'other',
})
ERROR_TYPES = frozenset({
    'Error', 'UnhandledRejection', 'AbortError', 'TimeoutError',
    'TypeError', 'ValueError', 'OSError',
    'SourceChanged', 'OverlayIndexUnavailable', 'ValidationError',
    'PermissionDenied', 'TransportError', 'SQLiteError', 'DomainError',
    'OtherError', 'unknown',
})
PHASES = delivery_trace.PHASES | frozenset({'inputs', 'prepare', 'finalize', 'serialize', 'sidebar', 'other'}) | input_phase_timings.PHASES
SQLITE_CATEGORIES = frozenset({'busy', 'locked', 'io', 'cannot_open',
                               'corrupt', 'schema', 'sqlite_other', 'unknown'})
_SERVER_BOUNDED_INTEGERS = {'request_seq': (1, 2**53 - 1),
                            'attempt': (1, 4),
                            'phase_dropped': (0, 1_000_000),
                            'phase_faults': (0, 1_000_000),
                            'sqlite_extended_code': (0, 65535),
                            'sqlite_primary_code': (0, 255),
                            # One attempt can perform an admitted comparison
                            # followed by one complete staged fallback scan.
                            'documents_examined': (0, 4_000_000),
                            'documents_selected': (0, 2_000_000),
                            'document_bytes': (0, 1024 * 1024 * 1024),
                            'document_batches': (0, 2_000_000)}

_ENUMS = {'flow': frozenset({'sent', 'received'}), 'db_kind': frozenset({'root', 'store'}),
          'holder_coverage': frozenset({'acquisition_start_only'}),'sqlite_category': SQLITE_CATEGORIES, 'event': EVENTS, 'route': ROUTES, 'status': STATUSES,
          'reason': REASONS, 'mode': MODES, 'outcome': OUTCOMES,
          'error_type': ERROR_TYPES, 'phase': PHASES}
_NUMBERS = frozenset({'duration_ms', 'monotonic_ms', 'scroll_top',
                      'scroll_height', 'client_height', 'holder_age_ms', 'queue_wait_ms',
                      'retry_ms', 'dom_delay_ms', 'ack_delay_ms', 'capture_claim_ms',
                      'change_check_ms', 'stage_open_ms', 'collect_ms', 'stage_write_ms', 'seal_ms',
                      'compare_ms', 'admit_ms', 'source_finalize_ms', 'cleanup_ms'})
_INTEGERS = frozenset({'rows', 'messages', 'items', 'chats',
                       'loading_count', 'seq', 'raw_examined', 'holder_count', 'context_dropped',
                       'rate_dropped', 'sampled_out', 'client_dropped', 'context_evicted',
                       'rate_rejected', 'queue_overflow', 'admission_dropped',
                       })
_BOOLEANS = frozenset({'has_transcript', 'busy', 'has_more',
                       'chats_complete', 'unread_complete'})
_CLIENT_FIELDS = frozenset({'event', 'route', 'status', 'reason', 'mode',
                            'outcome', 'error_type', 'tab_ref', 'seq',
                            'monotonic_ms', 'duration_ms', 'rows',
                            'scroll_top', 'scroll_height', 'client_height',
                            'has_transcript', 'loading_count', 'busy', 'phase', 'trace_ref', 'request_ref',
                            'client_dropped', 'retry_ms', 'queue_wait_ms', 'chat_ref', 'flow', 'dom_delay_ms', 'ack_delay_ms'})


class Diagnostics:
    def __init__(self, home: Path):
        self.directory = Path(home) / 'gui' / 'diagnostics'
        self.path = self.directory / 'events.jsonl'
        self.settings = self.directory / 'settings.json'
        self._lock = threading.RLock()
        self._writer_lock = threading.RLock()
        self._writes = queue.Queue(maxsize=256)
        self._worker = None
        self._closed = False
        self._secret = secrets.token_bytes(16)
        self.enabled = False
        self.slow_ms = 1000
        self.sample_rate = 0.01
        self.clock_ref = secrets.token_hex(8)
        self.generation = 0
        self._context = deque(maxlen=512)
        self._context_bytes = 0
        self._flight_counts = dict.fromkeys(('context_dropped', 'rate_dropped', 'sampled_out',
            'context_evicted', 'rate_rejected', 'queue_overflow', 'admission_dropped'), 0)
        self._rate_at = 0.0
        self._rate_rows = 0
        self._rate_ordinary = 0
        self._dropped_events = self._write_failures = 0
        try:
            with self.settings.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) <= 4096:
                data = json.loads(raw)
                self.enabled = type(data) is dict and data.get('enabled') is True
                if type(data) is dict:
                    self.slow_ms = self._slow(data.get('slow_ms', 1000))
                    self.sample_rate = self._sample(data.get('sample_rate', 0.01))
        except (OSError, ValueError, TypeError):
            pass

        delivery_trace.install(self)
        if self.enabled:
            try:
                self._start_writer()
            except Exception:
                self._write_failures = min(1_000_000,self._write_failures+1)

    @staticmethod
    def _write_loop(reference, pending):
        while True:
            try:
                entry = pending.get(timeout=0.5)
            except queue.Empty:
                sink = reference()
                if sink is None or sink._closed:
                    return
                del sink
                continue
            sink = reference()
            try:
                if sink is not None and sink.enabled and entry[0] == sink.generation:
                    with sink._writer_lock:
                        if sink.enabled and entry[0] == sink.generation:
                            sink.record(entry[1], origin_client=entry[1].get('origin') == 'browser')
            except Exception:
                if sink is not None:
                    sink._write_failures = min(1_000_000, sink._write_failures+1)
            finally:
                del sink
                pending.task_done()

    def _start_writer(self):
        self._closed = False
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=Diagnostics._write_loop,
            args=(weakref.ref(self), self._writes), name='ab-delivery-diagnostics', daemon=True)
        self._worker.start()

    def _enqueue(self, row):
        try:
            self._writes.put_nowait((self.generation, dict(row)))
            return True
        except queue.Full:
            self._count('queue_overflow')
            return False

    def flush(self, timeout=1.0):
        """Explicit offline/shutdown drain; never called on the delivery path."""
        deadline = time.monotonic()+max(0, min(float(timeout), 2))
        while self._writes.unfinished_tasks and time.monotonic()<deadline:
            time.sleep(0.001)
        return self._writes.unfinished_tasks == 0

    def close(self):
        self.flush()
        with self._lock:
            self._closed = True

    @staticmethod
    def _slow(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 50 <= value <= 60000:
            raise ValueError('slow_ms must be between 50 and 60000')
        return float(value)

    @staticmethod
    def _sample(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('sample_rate must be between 0 and 1')
        return float(value)

    def note_drop(self, category, count=1):
        self._count(category + '_dropped', count)

    def _count(self, key, count=1):
        with self._lock:
            if key in self._flight_counts:
                self._flight_counts[key] = min(1_000_000, self._flight_counts[key] + count)

    def configuration(self):
        with self._lock:
            return {'enabled': self.enabled, 'path': str(self.path),
                    'max_bytes': MAX_BYTES, 'max_files': MAX_FILES,
                    'dropped_events': self._dropped_events,
                    'write_failures': self._write_failures, 'slow_ms': self.slow_ms,
                    'sample_rate': self.sample_rate, 'context_rows': len(self._context),
                    'context_bytes': self._context_bytes, 'writer_queue': self._writes.qsize(), **self._flight_counts}

    def next_request_sequence(self):
        """Process-scoped, never reused; exhaustion omits correlation safely."""
        global _request_sequence
        if not self.enabled:
            return None
        with _REQUEST_LOCK:
            if _request_sequence >= 2**53 - 1:
                return None
            _request_sequence += 1
            return _request_sequence

    def set_enabled(self, value: bool, *, slow_ms=None, sample_rate=None) -> bool:
        if type(value) is not bool:
            return False
        try:
            slow = self._slow(self.slow_ms if slow_ms is None else slow_ms)
            sample = self._sample(self.sample_rate if sample_rate is None else sample_rate)
        except ValueError:
            return False
        with self._lock:
            temporary = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.directory.chmod(0o700)
                temporary = self.directory / f'.settings-{secrets.token_hex(8)}.tmp'
                raw = json.dumps({'enabled': value, 'slow_ms': slow, 'sample_rate': sample}, separators=(',', ':')).encode()
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, 'wb') as output:
                        fd = -1
                        output.write(raw)
                finally:
                    if fd >= 0:
                        os.close(fd)
                os.replace(temporary, self.settings)
            except OSError:
                return False
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            if self.enabled != value:
                self.generation += 1
            self.enabled = value
            self.slow_ms, self.sample_rate = slow, sample
            if not value:
                self._context.clear()
                self._context_bytes = 0
                while True:
                    try:
                        self._writes.get_nowait()
                        self._writes.task_done()
                    except queue.Empty:
                        break
            else:
                try:
                    self._start_writer()
                except Exception:
                    self._write_failures = min(1_000_000,self._write_failures+1)
            return True

    def chat_ref(self, chat: str) -> str | None:
        if type(chat) is not str or not 1 <= len(chat) <= 256:
            return None
        try:
            return hashlib.blake2s(chat.encode(), key=self._secret, digest_size=8).hexdigest()
        except UnicodeError:
            return None

    @staticmethod
    def _sanitize(data, *, client=False):
        if type(data) is not dict or data.get('event') not in EVENTS:
            return None
        allowed = _CLIENT_FIELDS if client else None
        out = {}
        for key, value in data.items():
            if allowed is not None and key not in allowed:
                continue
            if key in _ENUMS:
                if type(value) is str:
                    out[key] = (value if value in _ENUMS[key]
                                else 'OtherError' if key == 'error_type' else 'other')
            elif key in _SERVER_BOUNDED_INTEGERS and not client:
                lo, hi = _SERVER_BOUNDED_INTEGERS[key]
                if type(value) is int and lo <= value <= hi:
                    out[key] = value
            elif key in _NUMBERS:
                if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= (2**53 - 1 if key == 'monotonic_ms' else 1e9):
                    out[key] = round(float(value), 3)
            elif key in _INTEGERS:
                if type(value) is int and 0 <= value <= 2**53 - 1:
                    out[key] = min(value, 1_000_000) if key != 'seq' else value
            elif key in _BOOLEANS and type(value) is bool:
                out[key] = value
            elif key in ('tab_ref', 'request_ref', 'trace_ref', 'chat_ref') and type(value) is str and _TAB.fullmatch(value):
                out[key] = value
            elif key in ('chat_ref', 'clock_ref', 'database_ref', 'holder_ref',
                          'sample_ref') and not client and type(value) is str and _TAB.fullmatch(value):
                out[key] = value
        if 'event' not in out:
            return None
        out['ts'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        out['server_pid'] = os.getpid()
        out['origin'] = 'browser' if client else 'server'
        return out

    def _retention(self, event):
        # Inventory exclusion is an expected membership outcome, not an error.
        # A real exception or slow observation still promotes its context.
        exclusion = (event.get('event') == 'page_stage'
                     and event.get('route') in (
                         '/api/mesh/state', '/api/mesh/sidebar_refresh')
                     and event.get('status') == 'forbidden'
                     and event.get('reason') == 'viewer_not_member')
        error = (event.get('error_type') not in (None, 'unknown', 'OtherError')
                 or event.get('status') in ('error', 'locked', 'unavailable')
                 or event.get('status') == 'forbidden' and not exclusion
                 or event.get('outcome') in ('failed', 'aborted')
                 or event.get('event') == 'client_error')
        slow = (event.get('duration_ms', 0) >= self.slow_ms
                or event.get('queue_wait_ms', 0) >= self.slow_ms)
        breadcrumb = event.get('phase') in (
            'origin_minted', 'local_commit', 'outbox_attempt', 'transport_append',
            'append_ack_observed', 'local_append_completed',
            'local_snapshot_admitted', 'outbox_retry', 'outbox_dead', 'canonical_dom',
            'native_ack', 'send_reconciled', 'abandoned', 'shutdown') or (
            event.get('phase') in ('request_started', 'request_finished',
                                  'browser_request_started', 'browser_response')
            and event.get('route') == '/api/mesh/post') or (
            event.get('event') == 'route' and event.get('outcome') in ('enabled', 'changed'))
        return error or slow, breadcrumb

    def _admit(self, row):
        row['_wanted'] = True
        trigger, breadcrumb = self._retention(row)
        critical = trigger or breadcrumb
        if (self._rate_rows >= MAX_RATE_ROWS or not critical
                and self._rate_ordinary >= MAX_RATE_ROWS - CRITICAL_RESERVE):
            self._count('rate_rejected')
            if not row.get('_rate_denied'):
                row['_rate_denied'] = True
                self.note_drop('rate')
            admitted = False
        else:
            # Failed enqueue attempts consume admission capacity too.
            self._rate_rows += 1
            self._rate_ordinary += int(not critical)
            admitted = self._enqueue({**row, **self._flight_counts})
        row['_retained'] = admitted
        if not admitted and not row.get('_admission_failed'):
            row['_admission_failed'] = True
            self._count('admission_dropped')

    def flight_record(self, data, *, client=False):
        """Bounded metadata context and retention admission; disk I/O is queued."""
        try:
            with self._lock:
                if not self.enabled or self._closed:
                    return False
                event = self._sanitize(data, client=client)
                if event is None:
                    self.note_drop('context')
                    return False
                if not client:
                    event['clock_ref'] = self.clock_ref
                    event.setdefault('monotonic_ms', round(time.perf_counter()*1000, 3))
                now = time.monotonic()
                encoded_bytes = len(json.dumps(event, separators=(',', ':'))) + CONTEXT_BOOKKEEPING_BYTES
                if encoded_bytes > MAX_LINE_BYTES:
                    self.note_drop('context')
                    return False
                while self._context and (len(self._context) >= 512
                        or self._context_bytes + encoded_bytes > 256*1024
                        or now-self._context[0][0] > 30):
                    _, removed, removed_row = self._context.popleft()
                    self._context_bytes -= removed
                    self._count('context_evicted')
                    if removed_row.get('_wanted') and not removed_row.get('_retained'):
                        self.note_drop('context')
                self._context.append((now, encoded_bytes, event))
                self._context_bytes += encoded_bytes
                trigger, breadcrumb = self._retention(event)
                ref = (event.get('sample_ref') or event.get('request_ref')
                       or event.get('trace_ref'))
                if ref is None and event.get('request_seq') is not None:
                    ref = self.chat_ref(str(event['request_seq']))
                sampled = self.sample_rate == 1 or bool(ref and int(ref, 16)/(2**64) < self.sample_rate)
                if not (trigger or breadcrumb or sampled):
                    self._flight_counts['sampled_out'] = min(1_000_000, self._flight_counts['sampled_out']+1)
                    return True
                rows = [event]
                if trigger:
                    keys = ('sample_ref', 'trace_ref', 'request_ref', 'chat_ref',
                            'request_seq')
                    context = [row for _, _, row in self._context if row is not event
                               and any(event.get(k) and event.get(k) == row.get(k) for k in keys)]
                    # Even an uncorrelated error retains a small global pre-event window.
                    if not context:
                        context = [row for _, _, row in list(self._context)[-16:] if row is not event]
                    # The cause gets capacity first; then retain newest context.
                    rows += list(reversed(context[-47:]))
                if now-self._rate_at >= 1:
                    self._rate_at, self._rate_rows, self._rate_ordinary = now, 0, 0
                for row in rows:
                    if row.get('_retained'):
                        continue
                    self._admit(row)
                return True
        except Exception:
            self._write_failures = min(1_000_000, self._write_failures + 1)
            return False

    def record(self, data: dict, *, client=False, origin_client=False) -> bool:
        """Best effort: a logging failure never changes an application reply."""
        with self._writer_lock:
            if not self.enabled:
                return False
            try:
                event = self._sanitize(data, client=client)
                if event is not None:
                    event['origin'] = 'browser' if client or origin_client or data.get('origin') == 'browser' else 'server'
                    event['clock_ref'] = self.clock_ref if event['origin'] == 'server' else event.get('tab_ref', self.clock_ref)
                    event.setdefault('monotonic_ms', round(time.perf_counter()*1000, 3))
                if event is None:
                    self._dropped_events = min(1_000_000, self._dropped_events + 1)
                    return False
                raw = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
                if len(raw) > MAX_LINE_BYTES:
                    self._dropped_events = min(1_000_000, self._dropped_events + 1)
                    return False
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.directory.chmod(0o700)
                size = self.path.stat().st_size if self.path.exists() else 0
                if size + len(raw) > MAX_BYTES:
                    oldest = self.directory / 'events.2.jsonl'
                    oldest.unlink(missing_ok=True)
                    for index in (1, 0):
                        source = self.directory / ('events.jsonl' if index == 0 else 'events.1.jsonl')
                        target = self.directory / f'events.{index + 1}.jsonl'
                        if source.exists():
                            os.replace(source, target)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                             | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    chmod = getattr(os, 'fchmod', None)
                    if chmod is not None:
                        chmod(fd, 0o600)
                    with os.fdopen(fd, 'ab') as output:
                        fd = -1
                        output.write(raw)
                finally:
                    if fd >= 0:
                        os.close(fd)
                return True
            except Exception:  # noqa: BLE001 — diagnostics can never fail an app response
                self._write_failures = min(1_000_000, self._write_failures + 1)
                return False

    def stage(self, route, chat, phase, status, reason='none', **fields):
        if not self.enabled:
            return
        try:
            event = {'event': 'page_stage', 'route': route, 'phase': phase,
                     'status': status, 'reason': reason, **fields}
            ref = self.chat_ref(chat)
            if ref is not None:
                event['chat_ref'] = ref
            self.flight_record(event)
        except Exception:  # noqa: BLE001 — no diagnostic path affects the page
            pass

    def collect(self, events) -> tuple[int, int]:
        if type(events) is not list:
            return 0, 1
        accepted = sum(self.flight_record(event, client=True)
                       for event in events[:MAX_CLIENT_EVENTS] if type(event) is dict)
        return accepted, len(events) - accepted


def assign_request_sequence(diagnostics, req):
    """Telemetry allocation must not affect request handling."""
    try:
        sequence = diagnostics.next_request_sequence()
        req.diagnostic_sequence = sequence
        return sequence
    except Exception:
        return None


def input_failure_fields(exc):
    """Fixed metadata only: never retain arbitrary exception messages."""
    fields = {'error_type': type(exc).__name__, 'reason': 'other'}
    if isinstance(exc, sqlite3.Error):
        fields.update(error_type='SQLiteError', sqlite_category='unknown')
        code = getattr(exc, 'sqlite_errorcode', None)
        if type(code) is int and 0 <= code <= 65535:
            primary = code & 0xff
            fields.update(sqlite_extended_code=code, sqlite_primary_code=primary,
                          sqlite_category={5: 'busy', 6: 'locked', 10: 'io',
                              14: 'cannot_open', 11: 'corrupt', 17: 'schema'}.get(primary, 'sqlite_other'))
    elif isinstance(exc, OSError):
        fields['error_type'] = 'OSError'
    elif type(exc).__name__ in ('SourceChanged', 'OverlayIndexUnavailable'):
        # Preserve only known domain reason tokens, never exception text.
        reason = exc.args[0] if len(exc.args) == 1 else None
        if type(reason) is str and reason in REASONS:
            fields['reason'] = reason
    return fields


def capture_inputs(runtime, chat, app, req, attempt):
    """Time total inputs, including both acquisitions/checks; no root wait claim."""
    try:
        diagnostics = getattr(app, 'diagnostics', None)
        enabled = diagnostics is not None and diagnostics.enabled
    except Exception:
        enabled = False
    if not enabled:
        return runtime.inputs(chat)
    try:
        sequence = getattr(req, 'diagnostic_sequence', None)
        if sequence is None:
            sequence = assign_request_sequence(diagnostics, req)
        started = time.perf_counter()
    except Exception:
        sequence, started = None, None
    try:
        phases, phase_token = input_phase_timings.begin()
    except Exception:
        phases, phase_token = None, None
    failure = None
    try:
        return runtime.inputs(chat)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            input_phase_timings.end(phase_token)
        except Exception:
            if phase_token is not None:
                input_phase_timings.clear()
        try:
            fields = {'request_seq': sequence, 'attempt': attempt}
            if phases is not None:
                fields.update(phase_dropped=phases.dropped, phase_faults=phases.faults)
            if started is not None:
                fields['duration_ms'] = (time.perf_counter() - started) * 1000
            if failure is not None:
                fields.update(input_failure_fields(failure))
            reason = fields.pop('reason', 'none')
            diagnostics.stage('/api/mesh/chat_page', chat, 'inputs',
                              'pending' if failure is not None else 'ready', reason, **fields)
        except Exception:
            pass  # No diagnostic failure may replace inputs' result/exception.
        try:
            phase_rows = tuple(phases.rows) if phases is not None else ()
        except Exception:
            phase_rows = ()
        for phase, duration, failed in phase_rows:
            try:
                diagnostics.stage('/api/mesh/chat_page', chat, phase,
                    'pending' if failed else 'ready',
                    request_seq=sequence, attempt=attempt, duration_ms=duration)
            except Exception:
                pass


def record_page_stage(app, req, chat, phase, status, reason='none', *, attempt, **fields):
    """Correlate page stages without allowing a recorder fault into the route."""
    try:
        diagnostics = getattr(app, 'diagnostics', None)
        if diagnostics is not None and diagnostics.enabled:
            diagnostics.stage('/api/mesh/chat_page', chat, phase, status, reason,
                              request_seq=req.diagnostic_sequence, attempt=attempt, **fields)
    except Exception:
        pass
