"""Retained notification identities and drain-time authority boundaries."""
from __future__ import annotations

import pytest

from agentbridge.mesh import eventbus
from agentbridge.mesh.eventbus import Event
from agentbridge.mesh.node_notification_preparation import (
    MAX_COMPLETED, MAX_JOBS, NodeNotificationPreparation,
)
from agentbridge.mesh.node_notifications import MessageNotificationOperation
from agentbridge.mesh.page_operation import PageWorkResult
from test_page_operation import _gui_for_page
from test_retained_node_events import _inputs

pytest_plugins = ['test_retained_node_events']


def _event(world, *, chat=None, ident='one', sender='alice', ns=None):
    _mesh, _mirror, _provider, current_chat, base, _reader, _publisher = world
    return Event(eventbus.MESSAGE, chat or current_chat,
                 {'id': ident, 'from': sender}, base if ns is None else ns)


def test_pending_wake_recovers_without_new_event_and_uses_current_policy(
        world, monkeypatch):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    clock = [0.0]
    worker = NodeNotificationPreparation(mesh, clock=lambda: clock[0])
    assert worker.enqueue(_event(world))
    original = MessageNotificationOperation.prepare
    calls = 0

    def first_pending(self, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return PageWorkResult('work', 'source_refresh')
        return original(self, *args)

    monkeypatch.setattr(MessageNotificationOperation, 'prepare', first_pending)
    first = worker.run_one()
    assert (first.status, first.reason, first.notification) == (
        'pending', 'source_refresh', None)
    assert len(worker._jobs) == 1 and worker.wait_s() == 0.35
    _inputs(mesh, mirror, reader, publisher, chat, state={'mute': True})
    clock[0] = 0.35
    second = worker.run_one()
    assert second.status == 'ready' and second.notification is None
    assert calls == 2 and not worker._jobs
    assert not worker.enqueue(_event(world))  # content-free completed dedup


def test_due_job_rotates_past_cooling_other_chat(world):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    clock = [0.0]
    worker = NodeNotificationPreparation(mesh, clock=lambda: clock[0])
    assert worker.enqueue(_event(world, chat='pending-room', ident='missing'))
    assert worker.enqueue(_event(world))
    first = worker.run_one()
    assert first.status == 'pending' and first.wake.chat_id == 'pending-room'
    second = worker.run_one()
    assert second.status == 'ready' and second.notification.preview == 'one'
    assert worker.run_one().status == 'waiting'
    assert worker.wait_s(2.0) == 0.35


def test_bounded_overflow_gap_replacement_and_duplicate_preserve_backoff(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    clock = [0.0]
    worker = NodeNotificationPreparation(mesh, clock=lambda: clock[0])
    for i in range(MAX_JOBS + 1):
        assert worker.enqueue(_event(world, ident=f'identity-{i}'))
    assert len(worker._jobs) == MAX_JOBS
    assert worker.take_gap() is True and worker.take_gap() is False
    assert (chat, 'identity-0') not in worker._jobs
    key = (chat, 'identity-1')
    job = worker._jobs[key]
    job.next_due = 8.0
    assert not worker.enqueue(_event(world, ident='identity-1'))
    assert worker._jobs[key] is job and job.next_due == 8.0
    assert worker.enqueue(_event(world, ident='identity-1', ns=base + 1))
    assert worker._jobs[key] is not job
    assert worker._jobs[key].operation is None
    assert worker._jobs[key].next_due == 0.0
    assert len(worker._jobs) == MAX_JOBS


def test_completed_dedup_is_bounded_and_content_free(world):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    worker = NodeNotificationPreparation(mesh)
    assert worker.enqueue(_event(world))
    result = worker.run_one()
    assert result.status == 'ready' and result.notification.preview == 'one'
    assert not worker.enqueue(_event(world))
    assert worker.enqueue(_event(world, ns=result.wake.ns + 1))
    assert len(worker._completed) == 0
    assert set(worker._jobs[(chat, 'one')].__dict__) == {
        'wake', 'operation', 'next_due', 'exhausted_attempts'}
    worker.close()
    assert not worker._jobs and not worker._completed
    assert not worker.enqueue(_event(world))
    assert MAX_COMPLETED == 128


@pytest.mark.parametrize('change', ('session', 'lock', 'owner'))
def test_session_lock_or_owner_change_permanently_closes_work(
        world, tmp_path, change):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    app, session = _gui_for_page(mesh, tmp_path / 'gate')
    worker = NodeNotificationPreparation(mesh, app=app, session=session)
    assert worker.enqueue(_event(world))
    if change == 'session':
        app._session_generation += 1
    elif change == 'lock':
        app.lock.configure('disposable-lock')
        app.lock.lock()
    else:
        mesh.messaging.machine = 'replacement'
    assert worker.run_one().status == 'closed'
    assert not worker._jobs
    assert not worker.enqueue(_event(world))


def test_operation_budget_cools_then_restarts_without_gap(world, monkeypatch):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    clock = [0.0]
    worker = NodeNotificationPreparation(mesh, clock=lambda: clock[0])
    assert worker.enqueue(_event(world))
    original = MessageNotificationOperation.prepare
    calls = 0

    def exhausted_once(self, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return PageWorkResult('unavailable', 'operation_round_budget')
        return original(self, *args)

    monkeypatch.setattr(MessageNotificationOperation, 'prepare', exhausted_once)
    first = worker.run_one()
    assert first.status == 'pending' and first.reason == 'operation_round_budget'
    job = worker._jobs[(chat, 'one')]
    assert job.operation is None and job.exhausted_attempts == 1
    assert job.next_due == 0.7 and worker.run_one().status == 'waiting'
    clock[0] = 0.7
    second = worker.run_one()
    assert second.status == 'ready' and second.notification.preview == 'one'
    assert not worker.take_gap()


def test_overlay_work_requests_only_current_page_proofs(world, monkeypatch):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    worker = NodeNotificationPreparation(mesh, clock=lambda: 0.0)
    assert worker.enqueue(_event(world))
    requested = []
    monkeypatch.setattr(MessageNotificationOperation, 'prepare',
                        lambda *_args: PageWorkResult('work', 'overlay_proofs', ('proof',)))
    monkeypatch.setattr(mesh.local_inputs, 'request_page',
                        lambda *args, **kwargs: requested.append((args, kwargs)))
    monkeypatch.setattr(mesh.local_inputs, 'request',
                        lambda *_args, **_kwargs: pytest.fail('unexpected source request'))
    assert worker.run_one().status == 'pending'
    assert requested[0][0] == (chat,)
    assert requested[0][1]['proofs'] == ('proof',)
    assert requested[0][1]['index'] is not None


def test_expected_errors_and_failed_owner_scheduler_retain_identity(
        world, monkeypatch):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    clock = [0.0]
    worker = NodeNotificationPreparation(mesh, clock=lambda: clock[0])
    assert worker.enqueue(_event(world))

    def unavailable(_chat):
        raise OSError('disposable input failure')

    def failed_request(*_args, **_kwargs):
        raise RuntimeError('disposable schedule failure')

    with monkeypatch.context() as patch:
        patch.setattr(mesh.local_inputs, 'inputs', unavailable)
        patch.setattr(mesh.local_inputs, 'request', failed_request)
        first = worker.run_one()
    assert first.status == 'pending' and first.reason == 'storage_unavailable'
    assert (chat, 'one') in worker._jobs and worker.wait_s() == 0.35
    clock[0] = 0.35
    assert worker.run_one().status == 'ready'


def test_programmer_error_is_not_swallowed(world, monkeypatch):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    worker = NodeNotificationPreparation(mesh)
    assert worker.enqueue(_event(world))

    def bug(*_args):
        raise RuntimeError('programmer error')

    monkeypatch.setattr(MessageNotificationOperation, 'prepare', bug)
    with pytest.raises(RuntimeError, match='programmer error'):
        worker.run_one()
    assert len(worker._jobs) == 1


def test_reentrant_notifier_stop_closes_queued_work_before_handout(world):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    worker = NodeNotificationPreparation(mesh)
    notifier = mesh.notifier
    notifier._node_preparation = worker
    handed_out = []
    notifier.add_sink(lambda note: (handed_out.append(note), notifier.stop()))
    assert worker.enqueue(_event(world))
    step = worker.run_one(notifier=notifier,
                          notifier_generation=notifier._delivery_generation)
    assert len(handed_out) == 1
    assert step.status == 'closed' and step.notification is None
    assert not worker._jobs and not worker.enqueue(_event(world))


def test_notifier_generation_is_forwarded_only_when_supplied(world, monkeypatch):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    original = MessageNotificationOperation.finalize_notification
    seen = []

    def record(self, prepared, **kwargs):
        seen.append(kwargs.copy())
        kwargs.pop('notifier', None)
        kwargs.pop('notifier_generation', None)
        return original(self, prepared, **kwargs)

    monkeypatch.setattr(MessageNotificationOperation, 'finalize_notification', record)
    first = NodeNotificationPreparation(mesh)
    assert first.enqueue(_event(world))
    assert first.run_one().status == 'ready'
    assert seen[-1] == {'app': None, 'session': None}
    second = NodeNotificationPreparation(mesh)
    assert second.enqueue(_event(world))
    assert second.run_one(notifier=object(), notifier_generation=7).status == 'ready'
    assert seen[-1]['notifier_generation'] == 7
    assert 'notifier' in seen[-1]
    with pytest.raises(ValueError, match='requires a notifier'):
        second.run_one(notifier_generation=8)


def test_malformed_message_never_enters_queue(world):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    worker = NodeNotificationPreparation(mesh)
    assert not worker.enqueue(Event(eventbus.READ_MODEL, '', {}))
    bad = _event(world)
    bad.data['preview'] = 'forbidden payload'
    with pytest.raises(ValueError):
        worker.enqueue(bad)
    assert not worker._jobs
