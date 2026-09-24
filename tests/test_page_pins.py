"""Pin banners resolve bounded exact targets through the canonical page cut."""
from __future__ import annotations

import json
import time

import pytest

from agentbridge import crypto
from agentbridge.mesh.events import pin_signing_bytes
from agentbridge.mesh.page_operation import PageOperation
from agentbridge.mesh.paths import P
from agentbridge.transport import raw_documents

import test_local_page_operation as local_test


_inputs = local_test._inputs
_prepare = local_test._prepare
base_world = local_test.world


@pytest.fixture
def world(base_world):
    return base_world


def _message_ids(mesh, chat):
    return [m['id'] for m in mesh.store.messages(chat) if m.get('kind') == 'message']


def _run(world, *, limit=5):
    mesh, _provider, _root, reader, _publisher, chat, _encrypted = world
    inputs = _inputs(world)
    operation = PageOperation(mesh, chat, source_reader=reader, limit=limit)
    prepared, history = _prepare(operation, inputs, mesh)
    assert prepared.status == 'prepared', history
    finalized = prepared.prepared.finalize()
    assert finalized.status == 'page', finalized
    presentation = finalized.result.presentation
    assert presentation is not None and presentation.pins_json is not None
    return finalized, json.loads(presentation.pins_json)


def test_old_pinned_target_resolves_canonical_body_outside_selected_page(world):
    mesh, _provider, _root, _reader, _publisher, chat, _encrypted = world
    old = _message_ids(mesh, chat)[0]
    mesh.messaging.pin(chat, old)
    mesh.messaging.edit(chat, old, 'updated old pin')
    finalized, pins = _run(world, limit=5)
    assert old not in {m.id for m in finalized.result.page.messages}
    assert [(p['id'], p['body']) for p in pins] == [(old, 'updated old pin')]
    assert pins[0]['ns'] > 0 and pins[0]['until'] == 0


@pytest.mark.parametrize('cut', ['hide', 'clear', 'redact'])
def test_hidden_cleared_or_redacted_pin_target_does_not_render(world, cut):
    mesh, _provider, _root, _reader, _publisher, chat, _encrypted = world
    old = _message_ids(mesh, chat)[0]
    mesh.messaging.pin(chat, old)
    if cut == 'hide':
        mesh.messaging.hide(chat, [old])
    elif cut == 'clear':
        mesh.messaging.clear_chat(chat)
    else:
        mesh.messaging.redact(chat, [old])
    _finalized, pins = _run(world)
    assert pins == []


def test_expired_pin_is_omitted_without_lazy_cleanup(world):
    mesh, provider, _root, _reader, _publisher, chat, encrypted = world
    old = _message_ids(mesh, chat)[0]
    mesh.messaging.pin(chat, old)
    doc = provider.get_doc(P.pin(chat, old))
    doc['until_ns'] = 1
    if encrypted:
        doc['sig'] = crypto.sign(mesh.keystore.load(mesh.user), pin_signing_bytes(
            chat, old, mesh.user, doc['ns'], 1))
    provider.put_doc(P.pin(chat, old), doc)
    _finalized, pins = _run(world)
    assert pins == [] and provider.get_doc(P.pin(chat, old)) is not None


def test_bad_signature_and_never_member_pinner_are_ignored_in_encrypted_room(world):
    mesh, provider, _root, _reader, _publisher, chat, encrypted = world
    if not encrypted:
        pytest.skip('plaintext pins retain presence-based legacy semantics')
    old, second = _message_ids(mesh, chat)[:2]
    mesh.messaging.pin(chat, old)
    forged = provider.get_doc(P.pin(chat, old))
    forged['sig'] = 'invalid-signature'
    provider.put_doc(P.pin(chat, old), forged)
    mesh.accounts.create_human('outsider', 'password')
    ns = time.time_ns()
    provider.put_doc(P.pin(chat, second), {
        'by': 'outsider', 'ns': ns,
        'sig': crypto.sign(mesh.keystore.load('outsider'),
                           pin_signing_bytes(chat, second, 'outsider', ns, 0)),
    })
    _finalized, pins = _run(world)
    assert pins == []


def test_pinner_in_tenure_remains_eligible_after_removal(world):
    mesh, provider, _root, _reader, _publisher, chat, encrypted = world
    if not encrypted:
        pytest.skip('cryptographic ever-member rule')
    mesh.accounts.create_human('former', 'password')
    mesh.membership.add_members(chat, ['former'])
    mesh.membership.remove_member(chat, 'former')
    old = _message_ids(mesh, chat)[0]
    ns = time.time_ns()
    provider.put_doc(P.pin(chat, old), {
        'by': 'former', 'ns': ns,
        'sig': crypto.sign(mesh.keystore.load('former'),
                           pin_signing_bytes(chat, old, 'former', ns, 0)),
    })
    _finalized, pins = _run(world)
    assert [p['id'] for p in pins] == [old]


def test_group_history_on_join_hides_prejoin_pin_target(world):
    mesh, provider, _root, _reader, _publisher, chat, _encrypted = world
    raw = [m for m in mesh.store.messages(chat) if m.get('kind') == 'message']
    old = raw[0]
    mesh.messaging.pin(chat, old['id'])
    snapshot = provider.get_doc(P.meta(chat))
    snapshot['permissions']['send_history'] = False
    snapshot['members'][mesh.user]['joined_ns'] = old['ns'] + 1
    provider.put_doc(P.meta(chat), snapshot)
    _finalized, pins = _run(world)
    assert pins == []


def test_pin_source_change_after_prepare_rejects_finalizer(world):
    mesh, provider, _root, reader, publisher, chat, _encrypted = world
    old = _message_ids(mesh, chat)[0]
    mesh.messaging.pin(chat, old)
    inputs = _inputs(world)
    prepared, history = _prepare(PageOperation(mesh, chat, source_reader=reader, limit=5),
                                 inputs, mesh)
    assert prepared.status == 'prepared', history
    mesh.messaging.unpin(chat, old)
    publisher.publish(publisher.capture(), raw_documents.collect_documents(provider, reader.definition),
                      observed_ns=time.time_ns())
    finalized = prepared.prepared.finalize()
    assert finalized.status == 'unavailable' and finalized.result is None


def test_pinned_page_prepares_without_provider_or_fullfold(world, monkeypatch):
    mesh, provider, _root, reader, _publisher, chat, _encrypted = world
    old = _message_ids(mesh, chat)[0]
    mesh.messaging.pin(chat, old)
    inputs = _inputs(world)

    def forbidden(*_args, **_kwargs):
        pytest.fail('pin path used provider/fullfold during request')

    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    monkeypatch.setattr(mesh.messaging, 'pins', forbidden)
    for name in ('get_doc', 'list_docs', 'snapshot_docs', 'read_log', 'list_logs'):
        monkeypatch.setattr(provider, name, forbidden)
    prepared, history = _prepare(PageOperation(mesh, chat, source_reader=reader, limit=5),
                                 inputs, mesh)
    assert prepared.status == 'prepared', history
    finalized = prepared.prepared.finalize()
    assert finalized.status == 'page'
    assert [pin['id'] for pin in json.loads(finalized.result.presentation.pins_json)] == [old]


def test_pin_manifest_overflow_fails_closed_without_fullfold_or_provider_read(world, monkeypatch):
    mesh, provider, _root, reader, _publisher, chat, _encrypted = world
    for i in range(65):
        provider.put_doc(P.pin(chat, f'extra-{i:02d}'), {'by': mesh.user, 'ns': i + 1})
    inputs = _inputs(world)

    def forbidden(*_args, **_kwargs):
        pytest.fail('pin path used provider/fullfold during request')

    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    monkeypatch.setattr(mesh.messaging, 'pins', forbidden)
    for name in ('get_doc', 'list_docs', 'snapshot_docs', 'read_log', 'list_logs'):
        monkeypatch.setattr(provider, name, forbidden)
    result, _history = _prepare(PageOperation(mesh, chat, source_reader=reader, limit=5), inputs, mesh)
    assert result.status == 'unavailable' and result.reason == 'inputs_unavailable'
