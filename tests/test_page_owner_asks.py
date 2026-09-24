"""Bounded owner asks open only from a current room/source/session cut."""
from __future__ import annotations

import json

import pytest

from agentbridge.gui.context import GuiApp
from agentbridge.harness.runtime.permissions import PermissionLane, list_owner_asks
from agentbridge.harness.runtime.permissions import ask_path
from agentbridge.mesh.page_owner_asks import OwnerAskPageOperation
from agentbridge.mesh.service import Mesh
from agentbridge.mesh.sync import SyncEngine


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kwargs: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kwargs: None)
    app = GuiApp(tmp_path / 'root', home=tmp_path / 'home', machine='ask-box',
                 encrypt=True, local_inputs=True)
    manager = None
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        app.mesh.accounts.create_agent('manager', harness={'agent_tools_enabled': True})
        chat = app.mesh.create_chat('Ask room', members=['manager']).id
        app.mesh.outbox.flush_once()
        manager = Mesh(app.root, 'manager', 'manager-box', encrypt=True,
                       home=app.home, store_path=tmp_path / 'manager.sqlite')
        manager.sync.sync_once([chat])
        yield app, manager, chat
    finally:
        if manager is not None:
            manager.close()
        app.close()


def _ready(app, chat, *, aux=True):
    app.mesh.local_inputs.prepare_one()
    app.mesh.local_inputs.ingest(chat)
    if aux:
        app.mesh.local_inputs.auxiliary.ingest('runtime', chat)


def _page(app, chat):
    runtime = app.mesh.local_inputs
    reader, receipt, index = runtime.inputs(chat)
    operation = OwnerAskPageOperation(app.mesh, chat, source_reader=reader)
    for _ in range(12):
        prepared = operation.prepare(receipt, receipt, index)
        if prepared.status == 'work' and prepared.reason == 'overlay_proofs':
            for path, pub in prepared.work:
                app.mesh.store.verify_overlay_signature(index, path, pub)
            continue
        if prepared.status == 'restart':
            continue
        assert prepared.status == 'prepared', prepared
        final = app.finalize_page_read(app.capture_session_read(), prepared.prepared)
        assert final.status == 'page', final
        return json.loads(final.result.presentation.decoration_json)
    pytest.fail('owner ask operation did not converge')


def _ask(manager, chat, *, tool='shell'):
    return PermissionLane(manager, 'manager').publish_ask(
        chat_id=chat, kind='permission', tool=tool, detail='Owner approval',
        input_digest='a' * 64, timeout_s=300, run_id='run-1', call_id='call-1')


def test_room_owner_ask_matches_legacy_canonical_reader(world):
    app, manager, chat = world
    ask = _ask(manager, chat)
    expected = list_owner_asks(app.mesh, chat_id=chat)
    assert [item['id'] for item in expected] == [ask.id]
    _ready(app, chat)
    result = _page(app, chat)
    assert result['asks_complete'] is True
    assert result['asks'] == expected


def test_cold_aux_source_is_incomplete_not_empty_fact(world):
    app, manager, chat = world
    _ask(manager, chat)
    _ready(app, chat, aux=False)
    result = _page(app, chat)
    assert result == {'asks': [], 'asks_complete': False}


def test_ask_page_foreground_avoids_provider_and_fullfold(world, monkeypatch):
    app, manager, chat = world
    ask = _ask(manager, chat)
    _ready(app, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail('owner ask foreground used provider or fullfold')

    monkeypatch.setattr(app.mesh, 'snapshot', forbidden)
    monkeypatch.setattr(app.mesh.membership, 'chats_for', forbidden)
    monkeypatch.setattr(app.mesh.store, 'messages', forbidden)
    provider = app.mesh.tx._transport
    for name in ('get_doc', 'list_docs', 'read_log', 'snapshot_docs'):
        monkeypatch.setattr(provider, name, forbidden)
    result = _page(app, chat)
    assert result['asks_complete'] and [item['id'] for item in result['asks']] == [ask.id]


def test_forged_ask_envelope_is_dropped_after_fresh_source_ingestion(world):
    app, manager, chat = world
    ask = _ask(manager, chat)
    _ready(app, chat)
    assert [row['id'] for row in _page(app, chat)['asks']] == [ask.id]
    path = ask_path(chat, 'manager', ask.id)
    tampered = manager.tx.get_doc(path)
    tampered['ct'] = ('A' if tampered['ct'][:1] != 'A' else 'B') + tampered['ct'][1:]
    manager.tx.put_doc(path, tampered)
    app.mesh.local_inputs.auxiliary.ingest('runtime', chat)
    result = _page(app, chat)
    assert result == {'asks': [], 'asks_complete': True}


def test_late_runtime_source_mutation_cannot_hand_out_prepared_owner_ask(world):
    app, manager, chat = world
    _ask(manager, chat)
    _ready(app, chat)
    reader, receipt, index = app.mesh.local_inputs.inputs(chat)
    operation = OwnerAskPageOperation(app.mesh, chat, source_reader=reader)
    prepared = operation.prepare(receipt, receipt, index)
    if prepared.status == 'work' and prepared.reason == 'overlay_proofs':
        for path, pub in prepared.work:
            app.mesh.store.verify_overlay_signature(index, path, pub)
        prepared = operation.prepare(receipt, receipt, index)
    assert prepared.status == 'prepared', prepared
    app.mesh.tx.put_doc(f'chats/{chat}/runtime/owner-control/manager/asks/late.json',
                        {'header': {'id': 'late'}})
    result = app.finalize_page_read(app.capture_session_read(), prepared.prepared)
    assert result.status == 'unavailable' and result.result is None
