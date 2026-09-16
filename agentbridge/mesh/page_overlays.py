"""Request-local indexed transcript overlays; not a viewer authority lease.

The caller owns current membership input, session binding and final authority /
source validation. This module never reads a complete signed source or caches a
Directory decision. It preserves the legacy reaction and state verification gates.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import crypto
from ..core.models import ChatSnapshot
from ..store import overlay_index as index
from ..store.page_inputs import RawPageInputs, PageInputPosition
from .page_selection import _snapshot
from .paths import P
from .overlays import fold_reactions


class PageOverlaysUnavailable(RuntimeError):
    """Captured evidence cannot reproduce canonical behavior safely."""


class PageOverlayProofsPending(RuntimeError):
    def __init__(self, keys):
        self.keys = tuple(keys)
        super().__init__('exact source/key signature evidence is pending')


@dataclass(frozen=True)
class TranscriptOverlays:
    position: PageInputPosition
    reactions: dict
    state: dict
    key_dependencies: tuple[tuple[str, str | None], ...]


def _detached(inputs):
    inputs = _snapshot(inputs)
    selected = inputs.indexed
    if type(selected) is not index.IndexedOverlayInputs or selected.reactions_complete is not True:
        raise PageOverlaysUnavailable('complete_reaction_manifest_required')
    position = index._wanted(selected.position, Path(inputs.position.messages.database_path))
    if position != inputs.position.overlays:
        raise PageOverlaysUnavailable('inconsistent_overlay_position')
    docs, candidates, absent = selected.documents, selected.candidates, selected.absent_states
    if (type(docs) is not tuple or len(docs) > index.MAX_DEPENDENCIES
            or type(candidates) is not tuple or len(candidates) > 4096
            or type(absent) is not tuple or len(absent) > index.MAX_DEPENDENCIES):
        raise PageOverlaysUnavailable('invalid_overlay_bounds')
    owned, maps, used = {}, {}, 0
    targets = {r.key.id for r in inputs.rows + inputs.exact_rows}
    for doc in docs:
        if type(doc) is not index.DocumentSummary:
            raise PageOverlaysUnavailable('invalid_document_summary')
        kind, actor = index._doc_path(doc.path, position.chat_id)
        if (doc.kind, doc.actor) != (kind, actor) or doc.path in owned:
            raise PageOverlaysUnavailable('inconsistent_document_summary')
        shape = index._shape(doc.shape)
        if type(doc.empty) is not bool or type(doc.has_signature) is not bool:
            raise PageOverlaysUnavailable('invalid_document_flags')
        index._text(doc.shape_error, 'shape error', 64, empty=True)
        index._text(doc.scalars_json, 'scalars', 65536)
        used += 5 + sum(len(v.encode()) for v in (doc.path, kind, actor, doc.shape_error, doc.scalars_json))
        if used > index.MAX_SELECT_BYTES:
            raise OverflowError('overlay input byte budget exceeded')
        scalars = json.loads(doc.scalars_json)
        if type(scalars) is not dict:
            raise PageOverlaysUnavailable('invalid_state_scalars')
        owned[doc.path] = (kind, actor, doc.empty, shape, scalars)
    keys = set()
    for item in candidates:
        if type(item) is not index.OverlayCandidate or item.path not in owned:
            raise PageOverlaysUnavailable('candidate_document_missing')
        index._text(item.target, 'target')
        index._text(item.value, 'value', 4096, empty=True)
        key = (item.path, item.kind, item.target)
        if item.target not in targets or key in keys:
            raise PageOverlaysUnavailable('invalid_candidate_target')
        keys.add(key)
        if item.kind not in (('reaction',) if owned[item.path][0] == 'reactions' else ('hidden', 'starred')):
            raise PageOverlaysUnavailable('invalid_candidate_kind')
        used += sum(len(v.encode()) for v in (*key, item.value))
        maps.setdefault(item.path, {}).setdefault(item.kind, {})[item.target] = item.value
    absences = set()
    for path in absent:
        if index._doc_path(path, position.chat_id)[0] != 'state' or path in owned or path in absences:
            raise PageOverlaysUnavailable('invalid_state_absence')
        used += len(path.encode())
        absences.add(path)
    proof_map = {}
    if type(inputs.proofs) is not tuple or len(inputs.proofs) > index.MAX_DEPENDENCIES:
        raise PageOverlaysUnavailable('invalid_proof_bounds')
    for proof in inputs.proofs:
        if type(proof) is not tuple or len(proof) != 3:
            raise PageOverlaysUnavailable('invalid_proof')
        path, pub, value = proof
        if path not in owned or (value is not None and type(value) is not bool):
            raise PageOverlaysUnavailable('invalid_proof_document')
        key = (path, index._key(pub))
        if key in proof_map:
            raise PageOverlaysUnavailable('duplicate_proof')
        proof_map[key] = value
        used += len(path.encode()) + len(pub.encode())
    if used > index.MAX_SELECT_BYTES:
        raise OverflowError('overlay input byte budget exceeded')
    return inputs.position, owned, maps, absences, proof_map


def assemble_page_overlays(inputs: RawPageInputs, viewer: str, snapshot: ChatSnapshot,
                           *, directory=None, crypto_boundary=True) -> TranscriptOverlays:
    """Assemble only transcript-target reactions, hidden/starred IDs and cuts.

    Missing evidence requests background verification; no whole-map foreground
    fallback. Rerun this function after recapture, so keys are freshly resolved.
    The supplied membership snapshot is a request input, not continuing authority.
    """
    position, docs, maps, absent, proofs = _detached(inputs)
    index._text(viewer, 'viewer', 256)
    if type(snapshot) is not ChatSnapshot:
        raise PageOverlaysUnavailable('inconsistent_membership_input')
    chat, member_input, tenure_input = snapshot.id, snapshot.members, snapshot.tenure
    if chat != position.overlays.chat_id:
        raise PageOverlaysUnavailable('inconsistent_membership_input')
    if type(member_input) is not dict or type(tenure_input) is not dict:
        raise PageOverlaysUnavailable('invalid_membership_input')
    # Copy only bounded actor facts; never copy a history-sized tenure map.
    actors = {viewer} | {doc[1] for doc in docs.values()}
    members = frozenset(actor for actor in actors if actor in member_input)
    prior_members = frozenset(actor for actor in actors if actor in tenure_input)
    if viewer not in members:
        raise PageOverlaysUnavailable('viewer_not_member')
    if type(crypto_boundary) is not bool or (crypto_boundary and directory is None):
        raise ValueError('invalid crypto boundary')
    state_path = P.state(chat, viewer)
    if state_path not in docs and state_path not in absent:
        raise PageOverlaysUnavailable('viewer_state_not_captured')
    pending, dependencies, verified = [], [], {}

    def current_key(actor):
        pub = directory.sign_pub(actor)  # preserve pin/lifecycle failures
        # Do not validate before legacy signature/member short-circuits. None
        # records an opaque ignored value, not proof of a stable authority cut;
        # final validation must rerun current resolution, never compare it alone.
        recorded = pub if type(pub) is str and len(pub) <= 128 else None
        dependencies.append((actor, recorded))
        return pub

    def verification_key(pub, shape):
        # Signing-byte construction precedes the primitive. Inside the primitive
        # public-key decoding precedes signature decoding, including bad types.
        if not shape.signing_available:
            raise PageOverlaysUnavailable('malformed_gated_signing_input')
        if type(pub) is not str:
            raise PageOverlaysUnavailable('malformed_gated_public_key')
        try:
            index._text(pub, 'current public key', 128)
        except ValueError as exc:
            raise PageOverlaysUnavailable('unrepresentable_public_key') from exc
        try:
            key = crypto.b64d(pub)
        except ValueError:
            return None
        if len(key) != 32:
            return None
        if not shape.signature_string:
            raise PageOverlaysUnavailable('malformed_gated_signing_input')
        return key

    def accepted(path, pub, key):
        if key is None:
            return False
        value = proofs.get((path, key))
        if value is None:
            pending.append((path, pub))
            return False
        return value

    for path in sorted(docs):
        kind, actor, _empty, shape, _scalars = docs[path]
        if kind != 'reactions' or not shape.is_dict:
            continue
        selected = maps.get(path, {}).get('reaction', {})
        if crypto_boundary:
            pub = current_key(actor)
            if not shape.signature_truthy or not pub or not (actor in members or actor in prior_members):
                continue
            key = verification_key(pub, shape)
            if not selected or not accepted(path, pub, key):
                continue
        verified[actor] = selected

    state = {}
    if state_path in docs:
        _kind, actor, empty, shape, scalars = docs[state_path]
        allow = shape.is_dict and not empty
        if allow and crypto_boundary:
            pub = current_key(actor)
            allow = bool(pub and shape.signature_truthy) and accepted(
                state_path, pub, verification_key(pub, shape))
        if allow:
            if not shape.viewer_ids_compatible:
                raise PageOverlaysUnavailable('viewer_ids_not_representable')
            # These are transcript fields only, never the public my_state schema.
            state = {key: value for key, value in scalars.items() if key in ('cleared', 'deleted')}
            candidates = maps.get(state_path, {})
            state['hidden'] = list(candidates.get('hidden', {}))
            state['starred'] = list(candidates.get('starred', {}))
    if pending:
        raise PageOverlayProofsPending(pending)
    return TranscriptOverlays(position, fold_reactions(verified), state, tuple(dependencies))
