"""Opt-in sidebar never infers complete room or unread state from a page."""
from __future__ import annotations

import json

import pytest

from agentbridge.core.models import BodyRecord, Envelope, MsgKind
from agentbridge.gui import api_chats
from agentbridge.gui.context import GuiApp
from agentbridge.gui.routing import Request
from agentbridge.mesh.service import Mesh
from agentbridge.mesh.page_operation import PageOperation
from agentbridge.mesh.sync import SyncEngine
from agentbridge.mesh.paths import P
from agentbridge.store import aux_inputs

from conftest import refresh_cloud


@pytest.fixture(params=[False, True], ids=['plain', 'encrypted'])
def world(tmp_path, monkeypatch, request, clouds):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kwargs: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kwargs: None)
    clouds.factory_auto_refresh = False
    app = GuiApp(clouds.root(tmp_path / 'root'), home=tmp_path / 'home', machine='sidebox',
                 encrypt=request.param, local_inputs=True)
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        chat = app.mesh.create_chat('Sidebar bounded')
        yield app, chat.id, request.param
    finally:
        app.close()


def _ready(app, chat):
    runtime = app.mesh.local_inputs
    for _ in range(12):
        if app.mesh.outbox.flush_once() == 0:
            break
    refresh_cloud(app)
    app.mesh.sync.sync_once([chat])
    runtime.prepare_one()
    runtime.ingest(chat)
    runtime.auxiliary.request('users')
    runtime.auxiliary.ingest('users')


def _plain_record(mesh, chat, ident, ns, sender, body):
    return Envelope(
        id=ident, ns=ns, ts='2026-01-01T00:00:00Z', from_=sender,
        kind=MsgKind.MESSAGE,
        **mesh.sealer.seal(chat, ident, ns, BodyRecord(body=body)),
    ).to_dict()


def _state(app, *, rounds=12):
    value = None
    for _ in range(rounds):
        value = api_chats.state(app, Request())
        if value.get('chats_complete'):
            return value
        app.mesh.local_inputs.prepare_one()
    return value


def test_optin_never_calls_fullfold_and_preserves_verified_viewer_flags(world, monkeypatch):
    app, chat, _encrypted = world
    mesh = app.mesh
    message = mesh.post(chat, 'canonical preview')
    mesh.messaging.set_chat_flag(chat, 'archived', True)
    mesh.messaging.set_chat_flag(chat, 'pinned', True)
    mesh.messaging.set_chat_flag(chat, 'mute', True)
    mesh.messaging.set_chat_flag(chat, 'forced_unread', True)
    _ready(app, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail('opt-in sidebar used a fullfold')

    monkeypatch.setattr(mesh, 'chats_for', forbidden)
    monkeypatch.setattr(mesh, 'chat_overview', forbidden)
    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    monkeypatch.setattr(PageOperation, '_pin_presentation', forbidden)
    monkeypatch.setattr(PageOperation, '_receipt_presentation', forbidden)
    state = _state(app)
    assert state['chats_complete'] and state['sidebar_status'] == 'ready'
    assert state['caps']['chat_page_v1'] is True
    row = next(c for c in state['chats'] if c['id'] == chat)
    assert row['last']['body'] == 'canonical preview'
    assert row['last']['ns'] == message.ns
    assert row['archived'] and row['pinned'] and row['mute'] and row['forced_unread']
    assert row['unread_complete'] and row['unread'] == 0
    assert state['metadata_status']['profiles'] == 'pending'
    assert state['users_complete'] and state['user_status'] == 'ready'
    assert 'presence' not in state['users']['viewer']


def test_forged_viewer_state_cannot_set_sidebar_flags(world):
    app, chat, encrypted = world
    if not encrypted:
        pytest.skip('signed-state authority needs E2EE')
    mesh = app.mesh
    mesh.post(chat, 'still visible')
    mesh.tx.put_doc(P.state(chat, mesh.user), {
        'ns': 1, 'archived': True, 'pinned': True, 'mute': True,
        'forced_unread': True, 'read_ns': 2**60, 'sig': 'forged',
    })
    _ready(app, chat)
    state = _state(app)
    row = next(c for c in state['chats'] if c['id'] == chat)
    assert not row['archived'] and not row['pinned'] and not row['mute']
    assert not row['forced_unread'] and row['unread'] == 0


def test_incomplete_large_history_has_lower_bound_not_false_zero(world, clouds):
    app, chat, encrypted = world
    mesh = app.mesh
    if encrypted:
        pytest.skip('disposable bulk raw history is plaintext')
    meta = mesh.tx.get_doc(P.meta(chat))
    base = max(member['joined_ns'] for member in meta['members'].values()) + 1
    clouds.replace_log(app.root, chat, 'other', [
        _plain_record(mesh, chat, f'm{i:04d}', base + i, 'other', f'body-{i}')
        for i in range(300)
    ])
    _ready(app, chat)
    state = _state(app)
    row = next(c for c in state['chats'] if c['id'] == chat)
    assert row['last']['body'] == 'body-299'
    assert row['unread_complete'] is False
    assert row['unread'] == 50 and row['unread_lower_bound'] == 50
    assert row['first_unread_ns'] is None
    assert row['mention'] is None


def test_hidden_tail_has_pending_preview_and_unknown_unread(world, clouds):
    app, chat, encrypted = world
    if encrypted:
        pytest.skip('disposable bulk raw history is plaintext')
    mesh = app.mesh
    meta = mesh.tx.get_doc(P.meta(chat))
    base = max(member['joined_ns'] for member in meta['members'].values()) + 1
    records = [
        _plain_record(mesh, chat, f'm{i:04d}', base + i, 'viewer', f'body-{i}')
        for i in range(1100)
    ]
    clouds.replace_log(app.root, chat, 'viewer', records)
    refresh_cloud(app)
    mesh.sync.sync_once([chat])
    mesh.messaging.hide(chat, [r['id'] for r in records])
    _ready(app, chat)
    state = _state(app)
    row = next(c for c in state['chats'] if c['id'] == chat)
    assert row['last'] is None and row['preview_pending']
    assert row['unread'] is None and row['unread_lower_bound'] == 0
    assert row['unread_complete'] is False and row['first_unread_ns'] is None


def test_delete_for_me_hides_only_after_exhaustion_or_resurrecting_real_message(world):
    app, chat, _encrypted = world
    mesh = app.mesh
    old = mesh.post(chat, 'old')
    mesh.messaging.set_chat_flag(chat, 'deleted', old.ns)
    _ready(app, chat)
    state = _state(app)
    assert state['chats_complete'] and not any(c['id'] == chat for c in state['chats'])
    mesh.post(chat, 'new')
    _ready(app, chat)
    state = _state(app)
    assert any(c['id'] == chat and c['last']['body'] == 'new' for c in state['chats'])


def test_room_limit_and_cold_room_are_explicitly_incomplete(world, monkeypatch):
    app, chat, _encrypted = world
    mesh = app.mesh
    # A cold source contributes no guessed membership entry or empty sidebar.
    pending = api_chats.state(app, Request())
    assert pending['chats_complete'] is False and pending['sidebar_status'] == 'rooms_pending'
    assert not any(c['id'] == chat for c in pending['chats'])
    monkeypatch.setattr(mesh.tx, 'list_chat_ids', lambda: [f'room-{n}' for n in range(129)])
    over = api_chats.state(app, Request())
    assert over['chats'] == [] and over['chats_complete'] is False
    assert over['sidebar_status'] == 'room_limit'


def test_directory_and_serialized_response_budgets_fail_explicitly(world, monkeypatch):
    app, _chat, _encrypted = world
    mesh = app.mesh
    def no_provider_user_walk():
        pytest.fail('sidebar requested provider-wide Directory.names')

    monkeypatch.setattr(mesh.directory, 'names', no_provider_user_walk)
    auxiliary = mesh.local_inputs.auxiliary
    monkeypatch.setattr(auxiliary, 'inputs',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            aux_inputs.AuxInputsUnavailable('aux_document_budget')))
    capped = api_chats.state(app, Request())
    assert capped['users'] == {} and capped['chats'] == []
    assert capped['users_complete'] is False and capped['user_status'] == 'user_limit'

    from agentbridge.gui import api_sidebar_pages
    monkeypatch.setattr(api_sidebar_pages, '_users',
                        lambda _mesh: ({'viewer': {'display': 'x' * (4 * 1024 * 1024)}}, True, 'ready'))
    oversized = api_chats.state(app, Request())
    assert oversized['users'] == {} and oversized['chats'] == []
    assert oversized['chats_complete'] is False
    assert oversized['sidebar_status'] == 'response_byte_budget'


def test_equal_ns_preview_and_membership_removal_are_canonical(world, clouds):
    app, chat, encrypted = world
    if encrypted:
        pytest.skip('disposable equal-ns raw records are plaintext')
    mesh = app.mesh
    meta = mesh.tx.get_doc(P.meta(chat))
    ns = max(member['joined_ns'] for member in meta['members'].values()) + 1
    for sender in ('viewer', 'zeta'):
        clouds.replace_log(app.root, chat, sender, [
            _plain_record(mesh, chat, f'equal-{sender}', ns, sender, sender),
        ])
    _ready(app, chat)
    row = next(c for c in _state(app)['chats'] if c['id'] == chat)
    assert row['last']['body'] == 'zeta'
    mesh.membership.leave(chat)
    mesh.sync.sync_once([chat])
    _ready(app, chat)
    state = _state(app)
    assert not any(c['id'] == chat for c in state['chats'])


def test_default_gui_requires_local_inputs_and_rejects_false(tmp_path, monkeypatch, clouds):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kwargs: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kwargs: None)
    clouds.factory_auto_refresh = False
    root = clouds.root(tmp_path / 'default-root')
    with pytest.raises(ValueError, match='GUI requires local_inputs=True'):
        GuiApp(root, home=tmp_path / 'rejected-home', local_inputs=False)
    app = GuiApp(root, home=tmp_path / 'default-home', machine='paged-box', encrypt=False)
    try:
        assert app.local_inputs_enabled is True
        assert app.signup('viewer', '', 'secret')['ok']
        assert app.mesh.local_inputs is not None
        chat = app.mesh.create_chat('Paged')
        state = api_chats.state(app, Request())
        assert state['caps']['chat_page_v1'] is True
        assert 'chats_complete' in state and 'users_complete' in state
        assert not state['chats_complete']
        _ready(app, chat.id)
        assert any(c['id'] == chat.id for c in _state(app)['chats'])
    finally:
        app.close()


def test_sidebar_phase_diagnostics_use_existing_private_schema(world):
    from agentbridge.gui import api_sidebar_pages
    app, chat, _encrypted = world
    app.mesh.post(chat, 'private message must not appear in logs')
    _ready(app, chat)
    assert _state(app)['chats_complete']
    assert app.diagnostics.set_enabled(True, sample_rate=1)
    row, complete = api_sidebar_pages._room(app, app.mesh, app.capture_session_read(), chat)
    assert complete and row is not None
    assert app.diagnostics.flush()
    raw = app.diagnostics.path.read_text()
    events = [event for line in raw.splitlines() if
              (event := json.loads(line))['event'] == 'page_stage'
              and event['route'] == '/api/mesh/state']
    assert len(events) == 3
    phases = {e['phase']: e for e in events}
    assert set(phases) == {'inputs', 'prepare', 'finalize'}
    assert [phases[p]['status'] for p in ('inputs', 'prepare', 'finalize')] == [
        'ready', 'prepared', 'page']
    for event in events:
        assert event['event'] == 'page_stage' and event['route'] == '/api/mesh/state'
        assert event['duration_ms'] >= 0 and len(event['chat_ref']) == 16
    assert chat not in raw and 'private message' not in raw
    assert app.diagnostics.set_enabled(False)
    assert api_sidebar_pages._room(app, app.mesh, app.capture_session_read(), chat)[1]
    assert app.diagnostics.flush()
    assert app.diagnostics.path.read_text() == raw


@pytest.mark.parametrize('case,expected_phases,resolved', [
    ('inputs_error', ['inputs'], False),
    ('prepare_error', ['inputs', 'prepare'], False),
    ('forbidden', ['inputs', 'prepare'], True),
    ('work', ['inputs', 'prepare'], False),
    ('prepare_restarts', ['inputs'] + ['prepare', 'inputs'] * 4, False),
    ('finalize_error', ['inputs', 'prepare', 'finalize'], False),
    ('finalize_locked', ['inputs', 'prepare', 'finalize'], False),
    ('finalize_restarts', ['inputs'] + ['prepare', 'finalize', 'inputs'] * 4, False),
])
def test_sidebar_phase_diagnostics_include_early_exits(monkeypatch, case, expected_phases, resolved):
    from types import SimpleNamespace as NS
    from agentbridge.gui import api_sidebar_pages as module
    from agentbridge.gui.diagnostics import Diagnostics
    events = []

    def stage(route, chat, phase, status, reason, **fields):
        event = Diagnostics._sanitize(dict(event='page_stage', route=route, phase=phase,
                                          status=status, reason=reason, **fields))
        if phase != 'sidebar':
            events.append(event)

    def inputs(_chat):
        if case == 'inputs_error':
            raise OSError('PRIVATE exception text')
        return (None, None, None)

    def prepare(*_args):
        if case == 'prepare_error':
            raise ValueError('PRIVATE exception text')
        status = {'forbidden': 'forbidden', 'work': 'work',
                  'prepare_restarts': 'restart'}.get(case, 'prepared')
        return NS(status=status, reason='overlay_proofs' if status == 'work' else 'none',
                  prepared=None, work=())

    def finalize(*_args):
        if case == 'finalize_error':
            raise OSError('PRIVATE exception text')
        return NS(status='restart' if case == 'finalize_restarts' else 'locked',
                  reason='app_locked', result=None)

    runtime = NS(request=lambda *a, **k: None, request_page=lambda *a, **k: None,
                 inputs=inputs, unread=None)
    mesh = NS(local_inputs=runtime, user='viewer')
    token = NS(app_identity='diagnostics-test', generation=1)
    app = NS(diagnostics=NS(stage=stage), finalize_page_read=finalize)
    monkeypatch.setattr(module, 'PageOperation', lambda *a, **k: NS(prepare=prepare))
    assert module._room(app, mesh, token, 'room') == (None, resolved)
    assert [e['phase'] for e in events] == expected_phases
    assert all(e['duration_ms'] >= 0 for e in events)
    if case.endswith('_error'):
        assert events[-1]['status'] == 'error'
        assert events[-1]['error_type'] in ('OSError', 'ValueError')
    assert 'PRIVATE' not in json.dumps(events)

    # A broken recorder must leave the exact same canonical control flow/result.
    def broken(*_args, **_kwargs):
        raise RuntimeError('diagnostics unavailable')
    app.diagnostics.stage = broken
    assert module._room(app, mesh, token, 'room') == (None, resolved)
    del app.diagnostics
    assert module._room(app, mesh, token, 'room') == (None, resolved)
