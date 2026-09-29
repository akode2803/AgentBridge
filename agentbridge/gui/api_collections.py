"""Finalized, bounded chat metadata and filtered canonical message windows."""
from __future__ import annotations

import json
import re
import sqlite3

from ..core.models import ChatSnapshot
from ..mesh.page_operation import PageOperation
from ..store import local_source, overlay_index, page_inputs
from ..transport.authority_observation import _part
from .context import session_read_binding
from .routing import authed_read_token
from .serialize import chat_json, message_json

MAX_ITEMS = 50
MAX_RAW_SCAN = 100
MAX_ITEM_STEPS = 2000
MAX_ITEM_BYTES = 384 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
_LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_IMAGE_EXTS = frozenset(('png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'))
_KINDS = frozenset(('media', 'docs', 'links', 'starred', 'search'))


def _state(token, reason, status='pending'):
    return {'status': status, 'reason': reason, 'retry_after_ms': 350,
            'session_binding': session_read_binding(token)}


def _meta(presentation):
    meta = chat_json(ChatSnapshot.from_dict(json.loads(presentation.snapshot_json)), full=True)
    viewer = json.loads(presentation.viewer_state_json)
    meta['archived'] = viewer.get('archived', False)
    if viewer.get('mute') is not None:
        meta['mute'] = viewer['mute']
    if meta['kind'] == 'dm':
        meta['blocked'] = viewer['blocked']
    return meta, viewer


def _metadata_status(viewer):
    return {'origin': 'deferred', 'counts': 'unknown', 'pins': 'deferred',
            'receipts': 'deferred', 'blocking': 'ready',
            'mute': 'ready' if viewer.get('mute') is not None else 'pending'}


def _read(app, mesh, token, chat, *, cursor=None, summary=False):
    runtime = mesh.local_inputs
    if runtime is None:
        return _state(token, 'local_paging_disabled', 'unavailable'), None
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    operation = None
    for _ in range(4):
        try:
            reader, receipt, index = runtime.inputs(chat)
        except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
                OSError, sqlite3.Error):
            reason = runtime.preparation_health(chat)
            return (_state(token, reason, 'unavailable') if reason == 'schema_preparation_failed'
                    else _state(token, 'local_inputs_pending')), None
        if operation is None:
            position = cursor.position if cursor is not None else None
            operation = PageOperation(
                mesh, chat, source_reader=reader,
                before=cursor.before if cursor is not None and not cursor.inclusive else None,
                expected_position=position if cursor is not None and not cursor.inclusive else None,
                window_before=cursor.before if cursor is not None and cursor.inclusive else None,
                window_inclusive=bool(cursor is not None and cursor.inclusive),
                limit=1 if summary or cursor is not None and cursor.inclusive else MAX_RAW_SCAN,
                scan_budget=1 if summary or cursor is not None and cursor.inclusive else MAX_RAW_SCAN,
                summary_only=True)
        work = operation.prepare(receipt, receipt, index)
        if work.status == 'restart':
            continue
        if work.status == 'work':
            if work.reason == 'overlay_proofs':
                runtime.request_page(chat, index=index, proofs=work.work)
            else:
                runtime.request(chat, selected=True, activity=True)
            return _state(token, work.reason), None
        if work.status == 'forbidden':
            return _state(token, 'viewer_not_member', 'forbidden'), None
        if work.status != 'prepared':
            state = 'reset_required' if work.reason == 'continuation_changed' else 'unavailable'
            return _state(token, work.reason or 'page_unavailable', state), None
        final = app.finalize_page_read(token, work.prepared)
        if final.status == 'restart':
            continue
        if final.status != 'page' or final.result is None:
            return _state(token, final.reason or 'page_changed', final.status), None
        selected, presentation = final.result.page, final.result.presentation
        if selected is None or presentation is None:
            return _state(token, 'presentation_unavailable', 'unavailable'), None
        version = app.page_cursors.version(token, chat, selected,
                                            local_trust_version=final.result.local_trust_version)
        # Inclusive windows cannot supply expected_position to PageOperation;
        # this equality check binds the freshly finalized authority/source cut.
        if cursor is not None and (cursor.version != version
                or cursor.position != selected.position
                or cursor.inclusive and selected.newest_examined != cursor.before):
            return _state(token, 'continuation_changed', 'reset_required'), None
        meta, viewer = _meta(presentation)
        return None, (selected, presentation, version, meta, viewer)
    return _state(token, 'page_progress'), None


@authed_read_token
def chat_summary(app, req, mesh, token):
    chat = _part(req.params.get('id', ''))
    error, result = _read(app, mesh, token, chat, summary=True)
    if error:
        return error
    _selected, _presentation, version, meta, viewer = result
    out = {'status': 'ready', 'chat_id': chat, 'meta': meta, 'page_version': version,
           'session_binding': session_read_binding(token),
           'metadata_status': _metadata_status(viewer)}
    if len(json.dumps(out, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        return _state(token, 'response_byte_budget', 'unavailable')
    return out


def _item_key(message, kind, occurrence):
    # JSON encoding prevents delimiter collisions in arbitrary message IDs.
    return json.dumps([message.id, kind, occurrence], ensure_ascii=False, separators=(',', ':'))


def _file_item(message, file, kind, index):
    if not isinstance(file, dict):
        return None
    name = file.get('name')
    if not isinstance(name, str):
        return None
    image = name.rsplit('.', 1)[-1].lower() in _IMAGE_EXTS
    if image != (kind == 'media'):
        return None
    return {**file, 'from': message.from_, 'ts': message.ts, 'msg_id': message.id,
            'item_key': _item_key(message, 'file', index)}


def _search_span(body, query):
    """Map a casefold match back to original Python-codepoint boundaries."""
    folded_start = body.casefold().find(query)
    if folded_start < 0:
        return None
    folded_end = folded_start + len(query)
    consumed = start = 0
    for index, char in enumerate(body):
        next_consumed = consumed + len(char.casefold())
        if consumed <= folded_start < next_consumed:
            start = index
        if consumed < folded_end <= next_consumed:
            return start, index + 1
        consumed = next_consumed
    return None


def _items(message, kind, starred, query, offset, occurrence, viewer):
    """Yield (item, next offset, next occurrence), incrementally and in order.

    File offsets are array indices; link offsets are character positions, so a
    huge message never needs prior matches flattened just to resume it.
    """
    if kind in ('media', 'docs'):
        for index in range(offset, len(message.files or ())):
            item = _file_item(message, message.files[index], kind, index)
            yield item, index + 1, index + 1
    elif kind == 'links':
        for match in _LINK_RE.finditer(message.body or '', offset):
            item = {'url': match.group(), 'from': message.from_, 'ts': message.ts,
                    'msg_id': message.id, 'item_key': _item_key(message, kind, occurrence)}
            occurrence += 1
            yield item, match.end(), occurrence
    elif offset == 0:
        span = _search_span(message.body or '', query) if kind == 'search' else None
        if kind == 'starred' and message.id in starred or span is not None:
            item = {**message_json(message, viewer), 'item_key': _item_key(message, kind, 0)}
            if span is not None:
                item['match_start'], item['match_end'] = span
            yield item, 1, 1


def _might_have_more_in_message(message, kind, offset):
    if kind in ('media', 'docs'):
        # Remaining files can include other types; a conservative continuation
        # avoids a potentially large second filter walk in this request.
        return offset < len(message.files or ())
    if kind == 'links':
        return _LINK_RE.search(message.body or '', offset) is not None
    return False


def _might_have_match_in_selected(messages, current_index, kind, starred, query,
                                  remaining_steps):
    """Check already-selected older rows only; budget exhaustion stays conservative."""
    older = messages[:len(messages) - current_index - 1]
    for message in reversed(older):
        if message.event is not None or message.deleted or message.undecrypted:
            continue
        if kind == 'starred':
            if message.id in starred:
                return True
        elif kind == 'search':
            if query in (message.body or '').casefold():
                return True
        elif kind == 'links':
            if _LINK_RE.search(message.body or '') is not None:
                return True
        else:
            for file in message.files or ():
                if remaining_steps <= 0:
                    return True
                remaining_steps -= 1
                if _file_item(message, file, kind, 0) is not None:
                    return True
    return False


@authed_read_token
def chat_collection(app, req, mesh, token):
    chat = _part(req.params.get('id', ''))
    kind = req.params.get('kind', '')
    if kind not in _KINDS:
        return _state(token, 'invalid_collection_kind', 'unavailable')
    raw_query = req.params.get('query', '').strip() if kind == 'search' else ''
    if kind == 'search' and not 2 <= len(raw_query) <= 200:
        return _state(token, 'invalid_search_query', 'unavailable')
    query = raw_query.casefold()
    if len(query.encode()) > 800:
        return _state(token, 'invalid_search_query', 'unavailable')
    cursor = None
    if 'cursor' in req.params:
        cursor = app.collection_cursors.resolve(req.params['cursor'], token, chat, kind, query)
        if cursor is None:
            return _state(token, 'continuation_expired', 'reset_required')
    error, result = _read(app, mesh, token, chat, cursor=cursor)
    if error:
        return error
    selected, presentation, version, meta, viewer = result
    starred = frozenset(presentation.starred)
    items, item_bytes, steps = [], 0, 0
    next_position = None
    stopped = False
    for message_index, message in enumerate(reversed(selected.messages)):
        if message.event is not None or message.deleted or message.undecrypted:
            continue
        key = page_inputs.MessageKey(message.ns, message.from_, message.id)
        start = cursor.offset if cursor is not None and cursor.inclusive else 0
        occurrence = cursor.occurrence if cursor is not None and cursor.inclusive else 0
        for item, next_offset, next_occurrence in _items(message, kind, starred, query,
                                                         start, occurrence, mesh.user):
            steps += 1
            if steps > MAX_ITEM_STEPS:
                next_position = (key, True, start, occurrence)
                break
            if item is not None:
                size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode())
                if size > MAX_ITEM_BYTES:
                    return _state(token, 'oversized_collection_item', 'unavailable')
                if size + item_bytes > MAX_ITEM_BYTES:
                    next_position = (key, True, start, occurrence)
                    break
                items.append(item)
                item_bytes += size
            start, occurrence = next_offset, next_occurrence
            if len(items) >= MAX_ITEMS:
                if _might_have_more_in_message(message, kind, start):
                    next_position = (key, True, start, occurrence)
                elif _might_have_match_in_selected(selected.messages, message_index, kind,
                                                  starred, query, MAX_ITEM_STEPS - steps):
                    next_position = (key, False, 0, 0)
                elif selected.has_more:
                    # All older rows in this raw window have been checked.
                    next_position = (selected.oldest_examined, False, 0, 0)
                stopped = True
                break
        if next_position is not None or stopped:
            break
        # Only the first selected message may resume an inclusive cursor.
        cursor = None
    if next_position is None and not stopped and selected.has_more:
        next_position = (selected.oldest_examined, False, 0, 0)
    continuation = (app.collection_cursors.issue(token, chat, kind, query, selected, version,
                    before=next_position[0], inclusive=next_position[1],
                    offset=next_position[2], occurrence=next_position[3])
                    if next_position is not None else None)
    out = {'status': 'page', 'chat_id': chat, 'kind': kind, 'meta': meta, 'items': items,
           'page_version': version, 'session_binding': session_read_binding(token),
           'metadata_status': _metadata_status(viewer), 'continuation': continuation,
           'has_more': continuation is not None,
           'history_exhausted': selected.history_exhausted and continuation is None,
           'scan_budget_exhausted': (selected.scan_budget_exhausted
                                     or selected.raw_examined >= MAX_RAW_SCAN and selected.has_more),
           'raw_examined': selected.raw_examined}
    if len(json.dumps(out, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        return _state(token, 'response_byte_budget', 'unavailable')
    return out


GET = {'/api/mesh/chat_summary': chat_summary,
       '/api/mesh/chat_collection': chat_collection}
POST = {}
