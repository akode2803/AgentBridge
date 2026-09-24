"""Pure bounded receipt tiers from separately captured, current-cut inputs.

This module does not authorize a viewer, read a provider, or retain evidence.
The caller owns current membership/key resolution and the final chat/presence
source comparison before publishing these request-local results.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .. import crypto
from ..core.models import Message, MsgKind, ReceiptState
from ..store import overlay_index as index
from ..store.page_inputs import RawPageInputs
from .page_overlays import PageOverlayProofsPending, PageOverlaysUnavailable
from .page_selection import _snapshot
from .paths import P

MAX_MEMBERS = 64
MAX_MESSAGES = 200


def _members(members):
    if type(members) is not tuple or len(members) > MAX_MEMBERS:
        raise ValueError('invalid receipt member budget')
    if len(set(members)) != len(members):
        raise ValueError('duplicate receipt member')
    for member in members:
        index._chat(member)
    return members


def verified_cursors(inputs: RawPageInputs, members: tuple[str, ...], directory,
                     *, crypto_boundary: bool) -> dict[str, dict[str, int]]:
    """Verify exact member state scalars once per member, with no ID maps.

    An absent, empty, non-object or invalidly signed state supplies zero.
    A pending proof instead requests off-path verification and a new capture.
    """
    members = _members(members)
    if type(crypto_boundary) is not bool or (crypto_boundary and directory is None):
        raise ValueError('invalid receipt crypto boundary')
    inputs = _snapshot(inputs)
    if inputs.rows or inputs.exact_rows or inputs.lookahead is not None or inputs.absent_ids:
        raise ValueError('receipt inputs must contain only member state')
    selected = inputs.indexed
    if (type(selected) is not index.IndexedOverlayInputs
            or selected.reactions_complete is not False
            or selected.candidates or len(selected.documents) > MAX_MEMBERS
            or len(selected.absent_states) > MAX_MEMBERS):
        raise PageOverlaysUnavailable('invalid exact receipt state capture')
    position = index._wanted(selected.position, Path(inputs.position.messages.database_path))
    if position != inputs.position.overlays or inputs.position.messages.chat_id != position.chat_id:
        raise PageOverlaysUnavailable('receipt state position changed')
    expected = {P.state(position.chat_id, member): member for member in members}
    documents, absent = {}, set()
    for doc in selected.documents:
        if type(doc) is not index.DocumentSummary or doc.path not in expected or doc.path in documents:
            raise PageOverlaysUnavailable('unexpected receipt state document')
        if (doc.kind, doc.actor) != ('state', expected[doc.path]) or type(doc.empty) is not bool \
                or type(doc.has_signature) is not bool:
            raise PageOverlaysUnavailable('invalid receipt state document')
        shape = index._shape(doc.shape)
        index._text(doc.shape_error, 'receipt shape', 64, empty=True)
        index._text(doc.scalars_json, 'receipt scalars', 65536)
        scalars = json.loads(doc.scalars_json)
        if type(scalars) is not dict:
            raise PageOverlaysUnavailable('invalid receipt scalars')
        documents[doc.path] = (doc.empty, shape, scalars)
    for path in selected.absent_states:
        if path not in expected or path in documents or path in absent:
            raise PageOverlaysUnavailable('invalid receipt state absence')
        absent.add(path)
    if set(expected) != set(documents) | absent:
        raise PageOverlaysUnavailable('incomplete receipt state capture')
    if type(inputs.proofs) is not tuple or len(inputs.proofs) > MAX_MEMBERS:
        raise PageOverlaysUnavailable('invalid receipt proofs')
    proofs = {}
    for row in inputs.proofs:
        if type(row) is not tuple or len(row) != 3:
            raise PageOverlaysUnavailable('invalid receipt proof')
        path, pub, value = row
        if path not in documents or (value is not None and type(value) is not bool):
            raise PageOverlaysUnavailable('foreign receipt proof')
        key = (path, index._key(pub))
        if key in proofs:
            raise PageOverlaysUnavailable('duplicate receipt proof')
        proofs[key] = value
    pending, cursors = [], {}
    for member in members:
        path = P.state(position.chat_id, member)
        read = delivered = 0
        if path in documents:
            empty, shape, scalars = documents[path]
            accepted = shape.is_dict and not empty
            if accepted and crypto_boundary:
                pub = directory.sign_pub(member)
                accepted = bool(pub and shape.signature_truthy)
                if accepted:
                    if not shape.signing_available or not shape.signature_string:
                        raise PageOverlaysUnavailable('malformed gated receipt signing input')
                    if type(pub) is not str:
                        raise PageOverlaysUnavailable('malformed current receipt key')
                    index._text(pub, 'current receipt key', 128)
                    try:
                        key = crypto.b64d(pub)
                    except ValueError:
                        accepted = False
                    else:
                        if len(key) != 32:
                            accepted = False
                        elif proofs.get((path, key)) is None:
                            pending.append((path, pub))
                            accepted = False
                        else:
                            accepted = proofs[path, key]
            if accepted:
                try:
                    read = int(scalars.get('read_ns', 0))
                    delivered = int(scalars.get('delivered_ns', 0))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise PageOverlaysUnavailable('invalid receipt cursor') from exc
        cursors[member] = {'read_ns': read, 'delivered_ns': delivered}
    if pending:
        raise PageOverlayProofsPending(pending)
    return cursors


def receipt_visibility(viewer: str, members: tuple[str, ...], directory) -> dict[str, bool]:
    """Resolve both existing receipt toggles via current account lookups."""
    index._chat(viewer)
    members = _members(members)
    account = directory.get(viewer)
    may_view = bool(account and account.privacy.view_read_receipts)
    result = {}
    for member in members:
        other = directory.get(member)  # Capture each current account even if viewer opts out.
        result[member] = bool(may_view and other and other.privacy.read_receipts)
    return result


def assemble_receipts(messages: tuple[Message, ...] | list[Message], viewer: str,
                      members: tuple[str, ...], cursors: dict,
                      visibility: dict, presence_floors: dict) -> dict[str, dict]:
    """Exact legacy receipt ladder for selected own messages, sans transport."""
    index._chat(viewer)
    members = _members(members)
    if (type(messages) not in (tuple, list) or len(messages) > MAX_MESSAGES
            or type(cursors) is not dict or type(visibility) is not dict
            or type(presence_floors) is not dict
            or any(set(mapping) != set(members)
                   for mapping in (cursors, visibility, presence_floors))):
        raise ValueError('incomplete receipt inputs')
    for member in members:
        cursor = cursors[member]
        if (type(cursor) is not dict or set(cursor) != {'read_ns', 'delivered_ns'}
                or any(type(v) is not int for v in cursor.values())
                or type(visibility[member]) is not bool
                or type(presence_floors[member]) not in (int, float)
                or not math.isfinite(presence_floors[member])
                or presence_floors[member] < 0):
            raise ValueError('invalid receipt member input')
    out, seen, chat = {}, set(), None
    for message in messages:
        if (type(message) is not Message or type(message.id) is not str
                or not message.id or message.id in seen or type(message.ns) is not int
                or type(message.chat_id) is not str
                or not message.chat_id or (chat is not None and message.chat_id != chat)):
            raise ValueError('invalid selected receipt message')
        chat = message.chat_id
        seen.add(message.id)
        if message.from_ != viewer or message.kind is not MsgKind.MESSAGE or message.deleted:
            continue
        if not members:
            out[message.id] = {'state': ReceiptState.READ.value, 'read_by': [],
                               'delivered_to': [], 'pending': [], 'total': 0}
            continue
        read_by, delivered_to, pending = [], [], []
        for member in members:
            cursor = cursors[member]
            if not visibility[member]:
                pending.append(member)
            elif cursor['read_ns'] >= message.ns:
                read_by.append(member)
            elif max(cursor['delivered_ns'], presence_floors[member]) >= message.ns:
                delivered_to.append(member)
            else:
                pending.append(member)
        state = (ReceiptState.SENT.value if pending else ReceiptState.DELIVERED.value
                 if delivered_to else ReceiptState.READ.value)
        out[message.id] = {'state': state, 'read_by': sorted(read_by),
                           'delivered_to': sorted(delivered_to), 'pending': sorted(pending),
                           'total': len(members)}
    return out
