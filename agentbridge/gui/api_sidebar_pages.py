"""Bounded sidebar rows from fresh finalized canonical page requests.

Room IDs are positioning hints only. Every emitted row has an independent
membership/source/session final cut; unresolved rooms make the inventory
explicitly incomplete rather than presenting omission as removal.
"""
from __future__ import annotations

import json
import sqlite3
import time

from ..core.models import ChatSnapshot, MsgKind
from ..mesh.page_operation import PageOperation
from ..mesh.readmodel import unread_info
from ..mesh.unread_counts import UnreadSession
from ..store import aux_inputs, local_source, overlay_index, sidebar_cache
from ..transport.authority_observation import _part
from .serialize import chat_json, snippet_json
from . import sidebar_users
from .context import session_read_binding
from .routing import authed_read_token


MAX_ROOMS = 128
MAX_USERS = 2048
MAX_USERS_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PAGE_LIMIT = 50
SCAN_BUDGET = 1000


def _users(mesh):
    """Public account-name fallback from complete admitted raw input only."""
    auxiliary = mesh.local_inputs.auxiliary
    auxiliary.request('users')
    try:
        reader, receipt, inputs = auxiliary.inputs(
            'users', max_documents=MAX_USERS, max_bytes=MAX_USERS_BYTES)
        users = sidebar_users.public_users(inputs)
        with reader.finalization(receipt):
            pass  # exact source position; no payload parse under root/Store.
        return users, True, 'ready'
    except aux_inputs.AuxInputsUnavailable as exc:
        if exc.args == ('aux_document_budget',):
            return {}, False, 'user_limit'
        if exc.args == ('aux_byte_budget',):
            return {}, False, 'user_byte_budget'
        return {}, False, 'users_pending'
    except (sidebar_users.UsersUnavailable, local_source.SourceChanged,
            OSError, sqlite3.Error, OverflowError, ValueError):
        return {}, False, 'users_pending'


def _summary(snapshot, selection, viewer, state):
    page = selection.messages
    exhausted = selection.history_exhausted
    last_real = next((m for m in reversed(page) if m.kind is MsgKind.MESSAGE), None)
    # A post-delete real message resurrects the row. Without one, older
    # unexamined history cannot establish whether the deletion still hides it.
    if state['deleted'] and last_real is None:
        return None, exhausted
    overview = unread_info(list(page), viewer, state)
    observed = overview['unread']
    entry = chat_json(snapshot)
    entry.update(
        last=snippet_json(page[-1]) if page else None,
        preview_pending=not page and not exhausted,
        unread=observed if exhausted or observed else None,
        unread_lower_bound=observed,
        unread_complete=exhausted,
        first_unread_ns=overview['first_unread_ns'] if exhausted else None,
        mention=overview['mention'] if exhausted or overview['mention'] else None,
        forced_unread=bool(state['forced_unread']),
        archived=bool(state['archived']), pinned=bool(state['pinned']),
        mute=state['mute'], hidden=False,
    )
    return entry, True


def _stage(app, chat, phase, status, reason='none', **fields):
    """Diagnostic failures must never affect canonical sidebar outcomes."""
    try:
        if (diagnostics := getattr(app, 'diagnostics', None)) is not None:
            diagnostics.stage('/api/mesh/sidebar_refresh', chat, phase, status, reason, **fields)
    except Exception:  # noqa: BLE001 — telemetry is best effort
        pass


def _timed_stage(app, chat, phase, call, *args):
    started = time.perf_counter()
    try:
        value = call(*args)
    except Exception as exc:
        _stage(app, chat, phase, 'error', 'other',
               duration_ms=(time.perf_counter() - started) * 1000,
               error_type=type(exc).__name__)
        raise
    _stage(app, chat, phase, 'ready' if phase == 'inputs' else value.status,
           'none' if phase == 'inputs' else (value.reason or 'none'),
           duration_ms=(time.perf_counter() - started) * 1000)
    return value


def _room(app, mesh, token, chat, *, activity=False):
    runtime = mesh.local_inputs
    try:
        chat = _part(chat)
        # A scoped sidebar refresh names the row that changed. Treat it as a
        # scheduling hint for that source so quiet-background backoff does not
        # become visible message latency. The hint carries no payload or
        # authority; ordinary complete collection and canonical checks still
        # decide what may be published.
        runtime.request(chat, activity=activity)
        runtime.request_page(chat)
        session = UnreadSession(token.app_identity, token.generation, mesh.user)
        unread = runtime.unread
        if unread is not None:
            unread.request(chat, session)
        candidate = None if unread is None else unread.candidate(chat, session)
        reader, receipt, index = _timed_stage(app, chat, 'inputs', runtime.inputs, chat)
        operation = PageOperation(mesh, chat, source_reader=reader,
                                  limit=PAGE_LIMIT, scan_budget=SCAN_BUDGET,
                                  summary_only=True, unread_candidate=candidate)
        for _ in range(4):
            value = _timed_stage(app, chat, 'prepare', operation.prepare, receipt, receipt, index)
            if value.status not in ('prepared', 'restart') and not (
                    value.status == 'forbidden' and value.reason == 'viewer_not_member'):
                _stage(app, chat, 'sidebar', value.status, value.reason or 'none')
            if candidate is not None and value.status not in ('prepared', 'forbidden'):
                # A count is only comparison evidence. Its stale or over-budget
                # closure must not suppress a fresh ordinary sidebar summary.
                unread.invalidate(chat, session)
                candidate = operation.unread_candidate = None
                reader, receipt, index = _timed_stage(app, chat, 'inputs', runtime.inputs, chat)
                continue
            if value.status == 'restart':
                reader, receipt, index = _timed_stage(app, chat, 'inputs', runtime.inputs, chat)
                continue
            if value.status == 'work':
                if value.reason == 'overlay_proofs':
                    runtime.request_page(chat, index=index, proofs=value.work)
                else:
                    runtime.request(chat, activity=True)
                return None, False
            if value.status == 'forbidden':
                return None, True
            if value.status != 'prepared':
                return None, False
            final = _timed_stage(app, chat, 'finalize', app.finalize_page_read, token, value.prepared)
            if final.status not in ('page', 'restart') and not (
                    final.status == 'forbidden' and final.reason == 'viewer_not_member'):
                _stage(app, chat, 'sidebar', final.status, final.reason or 'none')
            if candidate is not None and final.status not in ('page', 'locked'):
                unread.invalidate(chat, session)
                candidate = operation.unread_candidate = None
                reader, receipt, index = _timed_stage(app, chat, 'inputs', runtime.inputs, chat)
                continue
            if final.status == 'restart':
                reader, receipt, index = _timed_stage(app, chat, 'inputs', runtime.inputs, chat)
                continue
            if final.status != 'page' or final.result is None:
                return None, False
            selected, presentation = final.result.page, final.result.presentation
            if selected is None or presentation is None:
                return None, False
            snapshot = ChatSnapshot.from_dict(json.loads(presentation.snapshot_json))
            if snapshot.deleted:
                return None, True
            state = json.loads(presentation.viewer_state_json)
            row, resolved = _summary(snapshot, selected, mesh.user, state)
            if row is not None and final.result.unread is not None:
                row.update(final.result.unread)
            return row, resolved
    except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
            OSError, sqlite3.Error, ValueError, TypeError):
        return None, False
    return None, False


def capture_sidebar(app, mesh, token):
    """Return admitted cached rows and queue bounded canonical reconciliation."""
    users, users_complete, user_status = _users(mesh)
    out = {'users': users, 'chats': [], 'chats_complete': False,
           'users_complete': users_complete, 'user_status': user_status,
           'metadata_status': {'profiles': 'pending', 'presence': 'pending',
                               'live': 'deferred'}}
    try:
        ids = mesh.tx.list_chat_ids()
    except (OSError, ValueError, TypeError):
        try:
            out['chats'] = mesh.store.cached_sidebar(mesh.user)
        except (sidebar_cache.SidebarCacheUnavailable, OSError, sqlite3.Error,
                OverflowError, ValueError):
            pass
        out['sidebar_status'] = 'inventory_pending'
        return out
    if (type(ids) is not list or len(ids) > MAX_ROOMS
            or any(type(chat) is not str for chat in ids)
            or len(set(ids)) != len(ids)):
        out['sidebar_status'] = 'room_limit' if type(ids) is list and len(ids) > MAX_ROOMS else 'inventory_pending'
        return out
    allowed = frozenset(ids)
    cache_ready = True
    try:
        mesh.store.prune_sidebar(mesh.user, allowed)
        out['chats'] = mesh.store.cached_sidebar(mesh.user, allowed_ids=allowed)
    except (sidebar_cache.SidebarCacheUnavailable, OSError, sqlite3.Error,
            OverflowError, ValueError):
        cache_ready = False
        out['chats'] = []
    app.sidebar_refresh.request_inventory(token, ids)
    if not cache_ready:
        app.sidebar_refresh.request_all(token)
    progress = app.sidebar_refresh.status(token)
    out['chats_complete'] = progress['complete'] and cache_ready
    out['sidebar_pending'] = progress['pending'] + progress['running']
    out['chats_removed'] = list(progress['removed'])
    out['sidebar_status'] = ('cache_pending' if not cache_ready else
                             'ready' if progress['complete'] else 'rooms_pending')
    if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        out.update(users={}, chats=[], chats_complete=False, users_complete=False,
                   chats_removed=[], user_status='response_byte_budget',
                   sidebar_status='response_byte_budget')
    return out


@authed_read_token
def refresh_sidebar(app, req, mesh, token):
    """Resolve at most one room and publish only its finalized presentation."""
    if req.data.get('refresh_all') is True:
        app.sidebar_refresh.request_all(token)
    preferred = req.data.get('chat_id') or ''
    if type(preferred) is not str:
        preferred = ''
    if preferred:
        try:
            preferred = _part(preferred)
            if not app.sidebar_refresh.request_chat(token, preferred):
                preferred = ''
        except ValueError:
            preferred = ''
    chat = app.sidebar_refresh.claim(token, preferred=preferred)
    if chat is None:
        progress = app.sidebar_refresh.status(token)
        return {'ok': True, 'status': 'ready' if progress['complete'] else 'busy',
                'changed': False, 'has_more': not progress['complete'],
                'chats_complete': progress['complete'],
                'retry_after_ms': 100,
                'session_binding': session_read_binding(token)}
    started = time.perf_counter()
    row, resolved, changed, published = None, False, False, False
    try:
        row, resolved = _room(app, mesh, token, chat,
                              activity=bool(preferred and chat == preferred))
        if resolved and app.validate_session_read(token):
            changed = mesh.store.publish_sidebar(
                mesh.user, chat, row, updated_ns=time.time_ns())
            changed = (app.sidebar_refresh.record_presentation(
                token, chat, visible=row is not None) or changed)
            published = True
    finally:
        # A canonical result is not complete until its display row (including
        # an intentional removal) is durable.  Publication failures must leave
        # the claim pending for a later bounded retry.
        app.sidebar_refresh.finish(token, chat, resolved=published)
    _stage(app, chat, 'sidebar', 'ready' if resolved else 'pending',
           duration_ms=(time.perf_counter() - started) * 1000,
           rows=int(row is not None))
    progress = app.sidebar_refresh.status(token)
    return {'ok': True, 'status': 'ready' if resolved else 'pending',
            'changed': changed, 'has_more': not progress['complete'],
            'chats_complete': progress['complete'],
            'retry_after_ms': 350 if not resolved else 0,
            'session_binding': session_read_binding(token)}
