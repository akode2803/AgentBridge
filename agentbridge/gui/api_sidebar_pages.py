"""Opt-in bounded sidebar rows from fresh finalized canonical page requests.

Room IDs are positioning hints only. Every emitted row has an independent
membership/source/session final cut; unresolved rooms make the inventory
explicitly incomplete rather than presenting omission as removal.
"""
from __future__ import annotations

import json
import sqlite3

from ..core.models import ChatSnapshot, MsgKind
from ..mesh.page_operation import PageOperation
from ..mesh.readmodel import unread_info
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part
from .serialize import chat_json, snippet_json, user_json


MAX_ROOMS = 128
MAX_USERS = 2048
MAX_USERS_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PAGE_LIMIT = 50
SCAN_BUDGET = 1000


def _users(mesh):
    """Account identity fallback; profile and presence gates are deferred."""
    users = {}
    names = mesh.directory.names()
    if len(names) > MAX_USERS:
        raise OverflowError('user_limit')
    used = 0
    for name in names:
        account = mesh.directory.get(name)
        if account is None:
            continue
        entry = user_json(account, {
            'display': account.display or name, 'kind': account.kind.value,
            'photo_visible': False,
        }, me=None)
        used += len(json.dumps({name: entry}, ensure_ascii=False).encode())
        if used > MAX_USERS_BYTES:
            raise OverflowError('user_byte_budget')
        users[name] = entry
    return users


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


def _room(app, mesh, token, chat):
    runtime = mesh.local_inputs
    try:
        chat = _part(chat)
        runtime.request(chat)
        runtime.request_page(chat)
        reader, receipt, index = runtime.inputs(chat)
        operation = PageOperation(mesh, chat, source_reader=reader,
                                  limit=PAGE_LIMIT, scan_budget=SCAN_BUDGET,
                                  summary_only=True)
        for _ in range(4):
            value = operation.prepare(receipt, receipt, index)
            if value.status == 'restart':
                reader, receipt, index = runtime.inputs(chat)
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
            final = app.finalize_page_read(token, value.prepared)
            if final.status == 'restart':
                reader, receipt, index = runtime.inputs(chat)
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
            return _summary(snapshot, selected, mesh.user, state)
    except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
            OSError, sqlite3.Error, ValueError, TypeError):
        return None, False
    return None, False


def capture_sidebar(app, mesh, token):
    """Independent complete/incomplete room inventory with a hard row budget."""
    try:
        users = _users(mesh)
    except OverflowError as exc:
        return {'users': {}, 'chats': [], 'chats_complete': False,
                'sidebar_status': str(exc),
                'metadata_status': {'profiles': 'pending', 'presence': 'pending',
                                    'live': 'deferred'}}
    out = {'users': users, 'chats': [], 'chats_complete': False,
           'metadata_status': {'profiles': 'pending', 'presence': 'pending',
                               'live': 'deferred'}}
    try:
        ids = mesh.tx.list_chat_ids()
    except (OSError, ValueError, TypeError):
        out['sidebar_status'] = 'inventory_pending'
        return out
    if (type(ids) is not list or len(ids) > MAX_ROOMS
            or any(type(chat) is not str for chat in ids)
            or len(set(ids)) != len(ids)):
        out['sidebar_status'] = 'room_limit' if type(ids) is list and len(ids) > MAX_ROOMS else 'inventory_pending'
        return out
    complete = True
    for chat in ids:
        if not app.validate_session_read(token):
            out['sidebar_status'] = 'session_changed'
            return out
        row, resolved = _room(app, mesh, token, chat)
        if row is not None:
            out['chats'].append(row)
        if not resolved:
            complete = False
    out['chats_complete'] = complete
    out['sidebar_status'] = 'ready' if complete else 'rooms_pending'
    if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
        out.update(users={}, chats=[], chats_complete=False,
                   sidebar_status='response_byte_budget')
    return out
