"""Exact sidebar unread counts are bounded work and freshly checked handouts.

The whole-history projection below is an independent, test-only oracle. Tests
advance worker quanta explicitly; a sidebar request must never do that work.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass

import pytest

from agentbridge.core.models import BodyRecord, Envelope, MsgKind
from agentbridge.gui import api_chats
from agentbridge.gui.routing import Request
from agentbridge.mesh.paths import P
from agentbridge.mesh.readmodel import unread_info
from agentbridge.mesh.service import Mesh

from test_gui_chat_pages import _ready, _settled_page, page_app as page_app


@pytest.fixture
def unread_world(page_app, clouds):
    """Real signed peer messages, with all asynchronous owners under test control."""
    app, original_chat = page_app
    app.mesh.accounts.create_human('peer', 'peer-pass')
    chat = api_chats.create_chat(app, Request(data={
        'name': 'Exact unread', 'members': ['peer'],
    }))['chat']['id']
    _ready(app, original_chat)
    _ready(app, chat)
    peer = Mesh(clouds.bare(app.root), 'peer', 'unread-peer', encrypt=True, home=app.home,
                store_path=app.home / 'unread-peer.sqlite')
    try:
        peer.sync.sync_once([chat])
        yield app, chat, peer
    finally:
        peer.close()


def _records(app, chat, sender, count, *, prefix='unread', start=None,
             tags=(), reply_to=None):
    """Avoid transport throughput in large-history tests; retain real crypto."""
    if start is None:
        meta = app.mesh.tx.get_doc(P.meta(chat))
        start = max(member['joined_ns'] for member in meta['members'].values()) + 10
    rows = []
    for number in range(count):
        ident, ns = f'{prefix}-{number:04d}', start + number
        body = BodyRecord(body=f'{prefix} private body {number}', tags=list(tags),
                          reply_to=reply_to)
        rows.append(Envelope(
            id=ident, ns=ns, ts='2026-01-01T00:00:00Z', from_=sender.user,
            kind=MsgKind.MESSAGE, **sender.sealer.seal(chat, ident, ns, body),
        ).to_dict())
    app.mesh.store.upsert_messages(chat, rows)
    if sender is not app.mesh:
        sender.store.upsert_messages(chat, rows)
    return rows


def _admit(app, chat):
    """Deliberate source admission, never an implicit side effect of polling."""
    runtime = _ready(app, chat)
    for _ in range(12):
        page = _settled_page(app, chat, limit='1')
        if page['status'] == 'page':
            return runtime
        assert page.get('reason') in ('terminal_classification_pending', 'overlay_proofs'), page
        assert runtime.prepare_one()
    pytest.fail(f'admitted source did not finish preparation: {page}')


def _oracle(app, chat):
    projection = app.mesh.conversation_projection(chat)
    return unread_info(list(projection.messages), app.mesh.user,
                       projection.viewer_state)


def _sidebar(app, chat):
    """Use the real bounded refresh/state routes and their final session checks."""
    api_chats.state(app, Request())  # Capture inventory and queue reconciliation.
    request = Request(data={'chat_id': chat})
    for _ in range(100):
        refreshed = api_chats.refresh_sidebar(app, request)
        result = api_chats.state(app, Request())
        row = next((item for item in result.get('chats', ())
                    if item['id'] == chat), None)
        if row is not None or (refreshed['status'] == 'ready'
                               and not refreshed['has_more']):
            return row
        app.mesh.local_inputs.prepare_one()
    pytest.fail('sidebar refresh did not converge')


def _assert_exact(row, oracle):
    assert row is not None
    assert row['unread_complete'] is True
    assert row['unread'] == row['unread_lower_bound'] == oracle['unread']
    assert row['first_unread_ns'] == oracle['first_unread_ns']
    assert row['mention'] is oracle['mention']
    assert row['forced_unread'] is oracle['forced_unread']


def _owned_values(value):
    """Inspect retained value records, without walking back into Mesh owners."""
    yield value
    if is_dataclass(value):
        for entry in fields(value):
            yield from _owned_values(getattr(value, entry.name))
    elif type(value) is dict:
        for key, child in value.items():
            yield from _owned_values(key)
            yield from _owned_values(child)
    elif type(value) in (tuple, list, set, frozenset):
        for child in value:
            yield from _owned_values(child)


def test_signed_history_fixture_has_nonzero_unread_oracle(unread_world):
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 3, tags=('viewer',))
    _admit(app, chat)
    assert _oracle(app, chat) == {
        'unread': 3, 'first_unread_ns': rows[0]['ns'],
        'mention': True, 'forced_unread': False,
    }


def _session(app):
    from agentbridge.mesh.unread_counts import UnreadSession
    token = app.capture_session_read()
    assert token is not None and token.mesh is app.mesh
    return UnreadSession(token.app_identity, token.generation, token.mesh.user)


def _controlled(app):
    runtime = app.mesh.local_inputs
    counter = runtime.unread
    assert counter._thread is None, 'deterministic fixture unexpectedly started a worker'
    now = [counter._clock()]
    counter._clock = lambda: now[0]
    return runtime, counter, now


def _drive(app, chat, counter, now, *, complete=True, rounds=100):
    session, history = _session(app), []
    assert counter.request(chat, session)
    for _ in range(rounds):
        value = counter.step()
        history.append((value.status, value.reason))
        assert 0 <= value.raw_examined <= 256
        assert 0 <= value.visible_examined <= 64
        current = counter.candidate(chat, session) if complete else counter._jobs[chat].progress
        if current is not None:
            assert current.complete is complete
            return current, history
        app.mesh.local_inputs.prepare_one()
        now[0] += 8.0  # Advance injected time, including the maximum retry delay.
    pytest.fail(f'unread worker did not converge: {history}')


def test_more_than_four_quanta_match_whole_history_oracle(unread_world):
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 321, tags=('viewer',))
    _admit(app, chat)
    expected = _oracle(app, chat)
    _runtime, counter, now = _controlled(app)
    candidate, history = _drive(app, chat, counter, now)
    assert sum(status == 'scanning' for status, _reason in history) >= 5
    assert candidate.count == len(rows) == expected['unread']
    assert candidate.first_unread_ns == rows[0]['ns']
    _assert_exact(_sidebar(app, chat), expected)


def test_high_read_cursor_still_scans_old_edited_message(unread_world, monkeypatch):
    from agentbridge.mesh import messaging
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 330)
    read_ns = rows[-1]['ns'] + 100
    app.mesh.messaging._state(chat)._merge(read_ns=read_ns)
    with monkeypatch.context() as patch:
        patch.setattr(messaging, 'next_ns', lambda: read_ns + 1)
        peer.edit(chat, rows[0]['id'], 'old message newly edited @viewer')
    _admit(app, chat)
    expected = _oracle(app, chat)
    assert expected == {'unread': 1, 'first_unread_ns': rows[0]['ns'],
                        'mention': True, 'forced_unread': False}
    _runtime, counter, now = _controlled(app)
    _candidate, history = _drive(app, chat, counter, now)
    assert sum(status == 'scanning' for status, _reason in history) >= 5
    _assert_exact(_sidebar(app, chat), expected)


def test_entire_invisible_quantum_advances_raw_boundary_without_false_completion(unread_world):
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 600)
    app.mesh.hide(chat, [row['id'] for row in rows[1:]])
    _admit(app, chat)
    expected = _oracle(app, chat)
    _runtime, counter, now = _controlled(app)
    first, history = _drive(app, chat, counter, now, complete=False)
    assert history[-1][0] == 'scanning'
    assert first.count == 0 and first.before is not None
    assert first.before.id == rows[-256]['id']
    assert counter.candidate(chat, _session(app)) is None
    final, _history = _drive(app, chat, counter, now)
    assert final.count == 1 and final.first_unread_ns == rows[0]['ns']
    _assert_exact(_sidebar(app, chat), expected)


@pytest.mark.parametrize('filtering', ['hidden', 'clear', 'redact', 'mention', 'reply', 'forced'])
def test_canonical_visibility_and_mention_semantics(unread_world, filtering):
    app, chat, peer = unread_world
    parent = app.mesh.post(chat, 'own parent')
    rows = _records(app, chat, peer, 70, start=parent.ns + 10,
                    tags=('all',) if filtering == 'mention' else (),
                    reply_to={'id': parent.id, 'from': 'viewer'} if filtering == 'reply' else None)
    if filtering == 'hidden':
        app.mesh.hide(chat, [row['id'] for row in rows[::2]])
    elif filtering == 'clear':
        app.mesh.star(chat, [rows[0]['id']])
        app.mesh.clear_chat(chat, keep_starred=True)
    elif filtering == 'redact':
        peer.redact(chat, [rows[0]['id']])
    elif filtering == 'forced':
        app.mesh.messaging._state(chat)._merge(read_ns=rows[-1]['ns'] + 1,
                                               forced_unread=True)
    _admit(app, chat)
    expected = _oracle(app, chat)
    _runtime, counter, now = _controlled(app)
    _drive(app, chat, counter, now)
    _assert_exact(_sidebar(app, chat), expected)


def test_foreground_never_drives_background_or_full_history(unread_world, monkeypatch):
    app, chat, peer = unread_world
    _records(app, chat, peer, 140)
    runtime = _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    _drive(app, chat, counter, now)

    def forbidden(*_args, **_kwargs):
        pytest.fail('foreground performed background work or a history/provider walk')

    monkeypatch.setattr(counter, 'step', forbidden)
    monkeypatch.setattr(counter, '_run_quantum', forbidden)
    monkeypatch.setattr(runtime, 'prepare_one', forbidden)
    monkeypatch.setattr(runtime, 'ingest', forbidden)
    monkeypatch.setattr(app.mesh.messaging, 'conversation_projection', forbidden)
    monkeypatch.setattr(app.mesh.messaging, 'messages_for', forbidden)
    monkeypatch.setattr(app.mesh.store, 'messages', forbidden)
    monkeypatch.setattr(app.mesh.tx._transport, 'get_doc', forbidden)
    monkeypatch.setattr(app.mesh.tx._transport, 'list_docs', forbidden)
    row = _sidebar(app, chat)
    assert row is not None and row['unread_complete'] and row['unread'] == 140


def test_retained_candidate_and_progress_contain_comparison_scalars_only(unread_world):
    from agentbridge.core.models import Message
    from agentbridge.mesh.epoch_inputs import EpochObservation, IdentityObservation
    from agentbridge.mesh.page_operation import PageOperation
    app, chat, peer = unread_world
    _records(app, chat, peer, 100, prefix='unique-private-plaintext')
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    first, _history = _drive(app, chat, counter, now, complete=False)
    final, _history = _drive(app, chat, counter, now)
    for candidate in (first, final, counter._jobs[chat]):
        values = tuple(_owned_values(candidate))
        assert not any(isinstance(value, (Message, EpochObservation, IdentityObservation,
                                         PageOperation, bytes, bytearray)) for value in values)
        assert not any(type(value) is str and 'unique-private-plaintext private body' in value
                       for value in values)
        assert not any(type(value) is dict and {'body', 'ct', 'resident'} & value.keys()
                       for value in values)


def test_queue_capacity_and_repeated_requests_preserve_job_order(page_app):
    app, _chat = page_app
    _runtime, counter, _now = _controlled(app)
    session = _session(app)
    for number in range(128):
        assert counter.request(f'queued-{number}', session)
    original = [(name, job.order, job.due, job.failures) for name, job in counter._jobs.items()]
    for _ in range(5):
        for number in reversed(range(128)):
            assert counter.request(f'queued-{number}', session)
    assert [(name, job.order, job.due, job.failures) for name, job in counter._jobs.items()] == original
    assert not counter.request('overflow', session)
    assert len(counter._jobs) == 128


def test_dispatch_rate_and_retry_backoff_survive_request_churn(page_app, monkeypatch):
    from agentbridge.mesh.unread_runtime import UnreadStep
    app, chat = page_app
    _runtime, counter, now = _controlled(app)
    session, attempts = _session(app), []

    def pending(selected, _job):
        attempts.append((selected, now[0]))
        return None, UnreadStep('pending', selected, reason='cold-test-inputs')

    monkeypatch.setattr(counter, '_run_quantum', pending)
    assert counter.request(chat, session)
    for delay in (.5, 1., 2., 4., 8., 8.):
        assert counter.step().status == 'pending'
        due = counter._jobs[chat].due
        assert due == pytest.approx(now[0] + delay)
        for _ in range(10):
            assert counter.request(chat, session, selected=True)
            assert counter._jobs[chat].due == due
        now[0] += .249
        assert counter.step().status == 'idle'
        now[0] = due
    assert len(attempts) == 6


def test_selected_and_pending_jobs_do_not_starve_round_robin(page_app, monkeypatch):
    from agentbridge.mesh.unread_runtime import UnreadStep
    app, _chat = page_app
    _runtime, counter, now = _controlled(app)
    session, calls = _session(app), []

    def pending(chat, _job):
        calls.append(chat)
        return None, UnreadStep('pending', chat, reason='independent-pending-source')

    monkeypatch.setattr(counter, '_run_quantum', pending)
    for chat in ('selected', 'normal-one', 'normal-two'):
        assert counter.request(chat, session, selected=chat == 'selected')
    for _ in range(6):
        for chat in reversed(('selected', 'normal-one', 'normal-two')):
            assert counter.request(chat, session, selected=chat == 'selected')
        assert counter.step().status == 'pending'
        now[0] += 8
    assert calls == ['selected', 'selected', 'normal-one',
                     'selected', 'selected', 'normal-two']


def _change_dependency(app, chat, peer, rows, change):
    if change == 'delayed_raw':
        _records(app, chat, peer, 1, prefix='late-old', start=rows[0]['ns'] - 1)
    elif change == 'viewer_state':
        app.mesh.messaging._state(chat)._merge(read_ns=rows[-1]['ns'])
        _admit(app, chat)
    elif change == 'overlay':
        app.mesh.hide(chat, [rows[-1]['id']])
        _admit(app, chat)
    elif change == 'trust':
        app.mesh.key_pins.forget('peer')
    elif change == 'epoch':
        app.mesh.keys._cache.clear()
    elif change == 'lifecycle_head':
        from agentbridge.store.lifecycle_heads import PREFIX
        with app.mesh.store._conn() as conn:
            conn.execute('UPDATE docs SET payload=payload || ? WHERE path=?',
                         (' ', PREFIX + 'peer'))
    else:
        raise AssertionError(change)


@pytest.mark.parametrize('change', ['delayed_raw', 'viewer_state', 'overlay',
                                     'trust', 'epoch', 'lifecycle_head'])
def test_interquantum_dependency_changes_restart_without_double_count(unread_world, change):
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 150)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    partial, _history = _drive(app, chat, counter, now, complete=False)
    assert partial.count == 64
    _change_dependency(app, chat, peer, rows, change)
    now[0] += 8
    observed = counter.step()
    assert observed.status not in ('complete', 'scanning'), observed
    assert counter.candidate(chat, _session(app)) is None
    expected = _oracle(app, chat)
    final, history = _drive(app, chat, counter, now)
    assert final.count == expected['unread'], history
    _assert_exact(_sidebar(app, chat), expected)


@pytest.mark.parametrize('change', ['delayed_raw', 'viewer_state', 'overlay',
                                     'trust', 'epoch', 'lifecycle_head'])
def test_stale_complete_candidate_falls_back_to_ordinary_summary(unread_world, change):
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 150)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    old, _history = _drive(app, chat, counter, now)
    assert old.count == 150
    _change_dependency(app, chat, peer, rows, change)
    # There is no background step or replacement finalizer between mutation
    # and handout. The sidebar must independently discover the stale evidence.
    row = _sidebar(app, chat)
    assert row is not None, 'a stale aggregate poisoned the normal recent summary'
    assert row['unread_complete'] is False
    assert row['unread_lower_bound'] <= 50
    assert row['unread'] != old.count
    assert row['last'] is not None
    assert counter.candidate(chat, _session(app)) is None


def test_later_quantum_clock_rollback_does_not_hide_behind_earliest_time(unread_world, monkeypatch):
    from agentbridge.mesh import membership_coordinator
    app, chat, peer = unread_world
    _records(app, chat, peer, 200)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    wall = [membership_coordinator.time.time_ns()]
    monkeypatch.setattr(membership_coordinator.time, 'time_ns', lambda: wall[0])
    first, _history = _drive(app, chat, counter, now, complete=False)
    wall[0] += 100
    now[0] += 8
    assert counter.step().status == 'scanning'
    second = counter._jobs[chat].progress
    assert second.evidence.observed_ns == first.evidence.observed_ns
    assert second.evidence.validated_ns == wall[0]
    wall[0] -= 50  # Still after first quantum, but before the most recent one.
    now[0] += 8
    rolled_back = counter.step()
    assert rolled_back.status not in ('complete', 'scanning'), rolled_back
    assert counter.candidate(chat, _session(app)) is None
    assert counter._jobs[chat].progress is None


def test_prior_quantum_dependencies_are_recaptured_even_when_not_in_recent_page(unread_world, monkeypatch):
    from agentbridge.mesh import epoch_inputs, membership_coordinator
    app, chat, peer = unread_world
    # The peer only occurs in the older window, outside the recent summary.
    older = _records(app, chat, peer, 1, tags=('viewer',))
    _records(app, chat, app.mesh, 90, prefix='own-newer', start=older[-1]['ns'] + 1)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    complete, _history = _drive(app, chat, counter, now)
    assert 'peer' in complete.evidence.accounts
    assert any(name == 'peer' for name, _digest in complete.evidence.heads)
    assert complete.evidence.epochs
    actors, epochs = [], []
    original_get = membership_coordinator._Round.get
    original_epoch = epoch_inputs.capture_epoch

    def get(round_, name):
        actors.append(name)
        return original_get(round_, name)

    def epoch(service, selected, number, **kwargs):
        epochs.append(number)
        return original_epoch(service, selected, number, **kwargs)

    monkeypatch.setattr(membership_coordinator._Round, 'get', get)
    monkeypatch.setattr(epoch_inputs, 'capture_epoch', epoch)
    _assert_exact(_sidebar(app, chat), _oracle(app, chat))
    assert 'peer' in actors
    assert set(number for number, _digest in complete.evidence.epochs).issubset(epochs)


def test_one_operation_attempt_per_quantum_has_bounded_ledger(unread_world, monkeypatch):
    from agentbridge.mesh import unread_runtime
    app, chat, peer = unread_world
    _records(app, chat, peer, 150)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    operations = []
    original = unread_runtime.PageOperation

    def operation(*args, **kwargs):
        value = original(*args, **kwargs)
        operations.append(value)
        assert kwargs['limit'] <= 64 and kwargs['scan_budget'] <= 256
        limits = value.ledger.operation_limits
        assert limits.max_rounds == 1
        assert limits.max_steps <= 2048 and limits.max_crypto <= 2048
        assert limits.max_bytes <= 16 * 1024 * 1024
        return value

    monkeypatch.setattr(unread_runtime, 'PageOperation', operation)
    assert counter.request(chat, _session(app))
    for _ in range(30):
        before = len(operations)
        value = counter.step()
        assert len(operations) - before <= 1
        if value.status == 'complete':
            break
        app.mesh.local_inputs.prepare_one()
        now[0] += 8
    else:
        pytest.fail('bounded operations never completed')
    assert operations and all(op.ledger.rounds == 1 for op in operations)


def test_session_advance_discards_old_counts_and_rejects_late_enqueue(unread_world):
    app, chat, peer = unread_world
    _records(app, chat, peer, 80)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    old_session = _session(app)
    _drive(app, chat, counter, now)
    with app._lock:
        app._advance_session_generation()
        app._session_read_ready = True  # Re-adopt the same Mesh in the new session.
    assert counter.candidate(chat, old_session) is None
    assert not counter.request(chat, old_session)
    assert counter.request(chat, _session(app))
    assert counter.candidate(chat, _session(app)) is None


def test_clear_during_inflight_quantum_rejects_late_publication(unread_world, monkeypatch):
    import threading
    app, chat, peer = unread_world
    _records(app, chat, peer, 1)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    old, _history = _drive(app, chat, counter, now)
    counter.invalidate(chat, _session(app))
    now[0] += 8
    entered, release, results = threading.Event(), threading.Event(), []
    from agentbridge.mesh.unread_runtime import UnreadStep

    def delayed(_chat, _job):
        entered.set()
        assert release.wait(5)
        return old, UnreadStep('complete', chat)

    monkeypatch.setattr(counter, '_run_quantum', delayed)
    worker = threading.Thread(target=lambda: results.append(counter.step()))
    worker.start()
    try:
        assert entered.wait(5)
        with app._lock:
            app._advance_session_generation()
            app._session_read_ready = True
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert [value.status for value in results] == ['stale']
        assert counter.candidate(chat, old.session) is None
        assert counter.candidate(chat, _session(app)) is None
    finally:
        release.set()
        worker.join(5)


def test_stop_joins_inflight_work_without_gui_lock_and_discards_candidate(unread_world, monkeypatch):
    import threading
    from agentbridge.mesh.unread_runtime import UnreadStep
    app, chat, peer = unread_world
    _records(app, chat, peer, 1)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    old, _history = _drive(app, chat, counter, now)
    counter.invalidate(chat, old.session)
    now[0] += 8
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def delayed(_chat, _job):
        try:
            # Real GUI locks are held in the caller; worker must never need them.
            entered.set()
            assert release.wait(5)
            return old, UnreadStep('complete', chat)
        except BaseException as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(counter, '_run_quantum', delayed)
    worker = threading.Thread(target=counter.step)
    stopper = threading.Thread(target=lambda: (counter.stop(), stopped.set()))
    try:
        with app._lock, app.lock._mx:
            worker.start()
            assert entered.wait(5)
            stopper.start()
            assert not stopped.wait(.05), 'stop returned while a quantum still owned inputs'
            release.set()
            worker.join(5)
            stopper.join(5)
        assert stopped.is_set() and not errors
        assert not worker.is_alive() and not stopper.is_alive()
        assert counter.candidate(chat, old.session) is None
        assert not counter.request(chat, old.session)
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(5)
        if stopper.ident is not None:
            stopper.join(5)


def test_future_lifecycle_deadline_invalidates_completed_count_without_source_change(
        unread_world, monkeypatch):
    from agentbridge import crypto
    from agentbridge.core.jsonkit import canonical_json_bytes
    from agentbridge.mesh import lifecycle, membership_coordinator
    app, chat, peer = unread_world
    wall = [membership_coordinator.time.time_ns()]
    monkeypatch.setattr(membership_coordinator.time, 'time_ns', lambda: wall[0])
    current = lifecycle.resolve_lifecycle(app.mesh.directory, 'peer', store=app.mesh.store)
    future = {**current, 'id': 'future-peer-state',
              'ns': wall[0] + lifecycle._FUTURE_SKEW_NS + 100,
              'action': 'state', 'previous_id': current['id'],
              'active': False, 'deactivated': ''}
    app.mesh.tx.put_doc(lifecycle._path('peer', future['id']), {
        'record': future, 'sig': crypto.sign(peer.keystore.load('peer'),
                                            canonical_json_bytes(future)),
        'subject_sig': '',
    })
    older = _records(app, chat, app.mesh, 140, prefix='own-old')
    _records(app, chat, peer, 1, start=older[-1]['ns'] + 1)
    runtime = _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    complete, _history = _drive(app, chat, counter, now)
    assert complete.evidence.deadline_ns == wall[0] + 100
    assert complete.count == 1
    position = runtime.inputs(chat)[1:]
    head = app.mesh.store.observe_lifecycle_head('peer')
    wall[0] += 100  # Inclusive future-admission edge; no documents are written.
    assert runtime.inputs(chat)[1:] == position
    assert app.mesh.store.observe_lifecycle_head('peer') == head
    row = _sidebar(app, chat)
    assert row is not None and row['unread_complete'] is False
    assert counter.candidate(chat, _session(app)) is None


def test_completed_candidate_rejects_clock_rollback_after_later_handout(unread_world, monkeypatch):
    from agentbridge.mesh import membership_coordinator
    app, chat, peer = unread_world
    _records(app, chat, peer, 100)
    _admit(app, chat)
    expected = _oracle(app, chat)
    _runtime, counter, now = _controlled(app)
    wall = [membership_coordinator.time.time_ns()]
    monkeypatch.setattr(membership_coordinator.time, 'time_ns', lambda: wall[0])
    complete, _history = _drive(app, chat, counter, now)
    wall[0] += 100
    _assert_exact(_sidebar(app, chat), expected)
    wall[0] -= 50  # Newer than scan completion, older than successful handout.
    assert wall[0] > complete.evidence.validated_ns
    row = _sidebar(app, chat)
    assert row is not None and row['unread_complete'] is False
    assert counter.candidate(chat, _session(app)) is None


@pytest.mark.parametrize('kind', ['accounts', 'heads', 'epochs', 'bytes'])
def test_dependency_evidence_hard_caps_fail_closed(unread_world, kind):
    from dataclasses import replace
    from agentbridge.mesh import membership_coordinator, unread_counts
    app, chat, peer = unread_world
    _records(app, chat, peer, 1)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    candidate, _history = _drive(app, chat, counter, now)
    changes = {
        'accounts': {'accounts': tuple(f'actor-{n}' for n in range(65))},
        'heads': {'heads': tuple((f'subject-{n}', 'a' * 64) for n in range(65))},
        'epochs': {'epochs': tuple((n, 'b' * 64) for n in range(65))},
        'bytes': {'accounts': ('x' * (64 * 1024),)},
    }
    with pytest.raises(membership_coordinator._Stop) as raised:
        unread_counts._bounded(replace(candidate.evidence, **changes[kind]))
    assert raised.value.reason == 'unread_dependency_budget'
    assert unread_counts.MAX_JOBS * unread_counts.MAX_EVIDENCE_BYTES <= 8 * 1024 * 1024


def test_count_over_safe_integer_limit_never_publishes_exact_result(unread_world):
    from dataclasses import replace
    from agentbridge.mesh.unread_counts import MAX_COUNT
    app, chat, peer = unread_world
    _records(app, chat, peer, 70)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    partial, _history = _drive(app, chat, counter, now, complete=False)
    assert MAX_COUNT == 2**53 - 1
    counter._jobs[chat].progress = replace(partial, count=MAX_COUNT)
    now[0] += 8
    step = counter.step()
    assert step.status == 'pending' and step.reason == 'unread_count_budget'
    assert counter.candidate(chat, _session(app)) is None
    row = _sidebar(app, chat)
    assert row is not None and row['unread_complete'] is False
    assert row['unread_lower_bound'] <= 50


def test_real_quantum_does_not_acquire_gui_session_or_screen_locks(unread_world):
    import threading
    app, chat, peer = unread_world
    _records(app, chat, peer, 3)
    _admit(app, chat)
    _runtime, counter, _now = _controlled(app)
    assert counter.request(chat, _session(app))
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(counter.step()))
    with app._lock, app.lock._mx:
        worker.start()
        worker.join(5)
    worker.join(5)
    assert not worker.is_alive() and outcomes
    assert outcomes[0].status in ('complete', 'scanning', 'pending', 'reset_required')


def test_history_on_join_excludes_old_raw_rows_but_still_reaches_exhaustion(unread_world):
    app, chat, peer = unread_world
    app.mesh.membership.set_permissions(chat, {'send_history': False})
    joined = app.mesh.messaging.snapshot(chat).members['viewer'].joined_ns
    _records(app, chat, peer, 300, prefix='pre-join', start=joined - 400)
    visible = _records(app, chat, peer, 70, prefix='post-join', start=joined + 10)
    _admit(app, chat)
    expected = _oracle(app, chat)
    assert expected['unread'] == len(visible)
    _runtime, counter, now = _controlled(app)
    complete, history = _drive(app, chat, counter, now)
    assert complete.count == len(visible)
    assert sum(status == 'scanning' for status, _reason in history) >= 2
    _assert_exact(_sidebar(app, chat), expected)


def test_forged_signed_state_cannot_zero_unread_or_force_badge(unread_world):
    app, chat, peer = unread_world
    _records(app, chat, peer, 80, tags=('viewer',))
    app.mesh.tx.put_doc(P.state(chat, app.mesh.user), {
        'ns': 1, 'read_ns': 2**63 - 1, 'forced_unread': True,
        'hidden': [], 'sig': 'forged-unread-state',
    })
    _admit(app, chat)
    expected = _oracle(app, chat)
    assert expected['unread'] == 80 and not expected['forced_unread']
    _runtime, counter, now = _controlled(app)
    _drive(app, chat, counter, now)
    _assert_exact(_sidebar(app, chat), expected)


@pytest.mark.parametrize('change', ['epoch', 'trust', 'lifecycle_head'])
def test_dependency_race_after_recapture_cannot_pass_real_final_gates(
        unread_world, monkeypatch, change):
    from agentbridge.mesh import page_operation
    app, chat, peer = unread_world
    rows = _records(app, chat, peer, 100)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    _drive(app, chat, counter, now)
    original_key, changed = page_operation._Sealer.key, []

    def change_after_observation(sealer, selected, epoch):
        value = original_key(sealer, selected, epoch)
        if selected == chat and not changed:
            changed.append(change)
            # Use the actual observation then race its mutable local input.
            # No finalizer, canonical verdict or sidebar output is replaced.
            _change_dependency(app, chat, peer, rows, change)
        return value

    monkeypatch.setattr(page_operation._Sealer, 'key', change_after_observation)
    row = _sidebar(app, chat)
    assert changed == [change]
    assert row is not None and row['unread_complete'] is False
    assert row['unread_lower_bound'] <= 50
    assert counter.candidate(chat, _session(app)) is None


def test_double_session_advance_rejects_both_delayed_generations(unread_world):
    app, chat, peer = unread_world
    _records(app, chat, peer, 3)
    _admit(app, chat)
    _runtime, counter, now = _controlled(app)
    first = _session(app)
    _drive(app, chat, counter, now)
    with app._lock:
        app._advance_session_generation()
        app._session_read_ready = True
    middle = _session(app)
    with app._lock:
        app._advance_session_generation()
        app._session_read_ready = True
    newest = _session(app)
    assert not counter.request(chat, first)
    assert not counter.request(chat, middle)
    assert counter.request(chat, newest)
    assert counter.candidate(chat, first) is None
    assert counter.candidate(chat, middle) is None


def test_session_advance_before_first_job_establishes_generation_floor(page_app):
    app, chat = page_app
    _runtime, counter, _now = _controlled(app)
    old = _session(app)
    assert not counter._jobs
    with app._lock:
        app._advance_session_generation()
        app._session_read_ready = True
    current = _session(app)
    assert not counter.request(chat, old)
    assert counter.request(chat, current)


def test_previously_captured_candidate_cannot_republish_after_newer_handout(
        unread_world, monkeypatch):
    from agentbridge.mesh import membership_coordinator
    app, chat, peer = unread_world
    _records(app, chat, peer, 100)
    _admit(app, chat)
    expected = _oracle(app, chat)
    _runtime, counter, now = _controlled(app)
    wall = [membership_coordinator.time.time_ns()]
    monkeypatch.setattr(membership_coordinator.time, 'time_ns', lambda: wall[0])
    old, _history = _drive(app, chat, counter, now)
    wall[0] += 100
    _assert_exact(_sidebar(app, chat), expected)
    current = counter.candidate(chat, _session(app))
    assert current is not old
    original_candidate = counter.candidate
    monkeypatch.setattr(counter, 'candidate', lambda selected, session:
                        old if selected == chat else original_candidate(selected, session))
    wall[0] += 100
    row = _sidebar(app, chat)
    assert row is not None and row['unread_complete'] is False
    assert row['unread_lower_bound'] <= 50
