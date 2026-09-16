"""Background lifecycle input publication and live-source bracketing.

Receipts identify raw local evidence, never authority or remote completeness.
No reader rebuilds a pending source or falls back to a full lifecycle scan.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from ..store import document_observation, lifecycle_inputs
from ..transport.cache import CachingTransport
from ..transport.mirror_observation import (
    MirrorCaptureUnavailable, MirrorExpectedPosition, MirrorPositionValidation,
    MirrorSelection, MirrorSelectionRequest,
)


class LifecycleSourceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LifecycleSourceReceipt:
    position: document_observation.DocumentPosition
    mirror: MirrorExpectedPosition


def _source(mirror):
    payload = json.dumps([mirror.root_identity, mirror.cache_identity])
    return 'lifecycle-v1:' + hashlib.sha256(payload.encode()).hexdigest()


def _match(transport, expected):
    result = transport.validate_mirror_position(expected)
    if type(result) is not MirrorPositionValidation or result.status != 'matched':
        raise LifecycleSourceUnavailable('lifecycle_mirror_changed_or_unavailable')


def _receipt(transport, store, receipt):
    if type(transport) is not CachingTransport:
        raise LifecycleSourceUnavailable('unsupported_lifecycle_transport')
    if type(receipt) is not LifecycleSourceReceipt:
        raise TypeError('expected LifecycleSourceReceipt')
    position, mirror = receipt.position, receipt.mirror
    if type(mirror) is not MirrorExpectedPosition:
        raise ValueError('invalid lifecycle mirror position')
    mirror = replace(mirror)
    position = document_observation._validate_expected(position, store.path)
    if position.source_id != _source(mirror):
        raise ValueError('inconsistent lifecycle source binding')
    return LifecycleSourceReceipt(position, mirror)


def publish_lifecycle_source(transport, store, *, max_documents=20_000,
                             max_bytes=16 * 1024 * 1024, max_examined_paths=100_000):
    """One explicit bounded background rebuild, with no internal retry."""
    if type(transport) is not CachingTransport:
        raise LifecycleSourceUnavailable('unsupported_lifecycle_transport')
    selection = transport.capture_mirror_selection(MirrorSelectionRequest(
        expected=None, exact_paths=(), complete_prefixes=('lifecycle/',),
        max_records=max_documents, max_bytes=max_bytes,
        max_examined_paths=max_examined_paths,
    ))
    if isinstance(selection, MirrorCaptureUnavailable):
        raise LifecycleSourceUnavailable(selection.reason)
    if type(selection) is not MirrorSelection or selection.provenance != 'provider_observed':
        raise LifecycleSourceUnavailable('lifecycle_source_not_provider_observed')
    expected = store.capture_document_position(_source(selection.position))
    pending = store.invalidate_document_observation(expected)
    _match(transport, selection.position)
    documents = {row.path: row.decoded()
                 for group in selection.complete_prefixes for row in group.records}
    position = store.publish_document_batch(
        pending, documents, cursor=pending.cursor, full=True,
        retain_tombstones=False, max_documents=max_documents, max_bytes=max_bytes,
    )
    _match(transport, selection.position)
    return LifecycleSourceReceipt(position, selection.position)


def capture_lifecycle_inputs(transport, store, receipt, subject, **limits):
    """Capture one complete bounded subject range, bracketed by live evidence.

    The coordinator still checks pins, signatures, lifecycle publication,
    membership, session and all final input positions after canonical assembly.
    """
    receipt = _receipt(transport, store, receipt)
    _match(transport, receipt.mirror)
    conn = document_observation._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        result = lifecycle_inputs.capture_subject(conn, store.path, receipt.position, subject, **limits)
    finally:
        conn.close()
    if store.capture_document_position(receipt.position.source_id) != receipt.position:
        raise LifecycleSourceUnavailable('lifecycle_source_changed')
    _match(transport, receipt.mirror)
    return result
