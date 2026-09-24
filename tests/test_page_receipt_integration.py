"""Canonical local page receipts across chat and independent presence cuts."""
from __future__ import annotations

import json

import pytest

from agentbridge import crypto
from agentbridge.mesh.events import state_signing_bytes
from agentbridge.mesh.page_operation import PageOperation
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.mesh.sealer import E2EESealer
from agentbridge.store import lifecycle_inputs, local_source
from agentbridge.transport.folder import FolderTransport


@pytest.fixture(params=[False, True], ids=['plain', 'encrypted'])
def world(tmp_path, request):
    provider = FolderTransport(tmp_path / 'provider')
    mesh = Mesh(provider, 'aryan', 'receipt-box', encrypt=request.param,
                home=tmp_path / 'home', local_inputs=True)
    try:
        mesh.store.prepare_terminal_observation()
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        mesh.accounts.create_human('aryan', 'password')
        mesh.accounts.create_human('bob', 'password')
        chat = mesh.membership.create_chat('Receipts', members=['bob'])
        message = mesh.post(chat.id, 'own selected message')
        mesh.outbox.flush_once()
        mesh.sync.sync_once([chat.id])
        yield mesh, provider, chat.id, message, request.param
    finally:
        mesh.close()


def _presence_runtime(mesh):
    # Runtime owns the independent registered presence source; setup never
    # imports a provider API into the page preparation path.
    return mesh.local_inputs.presence


def _ingest(mesh, chat, *, presence=True):
    mesh.local_inputs.ingest(chat)
    mesh.store.refresh_terminal_observation(
        f'{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}')
    if presence:
        _presence_runtime(mesh).ingest()
    return mesh.local_inputs.inputs(chat)


def _prepare(mesh, chat, inputs=None, *, limit=10):
    reader, receipt, index = _ingest(mesh, chat) if inputs is None else inputs
    operation = PageOperation(mesh, chat, source_reader=reader, limit=limit)
    for _ in range(12):
        value = operation.prepare(receipt, receipt, index)
        if value.status == 'work' and value.reason == 'overlay_proofs':
            for path, public in value.work:
                mesh.store.verify_overlay_signature(index, path, public)
            continue
        if value.status == 'restart':
            continue
        assert value.status == 'prepared', value
        return value, inputs or (reader, receipt, index)
    pytest.fail('local receipt page did not converge')


def _final(mesh, chat, inputs=None):
    prepared, inputs = _prepare(mesh, chat, inputs)
    final = prepared.prepared.finalize()
    assert final.status == 'page', final
    presentation = final.result.presentation
    receipts = None if presentation.receipts_json is None else json.loads(presentation.receipts_json)
    return final, receipts, inputs


def _state(mesh, chat, who, *, read_ns=0, delivered_ns=0, valid=True):
    fields = {'read_ns': read_ns, 'delivered_ns': delivered_ns}
    if isinstance(mesh.sealer, E2EESealer):
        sig = crypto.sign(mesh.keystore.load(who),
                          state_signing_bytes(chat, who, 41, fields))
        doc = {**fields, 'ns': 41, 'sig': sig if valid else 'forged-signature'}
    else:
        doc = fields
    mesh.tx.put_doc(P.state(chat, who), doc)


def _heartbeat(mesh, who, ns):
    mesh.tx.put_doc(P.presence(who, 'remote'), {
        'user': who, 'machine': 'remote', 'last_seen_ns': ns,
        'last_seen': '2026-01-01T00:00:00Z', 'online': True,
    })


def test_selected_own_receipt_ladder_matches_legacy_shape(world):
    mesh, _provider, chat, message, _encrypted = world
    finalized, sent, _inputs = _final(mesh, chat)
    assert sent[message.id] == {
        'state': 'sent', 'read_by': [], 'delivered_to': [],
        'pending': ['bob'], 'total': 1,
    }
    legacy = mesh.receipts_for(chat, messages=list(finalized.result.page.messages))[message.id]
    assert {k: v for k, v in legacy.items() if k != 'transport'} == sent[message.id]
    assert finalized.result.send_statuses.get(message.id) == legacy.get('transport')
    _heartbeat(mesh, 'bob', message.ns + 1)
    _finalized, delivered, _inputs = _final(mesh, chat)
    assert delivered[message.id] == {
        'state': 'delivered', 'read_by': [], 'delivered_to': ['bob'],
        'pending': [], 'total': 1,
    }
    _state(mesh, chat, 'bob', read_ns=message.ns + 1)
    _finalized, read, _inputs = _final(mesh, chat)
    assert read[message.id] == {
        'state': 'read', 'read_by': ['bob'], 'delivered_to': [],
        'pending': [], 'total': 1,
    }


def test_privacy_and_invalid_signature_do_not_leak_read_tier(world):
    mesh, provider, chat, message, encrypted = world
    _heartbeat(mesh, 'bob', message.ns + 1)
    _state(mesh, chat, 'bob', read_ns=message.ns + 1, valid=not encrypted)
    _finalized, receipt, _inputs = _final(mesh, chat)
    assert receipt[message.id]['state'] == ('delivered' if encrypted else 'read')

    bob = provider.get_doc(P.user('bob'))
    bob.setdefault('privacy', {})
    bob['privacy']['read_receipts'] = False
    mesh.tx.put_doc(P.user('bob'), bob)
    _finalized, gated, _inputs = _final(mesh, chat)
    assert gated[message.id]['state'] == 'sent'
    assert gated[message.id]['pending'] == ['bob']

    bob['privacy']['read_receipts'] = True
    mesh.tx.put_doc(P.user('bob'), bob)
    mesh.privacy.set_privacy({'view_read_receipts': False})
    _finalized, viewer_gated, _inputs = _final(mesh, chat)
    assert viewer_gated[message.id]['state'] == 'sent'


def test_group_lowest_tier_and_sorted_member_sets(world):
    mesh, _provider, chat, message, _encrypted = world
    mesh.accounts.create_human('carl', 'password')
    mesh.membership.add_members(chat, ['carl'])
    mesh.outbox.flush_once()
    mesh.sync.sync_once([chat])
    _heartbeat(mesh, 'bob', message.ns + 1)
    _finalized, first, _inputs = _final(mesh, chat)
    assert first[message.id] == {
        'state': 'sent', 'read_by': [], 'delivered_to': ['bob'],
        'pending': ['carl'], 'total': 2,
    }
    _state(mesh, chat, 'carl', read_ns=message.ns + 1)
    _finalized, second, _inputs = _final(mesh, chat)
    assert second[message.id] == {
        'state': 'delivered', 'read_by': ['carl'], 'delivered_to': ['bob'],
        'pending': [], 'total': 2,
    }
    _state(mesh, chat, 'bob', read_ns=message.ns + 1)
    _finalized, third, _inputs = _final(mesh, chat)
    assert third[message.id] == {
        'state': 'read', 'read_by': ['bob', 'carl'], 'delivered_to': [],
        'pending': [], 'total': 2,
    }


def test_pending_presence_serves_transcript_without_synthetic_sent(world):
    mesh, _provider, chat, message, _encrypted = world
    inputs = _ingest(mesh, chat, presence=False)
    final, receipts, _inputs = _final(mesh, chat, inputs)
    assert [m.id for m in final.result.page.messages if m.id == message.id] == [message.id]
    assert receipts is None


def test_self_chat_needs_no_presence_source(world):
    mesh, _provider, _chat, _message, _encrypted = world
    note = mesh.membership.create_self_chat()
    message = mesh.post(note.id, 'private note')
    mesh.outbox.flush_once()
    mesh.sync.sync_once([note.id])
    inputs = _ingest(mesh, note.id, presence=False)
    _finalized, receipts, _inputs = _final(mesh, note.id, inputs)
    assert receipts[message.id] == {
        'state': 'read', 'read_by': [], 'delivered_to': [], 'pending': [], 'total': 0,
    }


def test_late_presence_heartbeat_rejects_prepared_receipts_without_changing_chat_cut(world):
    mesh, _provider, chat, message, _encrypted = world
    inputs = _ingest(mesh, chat)
    prepared, _inputs = _prepare(mesh, chat, inputs)
    chat_receipt = inputs[1]
    _heartbeat(mesh, 'bob', message.ns + 1)
    # Presence retirement precedes any transport write. Chat source remains
    # usable; only this prepared companion receipt becomes stale.
    assert local_source.capture(mesh.store, chat_receipt.source.source_id) == chat_receipt.source
    result = prepared.prepared.finalize()
    assert result.status == 'unavailable' and result.result is None


def test_pending_companion_mutation_intent_rejects_final_handoff(world):
    mesh, _provider, chat, _message, _encrypted = world
    inputs = _ingest(mesh, chat)
    prepared, _inputs = _prepare(mesh, chat, inputs)
    from agentbridge.store.source_selectors import Selector

    intent = mesh.local_inputs.coordinator.begin((Selector('doc_exact', P.presence('bob', 'remote')),))
    try:
        result = prepared.prepared.finalize()
    finally:
        mesh.local_inputs.coordinator.complete(intent)
    assert result.status == 'unavailable' and result.result is None


def test_receipt_page_never_reads_provider_or_fullfold_after_ingestion(world, monkeypatch):
    mesh, provider, chat, message, _encrypted = world
    _heartbeat(mesh, 'bob', message.ns + 1)
    inputs = _ingest(mesh, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail('receipt page used provider or fullfold')

    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    for name in ('get_doc', 'list_docs', 'snapshot_docs', 'read_log', 'list_logs'):
        monkeypatch.setattr(provider, name, forbidden)
    final, receipts, _inputs = _final(mesh, chat, inputs)
    assert receipts[message.id]['state'] == 'delivered'
    assert final.result.send_statuses is not None
