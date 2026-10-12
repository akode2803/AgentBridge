"""Bounded reaction-document v2 facts; these are not event acceptance proofs."""
from __future__ import annotations

import json
import hashlib
import re
import secrets
from dataclasses import dataclass


MAX_LATEST_OPERATIONS = 128
MAX_MAPPING_ENTRIES = 4096
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_NS = 2**63 - 1
_OPS_ORIGINS = frozenset({'native', 'resealed'})
_STATE_BOUND_ID = re.compile(
    r'^rxs-(?P<ns>[1-9][0-9]{0,18})-(?P<nonce>[0-9a-f]{8})-'
    r'(?P<digest>[0-9a-f]{64})$'
)


@dataclass(frozen=True)
class ReactionOperation:
    id: str
    ns: int
    emoji: str
    previous: str | None

    def to_dict(self) -> dict:
        return {"id": self.id, "ns": self.ns, "emoji": self.emoji,
                "previous": self.previous}


def state_bound_operation_id(ns: int, state: dict) -> str:
    """Mint an opaque operation ID bound to a same-cut signed state checkpoint."""
    if (type(ns) is not int or not 1 <= ns <= MAX_NS or type(state) is not dict
            or state.get('ns') != ns or not _text(state.get('sig'), 4096)):
        raise ValueError('invalid reaction state checkpoint')
    digest = hashlib.sha256(f"{ns}|{state['sig']}".encode()).hexdigest()
    return f'rxs-{ns}-{secrets.token_hex(4)}-{digest}'


def matches_state_checkpoint(operation: ReactionOperation, state: dict) -> bool:
    """Require the current signed state to be the operation's exact checkpoint."""
    if type(operation) is not ReactionOperation or type(state) is not dict:
        return False
    match = _STATE_BOUND_ID.fullmatch(operation.id)
    if match is None or int(match['ns']) != operation.ns \
            or state.get('ns') != operation.ns or not _text(state.get('sig'), 4096):
        return False
    digest = hashlib.sha256(
        f"{operation.ns}|{state['sig']}".encode()
    ).hexdigest()
    return secrets.compare_digest(match['digest'], digest)


def _text(value, limit: int, *, empty: bool = False) -> bool:
    if type(value) is not str or (not value and not empty):
        return False
    try:
        return len(value.encode('utf-8')) <= limit
    except UnicodeEncodeError:
        return False


def is_v2_candidate(doc) -> bool:
    return type(doc) is dict and (
        doc.get('schema') == 2 or ('v' in doc and 'ops' in doc)
    )


def parse_v2(doc: dict) -> tuple[dict[str, str], dict[str, ReactionOperation]]:
    """Reject partial or contradictory v2 documents before any field is used."""
    fields = set(doc) if type(doc) is dict else set()
    if (type(doc) is not dict or fields not in (
                {'schema', 'v', 'ops', 'ops_origin', 'ns', 'sig'},
                {'schema', 'v', 'ops', 'ops_origin', 'ns', 'sig', 'ops_sig'},
            )
            or type(doc['schema']) is not int or doc['schema'] != 2
            or type(doc['ops_origin']) is not str or doc['ops_origin'] not in _OPS_ORIGINS
            or type(doc['ns']) is not int or not 1 <= doc['ns'] <= MAX_NS
            or not _text(doc['sig'], 4096, empty=True)
            or 'ops_sig' in doc and not _text(doc['ops_sig'], 4096, empty=True)
            or type(doc['v']) is not dict or type(doc['ops']) is not dict
            or len(doc['v']) > MAX_MAPPING_ENTRIES
            or not 1 <= len(doc['ops']) <= MAX_LATEST_OPERATIONS):
        raise ValueError('invalid reaction v2 document')
    mapping = {}
    for target, emoji in doc['v'].items():
        if not _text(target, 4096) or not _text(emoji, 128):
            raise ValueError('invalid reaction v2 mapping')
        mapping[target] = emoji
    operations = {}
    ids = set()
    namespaces = set()
    for target, value in doc['ops'].items():
        if (not _text(target, 4096) or type(value) is not dict
                or set(value) != {'id', 'ns', 'emoji', 'previous'}
                or not _text(value['id'], 128)
                or type(value['ns']) is not int
                or not 1 <= value['ns'] <= doc['ns']
                or not _text(value['emoji'], 128, empty=True)
                or value['previous'] is not None
                and not _text(value['previous'], 128)
                or value['previous'] == value['id']
                or value['id'] in ids or value['ns'] in namespaces):
            raise ValueError('invalid reaction v2 operation')
        if (value['emoji'] and mapping.get(target) != value['emoji']
                or not value['emoji'] and target in mapping):
            raise ValueError('reaction operation disagrees with mapping')
        ids.add(value['id'])
        namespaces.add(value['ns'])
        operations[target] = ReactionOperation(**value)
    if max(namespaces) != doc['ns']:
        raise ValueError('reaction v2 namespace disagrees with operations')
    if any(op.previous in ids for op in operations.values()):
        raise ValueError('reaction operation links a current target')
    body = json.dumps({'schema': 2, 'v': mapping, 'ops': doc['ops'],
                       'ops_origin': doc['ops_origin'], 'ns': doc['ns']},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    try:
        if len(body.encode('utf-8')) > MAX_DOCUMENT_BYTES:
            raise ValueError('reaction v2 document exceeds budget')
    except UnicodeEncodeError as exc:
        raise ValueError('invalid reaction v2 UTF-8') from exc
    return mapping, operations


def parse_native_v2(doc: dict) -> tuple[dict[str, str], dict[str, ReactionOperation]]:
    """Require structurally valid native ops; caller must verify the signature."""
    result = parse_v2(doc)
    if doc['ops_origin'] != 'native':
        raise ValueError('reaction operations are not native')
    if 'ops_sig' in doc and bool(doc['sig']) != bool(doc['ops_sig']):
        raise ValueError('reaction v2 signatures are incomplete')
    return result


def signing_bytes(chat_id: str, user: str, doc: dict) -> bytes:
    mapping, operations = parse_v2(doc)
    payload = json.dumps({
        'domain': 'react2', 'chat': chat_id, 'user': user,
        'document': {
            'schema': 2, 'v': mapping,
            'ops': {target: op.to_dict() for target, op in operations.items()},
            'ops_origin': doc['ops_origin'], 'ns': doc['ns'],
            # Dual-signature documents keep ``sig`` compatible with v1
            # readers. Bind that compatibility signature into the v2 proof so
            # a transport writer cannot selectively break old clients.
            **({'legacy_sig': doc['sig']} if 'ops_sig' in doc else {}),
        },
    }, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return payload.encode('utf-8')


def operation_signature(doc: dict) -> str:
    """Return the signature covering v2 operation evidence.

    The six-field shape is the short-lived pre-compatibility v2 form. New
    documents reserve ``sig`` for the legacy mapping recipe and carry the
    complete v2 proof in ``ops_sig``.
    """
    parse_v2(doc)
    if 'ops_sig' in doc and bool(doc['sig']) != bool(doc['ops_sig']):
        raise ValueError('reaction v2 signatures are incomplete')
    return doc['ops_sig'] if 'ops_sig' in doc else doc['sig']


__all__ = [
    'MAX_LATEST_OPERATIONS', 'MAX_MAPPING_ENTRIES', 'ReactionOperation',
    'is_v2_candidate', 'matches_state_checkpoint', 'operation_signature',
    'parse_v2', 'parse_native_v2', 'signing_bytes', 'state_bound_operation_id',
]
