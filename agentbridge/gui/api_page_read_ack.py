"""Fresh canonical, page-bound read-through acknowledgment; no caller cutoff."""
from __future__ import annotations

import sqlite3

from ..mesh.overlays import _state_lock
from ..mesh.page_operation import PageOperation, _ERRORS, _failure
from ..mesh.page_read_ack import PreparedReadAck
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part
from .context import session_read_binding
from .routing import authed_read_token


def _reply(token, status, reason):
    status = {'restart': 'reset_required', 'work': 'pending'}.get(status, status)
    return dict(status=status, reason=reason, retry_after_ms=350,
                session_binding=session_read_binding(token))


@authed_read_token
def chat_page_read(app, req, mesh, token):
    if (type(req.data) is not dict
            or set(req.data) != {'chat_id', 'page_version', 'read_ack_token'}):
        return _reply(token, 'reset_required', 'invalid_read_ack')
    try:
        chat = _part(req.data['chat_id'])
    except (TypeError, ValueError):
        return _reply(token, 'reset_required', 'invalid_read_ack')
    position = app.page_read_tokens.resolve_read(req.data['read_ack_token'], token,
                                                chat, req.data['page_version'])
    if position is None:
        return _reply(token, 'reset_required', 'read_ack_expired')
    runtime = mesh.local_inputs
    if runtime is None:
        return _reply(token, 'unavailable', 'local_paging_disabled')
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    # Same lock as all in-process per-user overlay merges. Never merge a
    # presentation subset: hidden/starred/runtime/unknown signed fields survive.
    with _state_lock(chat, mesh.user):
        operation = None
        for _ in range(4):
            try:
                reader, receipt, index = runtime.inputs(chat)
            except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
                    OSError, sqlite3.Error):
                return _reply(token, 'pending', 'local_inputs_pending')
            if operation is None:
                operation = PageOperation(mesh, chat, source_reader=reader,
                    window_before=position.before, window_inclusive=True,
                    limit=position.limit, defer_receipts=True)
            work = operation.prepare(receipt, receipt, index)
            if work.status == 'restart':
                continue
            if work.status == 'work':
                if work.reason == 'overlay_proofs':
                    runtime.request_page(chat, index=index, proofs=work.work)
                return _reply(token, 'pending', work.reason)
            if work.status == 'forbidden':
                return _reply(token, 'forbidden', 'viewer_not_member')
            if work.status != 'prepared':
                return _reply(token, work.status, work.reason)
            if work.prepared._fence.position != position.position:
                return _reply(token, 'reset_required', 'read_ack_inputs_changed')
            try:
                ack = PreparedReadAck(work.prepared)
            except _ERRORS as exc:
                failed = _failure(exc)
                return _reply(token, failed.status, failed.reason)
            final = app.finalize_page_read(token, work.prepared, mutation=ack.mutation,
                                           expected_trust_version=position.trust_version)
            if final.status != 'page':
                status = ('reset_required' if final.status == 'restart'
                          or final.reason.endswith('_changed') else final.status)
                return _reply(token, status, final.reason or 'read_ack_changed')
            # The intent is now durable. Provider work is outside session,
            # identity, pin and SQL locks. A failed/ambiguous provider write keeps
            # its intent pending and can never be retried with the same ticket.
            try:
                # Cancellation observed before provider start can still avoid
                # the write. Reservation was the acceptance cut; a session
                # change after this check cannot undo an in-flight operation.
                if app.lock.expire_if_idle():
                    return _reply(token, 'locked', 'app_locked')
                if not app.validate_session_read(token):
                    return _reply(token, 'unavailable', 'session_changed')
                ack.execute()
            finally:
                if ack.mutation is not None and ack.mutation._state == 'reserved':
                    ack.mutation.abort_unstarted()
            if ack.mutation is not None:
                runtime.request(chat, selected=True, activity=True)
            return dict(ok=True, status='acknowledged', read_ns=str(ack.read_ns),
                        session_binding=session_read_binding(token))
    return _reply(token, 'pending', 'page_progress')
