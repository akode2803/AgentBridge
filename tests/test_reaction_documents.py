"""Bounded reaction operation evidence and legacy document compatibility."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib

import pytest

from agentbridge.mesh.events import reaction_signing_bytes, reaction_v2_signing_bytes
from agentbridge.mesh.overlays import ChatOverlays, reaction_map
from agentbridge.mesh.paths import P
from agentbridge.mesh.reaction_documents import (
    MAX_LATEST_OPERATIONS, MAX_MAPPING_ENTRIES, MAX_NS, operation_signature,
    parse_native_v2, parse_v2,
)


class Documents:
    def __init__(self):
        self.data = {}

    def get_doc(self, path, default=None):
        return self.data.get(path, default)

    def put_doc(self, path, doc):
        self.data[path] = doc


def _signed(data):
    return hashlib.sha256(data).hexdigest()


def test_add_remove_readd_returns_immutable_distinct_linked_evidence():
    tx = Documents()
    overlays = ChatOverlays(tx, 'chat')
    add = overlays.set_reaction('alice', 'm1', '👍', signer=_signed)
    remove = overlays.set_reaction('alice', 'm1', None, signer=_signed)
    again = overlays.set_reaction('alice', 'm1', '👍', signer=_signed)
    assert len({add.id, remove.id, again.id}) == 3
    assert add.previous is None and remove.previous == add.id
    assert again.previous == remove.id
    assert add.ns < remove.ns < again.ns
    with pytest.raises(FrozenInstanceError):
        add.emoji = 'changed'
    doc = tx.get_doc(P.reactions('chat', 'alice'))
    assert doc['schema'] == 2 and doc['ops_origin'] == 'native'
    assert doc['v'] == {'m1': '👍'}
    assert parse_v2(doc)[1]['m1'] == again
    assert parse_native_v2(doc)[1]['m1'] == again
    assert doc['sig'] == _signed(reaction_signing_bytes(
        'chat', 'alice', doc['ns'], doc['v']))
    assert doc['ops_sig'] == _signed(reaction_v2_signing_bytes('chat', 'alice', doc))
    assert operation_signature(doc) == doc['ops_sig']


def test_unrelated_target_preserves_latest_operation_and_bounded_eviction():
    tx = Documents()
    overlays = ChatOverlays(tx, 'chat')
    first = overlays.set_reaction('alice', 'm0', '👍', signer=_signed)
    second = overlays.set_reaction('alice', 'm1', '👀', signer=_signed)
    doc = tx.get_doc(P.reactions('chat', 'alice'))
    assert parse_v2(doc)[1] == {'m0': first, 'm1': second}
    for index in range(2, MAX_LATEST_OPERATIONS + 1):
        overlays.set_reaction('alice', f'm{index}', '✅', signer=_signed)
    doc = tx.get_doc(P.reactions('chat', 'alice'))
    mapping, operations = parse_v2(doc)
    assert len(mapping) == MAX_LATEST_OPERATIONS + 1
    assert len(operations) == MAX_LATEST_OPERATIONS
    assert 'm0' in mapping and 'm0' not in operations
    assert 'm1' in operations
    resumed = overlays.set_reaction('alice', 'm0', '🎉', signer=_signed)
    assert resumed.previous is None  # evicted history cannot be claimed


def test_v2_strict_relations_and_signing_bind_all_fields():
    tx = Documents()
    overlays = ChatOverlays(tx, 'chat')
    overlays.set_reaction('alice', 'm1', '👍', signer=_signed)
    original = tx.get_doc(P.reactions('chat', 'alice'))
    changed = {**original, 'ops': {'m1': {**original['ops']['m1'], 'id': 'forged'}}}
    assert reaction_v2_signing_bytes('chat', 'alice', changed) != reaction_v2_signing_bytes(
        'chat', 'alice', original)
    changed_origin = {**original, 'ops_origin': 'resealed'}
    assert reaction_v2_signing_bytes('chat', 'alice', changed_origin) != (
        reaction_v2_signing_bytes('chat', 'alice', original))
    assert parse_v2(changed_origin)[1]['m1'].id == original['ops']['m1']['id']
    with pytest.raises(ValueError, match='not native'):
        parse_native_v2(changed_origin)
    for broken in (
        {**original, 'v': {}},
        {**original, 'ops': {'m1': {**original['ops']['m1'], 'emoji': ''}}},
        {**original, 'ops': {'m1': {**original['ops']['m1'], 'ns': True}}},
        {**original, 'ops': {'m1': {**original['ops']['m1'], 'id': '\ud800'}}},
        {**original, 'ops_origin': 'invented'},
    ):
        with pytest.raises(ValueError):
            parse_v2(broken)
        assert reaction_map(broken) == {}


def test_forged_v2_is_replaced_without_laundering_or_lockout():
    tx = Documents()
    path = P.reactions('chat', 'alice')
    overlays = ChatOverlays(tx, 'chat')
    overlays.set_reaction('alice', 'old', '👍', signer=_signed)
    genuine = tx.get_doc(path)
    tx.put_doc(path, {**genuine, 'v': {'forged': '👀'}})

    operation = overlays.set_reaction('alice', 'fresh', '✅', signer=_signed)

    written = tx.get_doc(path)
    assert written['v'] == {'fresh': '✅'}
    assert written['ops'] == {'fresh': operation.to_dict()}
    assert written['ops_sig'] == _signed(reaction_v2_signing_bytes(
        'chat', 'alice', written))


@pytest.mark.parametrize('forged', (
    {
        'schema': 2, 'v': {'forged': '👀'}, 'ops_origin': 'native',
        'ops': {'forged': {'id': 'foreign-operation', 'ns': MAX_NS,
                           'emoji': '👀', 'previous': None}},
        'ns': MAX_NS, 'sig': 'foreign', 'ops_sig': 'foreign',
    },
    {'v': {'forged': '👀'}, 'ns': MAX_NS, 'sig': 'foreign'},
))
def test_forged_namespace_cannot_exhaust_genuine_reaction_writes(forged):
    tx = Documents()
    path = P.reactions('chat', 'alice')
    tx.put_doc(path, forged)

    operation = ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'fresh', '✅', signer=_signed)

    written = tx.get_doc(path)
    assert operation.ns == written['ns'] < MAX_NS
    assert written['v'] == {'fresh': '✅'}
    assert written['ops'] == {'fresh': operation.to_dict()}


@pytest.mark.parametrize('forged', (
    {'m-old': '👍'},
    {'v': {'m-old': '👍'}, 'ns': 7, 'sig': 'foreign'},
))
def test_unverified_v1_mapping_is_not_laundered(forged):
    tx = Documents()
    path = P.reactions('chat', 'alice')
    tx.put_doc(path, forged)

    ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'fresh', '✅', signer=_signed)

    assert tx.get_doc(path)['v'] == {'fresh': '✅'}


@pytest.mark.parametrize('forged', (None, [], 'scalar', 7))
def test_non_object_reaction_document_cannot_block_genuine_write(forged):
    tx = Documents()
    path = P.reactions('chat', 'alice')
    tx.put_doc(path, forged)

    operation = ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'fresh', '✅', signer=_signed)

    written = tx.get_doc(path)
    assert written['v'] == {'fresh': '✅'}
    assert written['ops'] == {'fresh': operation.to_dict()}


@pytest.mark.parametrize('legacy', (False, True))
def test_keyless_writes_preserve_existing_plaintext_mapping(legacy):
    tx = Documents()
    path = P.reactions('chat', 'alice')
    if legacy:
        tx.put_doc(path, {'v': {'first': '👍'}, 'ns': 7, 'sig': ''})
    else:
        ChatOverlays(tx, 'chat').set_reaction(
            'alice', 'first', '👍', signer=lambda _data: '',
        )

    operation = ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'second', '✅', signer=lambda _data: '',
    )

    written = tx.get_doc(path)
    mapping, operations = parse_native_v2(written)
    assert mapping == {'first': '👍', 'second': '✅'}
    assert operations == {'second': operation}
    assert written['sig'] == written['ops_sig'] == ''


def test_v2_dual_signature_remains_visible_to_v1_readers():
    tx = Documents()
    ChatOverlays(tx, 'chat').set_reaction('alice', 'm1', '👍', signer=_signed)
    doc = tx.get_doc(P.reactions('chat', 'alice'))

    # This is the exact verification recipe used by pre-v2 readers, which
    # ignore the additional schema/operation fields.
    assert doc['sig'] == _signed(reaction_signing_bytes(
        'chat', 'alice', doc['ns'], reaction_map(doc)))
    assert doc['ops_sig'] == _signed(reaction_v2_signing_bytes(
        'chat', 'alice', doc))


def test_oversized_v1_map_migrates_deterministically_and_preserves_action():
    tx = Documents()
    path = P.reactions('chat', 'alice')
    mapping = {f'm-{index:05d}': '👍' for index in range(MAX_MAPPING_ENTRIES + 5)}
    tx.put_doc(path, {'v': mapping, 'ns': 7, 'sig': _signed(
        reaction_signing_bytes('chat', 'alice', 7, mapping))})

    operation = ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'priority-target', '🎉', signer=_signed)

    written = tx.get_doc(path)
    migrated, operations = parse_native_v2(written)
    assert len(migrated) == MAX_MAPPING_ENTRIES
    assert migrated['priority-target'] == '🎉'
    assert operations['priority-target'] == operation


def test_byte_oversized_v1_map_migrates_within_native_document_budget():
    tx = Documents()
    path = P.reactions('chat', 'alice')
    mapping = {
        f'{index:03d}-' + 'x' * 4000: '👍'
        for index in range(300)
    }
    tx.put_doc(path, {'v': mapping, 'ns': 7, 'sig': _signed(
        reaction_signing_bytes('chat', 'alice', 7, mapping))})

    operation = ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'priority-target', '🎉', signer=_signed)

    written = tx.get_doc(path)
    migrated, operations = parse_native_v2(written)
    assert len(migrated) < len(mapping)
    assert migrated['priority-target'] == '🎉'
    assert operations['priority-target'] == operation


def test_v1_bare_and_signed_shapes_keep_mapping_and_signing_recipe():
    mapping = {'m1': '👍'}
    bare = dict(mapping)
    signed = {'v': mapping, 'ns': 7, 'sig': 'legacy'}
    assert reaction_map(bare) == reaction_map(signed) == mapping
    assert reaction_signing_bytes('chat', 'alice', 7, reaction_map(signed)) == (
        b'chat|react|alice|7|{"m1":"\\ud83d\\udc4d"}'
    )
    tx = Documents()
    tx.put_doc(P.reactions('chat', 'alice'), signed)
    op = ChatOverlays(tx, 'chat').set_reaction('alice', 'm1', '👀', signer=_signed)
    assert op.previous is None
    assert tx.get_doc(P.reactions('chat', 'alice'))['schema'] == 2


def test_key_appearing_mid_write_does_not_promote_unsigned_native_ops():
    tx = Documents()
    path = P.reactions('chat', 'alice')
    untrusted = {
        'schema': 2, 'ops_origin': 'native', 'v': {'m1': '👍'},
        'ops': {'m1': {
            'id': 'forged-operation', 'ns': 7, 'emoji': '👍',
            'previous': None,
        }},
        'ns': 7, 'sig': '',
    }
    tx.put_doc(path, untrusted)
    calls = 0

    def key_appears(_data):
        nonlocal calls
        calls += 1
        return '' if calls == 1 else 'fresh-signature'

    operation = ChatOverlays(tx, 'chat').set_reaction(
        'alice', 'm1', '🎉', signer=key_appears,
    )
    written = tx.get_doc(path)
    assert calls == 3 and written['sig'] == written['ops_sig'] == 'fresh-signature'
    assert written['ops_origin'] == 'native'
    assert operation.previous is None
    assert written['ops'] == {'m1': operation.to_dict()}
