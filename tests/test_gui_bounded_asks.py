"""Local-enabled ask polling never full-folds rooms or turns pending into empty."""
from __future__ import annotations

import time

import pytest

from agentbridge.gui import api_agents
from agentbridge.gui.context import GuiApp
from agentbridge.gui.routing import Request
from agentbridge.harness import PeerService
from agentbridge.harness.settings import HarnessSettings
from agentbridge.harness.runtime.permissions import PermissionLane
from agentbridge.mesh.service import Mesh
from agentbridge.mesh.sync import SyncEngine
from types import SimpleNamespace


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kwargs: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kwargs: None)
    app = GuiApp(tmp_path / 'root', home=tmp_path / 'home', machine='viewer-box',
                 encrypt=True, local_inputs=True)
    remote = manager = None
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        # Host the owned agent on another machine so process truth correctly
        # leaves its pending ask visible in this GUI.
        remote = Mesh(app.root, 'viewer', 'remote-box', encrypt=True,
                      home=app.home, store_path=tmp_path / 'remote-owner.sqlite')
        remote.accounts.create_agent('manager', harness={'agent_tools_enabled': True})
        chat = app.mesh.create_chat('Asks', members=['manager']).id
        app.mesh.outbox.flush_once()
        manager = Mesh(app.root, 'manager', 'remote-box', encrypt=True,
                       home=app.home, store_path=tmp_path / 'manager.sqlite')
        manager.sync.sync_once([chat])
        ask = PermissionLane(manager, 'manager').publish_ask(
            chat_id=chat, kind='permission', tool='shell', detail='Allow?',
            input_digest='b' * 64, timeout_s=300, run_id='run-1', call_id='call-1')
        yield app, chat, ask
    finally:
        if manager is not None:
            manager.close()
        if remote is not None:
            remote.close()
        app.close()


def _ingest(app, chat, *, room=True, companions=True):
    runtime = app.mesh.local_inputs
    runtime.prepare_one()
    if room:
        runtime.ingest(chat)
        runtime.auxiliary.ingest('runtime', chat)
    if companions:
        for scope in ('identities', 'status', 'peer'):
            runtime.auxiliary.ingest(scope)


def _asks(app, chat=''):
    for _ in range(12):
        result = api_agents.asks(app, Request(params={'chat': chat} if chat else {}))
        if result.get('reason') != 'overlay_proofs':
            return result
        app.mesh.local_inputs.prepare_one()
    pytest.fail('ask proof work did not converge')


def test_global_room_owner_ask_and_empty_companions_are_complete(world):
    app, chat, ask = world
    _ingest(app, chat)
    result = _asks(app)
    assert result['ok'] and result['asks_complete'], result
    assert result['rooms_complete'] and result['peer_complete'] and result['timers_complete']
    assert [row['id'] for row in result['asks']] == [ask.id]
    assert result['resolved_room_ids'] == [chat]
    assert result['timers'] == [] and result['forbidden'] is False


def test_pending_room_does_not_silently_claim_no_asks(world):
    app, chat, ask = world
    _ingest(app, chat, room=False)
    result = _asks(app)
    assert result['ok'] and not result['asks_complete']
    assert not result['rooms_complete']
    assert result['resolved_room_ids'] == []
    assert not any(row['id'] == ask.id for row in result['asks'])


def test_same_room_ask_query_and_foreground_no_fullfold_or_provider(world, monkeypatch):
    app, chat, ask = world
    _ingest(app, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail('bounded asks polled provider or fullfold')

    mesh = app.mesh
    monkeypatch.setattr(mesh, 'snapshot', forbidden)
    monkeypatch.setattr(mesh.membership, 'chats_for', forbidden)
    monkeypatch.setattr(mesh.store, 'messages', forbidden)
    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    provider = mesh.tx._transport
    for name in ('get_doc', 'list_docs', 'snapshot_docs', 'read_log'):
        monkeypatch.setattr(provider, name, forbidden)
    result = _asks(app, chat)
    assert result['asks_complete'] and [row['id'] for row in result['asks']] == [ask.id]


def test_room_aux_mutation_after_prepare_invalidates_before_handoff(world):
    app, chat, ask = world
    _ingest(app, chat)
    result = _asks(app)
    assert result['asks_complete'] and result['asks'][0]['id'] == ask.id
    app.mesh.tx.put_doc(f'chats/{chat}/runtime/owner-control/manager/asks/changed.json',
                        {'header': {'id': 'changed'}})
    pending = _asks(app)
    assert pending['ok'] and not pending['asks_complete']
    assert pending['rooms_complete'] is False


def test_owned_agent_timer_is_present_with_complete_crossroom_asks(world):
    app, chat, first_ask = world
    app.mesh.tx.put_doc('status/manager_harness.json', {'timers': [
        {'id': 'wake-1', 'chat_id': chat,
         'at_ns': time.time_ns() + 60_000_000_000, 'note': 'Follow up'},
    ]})
    second_chat = app.mesh.create_chat('Another ask room', members=['manager']).id
    app.mesh.outbox.flush_once()
    manager = Mesh(app.root, 'manager', 'remote-box', encrypt=True,
                   home=app.home, store_path=app.home / 'manager-second.sqlite')
    try:
        manager.sync.sync_once([second_chat])
        second = PermissionLane(manager, 'manager').publish_ask(
            chat_id=second_chat, kind='question', tool='Clarify', detail='Which route?',
            input_digest='c' * 64, timeout_s=300, run_id='run-2', call_id='call-2')
        _ingest(app, chat)
        app.mesh.local_inputs.ingest(second_chat)
        app.mesh.local_inputs.auxiliary.ingest('runtime', second_chat)
        global_result = _asks(app)
        assert global_result['asks_complete'], global_result
        assert {row['id'] for row in global_result['asks']} == {first_ask.id, second.id}
        assert set(global_result['resolved_room_ids']) == {chat, second_chat}
        assert global_result['timers_complete']
        assert global_result['timers'] == [{
            'agent': 'manager', 'id': 'wake-1', 'chat_id': chat,
            'at_ns': global_result['timers'][0]['at_ns'], 'note': 'Follow up',
        }]
        selected = _asks(app, chat)
        assert selected['asks_complete'] and [a['id'] for a in selected['asks']] == [first_ask.id]
        assert selected['resolved_room_ids'] == [chat]
        assert selected['timers'][0]['id'] == 'wake-1'
    finally:
        manager.close()


def test_chatless_peer_and_timer_work_without_any_room(tmp_path, monkeypatch):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kwargs: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kwargs: None)
    app = GuiApp(tmp_path / 'root', home=tmp_path / 'home', machine='viewer-box',
                 encrypt=True, local_inputs=True)
    meshes = []
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        app.mesh.accounts.create_human('fable', 'fable-pass')
        remote = Mesh(app.root, 'viewer', 'remote-box', encrypt=True,
                      home=app.home, store_path=tmp_path / 'remote-owner.sqlite')
        meshes.append(remote)
        remote.accounts.create_agent('manager')
        fable = Mesh(app.root, 'fable', 'fable-box', encrypt=True,
                     home=app.home, store_path=tmp_path / 'fable.sqlite')
        meshes.append(fable)
        fable.accounts.create_agent('ops')
        target = Mesh(app.root, 'manager', 'remote-box', encrypt=True,
                      home=app.home, store_path=tmp_path / 'manager.sqlite')
        requester = Mesh(app.root, 'ops', 'remote-box', encrypt=True,
                         home=app.home, store_path=tmp_path / 'ops.sqlite')
        meshes.extend((target, requester))
        PeerService(requester).request('manager', 'status')
        settings = HarnessSettings.from_account(SimpleNamespace(agent=SimpleNamespace(
            harness={'peer_access': 'ask', 'peer_auto': [], 'peer_repair': False})))
        assert PeerService(target).serve_once(settings) == 1
        app.mesh.tx.put_doc('status/manager_harness.json', {'timers': [
            {'id': 'chatless-wake', 'chat_id': '', 'at_ns': time.time_ns() + 60_000_000_000,
             'note': 'Check status'},
        ]})
        for scope in ('identities', 'status', 'peer'):
            app.mesh.local_inputs.auxiliary.ingest(scope)

        def forbidden(*_args, **_kwargs):
            pytest.fail('chatless ask poll used fullfold or provider')

        monkeypatch.setattr(app.mesh, 'snapshot', forbidden)
        monkeypatch.setattr(app.mesh.membership, 'chats_for', forbidden)
        provider = app.mesh.tx._transport
        for name in ('get_doc', 'list_docs', 'read_log'):
            monkeypatch.setattr(provider, name, forbidden)
        result = _asks(app)
        assert result['ok'] and result['asks_complete'], result
        assert result['rooms_complete'] and result['peer_complete'] and result['timers_complete']
        assert result['resolved_room_ids'] == []
        assert len(result['asks']) == 1 and result['asks'][0]['kind'] == 'peer'
        assert result['asks'][0]['agent'] == 'manager'
        assert [timer['id'] for timer in result['timers']] == ['chatless-wake']
    finally:
        for mesh in reversed(meshes):
            mesh.close()
        app.close()


@pytest.mark.parametrize('timers', [
    [{'id': 'bad-note', 'chat_id': 'ROOM', 'at_ns': 1,
      'note': {'text': 'not a string'}}],
    [{'id': 'bad-weekly', 'chat_id': 'ROOM', 'at_ns': 1,
      'note': 'safe', 'repeat': {'kind': 'weekly', 'days': 'Mon'}}],
    [{'id': 'bad-time', 'chat_id': 'ROOM', 'at_ns': 'tomorrow', 'note': 'safe'}],
    {'id': 'not-a-list'},
])
def test_malformed_timer_payload_never_reaches_browser_as_complete(world, timers):
    app, chat, _ask_id = world
    if isinstance(timers, list):
        timers = [{**timer, 'chat_id': chat} for timer in timers]
    app.mesh.tx.put_doc('status/manager_harness.json', {'timers': timers})
    _ingest(app, chat)
    result = _asks(app)
    assert result['ok'] and not result['timers_complete']
    assert result['timers'] == []
    assert not result['asks_complete']  # browser retains prior same-session timer state


def test_removed_room_cannot_expose_timer_or_room_id(world):
    app, chat, _ask_id = world
    app.mesh.tx.put_doc('status/manager_harness.json', {'timers': [
        {'id': 'private-wake', 'chat_id': chat,
         'at_ns': time.time_ns() + 60_000_000_000, 'note': 'Private'},
    ]})
    _ingest(app, chat)
    assert [timer['id'] for timer in _asks(app)['timers']] == ['private-wake']
    app.mesh.membership.leave(chat)
    app.mesh.sync.sync_once([chat])
    app.mesh.local_inputs.ingest(chat)
    app.mesh.local_inputs.prepare_one()
    global_result = _asks(app)
    assert global_result['rooms_complete'] and global_result['asks_complete']
    assert global_result['resolved_room_ids'] == []
    assert global_result['asks'] == [] and global_result['timers'] == []
    assert chat not in str(global_result)


def test_unresolved_room_timer_is_pending_not_disclosed(world):
    app, chat, _ask_id = world
    app.mesh.tx.put_doc('status/manager_harness.json', {'timers': [
        {'id': 'pending-wake', 'chat_id': chat,
         'at_ns': time.time_ns() + 60_000_000_000, 'note': 'Pending'},
    ]})
    _ingest(app, chat, room=False)
    result = _asks(app)
    assert not result['rooms_complete'] and not result['timers_complete']
    assert not result['asks_complete'] and result['resolved_room_ids'] == []
    assert result['timers'] == []


def test_identity_source_over_2048_is_structured_incomplete_not_internal_error(tmp_path, monkeypatch):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kwargs: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kwargs: None)
    app = GuiApp(tmp_path / 'root', home=tmp_path / 'home', machine='capacity-box',
                 encrypt=False, local_inputs=True)
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        provider = app.mesh.tx._transport
        for index in range(2048):
            name = f'other-{index:04d}'
            provider.put_doc(f'users/{name}.json', {'name': name, 'kind': 'human'})
        app.mesh.local_inputs.auxiliary.ingest('identities')
        result = _asks(app)
        assert result['ok'] and result['asks'] == [] and result['timers'] == []
        assert result['resolved_room_ids'] == []
        assert not result['asks_complete'] and not result['peer_complete']
        assert not result['timers_complete']
    finally:
        app.close()



def test_ready_owner_ask_polls_do_not_enqueue_page_preparation(world, monkeypatch):
    app, chat, ask = world
    _ingest(app, chat)
    initial = _asks(app, chat)
    assert initial['asks_complete'], initial
    runtime = app.mesh.local_inputs
    requests = []
    original = runtime.request_page

    def observed(room, **kwargs):
        requests.append((room, kwargs))
        return original(room, **kwargs)

    monkeypatch.setattr(runtime, 'request_page', observed)
    for _ in range(3):
        result = _asks(app, chat)
        assert result['asks_complete'], result
        assert [row['id'] for row in result['asks']] == [ask.id]
    assert requests == []


def test_pending_owner_ask_inputs_enqueue_preparation(world, monkeypatch):
    app, chat, _ask = world
    _ingest(app, chat, room=False)
    runtime = app.mesh.local_inputs
    requests = []
    original = runtime.request_page

    def observed(room, **kwargs):
        requests.append((room, kwargs))
        return original(room, **kwargs)

    monkeypatch.setattr(runtime, 'request_page', observed)
    result = _asks(app, chat)
    assert not result['rooms_complete'] and not result['asks_complete']
    assert requests == [(chat, {})]


@pytest.mark.parametrize('reason', ['terminal_classification_pending', 'overlay_proofs'])
def test_owner_ask_work_preserves_preparation_request(world, monkeypatch, reason):
    from agentbridge.gui import api_bounded_asks

    app, chat, _ask = world
    _ingest(app, chat)
    runtime = app.mesh.local_inputs
    requests = []
    proof_work = (('proof-path', 'proof-key'),) if reason == 'overlay_proofs' else ()
    monkeypatch.setattr(api_bounded_asks.OwnerAskPageOperation, 'prepare',
        lambda *_args: SimpleNamespace(status='work', reason=reason, work=proof_work))
    monkeypatch.setattr(runtime, 'request_page',
        lambda room, **kwargs: requests.append((room, kwargs)) or True)
    result = _asks(app, chat)
    assert not result['rooms_complete'] and not result['asks_complete']
    assert len(requests) == 1 and requests[0][0] == chat
    if reason == 'overlay_proofs':
        assert requests[0][1]['proofs'] is proof_work
        assert requests[0][1]['index'].chat_id == chat
    else:
        assert requests[0][1] == {}



def test_owner_ask_preparation_follows_source_change_not_unchanged_ingestion(world, monkeypatch):
    app, chat, _ask = world
    _ingest(app, chat)
    assert _asks(app, chat)['asks_complete']
    runtime = app.mesh.local_inputs
    requests = []
    original = runtime.request_page

    def observed(room, **kwargs):
        requests.append((room, kwargs))
        return original(room, **kwargs)

    monkeypatch.setattr(runtime, 'request_page', observed)
    assert runtime.ingest(chat) is False
    assert requests == []
    app.mesh.membership.leave(chat)
    app.mesh.sync.sync_once([chat])
    runtime.ingest(chat)
    assert requests == [(chat, {})]
    assert runtime.prepare_one()
    result = _asks(app)
    assert result['rooms_complete'] and result['asks_complete']
    assert result['asks'] == [] and result['resolved_room_ids'] == []

def test_ready_owner_ask_auxiliary_recovers_without_terminal_wake(world, monkeypatch):
    app, chat, ask = world
    _ingest(app, chat)
    assert _asks(app, chat)['asks_complete']
    runtime = app.mesh.local_inputs
    app.mesh.tx.put_doc(f'chats/{chat}/runtime/owner-control/manager/asks/late.json',
                        {'header': {'id': 'late'}})
    page_requests, auxiliary_requests = [], []
    original = runtime.auxiliary.request

    def observed(scope, room='', **kwargs):
        auxiliary_requests.append((scope, room))
        return original(scope, room, **kwargs)

    monkeypatch.setattr(runtime, 'request_page',
        lambda room, **kwargs: page_requests.append((room, kwargs)) or True)
    monkeypatch.setattr(runtime.auxiliary, 'request', observed)
    pending = _asks(app, chat)
    assert not pending['asks_complete'], pending
    assert ('runtime', chat) in auxiliary_requests
    assert page_requests == []
    runtime.auxiliary.ingest('runtime', chat)
    recovered = _asks(app, chat)
    assert recovered['asks_complete'], recovered
    assert [row['id'] for row in recovered['asks']] == [ask.id]
    assert page_requests == []


@pytest.mark.parametrize('phase', ['prepare', 'finalize'])
def test_owner_ask_restart_exhaustion_queues_preparation(world, monkeypatch, phase):
    from agentbridge.gui import api_bounded_asks

    app, chat, _ask = world
    _ingest(app, chat)
    assert _asks(app, chat)['asks_complete']
    runtime = app.mesh.local_inputs
    attempts, requests = [], []

    def restart(*_args):
        attempts.append(phase)
        return SimpleNamespace(status='restart')

    if phase == 'prepare':
        monkeypatch.setattr(api_bounded_asks.OwnerAskPageOperation, 'prepare', restart)
    else:
        monkeypatch.setattr(app, 'finalize_page_read', restart)
    monkeypatch.setattr(runtime, 'request_page',
        lambda room, **kwargs: requests.append((room, kwargs)) or True)
    result = _asks(app, chat)
    assert not result['asks_complete'] and not result['rooms_complete'], result
    assert attempts == [phase] * 4
    assert requests == [(chat, {})]


def test_changed_source_admission_shutdown_preserves_winner(world, monkeypatch):
    import threading

    app, chat, _ask = world
    _ingest(app, chat)
    assert _asks(app, chat)['asks_complete']
    runtime = app.mesh.local_inputs
    app.mesh.membership.leave(chat)
    app.mesh.sync.sync_once([chat])
    entered, release = threading.Event(), threading.Event()
    errors, scheduled = [], []
    original = runtime.request_page

    def paused(room, **kwargs):
        entered.set()
        if not release.wait(2):
            raise RuntimeError('test release deadline')
        result = original(room, **kwargs)
        scheduled.append(result)
        return result

    def capture(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(runtime, 'request_page', paused)
    ingest = threading.Thread(target=capture, args=(lambda: runtime.ingest(chat),))
    stop = threading.Thread(target=capture, args=(runtime.stop,))
    try:
        ingest.start()
        assert entered.wait(2), errors
        stop.start()
        deadline = time.monotonic() + 1
        while not runtime._closed and time.monotonic() < deadline:
            time.sleep(.005)
        assert runtime._closed
        release.set()
        ingest.join(2)
        stop.join(2)
        assert not ingest.is_alive() and not stop.is_alive()
        assert errors == []
        assert scheduled == [False]
        winner = runtime.reader(chat).capture().source
        assert winner.ready and winner.writes_pending == 0
    finally:
        release.set()
        if ingest.ident is not None:
            ingest.join(3)
        if stop.ident is not None:
            stop.join(3)
