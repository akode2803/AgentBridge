"""Fresh receipt decoration for one already painted canonical page.

The opaque read token retains positioning only. Every request recomputes
membership, lifecycle, trust, keys, visibility and receipt privacy from current
admitted local inputs, then verifies that the result still belongs to the
painted page before returning message-id keyed decoration.
"""
from __future__ import annotations

import json
import sqlite3

from ..mesh.page_operation import PageOperation
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part
from .api_pages import _pending
from .context import session_read_binding
from .routing import authed_read_token


MAX_RECEIPT_BYTES = 1024 * 1024


@authed_read_token
def chat_page_receipts(app, req, mesh, token):
    if (type(req.data) is not dict
            or set(req.data) != {'chat_id', 'page_version', 'read_ack_token'}):
        return _pending(token, 'invalid_receipt_page', status='reset_required')
    try:
        chat = _part(req.data['chat_id'])
    except (TypeError, ValueError):
        return _pending(token, 'invalid_receipt_page', status='reset_required')
    page_version = req.data['page_version']
    position = app.page_read_tokens.resolve_read(
        req.data['read_ack_token'], token, chat, page_version,
    )
    if position is None:
        return _pending(token, 'receipt_page_expired', status='reset_required')
    runtime = mesh.local_inputs
    if runtime is None:
        return _pending(token, 'local_paging_disabled', status='unavailable')
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    operation = None
    for _ in range(4):
        try:
            reader, receipt, index = runtime.inputs(chat)
        except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
                OSError, sqlite3.Error):
            return _pending(token, 'local_inputs_pending')
        if operation is None:
            operation = PageOperation(
                mesh, chat, source_reader=reader,
                window_before=position.before, window_inclusive=True,
                limit=position.limit,
            )
        work = operation.prepare(receipt, receipt, index)
        if work.status == 'restart':
            continue
        if work.status == 'work':
            if work.reason == 'overlay_proofs':
                runtime.request_page(chat, index=index, proofs=work.work)
            return _pending(token, work.reason)
        if work.status == 'forbidden':
            return _pending(token, 'viewer_not_member', status='forbidden')
        if work.status != 'prepared':
            return _pending(token, work.reason or 'receipts_unavailable',
                            status='unavailable')
        if work.prepared._fence.position != position.position:
            return _pending(token, 'receipt_page_inputs_changed', status='reset_required')
        final = app.finalize_page_read(
            token, work.prepared, expected_trust_version=position.trust_version,
        )
        if final.status == 'restart':
            continue
        if final.status != 'page' or final.result is None:
            status = ('reset_required' if final.status == 'restart'
                      or final.reason.endswith('_changed') else final.status)
            return _pending(token, final.reason or 'receipt_page_changed', status=status)
        selected, presentation = final.result.page, final.result.presentation
        current_version = app.page_cursors.version(
            token, chat, selected,
            local_trust_version=final.result.local_trust_version,
        )
        if current_version != page_version:
            return _pending(token, 'receipt_page_version_changed', status='reset_required')
        if presentation is None or presentation.receipts_json is None:
            return _pending(token, 'receipt_inputs_pending')
        receipts = json.loads(presentation.receipts_json)
        send_statuses = final.result.send_statuses or {}
        for message_id, value in receipts.items():
            if message_id in send_statuses:
                value['transport'] = send_statuses[message_id]
        encoded = json.dumps(receipts, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()
        if (type(receipts) is not dict or len(receipts) > position.limit
                or len(encoded) > MAX_RECEIPT_BYTES):
            return _pending(token, 'receipt_response_budget', status='unavailable')
        return {
            'status': 'ready', 'chat_id': chat, 'page_version': page_version,
            'receipts': receipts, 'session_binding': session_read_binding(token),
        }
    return _pending(token, 'receipt_page_progress')


GET = {}
POST = {'/api/mesh/chat_page_receipts': chat_page_receipts}
