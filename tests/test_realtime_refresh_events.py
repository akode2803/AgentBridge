"""Event-first refresh preserves canonical gates and bounded recovery work."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agentbridge.gui import sse
from agentbridge.mesh import eventbus
from agentbridge.mesh.read_events import MAX_DEADLINES, ReadEvents


class _Lock:
    def __init__(self):
        self._mx = threading.Lock()
        self.locked = False

    def _expire_if_idle_locked(self):
        return self.locked


def _stream_world(notifier=None, maxsize=8):
    bus = eventbus.EventBus()
    mesh = SimpleNamespace(bus=bus, notifier=notifier, tx=SimpleNamespace())
    app = SimpleNamespace(mesh=mesh, lock=_Lock(), _lock=threading.RLock(),
                          generation=1, latency=None)
    app.capture_session_read = lambda: SimpleNamespace(mesh=mesh, generation=app.generation)
    app.validate_session_read = lambda token: token.mesh is app.mesh and token.generation == app.generation
    sub = bus.subscribe(maxsize=maxsize)
    stream = sse.stream(app, sub, .001)
    assert next(stream) == b': connected\n\n'
    return app, bus, sub, stream


def _decode(value):
    assert value.startswith(b'data: ')
    return json.loads(value[6:])


def test_queue_overflow_is_explicit_and_bounded():
    app, bus, sub, stream = _stream_world(maxsize=1)
    bus.publish(eventbus.Event(eventbus.MESSAGE, 'old', {'id': 'old'}))
    bus.publish(eventbus.Event(eventbus.MESSAGE, 'new', {'id': 'new'}))
    assert _decode(next(stream)) == {'type': 'control', 'reason': 'resync'}
    assert _decode(next(stream))['id'] == 'new'
    assert not sub.take_gap()
    stream.close()


@pytest.mark.parametrize('stage', ['before', 'during'])
@pytest.mark.parametrize('change', ['lock', 'session'])
def test_sse_fences_notify_before_and_after_computation(stage, change):
    calls = []
    app = None

    def mutate():
        if change == 'lock':
            app.lock.locked = True
        else:
            app.generation += 1  # Deliberately retain the same Mesh object.

    def consider(_event):
        calls.append(True)
        if stage == 'during':
            mutate()
        return SimpleNamespace(kind='message', chat_name='private room', chat_kind='group',
                               from_='peer', preview='private plaintext', ns=1, emoji='')

    app, bus, _sub, stream = _stream_world(SimpleNamespace(consider=consider))
    bus.publish(eventbus.Event(eventbus.MESSAGE, 'room', {'id': 'message'}))
    if stage == 'before':
        mutate()
    value = next(stream)
    assert b'private' not in value
    assert _decode(value) == {'type': 'control',
                              'reason': 'locked' if change == 'lock' else 'session_changed'}
    assert len(calls) == (stage == 'during')
    with pytest.raises(StopIteration):
        next(stream)


def test_idle_stream_expires_lock_without_read_model_poll():
    app, _bus, sub, stream = _stream_world()
    waits = []

    def quiet(timeout):
        waits.append(timeout)
        app.lock.locked = True
        return None

    sub.get = quiet
    assert _decode(next(stream)) == {'type': 'control', 'reason': 'locked'}
    assert len(waits) == 1 and waits[0] <= 1


def test_idle_heartbeat_is_content_free_not_resync():
    _app, _bus, _sub, stream = _stream_world()
    assert _decode(next(stream)) == {'type': 'heartbeat'}
    stream.close()


def test_read_model_frame_rejects_unrecognized_scope_payload():
    value = sse.frame(eventbus.Event(eventbus.READ_MODEL, 'room',
        {'scope': 'unknown', 'body': 'private plaintext'}))
    assert value['scope'] == 'global'
    assert 'body' not in value and 'private' not in json.dumps(value)


def test_deadlines_keep_minimum_and_clock_high_water_and_fire_once():
    now = [100]
    bus = eventbus.EventBus()
    sub = bus.subscribe()
    owner = ReadEvents(bus, clock=lambda: now[0])
    owner.note_deadline('room', 200, 100)
    owner.note_deadline('room', 300, 150)
    owner.note_deadline('room', None, 160)
    assert owner._deadlines == {'room': 200}
    now[0] = 170
    owner.tick()
    assert not list(sub.drain())
    now[0] = 200
    owner.tick()
    assert [(e.chat_id, e.data['scope']) for e in sub.drain()] == [('room', 'chat')]
    owner.tick()
    assert not list(sub.drain())
    now[0] = 190
    owner.tick()
    assert [e.data['scope'] for e in sub.drain()] == ['global']


def test_float_presence_deadline_is_conservatively_rounded():
    bus = eventbus.EventBus()
    sub = bus.subscribe()
    owner = ReadEvents(bus, clock=lambda: 11)
    owner.note_deadline('room', 11.5, 10)
    assert owner._deadlines == {'room': 11}
    owner.tick()
    assert [e.chat_id for e in sub.drain()] == ['room']


def test_deadline_overflow_has_one_global_fallback_and_shutdown_discards():
    now = [1]
    bus = eventbus.EventBus()
    sub = bus.subscribe()
    owner = ReadEvents(bus, clock=lambda: now[0])
    for number in range(MAX_DEADLINES + 20):
        owner.note_deadline(str(number), 10, 1)
    assert len(owner._deadlines) == MAX_DEADLINES
    assert owner._overflow_deadline == 10
    now[0] = 10
    owner.tick()
    assert [e.data['scope'] for e in sub.drain()] == ['global']
    owner.close()
    owner.changed('sidebar', 'room')
    owner.note_deadline('room', 20, 10)
    assert not owner._deadlines and not list(sub.drain())


@pytest.fixture
def two_client_world(unread_world):
    # Two independent encrypted Mesh identities sharing a disposable local
    # transport. The content-free upstream wake below is deliberately simulated;
    # this proves canonical admission ordering, not remote Supabase latency.
    return unread_world


from test_gui_unread_counts import unread_world as unread_world  # noqa: E402
from test_gui_chat_pages import page_app as page_app  # noqa: E402


def test_two_client_edit_wake_before_admission_heals_without_broad_poll(two_client_world):
    from test_gui_chat_pages import _ready, _settled_page

    app, chat, peer = two_client_world
    message = peer.post(chat, 'original peer body')
    peer.outbox.flush_once()
    app.mesh.sync.sync_once([chat])
    runtime = _ready(app, chat)
    initial = _settled_page(app, chat)
    assert any(row['id'] == message.id and row['body'] == 'original peer body'
               for row in initial['messages'])
    sub = app.mesh.bus.subscribe()
    try:
        peer.edit(chat, message.id, 'edited peer body')
        app.mesh.bus.publish(eventbus.Event(eventbus.MIRROR_UPDATE, ns=1))
        assert [e.type for e in sub.drain()] == [eventbus.MIRROR_UPDATE]
        # The early hint does not authorize provider input on the HTTP path.
        before = _settled_page(app, chat)
        assert any(row['id'] == message.id and row['body'] == 'original peer body'
                   for row in before['messages'])
        assert runtime.ingest(chat)
        admitted = list(sub.drain())
        assert any(e.type == eventbus.READ_MODEL and e.chat_id == chat
                   and e.data == {'scope': 'chat'} for e in admitted)
        after = _settled_page(app, chat)
        assert any(row['id'] == message.id and row['body'] == 'edited peer body'
                   for row in after['messages'])
        list(sub.drain())
        assert not runtime.ingest(chat)
        assert not [e for e in sub.drain() if e.type == eventbus.READ_MODEL]
    finally:
        sub.close()


def test_unchanged_aux_presence_and_terminal_work_do_not_feed_back(two_client_world):
    from test_gui_chat_pages import _ready, _settled_page

    app, chat, _peer = two_client_world
    runtime = _ready(app, chat)
    assert _settled_page(app, chat)['status'] == 'page'
    sub = app.mesh.bus.subscribe()
    try:
        runtime.auxiliary.ingest('status')
        assert any(e.type == eventbus.READ_MODEL for e in sub.drain())
        runtime.auxiliary.ingest('status')
        assert not list(sub.drain())
        runtime.presence.ingest()
        assert any(e.type == eventbus.READ_MODEL for e in sub.drain())
        runtime.presence.ingest()
        assert not list(sub.drain())
        runtime.request_page(chat)
        runtime.prepare_one()
        list(sub.drain())
        runtime.request_page(chat)
        runtime.prepare_one()
        assert not list(sub.drain())
    finally:
        sub.close()


def test_unread_completion_has_one_sidebar_readiness_wake(two_client_world):
    from test_gui_unread_counts import _admit, _controlled, _drive, _records

    app, chat, peer = two_client_world
    _records(app, chat, peer, 70)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    sub = app.mesh.bus.subscribe()
    try:
        candidate, _history = _drive(app, chat, counter, now)
        assert candidate.complete
        events = [e for e in sub.drain() if e.type == eventbus.READ_MODEL
                  and e.data.get('scope') == 'sidebar']
        assert len(events) == 1 and events[0].chat_id == chat
        now[0] += 60
        assert counter.step().status == 'idle'
        assert not list(sub.drain())
    finally:
        sub.close()


def test_local_lock_policy_change_requests_one_bootstrap_without_data_poll():
    app, _bus, _sub, stream = _stream_world()
    assert _decode(next(stream)) == {'type': 'heartbeat'}
    app.lock.enabled = True
    app.lock.autolock_min = 5
    assert _decode(next(stream)) == {'type': 'control', 'reason': 'server_state_changed'}
    assert _decode(next(stream)) == {'type': 'heartbeat'}
    stream.close()


def test_event_first_capability_matches_bootstrap_and_sidebar_only_for_supabase(page_app, monkeypatch):
    from agentbridge.gui import api_chats
    from agentbridge.gui.routing import Request

    app, _chat = page_app
    token = app.capture_session_read()
    assert api_chats._bridge_state_captured(app, token)['caps']['sse_refresh_v1'] is False
    assert api_chats.state(app, Request())['caps']['sse_refresh_v1'] is False
    # Capability wiring only: do not contact or claim to test a remote provider.
    monkeypatch.setattr(type(app.mesh.tx), 'scheme', property(lambda _self: 'supabase'))
    assert api_chats._bridge_state_captured(app, token)['caps']['sse_refresh_v1'] is True
    assert api_chats.state(app, Request())['caps']['sse_refresh_v1'] is True
