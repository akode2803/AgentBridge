"""Off-path preparation using the existing canonical signing recipes.

Candidates are intentionally unverified. Current authority is a caller duty,
not a cached property of an index or successful cryptographic predicate.
"""
from __future__ import annotations

import hashlib
import json

from ..store.document_observation import DocumentObservation
from ..store.overlay_index import (
    MAX_BUILD_BYTES, MAX_CANDIDATES, MAX_DOCUMENTS,
    IndexedDocument, OverlayCandidate, OverlayIndexPosition, OverlayIndexUnavailable, PreparedOverlayIndex,
)
from .events import reaction_signing_bytes, state_signing_bytes
from .overlays import UserState, reaction_map
from .overlay_source import OverlaySourceReceipt, capture_overlay_inputs

_STATE_SCALARS = (
    'cleared', 'deleted', 'read_ns', 'read_ts', 'delivered_ns', 'delivered_ts',
    'forced_unread', 'mute', 'archived', 'pinned',
)


def prepare_overlay_index(observation: DocumentObservation, chat_id: str) -> PreparedOverlayIndex:
    """Pure background normalization; preserves candidates with bad signatures."""
    from ..store.overlay_index import _chat, _doc_path

    chat_id = _chat(chat_id)
    if type(observation) is not DocumentObservation or not observation.position.initialized:
        raise OverlayIndexUnavailable('source_pending')
    if type(observation.records) is not tuple or len(observation.records) > MAX_DOCUMENTS:
        raise OverflowError('source document budget exceeded')
    documents, candidates, used, seen = [], [], 0, set()
    for record in observation.records:
        if record.deleted:
            continue
        prefix = f'chats/{chat_id}/overlays/'
        if not record.path.startswith(prefix):
            raise ValueError('source contains a document outside the bound chat')
        # Edit/redaction/pin inputs remain exact source documents, not these
        # per-actor index classes. Reject unexpected classes rather than ingest
        # trust, keys or runtime state through a broad chat prefix.
        remainder = record.path[len(prefix):].split('/')
        if len(remainder) != 2 or remainder[0] not in ('reactions', 'state', 'edits', 'redactions', 'pins'):
            raise ValueError('source contains an unsupported overlay path')
        if remainder[0] in ('edits', 'redactions', 'pins'):
            continue
        kind, actor = _doc_path(record.path, chat_id)
        if record.path in seen or type(record.payload_json) is not str:
            raise ValueError('invalid or duplicate source document')
        seen.add(record.path)
        raw = record.payload_json.encode()
        used += len(raw)
        if used > MAX_BUILD_BYTES:
            raise OverflowError('source byte budget exceeded')
        doc = json.loads(raw)
        # Current reaction_docs skips non-dicts; UserState.get returns {}.
        shape_error = ''
        if type(doc) is not dict:
            shape_error, doc = 'document_shape', {}
        signature = doc.get('sig') or ''
        if 'sig' in doc and type(doc['sig']) is not str:
            shape_error, signature = shape_error or 'signature_shape', ''
        scalars = {}
        entries = []
        if kind == 'reactions':
            if 'v' in doc and type(doc['v']) is not dict:
                shape_error = shape_error or 'reaction_map'
            mapping = reaction_map(doc)
            if len(mapping) + len(candidates) > MAX_CANDIDATES:
                raise OverflowError('candidate budget exceeded')
            entries = [OverlayCandidate(record.path, 'reaction', k, v) for k, v in mapping.items()]
            try:
                signing = reaction_signing_bytes(chat_id, actor, int(doc.get('ns', 0)), mapping)
            except (TypeError, ValueError, OverflowError):
                signing, shape_error = None, 'signing_input'
        else:
            fields = UserState.signed_fields(doc)
            try:
                signing = state_signing_bytes(chat_id, actor, int(doc.get('ns', 0)), fields)
            except (TypeError, ValueError, OverflowError):
                signing, shape_error = None, 'signing_input'
            scalars = {k: doc[k] for k in _STATE_SCALARS if k in doc}
            # Reuse set semantics for strings/dicts/lists; non-string IDs cannot
            # be safely matched by this TEXT-key index and remain explicit error.
            try:
                for name in ('hidden', 'starred'):
                    ids = set(doc.get(name, []))
                    if any(type(i) is not str or not i for i in ids):
                        raise ValueError('invalid viewer ID')
                    if len(candidates) + len(entries) + len(ids) > MAX_CANDIDATES:
                        raise OverflowError('candidate budget exceeded')
                    entries.extend(OverlayCandidate(record.path, name, i, '') for i in sorted(ids))
            except (TypeError, ValueError):
                shape_error = shape_error or 'viewer_ids'
                entries = []
        scalar_json = json.dumps(scalars, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if len(scalar_json.encode()) > 65536:
            raise OverflowError('state scalar budget exceeded')
        used += len(signing or b'') + len(scalar_json.encode())
        used += sum(sum(len(v.encode()) for v in (e.path, e.kind, e.target, e.value)) for e in entries)
        if used > MAX_BUILD_BYTES:
            raise OverflowError('prepared index byte budget exceeded')
        documents.append(IndexedDocument(
            record.path, kind, actor, hashlib.sha256(raw).hexdigest(), len(raw),
            signature, signing, not doc, shape_error, scalar_json,
        ))
        candidates.extend(entries)
    return PreparedOverlayIndex(observation.position, chat_id, tuple(documents), tuple(candidates))


def capture_indexed_overlay_inputs(transport, store, receipt, index, targets, state_paths=(), **budgets):
    """Bounded local-input cut only; NOT canonical viewer output/authority."""
    if type(index) is not OverlayIndexPosition or type(receipt) is not OverlaySourceReceipt:
        raise TypeError('expected index position and overlay source receipt')
    if index.source != receipt.position or index.chat_id != receipt.chat_id:
        raise ValueError('index and source receipt do not match')
    capture_overlay_inputs(transport, store, receipt, ())
    result = store.capture_overlay_index(index, targets, state_paths, **budgets)
    store.capture_overlay_index(index, ())  # no changed build may cross handout
    capture_overlay_inputs(transport, store, receipt, ())
    return result
