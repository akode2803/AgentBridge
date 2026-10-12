"""Current canonical event preparation and drain-time notification boundaries."""
from __future__ import annotations

import pytest
import sqlite3

from agentbridge.mesh import eventbus
from agentbridge.mesh.eventbus import Event
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.node_event_preparation import NodeMessageEventPreparation, MAX_RETAINED_CHATS
from agentbridge.mesh.node_event_pump import NodeEventPump
from agentbridge.mesh.node_notifications import MessageNotificationOperation, MessageNotificationWake
from agentbridge.mesh.page_operation import PageWorkResult, PageOperationLimits
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_inputs, node_event_journal, page_inputs
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport.cache import CachingTransport
from test_page_operation import _message, _inputs, _gui_for_page


@pytest.fixture
def world(tmp_path, clouds):
    provider = clouds.bare(tmp_path / 'mesh')
    mirror = CachingTransport(provider, auto_refresh=False)
    mesh = Mesh(mirror, 'aryan', 'event-box', home=tmp_path / 'home', local_inputs=True)
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human('aryan', 'disposable-password')
        mirror.refresh()
        chat = mesh.membership.create_chat('Retained events')
        mirror.refresh()
        mesh.sync.sync_once([chat.id])
        base = max(m['joined_ns'] for m in mirror.get_doc(P.meta(chat.id))['members'].values()) + 1
        mesh.store.forget_chat(chat.id)
        mesh.store.upsert_messages(chat.id, [_message('one', base, 'alice'),
                                           _message('two', base + 1, 'aryan')])
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        mesh.store.prepare_terminal_observation()
        lifecycle_inputs.prepare(mesh.store._conn())
        reader = LocalPageSource(mesh.local_inputs.coordinator, mesh.store, chat.id)
        publisher = SourcePublisher(reader.coordinator, mesh.store, reader.definition)
        # Plain message sender authorization still requires event-time tenure.
        meta = mirror.get_doc(P.meta(chat.id))
        meta['members']['alice'] = {'role': 'member', 'joined_ns': base - 1}
        meta.setdefault('tenure', {})['alice'] = [[base - 1, 0]]
        mirror.put_doc(P.meta(chat.id), meta)
        _inputs(mesh, mirror, reader, publisher, chat.id)
        node_event_journal.initialize(mesh.store)
        yield mesh, mirror, provider, chat.id, base, reader, publisher
    finally:
        mesh.close()


def _enqueue(mesh, chat, base, *, kind='message', ident='one', sender='alice'):
    conn = mesh.store._conn()
    conn.execute('INSERT INTO node_event_journal_items'
        '(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)',
        (chat, ident, base, sender, kind))
    conn.commit()


def _notification(world, *, limits=PageOperationLimits()):
    mesh, _mirror, _provider, chat, base, reader, _publisher = world
    wake = MessageNotificationWake.capture(Event(eventbus.MESSAGE, chat,
                                               {'id': 'one', 'from': 'alice'}, base))
    operation = MessageNotificationOperation(mesh, wake, source_reader=reader, limits=limits)
    _reader, receipt, index = mesh.local_inputs.inputs(chat)
    result = operation.prepare(receipt, receipt, index)
    return operation, result


def test_retained_message_commits_before_bus_publish_and_leaves_info_pending(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    for i in range(40):
        _enqueue(mesh, chat, base, kind='info', ident=f'info{i}')
    _enqueue(mesh, chat, base)
    sub = mesh.bus.subscribe()
    pump = NodeEventPump.for_mesh(mesh)
    step = pump.run_due()
    assert step.operation == 'events' and step.records == 1 and not step.gap
    events = list(sub.drain())
    assert [(e.type, e.data) for e in events] == [
        (eventbus.MESSAGE, {'id': 'one', 'from': 'alice'}),
        (eventbus.READ_MODEL, {'scope': 'chat'}),
    ]
    assert node_event_journal.peek_event_identities(mesh.store, kind='message') == ()
    assert len(node_event_journal.peek_event_identities(mesh.store, kind='info')) == 32
    assert 'one' not in str(pump._preparation._jobs)  # completed jobs retain no page


def test_pending_source_keeps_identity_and_other_chat_can_progress(world, monkeypatch):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, 'a-pending', base, ident='missing')
    _enqueue(mesh, chat, base)
    worker = NodeMessageEventPreparation(mesh, clock=lambda: 1.0)
    original = mesh.local_inputs.inputs
    attempts = []
    def capture(candidate):
        attempts.append(candidate)
        return original(candidate)
    monkeypatch.setattr(mesh.local_inputs, 'inputs', capture)
    # The unresolved head room cannot block the valid room's next quantum.
    pending = worker.run_one()
    assert (pending.status, pending.chat_id) == ('pending', 'a-pending')
    assert worker.run_one().status == 'consumed'
    assert len(node_event_journal.peek_event_identities(mesh.store)) == 1
    assert worker.run_one().status == 'waiting'
    assert len(attempts) == 2
    assert len(worker._jobs) <= MAX_RETAINED_CHATS


def test_work_discards_page_and_retains_exact_identity(world, monkeypatch):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base)
    pending = node_event_journal.peek_event_identities(mesh.store)
    monkeypatch.setattr('agentbridge.mesh.node_event_preparation.ExactMessageProjectionOperation.prepare',
                        lambda *_args: PageWorkResult('work', 'overlay_proofs', ()))
    worker = NodeMessageEventPreparation(mesh)
    assert worker.run_one().status == 'pending'
    assert node_event_journal.peek_event_identities(mesh.store) == pending
    assert worker._jobs[chat].operation.ledger.rounds == 0
    assert not hasattr(worker._jobs[chat], 'prepared')


def test_batch_split_preserves_later_work_and_retires_only_terminal_single_id(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base, ident='not/a/basename')
    _enqueue(mesh, chat, base)
    worker = NodeMessageEventPreparation(mesh)
    assert worker.run_one().status == 'split'
    assert len(node_event_journal.peek_event_identities(mesh.store)) == 2
    retired = worker.run_one()
    assert retired.status == 'consumed' and retired.gap and retired.records == 1
    assert node_event_journal.claim_gap(mesh.store)
    good = worker.run_one()
    assert good.status == 'consumed' and not good.gap
    assert [m.id for m in good.messages] == ['one']


def test_notification_read_uses_finalized_policy_and_bounded_current_message(world, monkeypatch):
    mesh, mirror, provider, _chat, _base, _reader, _publisher = world
    def forbidden(*_args, **_kwargs):
        pytest.fail('notification invoked provider/mirror/full-fold')
    monkeypatch.setattr(mesh.messaging, 'snapshot', forbidden)
    monkeypatch.setattr(mesh.messaging, 'messages_for', forbidden)
    monkeypatch.setattr(mesh.messaging, 'state_of', forbidden)
    for transport in (mirror, provider):
        for method in ('get_doc', 'list_docs', 'read_log', 'list_logs'):
            monkeypatch.setattr(transport, method, forbidden)
    operation, result = _notification(world)
    assert result.status == 'prepared', result
    read = operation.finalize_notification(result.prepared)
    assert read.status == "ready"
    note = read.notification
    assert note and note.preview == 'one' and note.chat_name == 'Retained events'
    assert operation.finalize_notification(result.prepared).notification is None


@pytest.mark.parametrize('change', ['hidden', 'clear', 'read', 'mute', 'redact', 'remove'])
def test_wake_does_not_reuse_old_content_or_policy_after_change(world, change):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    state = {}
    if change == 'hidden':
        state['hidden'] = ['one']
    elif change == 'clear':
        state['cleared'] = {'ns': base + 1}
    elif change == 'read':
        state['read_ns'] = base
    elif change == 'mute':
        state['mute'] = True
    elif change == 'redact':
        mirror.put_doc(P.redaction(chat, 'one'), {'by': 'alice', 'ns': base + 2})
    elif change == 'remove':
        meta = mirror.get_doc(P.meta(chat))
        meta['members'].pop(mesh.user)
        mirror.put_doc(P.meta(chat), meta)
    _inputs(mesh, mirror, reader, publisher, chat, state=state)
    operation, result = _notification(world)
    assert result.status in {'prepared', 'forbidden'}, result
    if result.prepared is not None:
        assert operation.finalize_notification(result.prepared).notification is None


def test_changes_after_prepare_and_gui_lock_withhold_notification(world, tmp_path):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    operation, result = _notification(world)
    assert result.status == 'prepared'
    _inputs(mesh, mirror, reader, publisher, chat, state={'mute': True})
    assert operation.finalize_notification(result.prepared).notification is None
    _inputs(mesh, mirror, reader, publisher, chat, state={})
    operation, result = _notification(world)
    app, session = _gui_for_page(mesh, tmp_path / 'gate')
    app.lock.configure('disposable-lock')
    app.lock.lock()
    assert operation.finalize_notification(result.prepared, app=app, session=session).notification is None


def test_shutdown_and_identity_movement_withhold_notification(world):
    mesh, _mirror, _provider, _chat, _base, _reader, _publisher = world
    operation, result = _notification(world)
    assert result.status == 'prepared'
    mesh.messaging.machine = 'replacement'
    assert operation.finalize_notification(result.prepared).notification is None


def test_wake_shape_is_immutable_and_rejects_payloads(world):
    mesh, _mirror, _provider, chat, base, reader, _publisher = world
    event = Event(eventbus.MESSAGE, chat, {'id': 'one', 'from': 'alice'}, base)
    wake = MessageNotificationWake.capture(event)
    event.data['from'] = 'forged'
    operation = MessageNotificationOperation(mesh, wake, source_reader=reader)
    receipt = reader.capture()
    index = mesh.local_inputs.inputs(chat)[2]
    prepared = operation.prepare(receipt, receipt, index)
    assert operation.finalize_notification(prepared.prepared).notification.from_ == 'alice'
    with pytest.raises(ValueError):
        MessageNotificationWake.capture(Event(eventbus.MESSAGE, chat,
            {'id': 'one', 'from': 'alice', 'preview': 'secret'}, base))


def test_exhausted_attempt_recovers_after_cooldown_without_erasing_work(world, monkeypatch):
    import agentbridge.mesh.node_event_preparation as preparation
    from agentbridge.mesh.page_operation import _Work, ExactMessageProjectionOperation
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base)
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])
    original = ExactMessageProjectionOperation._prepare
    def stalled(*_args):
        raise _Work('source_refresh')
    def operation(*args, **kwargs):
        return ExactMessageProjectionOperation(*args, **kwargs,
            limits=PageOperationLimits(max_rounds=1))
    monkeypatch.setattr(preparation, 'ExactMessageProjectionOperation', operation)
    monkeypatch.setattr(ExactMessageProjectionOperation, '_prepare', stalled)
    assert worker.run_one().status == 'pending'
    clock[0] = 1.0
    assert worker.run_one().status == 'pending'
    assert worker._jobs[chat].operation is None
    assert worker._jobs[chat].exhausted_attempts == 1
    assert len(node_event_journal.peek_event_identities(mesh.store)) == 1
    monkeypatch.setattr(ExactMessageProjectionOperation, '_prepare', original)
    assert worker.run_one().status == 'waiting'
    clock[0] = 2.0
    assert worker.run_one().status == 'consumed'


def test_oversized_head_is_proven_terminal_without_stranding_valid_message(world):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    row = _message('oversized', base + 2, 'alice')
    row['padding'] = 'x' * (page_inputs.MAX_BYTES + 1)
    mesh.store.upsert_messages(chat, [row])
    _inputs(mesh, mirror, reader, publisher, chat)
    _enqueue(mesh, chat, base + 2, ident='oversized')
    _enqueue(mesh, chat, base)
    worker = NodeMessageEventPreparation(mesh)
    first = worker.run_one()
    assert first.status == 'consumed' and first.gap
    assert node_event_journal.claim_gap(mesh.store)
    second = worker.run_one()
    assert second.status == 'consumed' and not second.gap
    assert [m.id for m in second.messages] == ['one']


@pytest.mark.parametrize('failure', ['peek', 'retire', 'gap', 'delivery'])
def test_journal_storage_failures_yield_without_erasing_work(world, monkeypatch, failure):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base, ident='not/a/basename' if failure == 'retire' else 'one')
    original_peek = node_event_journal.peek_event_identities
    expected = original_peek(mesh.store)
    pump = NodeEventPump.for_mesh(mesh)
    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError('disposable lock failure')
    method = {'peek': 'peek_event_identities', 'retire': 'retire_event_identities_as_gap',
              'gap': 'claim_gap', 'delivery': 'delivery_page'}[failure]
    monkeypatch.setattr(node_event_journal, method, broken)
    if failure == 'delivery':
        pump._prefer_delivery = True
    step = pump.run_due()
    assert step.operation == 'events' and step.gap
    assert original_peek(mesh.store) == expected


def test_notification_conflict_is_distinct_from_current_suppression(world):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    operation, work = _notification(world)
    _inputs(mesh, mirror, reader, publisher, chat, state={'mute': True})
    failed = operation.finalize_notification(work.prepared)
    assert failed.status != 'ready' and failed.notification is None
    operation, work = _notification(world)
    suppressed = operation.finalize_notification(work.prepared)
    assert suppressed.status == 'ready' and suppressed.notification is None


def test_canonical_membership_denial_consumes_without_message_payload_or_retry(world):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    _enqueue(mesh, chat, base)
    meta = mirror.get_doc(P.meta(chat))
    meta['members'].pop(mesh.user)
    mirror.put_doc(P.meta(chat), meta)
    _inputs(mesh, mirror, reader, publisher, chat)
    step = NodeMessageEventPreparation(mesh).run_one()
    assert step.status == 'consumed' and not step.gap and step.messages == ()
    assert node_event_journal.peek_event_identities(mesh.store) == ()


@pytest.mark.parametrize('change', ['rejoin', 'journal_aba', 'post_consume_failure'])
def test_prepared_denial_is_final_fenced_and_consumption_rolls_back(world, change, monkeypatch):
    from agentbridge.mesh.page_operation import ExactMessageProjectionOperation
    from agentbridge.store import local_source
    mesh, mirror, _provider, chat, base, reader, publisher = world
    _enqueue(mesh, chat, base)
    expected = node_event_journal.peek_event_identities(mesh.store)
    meta = mirror.get_doc(P.meta(chat))
    member = meta['members'].pop(mesh.user)
    mirror.put_doc(P.meta(chat), meta)
    _inputs(mesh, mirror, reader, publisher, chat)
    receipt, index = mesh.local_inputs.inputs(chat)[1:]
    prepared = ExactMessageProjectionOperation(mesh, chat, ('one',), source_reader=reader,
        event_identities=expected).prepare(receipt, receipt, index)
    assert prepared.status == 'prepared'
    if change == 'rejoin':
        meta['members'][mesh.user] = member
        mirror.put_doc(P.meta(chat), meta)
        _inputs(mesh, mirror, reader, publisher, chat)
    elif change == 'journal_aba':
        mesh.store._conn().execute('UPDATE node_event_journal_items SET ns=ns+1')
        mesh.store._conn().commit()
    else:
        original = node_event_journal.consume_event_identities_in_transaction
        def failed_after_consume(*args):
            original(*args)
            raise local_source.SourceChanged('local_inputs_changed')
        monkeypatch.setattr(node_event_journal, 'consume_event_identities_in_transaction', failed_after_consume)
    final = prepared.prepared.finalize()
    assert final.status in {'restart', 'unavailable'} and final.result is None
    assert len(node_event_journal.peek_event_identities(mesh.store)) == 1


def test_unknown_kind_gap_does_not_consume_info_or_starve_message(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base, ident='info', kind='info')
    for i in range(3):
        _enqueue(mesh, chat, base, ident=f'future{i}', kind='future-kind')
    _enqueue(mesh, chat, base)
    worker = NodeMessageEventPreparation(mesh)
    first = worker.run_one()
    assert first.status == 'consumed' and first.gap and first.records == 1
    second = worker.run_one()
    assert second.status == 'consumed' and not second.gap and second.messages[0].id == 'one'
    for _ in range(4):
        worker.run_one()
    assert node_event_journal.peek_event_identities(mesh.store, kind='unknown') == ()
    assert len(node_event_journal.peek_event_identities(mesh.store, kind='info')) == 1


def test_near_ceiling_payload_is_terminal_only_when_irreducible_capture_floor_exceeds_cap(world):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    row = _message('near-cap', base + 2, 'alice')
    # Set valid JSON payload to exactly the cap; sender/ID/selector/mandatory
    # overlay bytes make the single capture irreducibly too large.
    import json
    payload = json.dumps(row, ensure_ascii=False)
    row['padding'] = 'x' * (page_inputs.MAX_BYTES - len(payload.encode()) - len(', "padding": ""'))
    assert len(json.dumps(row, ensure_ascii=False).encode()) == page_inputs.MAX_BYTES
    mesh.store.upsert_messages(chat, [row])
    _inputs(mesh, mirror, reader, publisher, chat)
    assert mesh.store._conn().execute('SELECT length(CAST(payload AS BLOB)) FROM messages '
        'WHERE chat_id=? AND id=?', (chat, 'near-cap')).fetchone()[0] == page_inputs.MAX_BYTES
    _enqueue(mesh, chat, base + 2, ident='near-cap')
    _enqueue(mesh, chat, base)
    worker = NodeMessageEventPreparation(mesh)
    first = worker.run_one()
    assert first.status == 'consumed' and first.gap
    assert worker.run_one().messages[0].id == 'one'


def test_generic_single_capture_budget_failure_is_not_terminal_size_evidence(world, monkeypatch):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base)
    monkeypatch.setattr('agentbridge.mesh.node_event_preparation.ExactMessageProjectionOperation.prepare',
                        lambda *_args: PageWorkResult('unavailable', 'budget_exhausted'))
    first = NodeMessageEventPreparation(mesh).run_one()
    assert first.status == 'pending' and not first.gap
    assert len(node_event_journal.peek_event_identities(mesh.store)) == 1


def test_message_schema_failure_before_prepare_yields_and_preserves_work(world, monkeypatch):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _enqueue(mesh, chat, base)
    original = node_event_journal.peek_event_identities
    pump = NodeEventPump.for_mesh(mesh)
    def changed(*_args, **_kwargs):
        raise page_inputs.PageInputsChanged('message_schema_changed')
    with monkeypatch.context() as patch:
        patch.setattr(node_event_journal, 'peek_event_identities', changed)
        step = pump.run_due()
        assert step.operation == 'events' and step.gap
    assert len(original(mesh.store)) == 1
