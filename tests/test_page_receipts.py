"""Exact member state proofs and the bounded legacy receipt ladder."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentbridge import crypto
from agentbridge.core.models import Message, MsgKind
from agentbridge.mesh.events import state_signing_bytes
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_overlays import PageOverlayProofsPending
from agentbridge.mesh.page_receipts import (
    assemble_receipts, receipt_visibility, verified_cursors,
)
from agentbridge.mesh.paths import P
from agentbridge.store.db import Store
from agentbridge.store.page_inputs import MessageKey, SerializedMessage


CHAT = 'room'


class Directory:
    def __init__(self, keys, privacy=None):
        self.keys = keys
        self.privacy = privacy or {}
        self.keys_read = []
        self.accounts_read = []

    def sign_pub(self, name):
        self.keys_read.append(name)
        return self.keys.get(name)

    def get(self, name):
        self.accounts_read.append(name)
        prefs = self.privacy.get(name)
        return (SimpleNamespace(privacy=SimpleNamespace(**prefs))
                if prefs is not None else None)


def _state(bundle, user, *, read=0, delivered=0, sign_as=None):
    fields = {'read_ns': read, 'delivered_ns': delivered,
              'hidden': [f'other-{n}' for n in range(16)]}
    return {**fields, 'ns': 20, 'sig': crypto.sign(
        sign_as or bundle, state_signing_bytes(CHAT, user, 20, fields),
    )}


def _capture(tmp_path, documents, members, proofs=()):
    store = Store(tmp_path / 'store.sqlite')
    store.prepare_page_input_index()
    source = store.publish_document_batch(
        store.capture_document_position('member-source'), documents,
        cursor=1, full=True, retain_tombstones=False,
    )
    index = store.publish_overlay_index(prepare_overlay_index(
        store.capture_document_observation(source.source_id), CHAT,
    ))
    for path, pub in proofs:
        store.verify_overlay_signature(index, path, pub)
    inputs = store.capture_page_inputs(index, raw_limit=0,
                                       state_paths=tuple(P.state(CHAT, m) for m in members),
                                       proof_keys=tuple(proofs), include_reactions=False)
    return store, index, inputs


def _message(ident, ns, sender='viewer', *, deleted=False, kind=MsgKind.MESSAGE):
    return Message(id=ident, chat_id=CHAT, from_=sender, ns=ns,
                   kind=kind, body=ident, deleted=deleted)


def test_verified_member_cursors_exact_keys_invalid_signature_and_absence(tmp_path):
    bob, carol, wrong = (crypto.generate_identity() for _ in range(3))
    keys = {name: crypto.identity_pubs(bundle)[0]
            for name, bundle in [('bob', bob), ('carol', carol)]}
    docs = {
        P.state(CHAT, 'bob'): _state(bob, 'bob', read=25, delivered=35),
        P.state(CHAT, 'carol'): _state(carol, 'carol', read=99, sign_as=wrong),
        P.state(CHAT, 'dave'): ['not an object'],
    }
    members = ('bob', 'carol', 'dave', 'erica')
    proofs = tuple((P.state(CHAT, name), keys[name]) for name in ('bob', 'carol'))
    store, _index, inputs = _capture(tmp_path, docs, members, proofs)
    try:
        directory = Directory(keys)
        assert inputs.rows == inputs.exact_rows == ()
        assert not inputs.indexed.reactions_complete
        assert inputs.indexed.candidates == ()  # No hidden/starred ID map copy.
        assert verified_cursors(inputs, members, directory, crypto_boundary=True) == {
            'bob': {'read_ns': 25, 'delivered_ns': 35},
            'carol': {'read_ns': 0, 'delivered_ns': 0},
            'dave': {'read_ns': 0, 'delivered_ns': 0},
            'erica': {'read_ns': 0, 'delivered_ns': 0},
        }
        assert directory.keys_read == ['bob', 'carol']
    finally:
        store.close()


def test_missing_exact_signature_proof_requests_background_verification(tmp_path):
    bob = crypto.generate_identity()
    pub = crypto.identity_pubs(bob)[0]
    store, index, inputs = _capture(tmp_path, {P.state(CHAT, 'bob'): _state(bob, 'bob', read=50)},
                                    ('bob',))
    try:
        with pytest.raises(PageOverlayProofsPending) as pending:
            verified_cursors(inputs, ('bob',), Directory({'bob': pub}), crypto_boundary=True)
        assert pending.value.keys == ((P.state(CHAT, 'bob'), pub),)
        with pytest.raises(ValueError, match='only member state'):
            verified_cursors(replace(inputs, rows=(SerializedMessage(
                MessageKey(1, 'viewer', 'unexpected'), 'message',
                '{"id":"unexpected","ns":1,"from":"viewer"}'),)),
                ('bob',), Directory({'bob': pub}), crypto_boundary=True)
        store.verify_overlay_signature(index, P.state(CHAT, 'bob'), pub)
        recaptured = store.capture_page_inputs(index, raw_limit=0,
                                               state_paths=(P.state(CHAT, 'bob'),),
                                               proof_keys=((P.state(CHAT, 'bob'), pub),),
                                               include_reactions=False)
        assert verified_cursors(recaptured, ('bob',), Directory({'bob': pub}),
                                crypto_boundary=True)['bob']['read_ns'] == 50
    finally:
        store.close()


def test_plaintext_nondict_state_is_absent_and_valid_state_retains_cursors(tmp_path):
    store, _index, inputs = _capture(
        tmp_path, {P.state(CHAT, 'bob'): _state(None, 'bob', read=7, delivered=9,
                                                sign_as=crypto.generate_identity()),
                   P.state(CHAT, 'carol'): ['not a state']}, ('bob', 'carol'),
    )
    try:
        assert verified_cursors(inputs, ('bob', 'carol'), None, crypto_boundary=False) == {
            'bob': {'read_ns': 7, 'delivered_ns': 9},
            'carol': {'read_ns': 0, 'delivered_ns': 0},
        }
    finally:
        store.close()


def test_mixed_group_lowest_tier_sorted_privacy_and_presence_floor():
    members = ('zeta', 'alpha', 'beta')
    directory = Directory({}, {
        'viewer': {'view_read_receipts': True},
        'zeta': {'read_receipts': True},
        'alpha': {'read_receipts': True},
        'beta': {'read_receipts': False},
    })
    allowed = receipt_visibility('viewer', members, directory)
    assert allowed == {'zeta': True, 'alpha': True, 'beta': False}
    assert directory.accounts_read == ['viewer', *members]
    cursors = {'zeta': {'read_ns': 12, 'delivered_ns': 0},
               'alpha': {'read_ns': 0, 'delivered_ns': 0},
               'beta': {'read_ns': 100, 'delivered_ns': 100}}
    floors = {'zeta': 0, 'alpha': 10, 'beta': 100}
    result = assemble_receipts((_message('own', 10), _message('foreign', 10, 'alpha'),
                                _message('deleted', 10, deleted=True),
                                _message('info', 10, kind=MsgKind.INFO)),
                               'viewer', members, cursors, allowed, floors)
    assert result == {'own': {'state': 'sent', 'read_by': ['zeta'],
                             'delivered_to': ['alpha'], 'pending': ['beta'], 'total': 3}}
    cursors['alpha']['read_ns'] = 10
    assert assemble_receipts((_message('own', 10),), 'viewer', members, cursors,
                             {name: True for name in members}, floors)['own']['state'] == 'read'


def test_self_chat_and_viewer_privacy_gate_even_with_high_presence():
    own = _message('self', 5)
    assert assemble_receipts((own,), 'viewer', (), {}, {}, {}) == {
        'self': {'state': 'read', 'read_by': [], 'delivered_to': [],
                 'pending': [], 'total': 0},
    }
    directory = Directory({}, {'viewer': {'view_read_receipts': False},
                               'bob': {'read_receipts': True}})
    visibility = receipt_visibility('viewer', ('bob',), directory)
    assert directory.accounts_read == ['viewer', 'bob']
    assert assemble_receipts((own,), 'viewer', ('bob',),
                             {'bob': {'read_ns': 5, 'delivered_ns': 5}},
                             visibility, {'bob': 10})['self']['state'] == 'sent'


def test_incomplete_member_and_selected_message_inputs_rejected():
    with pytest.raises(ValueError, match='incomplete'):
        assemble_receipts((_message('own', 1),), 'viewer', ('bob',), {}, {}, {})
    with pytest.raises(ValueError, match='invalid selected'):
        assemble_receipts((_message('same', 1), _message('same', 2)),
                          'viewer', (), {}, {}, {})
    with pytest.raises(ValueError, match='budget'):
        assemble_receipts((), 'viewer', tuple(f'user{n}' for n in range(65)),
                          {}, {}, {})


def test_legacy_negative_cursor_zero_ns_and_fractional_presence_floor():
    message = _message('zero', 0)
    result = assemble_receipts((message,), 'viewer', ('bob',),
                               {'bob': {'read_ns': -1, 'delivered_ns': -1}},
                               {'bob': True}, {'bob': 0.5})
    assert result['zero']['state'] == 'delivered'
    with pytest.raises(ValueError, match='invalid receipt member'):
        assemble_receipts((message,), 'viewer', ('bob',),
                          {'bob': {'read_ns': 0, 'delivered_ns': 0}},
                          {'bob': True}, {'bob': float('nan')})


def test_explicit_delivered_cursor_and_sorted_multi_reader_tiers():
    members = ('zoe', 'amy', 'bob')
    cursors = {'zoe': {'read_ns': 10, 'delivered_ns': 0},
               'amy': {'read_ns': 11, 'delivered_ns': 0},
               'bob': {'read_ns': 0, 'delivered_ns': 13}}
    outcome = assemble_receipts((_message('own', 10),), 'viewer', members, cursors,
                                dict.fromkeys(members, True), dict.fromkeys(members, 0))
    assert outcome['own'] == {'state': 'delivered', 'read_by': ['amy', 'zoe'],
                              'delivered_to': ['bob'], 'pending': [], 'total': 3}
