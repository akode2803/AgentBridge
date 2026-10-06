"""Default GUI canonical transcript pages from admitted local inputs.

Companions arrive through a separately finalized bounded request. Neither route
falls back to the legacy complete-history fold when ingestion is pending.
"""
from __future__ import annotations

import json
import sqlite3

from ..core.models import ChatSnapshot
from ..mesh.page_operation import PageOperation
from ..mesh.page_read_ack import read_cutoff
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part
from .api_page_read_ack import chat_page_read
from .context import session_read_binding
from .diagnostics import capture_inputs, record_page_stage
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
    anchor_token = req.params.get('anchor')
    if cursor is not None and anchor_token is not None:
        return _pending(token, 'ambiguous_page_position', status='reset_required')
    continuation = None
    anchor = None
    if cursor is not None:
        continuation = app.page_cursors.resolve(cursor, token, chat)
        if continuation is None:
            return _pending(token, 'continuation_expired', status='reset_required')
    if anchor_token is not None:
        anchor = app.page_cursors.resolve_anchor(anchor_token, token, chat)
        if anchor is None:
            return _pending(token, 'window_anchor_expired', status='reset_required')
    # Routing hints contain no membership verdict. Selection is still checked
    # through canonical raw inputs and the final session/Store cut below.
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    if runtime.unread is not None:
        from ..mesh.unread_counts import UnreadSession
        runtime.unread.request(chat, UnreadSession(token.app_identity, token.generation, mesh.user),
                               selected=True)
    before = continuation.before if continuation else anchor.before if anchor else None
    expected = continuation.position if continuation else None
    operation = None
    defer_receipts = False
    for attempt in range(1, 5):
        try:
            reader, receipt, index = capture_inputs(
                runtime, chat, app, req, attempt)
        except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
                OSError, sqlite3.Error):
            result = _pending(token, 'local_inputs_pending')
            failure = runtime.preparation_health(chat)
            if failure == 'schema_preparation_failed':
                result.update(status='unavailable', reason=failure)
            return result
        if operation is None:
            operation = PageOperation(mesh, chat, source_reader=reader,
                                      before=continuation.before if continuation else None,
                                      window_before=anchor.before if anchor else None,
                                      window_inclusive=anchor.inclusive if anchor else False,
                                      expected_position=expected, limit=limit,
                                      defer_receipts=defer_receipts)
        work = operation.prepare(receipt, receipt, index)
        record_page_stage(app, req, chat, 'prepare', work.status,
                          work.reason or 'none', attempt=attempt)
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
        record_page_stage(app, req, chat, 'finalize',
                          final.status, final.reason or 'none', attempt=attempt,
                          rows=(len(final.result.page.messages) if final.result is not None
                                    and final.result.page is not None else 0),
                              raw_examined=(final.result.page.raw_examined if final.result is not None
                                            and final.result.page is not None else 0))
        if final.status == 'restart':
            if final.reason == 'receipt_presence_changed':
                # Receipt decorations must not make canonical history unreadable.
                # Recompute from fresh raw inputs with all authority checks; the
                # response explicitly reports unknown receipts for this pass.
                defer_receipts = True
                operation = None
            continue
        if final.status != 'page' or final.result is None:
            return _pending(token, final.reason or 'page_changed', status=final.status)
        selected, presentation = final.result.page, final.result.presentation
        if selected is None or presentation is None:
            return _pending(token, 'presentation_unavailable', status='unavailable')
        if anchor is not None and not anchor.matches_store(selected.position):
            return _pending(token, 'window_store_changed', status='reset_required')
        meta = chat_json(ChatSnapshot.from_dict(json.loads(presentation.snapshot_json)), full=True)
        viewer = json.loads(presentation.viewer_state_json)
        meta['archived'] = viewer.get('archived', False)
        if viewer.get('mute') is not None:
            meta['mute'] = viewer['mute']
        if meta['kind'] == 'dm':
            meta['blocked'] = viewer['blocked']
        result = {
            'status': 'page', 'meta': meta, 'me': mesh.user,
            'chat_id': chat, 'page_version': app.page_cursors.version(token, chat, selected,
                local_trust_version=final.result.local_trust_version),
            'messages': [message_json(message, mesh.user) for message in selected.messages],
            'starred': list(presentation.starred), 'starred_scope': 'page',
            'read_ns': viewer.get('read_ns', 0),
            # Decimal text preserves nanoseconds beyond JS Number precision.
            'read_cutoff_ns': str(read_cutoff(selected)),
            'has_more': selected.has_more, 'history_exhausted': selected.history_exhausted,
            'scan_budget_exhausted': selected.scan_budget_exhausted,
            # Lets the browser distinguish a legitimate all-filtered window
            # (clear/hide/history-on-join still examines raw rows) from an
            # impossible empty replacement of an already visible append-only
            # transcript. This is bounded selection evidence, not authority.
            'raw_examined': selected.raw_examined,
            'continuation': app.page_cursors.issue(token, chat, selected),
            'window_anchor': app.page_cursors.issue_anchor(token, chat, selected, before),
            'frozen_window_anchor': (app.page_cursors.issue_anchor(
                token, chat, selected, selected.newest_examined, inclusive=True)
                if selected.newest_examined is not None else None),
            'session_binding': session_read_binding(token),
            'metadata_status': {'receipts': 'deferred', 'pins': 'deferred',
                                'origin': 'deferred', 'profiles': 'deferred',
                                'pause': 'deferred', 'blocking': 'ready',
                                'mute': 'ready' if viewer.get('mute') is not None else 'pending',
                                'owner_controls': 'pending'},
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
        result['read_ack_token'] = app.page_read_tokens.issue_read(
            token, chat, selected, limit=limit, trust_version=final.result.local_trust_version,
            page_version=result['page_version'])
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            return _pending(token, 'response_byte_budget', status='unavailable')
        return result
    return _pending(token, 'page_progress')


GET = {'/api/mesh/chat_page': chat_page}
POST = {'/api/mesh/chat_page_read': chat_page_read}
