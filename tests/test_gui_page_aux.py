"""Selected auxiliary presentation reuses canonical membership and source cuts."""
from __future__ import annotations

import pytest

from agentbridge.core.timekit import utcnow_iso
from agentbridge.gui import api_page_aux
from agentbridge.gui.api_runtime import contributor_rows
from agentbridge.gui.routing import Request
from agentbridge.harness.runtime.handoffs import HandoffLedger
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger
from agentbridge.harness.runtime.controls import publish_pause
from agentbridge.mesh.service import Mesh

import test_gui_chat_pages as base


base_page_app = base.page_app


@pytest.fixture
def page_app(base_page_app):
    return base_page_app


def _aux(app, chat):
    return api_page_aux.chat_aux(app, Request(params={'id': chat}))


def _ready(app, chat):
    base._ready(app, chat)
    runtime = app.mesh.local_inputs.auxiliary
    assert runtime.ingest('status').ready
    assert runtime.ingest('runtime', chat).ready


def _settled_aux(app, chat):
    for _ in range(12):
        result = _aux(app, chat)
        if result.get('reason') != 'overlay_proofs':
            return result
        assert app.mesh.local_inputs.prepare_one()
    pytest.fail('auxiliary proofs did not converge')


def test_selected_aux_sources_reach_ready_and_match_pause_live_baseline(page_app):
    app, chat = page_app
    mesh = app.mesh
    mesh.post(chat, 'one message')
    publish_pause(mesh, paused=True, chat_id=chat)
    mesh.tx.put_doc('status/typing_bob.json', {
        'user': 'bob', 'chat_id': chat, 'updated': utcnow_iso(),
    })
    _ready(app, chat)
    result = _settled_aux(app, chat)
    assert result['status'] == 'ready', result
    assert result['session_binding']['viewer'] == mesh.user
    assert result['metadata_status']['pause'] == 'ready'
    assert result['metadata_status']['runtime'] == 'ready'
    assert result['metadata_status']['live'] == 'ready'
    assert result['agents_paused'] is True
    assert result['tasks'] == [] and result['runs'] == []
    assert any(f['human'] and f['agent'] == 'bob' for f in result['feeds'])


def test_nonempty_canonical_runtime_rows_match_legacy_reader(page_app):
    app, chat = page_app
    owner = app.mesh
    owner.accounts.create_agent('manager', harness={'agent_tools_enabled': True})
    owner.accounts.create_agent('specialist', harness={'agent_tools_enabled': True})
    owner.membership.add_members(chat, ['manager', 'specialist'])
    owner.outbox.flush_once()
    manager = Mesh(app.root, 'manager', 'manager-box', encrypt=True,
                   home=app.home, store_path=app.home / 'manager-runtime.sqlite')
    specialist = Mesh(app.root, 'specialist', 'specialist-box', encrypt=True,
                      home=app.home, store_path=app.home / 'specialist-runtime.sqlite')
    try:
        manager.sync.sync_once([chat])
        specialist.sync.sync_once([chat])
        runs = RunLedger(manager)
        tasks = TaskLedger(manager, runs)
        handoffs = HandoffLedger(manager, tasks)
        tasks.start_with_run(run_id='run-1', task_id='task-1', chat_id=chat,
                             trigger_id='message-1', provider='codex', model='test')
        offered = handoffs.offer(chat_id=chat, run_id='run-1',
                                 parent_task_id='task-1', destination_agent='specialist',
                                 objective='Review', reason='Review',
                                 success_criteria=('Return a finding',))
        manager.outbox.flush_once()
        expected = contributor_rows(owner, chat)
        assert expected and offered.events[0].meta.call_id in [row['id'] for row in expected]
        _ready(app, chat)
        result = _settled_aux(app, chat)
        assert result['status'] == 'ready', result
        assert result['metadata_status']['runtime'] == 'ready'
        assert result['tasks'] == expected
    finally:
        specialist.close()
        manager.close()


def test_ready_aux_does_not_use_mesh_snapshot_full_history_or_provider(page_app, monkeypatch):
    app, chat = page_app
    mesh = app.mesh
    mesh.post(chat, 'safe')
    _ready(app, chat)
    assert _settled_aux(app, chat)['status'] == 'ready'

    def forbidden(*_args, **_kwargs):
        pytest.fail('auxiliary request used provider, fullfold or raw Store messages')

    monkeypatch.setattr(mesh, 'snapshot', forbidden)
    monkeypatch.setattr(mesh.store, 'messages', forbidden)
    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    provider = mesh.tx._transport
    for name in ('get_doc', 'list_docs', 'snapshot_docs', 'read_log', 'list_logs'):
        monkeypatch.setattr(provider, name, forbidden)
    result = _aux(app, chat)
    assert result['status'] == 'ready', result


@pytest.mark.parametrize('change', ['status', 'runtime'])
def test_late_aux_source_mutation_rejects_prepared_finalizer(page_app, change):
    app, chat = page_app
    mesh = app.mesh
    mesh.post(chat, 'one')
    publish_pause(mesh, paused=False, chat_id=chat)
    _ready(app, chat)
    reader, receipt, index = mesh.local_inputs.inputs(chat)
    operation = api_page_aux.AuxiliaryPageOperation(app, mesh, chat, source_reader=reader)
    prepared = operation.prepare(receipt, receipt, index)
    if prepared.status == 'work' and prepared.reason == 'overlay_proofs':
        for path, pub in prepared.work:
            mesh.store.verify_overlay_signature(index, path, pub)
        prepared = operation.prepare(receipt, receipt, index)
    assert prepared.status == 'prepared', prepared
    if change == 'status':
        mesh.tx.put_doc('status/typing_bob.json', {
            'user': 'bob', 'chat_id': chat, 'updated': utcnow_iso(),
        })
    else:
        mesh.tx.put_doc(f'chats/{chat}/runtime/mutation.json', {'value': 1})
    result = app.finalize_page_read(app.capture_session_read(), prepared.prepared)
    assert result.status == 'unavailable' and result.result is None


def test_cold_aux_sources_explicitly_pending_and_not_false_unpaused(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'one')
    base._ready(app, chat)
    result = _settled_aux(app, chat)
    assert result['status'] == 'ready', result
    assert result['metadata_status']['pause'] == 'pending'
    assert result['metadata_status']['runtime'] == 'pending'
    assert result['metadata_status']['live'] == 'pending'
    assert 'agents_paused' not in result


def test_runtime_source_over_request_byte_budget_stays_pending_without_false_pause(page_app):
    app, chat = page_app
    mesh = app.mesh
    mesh.post(chat, 'one')
    publish_pause(mesh, paused=True, chat_id=chat)
    for i in range(3):
        mesh.tx.put_doc(f'chats/{chat}/runtime/large-{i}.json',
                        {'payload': 'x' * (3 * 1024 * 1024)})
    _ready(app, chat)
    result = _settled_aux(app, chat)
    assert result['status'] == 'ready', result
    assert result['metadata_status']['runtime'] == 'pending'
    assert result['metadata_status']['pause'] == 'pending'
    assert 'agents_paused' not in result and result['tasks'] == []


def test_session_change_refuses_prepared_auxiliary_handoff(page_app):
    app, chat = page_app
    mesh = app.mesh
    mesh.post(chat, 'one')
    _ready(app, chat)
    reader, receipt, index = mesh.local_inputs.inputs(chat)
    prepared = api_page_aux.AuxiliaryPageOperation(app, mesh, chat,
                                                   source_reader=reader).prepare(receipt, receipt, index)
    if prepared.status == 'work' and prepared.reason == 'overlay_proofs':
        for path, pub in prepared.work:
            mesh.store.verify_overlay_signature(index, path, pub)
        prepared = api_page_aux.AuxiliaryPageOperation(app, mesh, chat,
            source_reader=reader).prepare(receipt, receipt, index)
    assert prepared.status == 'prepared', prepared
    token = app.capture_session_read()
    app._session_generation += 1
    result = app.finalize_page_read(token, prepared.prepared)
    assert result.status == 'unavailable' and result.result is None


def test_presence_observation_expiry_is_checked_at_final_handoff(page_app, monkeypatch):
    from agentbridge.mesh import membership_coordinator

    app, chat = page_app
    app.mesh.post(chat, 'one')
    _ready(app, chat)
    presence = app.mesh.local_inputs.presence
    presence.ingest()
    receipt = presence.reader.capture()
    captured = presence.reader.capture_display_members(receipt, (app.mesh.user,))
    now = [captured.observed_ns + 29_500_000_000]
    monkeypatch.setattr(membership_coordinator.time, 'time_ns', lambda: now[0])
    reader, source, index = app.mesh.local_inputs.inputs(chat)
    operation = api_page_aux.AuxiliaryPageOperation(app, app.mesh, chat, source_reader=reader)
    work = operation.prepare(source, source, index)
    if work.status == 'work' and work.reason == 'overlay_proofs':
        for path, pub in work.work:
            app.mesh.store.verify_overlay_signature(index, path, pub)
        work = operation.prepare(source, source, index)
    assert work.status == 'prepared', work
    assert work.prepared._round.deadline == captured.observed_ns + 30_000_000_000
    now[0] += 700_000_000
    final = app.finalize_page_read(app.capture_session_read(), work.prepared)
    assert final.status == 'unavailable' and final.reason == 'clock_expired'
