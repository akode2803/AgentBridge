"""Local-only bounded room owner-ask inventory; no full membership folds."""
from __future__ import annotations

import json
import sqlite3

from ..mesh.page_owner_asks import OwnerAskPageOperation
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part


MAX_ROOMS = 128
MAX_ASKS = 512
MAX_BYTES = 1024 * 1024


def _room(app, mesh, token, chat):
    runtime = mesh.local_inputs
    try:
        chat = _part(chat)
        runtime.request(chat, selected=False)
        runtime.request_page(chat)
        reader, receipt, index = runtime.inputs(chat)
        operation = OwnerAskPageOperation(mesh, chat, source_reader=reader, app=app)
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
                return [], False, False
            if value.status == 'forbidden':
                return [], True, True
            if value.status != 'prepared':
                return [], False, False
            final = app.finalize_page_read(token, value.prepared)
            if final.status == 'restart':
                reader, receipt, index = runtime.inputs(chat)
                continue
            if final.status != 'page' or final.result is None:
                return [], False, False
            snapshot = json.loads(final.result.presentation.snapshot_json)
            if snapshot.get('deleted'):
                return [], True, True
            decoration = final.result.presentation.decoration_json
            result = json.loads(decoration) if decoration is not None else None
            if (type(result) is not dict or type(result.get('asks')) is not list
                    or type(result.get('asks_complete')) is not bool):
                return [], False, False
            return result['asks'], result['asks_complete'], False
    except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
            OSError, sqlite3.Error, ValueError, TypeError):
        return [], False, False
    return [], False, False


def capture_room_asks(app, mesh, token, *, chat_id=''):
    """Aggregate finalized rooms; emit only authorized IDs, never denied hints."""
    if chat_id:
        try:
            ids = [_part(chat_id)]
        except (ValueError, TypeError):
            return [], False, False, ()
    else:
        try:
            ids = mesh.tx.list_chat_ids()  # positioning only, never authority
        except (OSError, ValueError, TypeError):
            return [], False, False, ()
        if (type(ids) is not list or len(ids) > MAX_ROOMS
                or any(type(chat) is not str for chat in ids)
                or len(set(ids)) != len(ids)):
            return [], False, False, ()
    asks, complete, resolved = [], True, []
    for chat in ids:
        if not app.validate_session_read(token):
            return [], False, False, ()
        selected, settled, forbidden = _room(app, mesh, token, chat)
        if forbidden and chat_id:
            return [], True, True, ()
        if settled and not forbidden:
            resolved.append(chat)
        if not settled:
            complete = False
        asks.extend(selected)
        if len(asks) > MAX_ASKS or len(json.dumps(asks, ensure_ascii=False).encode()) > MAX_BYTES:
            return [], False, False, ()
    return asks, complete, False, tuple(resolved)
