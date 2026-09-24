"""Opt-in local canonical transcript pages; no full-history fallback.

Companion metadata is explicitly deferred until its own bounded, canonical
selectors are available. This endpoint does not replace the legacy GUI route.
"""
from __future__ import annotations

import json
import sqlite3

from ..core.models import ChatSnapshot
from ..mesh.page_operation import PageOperation
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part
from .context import session_read_binding
from .routing import authed_read_token
from .serialize import chat_json, message_json

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _pending(token, reason, *, status='pending'):
    return {'status': status, 'reason': reason, 'retry_after_ms': 350,
            'session_binding': session_read_binding(token)}


@authed_read_token
def chat_page(app, req, mesh, token):
    chat = _part(req.params.get('id', ''))
    runtime = mesh.local_inputs
    if runtime is None:
        return _pending(token, 'local_paging_disabled', status='unavailable')
    limit = req.int_param('limit', 50, 1, 200)
    cursor = req.params.get('cursor')
    continuation = None
    if cursor is not None:
        continuation = app.page_cursors.resolve(cursor, token, chat)
        if continuation is None:
            return _pending(token, 'continuation_expired', status='reset_required')
    # Routing hints contain no membership verdict. Selection is still checked
    # through canonical raw inputs and the final session/Store cut below.
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    before = continuation.before if continuation else None
    expected = continuation.position if continuation else None
    operation = None
    for _ in range(4):
        try:
            reader, receipt, index = runtime.inputs(chat)
        except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
                OSError, sqlite3.Error):
            result = _pending(token, 'local_inputs_pending')
            failure = runtime.preparation_health(chat)
            if failure == 'schema_preparation_failed':
                result.update(status='unavailable', reason=failure)
            return result
        if operation is None:
            operation = PageOperation(mesh, chat, source_reader=reader,
                                      before=before, expected_position=expected, limit=limit)
        work = operation.prepare(receipt, receipt, index)
        if work.status == 'restart':
            continue
        if work.status == 'work':
            if work.reason == 'overlay_proofs':
                runtime.request_page(chat, index=index, proofs=work.work)
            else:
                runtime.request(chat, selected=True, activity=True)
            return _pending(token, work.reason)
        if work.status == 'forbidden':
            return _pending(token, 'viewer_not_member', status='forbidden')
        if work.status != 'prepared':
            if work.reason == 'continuation_changed':
                return _pending(token, work.reason, status='reset_required')
            return _pending(token, work.reason or 'page_unavailable', status='unavailable')
        final = app.finalize_page_read(token, work.prepared)
        if final.status == 'restart':
            continue
        if final.status != 'page' or final.result is None:
            return _pending(token, final.reason or 'page_changed', status=final.status)
        selected, presentation = final.result.page, final.result.presentation
        if selected is None or presentation is None:
            return _pending(token, 'presentation_unavailable', status='unavailable')
        meta = chat_json(ChatSnapshot.from_dict(json.loads(presentation.snapshot_json)), full=True)
        viewer = json.loads(presentation.viewer_state_json)
        meta['archived'] = viewer.get('archived', False)
        if meta['kind'] == 'dm':
            meta['blocked'] = viewer['blocked']
        result = {
            'status': 'page', 'meta': meta, 'me': mesh.user,
            'chat_id': chat, 'page_version': app.page_cursors.version(token, chat, selected,
                local_trust_version=final.result.local_trust_version),
            'messages': [message_json(message, mesh.user) for message in selected.messages],
            'starred': list(presentation.starred), 'starred_scope': 'page',
            'read_ns': viewer.get('read_ns', 0),
            'has_more': selected.has_more, 'history_exhausted': selected.history_exhausted,
            'scan_budget_exhausted': selected.scan_budget_exhausted,
            'continuation': app.page_cursors.issue(token, chat, selected),
            'session_binding': session_read_binding(token),
            'metadata_status': {'receipts': 'deferred', 'pins': 'deferred',
                                'origin': 'deferred', 'profiles': 'deferred',
                                'pause': 'deferred', 'blocking': 'ready'},
        }
        if presentation.pins_json is not None:
            result['meta']['pins'] = json.loads(presentation.pins_json)
            result['metadata_status']['pins'] = 'ready'
        if presentation.receipts_json is not None:
            receipts = json.loads(presentation.receipts_json)
            for message in result['messages']:
                if message['id'] in receipts:
                    value = receipts[message['id']]
                    if message['id'] in (final.result.send_statuses or {}):
                        value['transport'] = final.result.send_statuses[message['id']]
                    message['receipt'] = value
            result['metadata_status']['receipts'] = 'ready'
        else:
            result['metadata_status']['receipts'] = 'pending'
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            return _pending(token, 'response_byte_budget', status='unavailable')
        return result
    return _pending(token, 'page_progress')


GET = {'/api/mesh/chat_page': chat_page}
POST = {}
