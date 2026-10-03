"""Opt-in, bounded GUI diagnostics with a fixed privacy-preserving schema."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 3
MAX_LINE_BYTES = 2048
MAX_CLIENT_EVENTS = 50
_TAB = re.compile(r'[0-9a-f]{16}\Z')

EVENTS = frozenset({
    'client_request', 'page_read', 'page_paint', 'transcript_state',
    'realtime', 'client_error', 'route', 'server_request', 'page_stage',
})
ROUTES = frozenset({
    '/api/state', '/api/mesh/state', '/api/mesh/chat',
    '/api/mesh/chat_page', '/api/mesh/chat_aux',
    '/api/mesh/chat_summary', '/api/mesh/chat_collection',
    '/api/mesh/read', '/api/mesh/mark_unread', '/api/mesh/asks',
    '/api/mesh/file', '/api/mesh/open_file', '/api/mesh/save',
    '/api/mesh/post', '/api/mesh/sidebar', '/api/mesh/events',
    '/api/mesh/ask', '/api/mesh/runtime', '/api/mesh/updates',
    'chats', 'chat', 'sidebar', 'details', 'media', 'search', 'settings',
    'about', 'home', 'other',
})
STATUSES = frozenset({
    'ready', 'page', 'pending', 'reset_required', 'unavailable',
    'forbidden', 'locked', 'not_found', 'error', 'ok', 'bytes',
    'busy', 'stale', 'painted', 'empty', 'loading', 'unsupported',
    'prepared', 'work', 'restart',
    'unknown', 'other',
})
REASONS = frozenset({
    'none', 'unknown', 'other', 'local_inputs_pending', 'local_paging_disabled',
    'overlay_proofs', 'terminal_classification_pending', 'continuation_changed',
    'continuation_expired', 'window_store_changed', 'page_inputs_changed',
    'page_mirror_changed', 'page_progress', 'source_refresh',
    'source_not_ready', 'source_mutation_pending', 'pending_mutation_budget',
    'mutation_scope_quarantined', 'rooms_pending', 'room_limit',
    'inventory_pending', 'users_pending', 'response_byte_budget',
    'operation_byte_budget', 'operation_step_budget', 'page_fetch_failed',
    'refresh_request_budget', 'page_metadata_invalid', 'session_changed',
    'viewer_not_member', 'app_locked', 'schema_preparation_failed',
    'read_failed', 'network_error', 'timeout', 'abort', 'stale_owner',
    'missing_transcript', 'scroll_restore', 'older_page_pending',
    'page_unavailable', 'page_changed', 'receipt_presence_changed',
    'source_changed', 'position_changed', 'budget_exhausted',
    'inputs_unavailable', 'key_unavailable',
    'local_inputs_changed', 'source_changed_during_finalization', 'index_pending',
})
MODES = frozenset({'first', 'older', 'refresh', 'latest', 'none', 'other'})
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
PHASES = frozenset({'inputs', 'prepare', 'finalize', 'serialize', 'sidebar', 'other'})
_ENUMS = {'event': EVENTS, 'route': ROUTES, 'status': STATUSES,
          'reason': REASONS, 'mode': MODES, 'outcome': OUTCOMES,
          'error_type': ERROR_TYPES, 'phase': PHASES}
_NUMBERS = frozenset({'duration_ms', 'monotonic_ms', 'scroll_top',
                      'scroll_height', 'client_height'})
_INTEGERS = frozenset({'rows', 'messages', 'items', 'chats',
                       'loading_count', 'seq', 'raw_examined'})
_BOOLEANS = frozenset({'has_transcript', 'busy', 'has_more',
                       'chats_complete', 'unread_complete'})
_CLIENT_FIELDS = frozenset({'event', 'route', 'status', 'reason', 'mode',
                            'outcome', 'error_type', 'tab_ref', 'seq',
                            'monotonic_ms', 'duration_ms', 'rows',
                            'scroll_top', 'scroll_height', 'client_height',
                            'has_transcript', 'loading_count', 'busy'})


class Diagnostics:
    def __init__(self, home: Path):
        self.directory = Path(home) / 'gui' / 'diagnostics'
        self.path = self.directory / 'events.jsonl'
        self.settings = self.directory / 'settings.json'
        self._lock = threading.RLock()
        self._secret = secrets.token_bytes(16)
        self.enabled = False
        try:
            with self.settings.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) <= 4096:
                data = json.loads(raw)
                self.enabled = type(data) is dict and data.get('enabled') is True
        except (OSError, ValueError, TypeError):
            pass

    def configuration(self):
        with self._lock:
            return {'enabled': self.enabled, 'path': str(self.path),
                    'max_bytes': MAX_BYTES, 'max_files': MAX_FILES}

    def set_enabled(self, value: bool) -> bool:
        if type(value) is not bool:
            return False
        with self._lock:
            temporary = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.directory.chmod(0o700)
                temporary = self.directory / f'.settings-{secrets.token_hex(8)}.tmp'
                raw = json.dumps({'enabled': value}, separators=(',', ':')).encode()
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
            self.enabled = value
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
            elif key in _NUMBERS:
                if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e9:
                    out[key] = round(float(value), 3)
            elif key in _INTEGERS:
                if type(value) is int and 0 <= value <= 2**53 - 1:
                    out[key] = min(value, 1_000_000) if key != 'seq' else value
            elif key in _BOOLEANS and type(value) is bool:
                out[key] = value
            elif key == 'tab_ref' and type(value) is str and _TAB.fullmatch(value):
                out[key] = value
            elif key == 'chat_ref' and not client and type(value) is str and _TAB.fullmatch(value):
                out[key] = value
        if 'event' not in out:
            return None
        out['ts'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        out['server_pid'] = os.getpid()
        out['origin'] = 'browser' if client else 'server'
        return out

    def record(self, data: dict, *, client=False) -> bool:
        """Best effort: a logging failure never changes an application reply."""
        with self._lock:
            if not self.enabled:
                return False
            try:
                event = self._sanitize(data, client=client)
                if event is None:
                    return False
                raw = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
                if len(raw) > MAX_LINE_BYTES:
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
            self.record(event)
        except Exception:  # noqa: BLE001 — no diagnostic path affects the page
            pass

    def collect(self, events) -> tuple[int, int]:
        if type(events) is not list:
            return 0, 1
        accepted = sum(self.record(event, client=True) for event in events[:MAX_CLIENT_EVENTS])
        return accepted, len(events) - accepted
