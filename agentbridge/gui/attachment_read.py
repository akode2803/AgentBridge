"""Exact canonical attachment handout around one external sealed-blob fetch."""
from __future__ import annotations

import hashlib
import re
import sqlite3

from ..mesh.attachments import attachment_path
from ..mesh.page_operation import PageOperation
from ..mesh.sealer import (E2EESealer, PlainSealer, observed_blob_epoch,
                           open_blob_observed)
from ..store import local_source, overlay_index
from ..transport.authority_observation import _part

_SHA = re.compile(r'[a-f0-9]{64}\Z')
_MAX_FILE_BYTES = 512 * 1024 * 1024


class _ObservedBlobPage(PageOperation):
    """Candidate is request-local; epoch proof joins the normal final fence."""

    def __init__(self, *args, sealed, blob_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.sealed, self.blob_id = sealed, blob_id
        self.candidate = self.digest = None

    def prepare(self, authority_receipt, overlay_receipt, index):
        self.candidate = self.digest = None  # Discard stale attempt bytes.
        return super().prepare(authority_receipt, overlay_receipt, index)

    def _decorate(self, round_, snapshot, receipt, index, expected, sealer):
        if type(self.mesh.sealer) is PlainSealer:
            candidate = self.mesh.sealer.open_blob(self.chat, self.blob_id, self.sealed)
        elif type(self.mesh.sealer) is E2EESealer:
            epoch = observed_blob_epoch(self.sealed)
            if epoch is None:
                candidate = None
            else:
                key = sealer.key(self.chat, epoch)  # observed key joins epoch fence
                candidate = (open_blob_observed(self.chat, self.blob_id, self.sealed,
                                                epoch, key) if key is not None else None)
        else:
            candidate = None
        self.candidate = candidate
        self.digest = hashlib.sha256(candidate).hexdigest() if candidate is not None else None
        return None, (), None


def _state(reason, status='pending'):
    return {'status': status, 'reason': reason, 'retry_after_ms': 350}


def _max_sealed_attachment_bytes(tx):
    """Bound sealed bytes by both the local ceiling and active transport."""
    transport_cap = int(getattr(tx, 'max_upload_bytes', 0) or 0)
    if transport_cap > 0:
        return min(_MAX_FILE_BYTES, transport_cap)
    return _MAX_FILE_BYTES


def _record(selection, anchor, message_id, blob_id):
    if (anchor.key is None or anchor.position != selection.position
            or selection.newest_examined != anchor.key
            or len(selection.messages) != 1):
        return None
    message = selection.messages[0]
    if (message.id != message_id or message.event is not None or message.deleted
            or message.undecrypted):
        return None
    for file in message.files or ():
        if (isinstance(file, dict) and file.get('id') == blob_id
                and type(file.get('sha256')) is str
                and _SHA.fullmatch(file['sha256'])
                and type(file.get('name')) is str):
            return file
    return None


def _exact(app, mesh, token, chat, message_id, blob_id, sealed=None):
    runtime = mesh.local_inputs
    if runtime is None:
        return _state('local_paging_disabled', 'unavailable'), None
    runtime.request(chat, selected=True)
    runtime.request_page(chat)
    for _ in range(4):
        try:
            reader, receipt, index = runtime.inputs(chat)
            anchor = reader.capture_message_anchor(receipt, index, message_id)
        except (local_source.SourceChanged, overlay_index.OverlayIndexUnavailable,
                OSError, sqlite3.Error):
            failure = runtime.preparation_health(chat)
            if failure == 'schema_preparation_failed':
                return _state(failure, 'unavailable'), None
            return _state('local_inputs_pending'), None
        operation_type = PageOperation if sealed is None else _ObservedBlobPage
        options = {} if sealed is None else {'sealed': sealed, 'blob_id': blob_id}
        operation = operation_type(mesh, chat, source_reader=reader,
                                   window_before=anchor.key,
                                   window_inclusive=anchor.key is not None,
                                   limit=1, scan_budget=1, summary_only=True, **options)
        work = operation.prepare(receipt, receipt, index)
        if work.status == 'restart':
            continue
        if work.status == 'work':
            if work.reason == 'overlay_proofs':
                runtime.request_page(chat, index=index, proofs=work.work)
            else:
                runtime.request(chat, selected=True, activity=True)
            return _state(work.reason), None
        if work.status == 'forbidden':
            return _state('viewer_not_member', 'forbidden'), None
        if work.status != 'prepared':
            return _state(work.reason or 'file_unavailable', 'unavailable'), None
        final = app.finalize_page_read(token, work.prepared)
        if final.status == 'restart':
            continue
        if final.status != 'page' or final.result is None:
            return _state(final.reason or 'file_changed', final.status), None
        selected = final.result.page
        record = _record(selected, anchor, message_id, blob_id) if selected is not None else None
        if record is None:
            return _state('file_not_found', 'not_found'), None
        if sealed is None:
            return None, record
        raw, digest = operation.candidate, operation.digest
        operation.candidate = operation.digest = None
        if raw is None or digest != record['sha256']:
            return _state('file_failed_verification', 'unavailable'), None
        return None, (record, raw)
    return _state('page_progress'), None


def read_attachment(app, mesh, token, chat_id, message_id, blob_id):
    """Two finalized exact reads around a transport fetch; never a history fold."""
    chat = _part(chat_id)
    if type(message_id) is not str or not message_id:
        return _state('message_id_required', 'not_found'), None
    path = attachment_path(chat, blob_id)
    error, preflight = _exact(app, mesh, token, chat, message_id, blob_id)
    if error:
        return error, None
    # This can allocate the whole transport object; post-fetch admission is
    # honest about that API limitation and honors the existing upload ceiling.
    sealed = mesh.tx.get_blob(path)
    if sealed is None and mesh.attachments is not None:
        sealed = mesh.attachments.local_sealed(blob_id)
    cap = _max_sealed_attachment_bytes(mesh.tx)
    if type(sealed) is not bytes or not sealed or len(sealed) > cap:
        return _state('file_unavailable', 'unavailable'), None
    error, final = _exact(app, mesh, token, chat, message_id, blob_id, sealed)
    if error:
        return error, None
    record, raw = final
    if record != preflight:
        return _state('file_changed', 'reset_required'), None
    return None, (record, raw)
