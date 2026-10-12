"""Actual GUI/background drains use fresh canonical reads and consumer gates."""
from __future__ import annotations

import json
import threading
import time

import pytest

from agentbridge.gui.sse import stream
from agentbridge.mesh import eventbus
from agentbridge.mesh.eventbus import Event
from agentbridge.mesh.node_notifications import MessageNotificationOperation
from agentbridge.mesh.notify import Notification
from agentbridge.mesh.page_operation import PageWorkResult
from test_page_operation import _gui_for_page, _inputs
from test_retained_node_events import _notification, world as world


def _wake(world):
    _mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    return Event(eventbus.MESSAGE, chat, {'id': 'one', 'from': 'alice'}, base)


def _decode(value):
    assert value.startswith(b'data: ')
    return json.loads(value[6:])


def _gui(mesh, home):
    app, token = _gui_for_page(mesh, home)
    app.home = home
    app._latency_sinks = {}
    return app, token


def _defer_once(monkeypatch):
    original = MessageNotificationOperation.prepare
    pending = threading.Event()

    def prepare(self, *args):
        if not pending.is_set():
            pending.set()
            return PageWorkResult('work', 'source_refresh')
        return original(self, *args)

    monkeypatch.setattr(MessageNotificationOperation, 'prepare', prepare)
    return pending


def test_background_pending_wake_retries_while_idle_without_new_event(world, monkeypatch):
    mesh = world[0]
    pending = _defer_once(monkeypatch)
    delivered = threading.Event()
    received = []
    mesh.notifier.add_sink(lambda note: (received.append(note), delivered.set()))
    mesh.notifier.start()  # Do not start ingestion, harnesses or provider polling.
    mesh.bus.publish(_wake(world))
    assert pending.wait(3)
    assert delivered.wait(5), 'retained wake did not progress without a new event'
    assert len(received) == 1 and received[0].preview == 'one'
    assert mesh.notifier._node_preparation._jobs == {}
    mesh.bus.publish(_wake(world))
    # A direct duplicate is also suppressed by the same bounded completion set.
    assert mesh.notifier.deliver(_wake(world)) is None
    mesh.notifier.stop()
    assert len(received) == 1


def test_background_retry_reads_new_mute_policy_without_legacy_fallback(world, monkeypatch):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    pending = _defer_once(monkeypatch)
    received = []
    mesh.notifier.add_sink(received.append)
    assert mesh.notifier.deliver(_wake(world)) is None
    assert pending.is_set()
    _inputs(mesh, mirror, reader, publisher, chat, state={'mute': True})
    for method in ('snapshot', 'messages_for', 'state_of'):
        monkeypatch.setattr(mesh.messaging, method,
            lambda *_args: pytest.fail('node notification used a legacy read'))
    preparation = mesh.notifier._node_preparation
    preparation.clock = lambda: time.monotonic() + 1
    assert mesh.notifier._drain_node() is None
    assert not received and not preparation._jobs


def test_background_old_generation_cannot_deliver_after_restart(world):
    mesh = world[0]
    operation, work = _notification(world)
    received = []
    mesh.notifier.add_sink(received.append)
    generation = mesh.notifier._delivery_generation
    mesh.notifier.stop()
    mesh.notifier.start()
    result = operation.finalize_notification(work.prepared, notifier=mesh.notifier,
                                             notifier_generation=generation)
    assert result.status == 'unavailable'
    assert result.reason == 'notification_consumer_changed'
    mesh.notifier.stop()
    assert not received


def test_background_stop_waits_for_immediate_handout_then_blocks_later_wakes(world):
    mesh = world[0]
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    received = []

    def sink(note):
        entered.set()
        assert release.wait(5)
        received.append(note)

    mesh.notifier.add_sink(sink)
    sender = threading.Thread(target=lambda: mesh.notifier.deliver(_wake(world)))
    sender.start()
    assert entered.wait(3)
    stopper = threading.Thread(target=lambda: (mesh.notifier.stop(), stopped.set()))
    stopper.start()
    assert not stopped.wait(.05)
    release.set()
    sender.join(5)
    stopper.join(5)
    assert not sender.is_alive() and not stopper.is_alive() and stopped.is_set()
    assert len(received) == 1
    assert mesh.notifier.deliver(_wake(world)) is None
    assert len(received) == 1


def test_background_sink_can_stop_notifier_without_job_lock_inversion(world):
    mesh = world[0]
    received = []

    def sink(note):
        received.append(note)
        mesh.notifier.stop()

    mesh.notifier.add_sink(sink)
    sender = threading.Thread(target=lambda: mesh.notifier.deliver(_wake(world)))
    sender.start()
    sender.join(5)
    assert not sender.is_alive()
    assert len(received) == 1 and mesh.notifier._node_preparation is None


def test_gui_retained_wake_gets_notification_without_delaying_message_frame(world, tmp_path, monkeypatch):
    mesh = world[0]
    app, token = _gui(mesh, tmp_path / 'gui')
    pending = _defer_once(monkeypatch)
    sub = mesh.bus.subscribe()
    output = stream(app, sub, .01, token=token)
    assert next(output) == b': connected\n\n'
    mesh.bus.publish(_wake(world))
    first = _decode(next(output))
    assert first['type'] == 'message' and first['id'] == 'one' and 'notify' not in first
    assert not pending.is_set()  # Content-free data handout precedes notification work.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = _decode(next(output))
        if result['type'] == 'notification':
            break
        assert result['type'] == 'heartbeat', result
    else:
        pytest.fail('idle GUI stream did not retry its retained wake')
    assert result['id'] == 'one' and result['notify']['preview'] == 'one'
    output.close()
    sub.close()


@pytest.mark.parametrize('change', ['lock', 'session'])
def test_gui_route_owner_change_withholds_pending_notification(world, tmp_path, monkeypatch, change):
    mesh = world[0]
    app, token = _gui(mesh, tmp_path / 'gui')
    _defer_once(monkeypatch)
    sub = mesh.bus.subscribe()
    output = stream(app, sub, .01, token=token)
    assert next(output) == b': connected\n\n'
    mesh.bus.publish(_wake(world))
    assert _decode(next(output))['type'] == 'message'
    assert _decode(next(output))['type'] == 'heartbeat'
    if change == 'lock':
        app.lock.configure('disposable-lock')
        app.lock.lock()
    else:
        app._session_generation += 1
    control = _decode(next(output))
    assert control == {'type': 'control', 'reason': 'locked' if change == 'lock' else 'session_changed'}
    with pytest.raises(StopIteration):
        next(output)
    sub.close()


def test_callback_releases_ingestion_locks_before_immediate_handout(world):
    mesh = world[0]
    entered, release = threading.Event(), threading.Event()
    done, errors = threading.Event(), []

    def sink(_note):
        entered.set()
        assert release.wait(5)

    def ingestion_probe():
        try:
            # Source work must not wait behind a slow external notification sink.
            with mesh.local_inputs._worker_lock:
                with mesh.local_inputs._lock:
                    mesh.local_inputs.inputs(world[3])
            done.set()
        except Exception as exc:
            errors.append(exc)

    mesh.notifier.add_sink(sink)
    sender = threading.Thread(target=lambda: mesh.notifier.deliver(_wake(world)))
    sender.start()
    assert entered.wait(3)
    probe = threading.Thread(target=ingestion_probe)
    probe.start()
    try:
        assert done.wait(3), errors
    finally:
        release.set()
        sender.join(5)
        probe.join(5)
    assert not sender.is_alive() and not probe.is_alive() and not errors


def test_callback_stop_does_not_join_a_drain_waiting_for_delivery_gate(world):
    mesh = world[0]
    preparation = None
    entered, second_locked, stop_done = threading.Event(), threading.Event(), threading.Event()
    release_second = threading.Event()
    received = []

    def sink(note):
        received.append(note)
        entered.set()
        assert second_locked.wait(3)
        mesh.notifier.stop()
        stop_done.set()

    mesh.notifier.add_sink(sink)
    # Create the actual owner before blocking two competing drain scopes.
    mesh.notifier._queue_node(_wake(world))
    preparation = mesh.notifier._node_preparation

    def competing_job():
        # This reproduces the historical jobs->delivery acquisition rather than
        # relying on scheduler timing. Cancellation cannot join this job lock.
        assert entered.wait(3)
        with preparation._lock:
            second_locked.set()
            with mesh.notifier._delivery_gate:
                release_second.set()

    second = threading.Thread(target=competing_job, daemon=True)
    # A legacy callback and an identity-only drain can share the notifier.
    # The legacy callback holds the delivery gate without owning this job lock.
    first = threading.Thread(target=lambda: mesh.notifier._deliver_note(
        Notification('message', world[3], 'room', 'alice', 'one', world[4])), daemon=True)
    second.start()
    first.start()
    first.join(5)
    second.join(5)
    assert stop_done.is_set() and release_second.is_set()
    assert not first.is_alive() and not second.is_alive() and len(received) == 1
    assert preparation.run_one().status == 'closed' and not preparation._jobs


def test_idle_background_without_sinks_does_not_duplicate_gui_projection(world, monkeypatch):
    mesh = world[0]
    observed = threading.Event()
    original = mesh.notifier._queue_node

    def enqueue(event, **kwargs):
        result = original(event, **kwargs)
        observed.set()
        return result

    monkeypatch.setattr(mesh.notifier, '_queue_node', enqueue)
    monkeypatch.setattr(mesh.local_inputs, 'inputs',
        lambda *_args: pytest.fail('background read without a notification sink'))
    mesh.notifier.start()
    mesh.bus.publish(_wake(world))
    assert observed.wait(3)
    mesh.notifier.stop()
    assert mesh.notifier._node_preparation is None
