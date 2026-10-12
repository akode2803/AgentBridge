"""Bounded reaction-document v2 facts; these are not event acceptance proofs."""
from __future__ import annotations

import json
from dataclasses import dataclass


MAX_LATEST_OPERATIONS = 128
MAX_MAPPING_ENTRIES = 4096
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_NS = 2**63 - 1
_OPS_ORIGINS = frozenset({'native', 'resealed'})


@dataclass(frozen=True)
class ReactionOperation:
    id: str
    ns: int
    emoji: str
    previous: str | None

    def to_dict(self) -> dict:
        return {"id": self.id, "ns": self.ns, "emoji": self.emoji,
                "previous": self.previous}


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
    if (type(doc) is not dict or set(doc) != {'schema', 'v', 'ops', 'ops_origin', 'ns', 'sig'}
            or type(doc['schema']) is not int or doc['schema'] != 2
            or type(doc['ops_origin']) is not str or doc['ops_origin'] not in _OPS_ORIGINS
            or type(doc['ns']) is not int or not 1 <= doc['ns'] <= MAX_NS
            or not _text(doc['sig'], 4096, empty=True)
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
    return result


def signing_bytes(chat_id: str, user: str, doc: dict) -> bytes:
    mapping, operations = parse_v2(doc)
    payload = json.dumps({
        'domain': 'react2', 'chat': chat_id, 'user': user,
        'document': {
            'schema': 2, 'v': mapping,
            'ops': {target: op.to_dict() for target, op in operations.items()},
            'ops_origin': doc['ops_origin'], 'ns': doc['ns'],
        },
    }, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return payload.encode('utf-8')


__all__ = [
    'MAX_LATEST_OPERATIONS', 'ReactionOperation', 'is_v2_candidate',
    'parse_v2', 'parse_native_v2', 'signing_bytes',
]
