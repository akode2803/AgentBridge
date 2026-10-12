"""Current canonical event preparation and drain-time notification boundaries."""
from __future__ import annotations

import pytest
import sqlite3

from agentbridge import crypto
from agentbridge.mesh import eventbus
from agentbridge.mesh import events
from agentbridge.mesh.eventbus import Event
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.node_event_preparation import NodeMessageEventPreparation, MAX_RETAINED_CHATS
from agentbridge.mesh.node_event_pump import NodeEventPump
from agentbridge.mesh.node_notifications import MessageNotificationOperation, MessageNotificationWake
from agentbridge.mesh.node_notification_preparation import NodeNotificationPreparation
from agentbridge.mesh.node_reactions import ReactionProjectionOperation
from agentbridge.mesh.overlays import ChatOverlays
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


def _signed_reaction(world, *, emoji='👍', target='two', event_id='reaction-info',
                     actor='alice', publish_document=True, hint_changes=None):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    bundle = mesh.keys.keystore.load(actor, upgrade=False)
    if bundle is None:
        mesh.accounts.create_human(actor, 'disposable-password')
        bundle = mesh.keys.keystore.load(actor, upgrade=False)
    assert bundle is not None
    meta = mirror.get_doc(P.meta(chat))
    if actor not in meta['members']:
        meta['members'][actor] = {'role': 'member', 'joined_ns': base - 1}
        meta.setdefault('tenure', {})[actor] = [[base - 1, 0]]
        mirror.put_doc(P.meta(chat), meta)
    operation = ChatOverlays(mirror, chat).set_reaction(
        actor, target, emoji,
        signer=lambda data: crypto.sign(bundle, data),
    )
    document = mirror.get_doc(P.reactions(chat, actor))
    if not publish_document:
        mirror.delete_doc(P.reactions(chat, actor))
    hint = {'type': events.EV_REACTION, 'msg_id': target, 'emoji': emoji,
            'to': 'forged-recipient', 'reaction_id': operation.id,
            'reaction_ns': operation.ns}
    hint.update(hint_changes or {})
    env = {'id': event_id, 'ns': operation.ns + 1, 'ts': '', 'from': actor,
           'kind': 'info', 'event': hint}
    env['sig'] = crypto.sign(bundle, events.signing_bytes(chat, env))
    mesh.store.upsert_messages(chat, (env,))
    _inputs(mesh, mirror, reader, publisher, chat)
    _enqueue(mesh, chat, env['ns'], kind='info', ident=event_id, sender=actor)
    return env, document


def test_signed_native_reaction_maps_to_identity_wake_and_fresh_notification(world):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    env, _document = _signed_reaction(world)
    sub = mesh.bus.subscribe()
    clock = [0.0]
    pump = NodeEventPump.for_mesh(mesh, clock=lambda: clock[0])

    first = pump.run_due()
    clock[0] = 0.35
    step = pump.run_due()

    assert first.operation == 'preparation'
    assert step.operation == 'events' and step.records == 1 and not step.gap
    emitted = list(sub.drain())
    reaction = next(event for event in emitted if event.type == eventbus.REACTION)
    assert reaction.data == {'id': env['id'], 'from': 'alice'}
    assert 'emoji' not in reaction.data and 'to' not in reaction.data
    notifications = NodeNotificationPreparation(mesh)
    assert notifications.enqueue(reaction)
    result = notifications.run_one()
    if result.status == 'pending':
        notifications._jobs[('reaction', chat, env['id'])].next_due = 0.0
        result = notifications.run_one()
    assert result.status == 'ready'
    assert result.notification.kind == 'reaction'
    assert result.notification.from_ == 'alice'
    assert result.notification.emoji == '👍'
    assert result.notification.preview == 'Reacted 👍 to your message'
    assert node_event_journal.peek_event_identities(mesh.store, kind='info') == ()


def test_authentic_reaction_waits_for_delayed_document_then_maps(world):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    env, document = _signed_reaction(world, publish_document=False)
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    first = worker.run_one()

    assert first.status == 'pending'
    assert len(node_event_journal.peek_event_identities(mesh.store, kind='info')) == 1
    clock[0] = 0.35
    pending_document = worker.run_one()
    assert pending_document.status == 'pending'
    mirror.put_doc(P.reactions(chat, 'alice'), document)
    _inputs(mesh, mirror, reader, publisher, chat)
    next(iter(worker._info_jobs.values())).next_due = 0.0
    second = worker.run_one()
    assert second.status == 'consumed' and not second.gap
    assert [(event.type, event.data) for event in second.reactions] == [
        (eventbus.REACTION, {'id': env['id'], 'from': 'alice'}),
    ]


def test_authentic_reaction_waits_for_delayed_target_then_maps(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    env, _document = _signed_reaction(world, target='future-target')
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    assert worker.run_one().status == 'pending'
    clock[0] = .35
    assert worker.run_one().status == 'pending'
    assert len(node_event_journal.peek_event_identities(mesh.store, kind='info')) == 1
    mesh.store.upsert_messages(
        chat, (_message('future-target', base + 3, 'aryan'),),
    )
    next(iter(worker._info_jobs.values())).next_due = 0.0
    final = worker.run_one()
    assert final.status == 'consumed' and not final.gap
    assert [(event.type, event.data) for event in final.reactions] == [
        (eventbus.REACTION, {'id': env['id'], 'from': 'alice'}),
    ]


def test_reaction_cannot_attach_to_target_created_after_operation(world):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    env, _document = _signed_reaction(world, target='future-target')
    reaction_ns = env['event']['reaction_ns']
    mesh.store.upsert_messages(
        chat, (_message('future-target', reaction_ns + 1, 'aryan'),),
    )
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    first = worker.run_one()
    clock[0] = .35
    final = worker.run_one() if first.status == 'pending' else first

    assert final.status == 'consumed' and not final.gap
    assert final.reactions == ()
    assert node_event_journal.peek_event_identities(
        mesh.store, kind='info') == ()


def test_reaction_decode_resource_failure_remains_retryable(world, monkeypatch):
    mesh, _mirror, _provider, chat, _base, reader, _publisher = world
    _signed_reaction(world)
    identities = node_event_journal.peek_event_identities(mesh.store, kind='info')
    operation = ReactionProjectionOperation(
        mesh, chat, tuple(item.message_id for item in identities),
        source_reader=reader, event_identities=identities,
    )
    _reader, receipt, index = mesh.local_inputs.inputs(chat)

    monkeypatch.setattr(
        page_inputs.SerializedMessage, 'decoded',
        lambda _self: (_ for _ in ()).throw(MemoryError()),
    )
    result = operation.prepare(receipt, receipt, index)

    assert result.status == 'unavailable'
    assert result.reason == 'resource_unavailable'
    assert node_event_journal.peek_event_identities(
        mesh.store, kind='info') == identities


def test_missing_actor_tenure_cannot_authorize_semantic_reaction(world):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    _env, _document = _signed_reaction(world)
    meta = mirror.get_doc(P.meta(chat))
    meta.get('tenure', {}).pop('alice', None)
    mirror.put_doc(P.meta(chat), meta)
    _inputs(mesh, mirror, reader, publisher, chat)
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    first = worker.run_one()
    clock[0] = .35
    final = worker.run_one() if first.status == 'pending' else first

    assert final.status == 'consumed' and final.reactions == () and not final.gap


def test_missing_reaction_document_does_not_starve_later_info(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    _signed_reaction(world, publish_document=False)
    generic = {'id': 'later-generic', 'ns': base + 2, 'from': 'bob',
               'kind': 'info', 'event': {'type': 'renamed', 'name': 'ignored'}}
    mesh.store.upsert_messages(chat, (generic,))
    _enqueue(mesh, chat, generic['ns'], kind='info', ident=generic['id'], sender='bob')
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    statuses = []
    for index in range(8):
        clock[0] = index * .35
        statuses.append(worker.run_one().status)
        remaining = node_event_journal.peek_event_identities(mesh.store, kind='info')
        if len(remaining) == 1:
            break

    remaining = node_event_journal.peek_event_identities(mesh.store, kind='info')
    assert [item.message_id for item in remaining] == ['reaction-info']
    assert statuses == ['pending', 'consumed']


def test_multiple_blocked_reactions_rotate_retry_and_do_not_starve_tail(world):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    alice, alice_document = _signed_reaction(
        world, event_id='alice-reaction', actor='alice', publish_document=False,
    )
    bob, bob_document = _signed_reaction(
        world, event_id='bob-reaction', actor='bob', publish_document=False,
    )
    generic = {'id': 'later-generic', 'ns': max(alice['ns'], bob['ns']) + 1,
               'from': 'carol', 'kind': 'info',
               'event': {'type': 'renamed', 'name': 'ignored'}}
    mesh.store.upsert_messages(chat, (generic,))
    _enqueue(mesh, chat, generic['ns'], kind='info', ident=generic['id'], sender='carol')
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    first_round = []
    for index in range(3):
        clock[0] = index * .35
        first_round.append(worker.run_one())

    assert [step.status for step in first_round] == ['pending', 'pending', 'consumed']
    assert [item.message_id for item in node_event_journal.peek_event_identities(
        mesh.store, kind='info')] == ['alice-reaction', 'bob-reaction']

    mirror.put_doc(P.reactions(chat, 'alice'), alice_document)
    mirror.put_doc(P.reactions(chat, 'bob'), bob_document)
    _inputs(mesh, mirror, reader, publisher, chat)
    reactions = []
    for index in range(3, 11):
        # A growing later tail cannot extend the finite sweep and prevent the
        # two retained identities from receiving another turn.
        later = {'id': f'continuous-{index}', 'ns': base + 100 + index,
                 'from': 'carol', 'kind': 'info',
                 'event': {'type': 'renamed', 'name': 'ignored'}}
        mesh.store.upsert_messages(chat, (later,))
        _enqueue(mesh, chat, later['ns'], kind='info', ident=later['id'], sender='carol')
        clock[0] = index * .35
        step = worker.run_one()
        reactions.extend(step.reactions)
        if len(reactions) == 2:
            break

    assert {(event.data['id'], event.data['from']) for event in reactions} == {
        ('alice-reaction', 'alice'), ('bob-reaction', 'bob'),
    }


@pytest.mark.parametrize('change', ('hint', 'tampered_document', 'resealed'))
def test_unproven_or_stale_reaction_is_terminal_generic_invalidation(world, change):
    mesh, mirror, _provider, chat, _base, reader, publisher = world
    hint = {'emoji': 'forged'} if change == 'hint' else None
    env, document = _signed_reaction(world, hint_changes=hint)
    if change in {'tampered_document', 'resealed'}:
        document = dict(document)
        if change == 'tampered_document':
            document['sig'] = document['sig'][:-1] + ('A' if document['sig'][-1] != 'A' else 'B')
        else:
            document['ops_origin'] = 'resealed'
        mirror.put_doc(P.reactions(chat, 'alice'), document)
        _inputs(mesh, mirror, reader, publisher, chat)

    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])
    first = worker.run_one()
    clock[0] = 0.35
    step = worker.run_one() if first.status == 'pending' else first

    assert step.status == 'consumed' and not step.gap and step.reactions == ()
    assert node_event_journal.peek_event_identities(mesh.store, kind='info') == ()
    assert env['event']['to'] == 'forged-recipient'


def test_mixed_equal_ns_info_batch_emits_only_verified_reaction(world):
    mesh, _mirror, _provider, chat, _base, _reader, _publisher = world
    env, _document = _signed_reaction(world)
    unrelated = {'id': 'same-ns-other-sender', 'ns': env['ns'], 'ts': '',
                 'from': 'bob', 'kind': 'info',
                 'event': {'type': 'renamed', 'name': 'ignored'}}
    mesh.store.upsert_messages(chat, (unrelated,))
    _enqueue(mesh, chat, unrelated['ns'], kind='info', ident=unrelated['id'], sender='bob')
    clock = [0.0]
    worker = NodeMessageEventPreparation(mesh, clock=lambda: clock[0])

    completed = []
    for index in range(4):
        clock[0] = index * .35
        step = worker.run_one()
        if step.status == 'consumed':
            completed.append(step)
        if not node_event_journal.peek_event_identities(mesh.store, kind='info'):
            break

    assert [step.records for step in completed] == [1, 1]
    reactions = tuple(event for step in completed for event in step.reactions)
    assert [(event.ns, event.data) for event in reactions] == [
        (env['ns'], {'id': env['id'], 'from': 'alice'}),
    ]
    assert node_event_journal.peek_event_identities(mesh.store, kind='info') == ()


@pytest.mark.parametrize('change', ('journal', 'source'))
def test_reaction_finalization_rejects_changed_cut_without_consuming(world, change):
    mesh, mirror, _provider, chat, base, reader, publisher = world
    row = {'id': 'generic-info', 'ns': base + 2, 'from': 'alice',
           'kind': 'info', 'event': {'type': 'renamed', 'name': 'ignored'}}
    mesh.store.upsert_messages(chat, (row,))
    _inputs(mesh, mirror, reader, publisher, chat)
    _enqueue(mesh, chat, row['ns'], kind='info', ident=row['id'])
    identities = node_event_journal.peek_event_identities(mesh.store, kind='info')
    operation = ReactionProjectionOperation(
        mesh, chat, tuple(item.message_id for item in identities),
        source_reader=reader, event_identities=identities,
    )
    _reader, receipt, index = mesh.local_inputs.inputs(chat)
    work = operation.prepare(receipt, receipt, index)
    assert work.status == 'prepared'
    if change == 'journal':
        mesh.store._conn().execute(
            'UPDATE node_event_journal_items SET ns=ns+1 WHERE chat=?', (chat,),
        )
        mesh.store._conn().commit()
    else:
        mirror.put_doc(P.reactions(chat, 'alice'), {'legacy': 'changed'})
        _inputs(mesh, mirror, reader, publisher, chat)

    result = work.prepared.finalize()

    assert result.status in {'restart', 'unavailable'} and result.result is None
    assert len(node_event_journal.peek_event_identities(mesh.store, kind='info')) == 1


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


def test_info_and_reaction_rows_emit_only_exact_content_free_invalidation(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    rows = (
        {'id': 'membership-info', 'ns': base + 2, 'from': 'alice', 'kind': 'info',
         'event': {'type': 'renamed', 'name': 'private-name'}},
        {'id': 'reaction-info', 'ns': base + 3, 'from': 'alice', 'kind': 'info',
         'event': {'type': 'reaction', 'msg_id': 'one', 'emoji': 'secret-emoji'}},
    )
    mesh.store.upsert_messages(chat, rows)
    for row in rows:
        _enqueue(mesh, chat, row['ns'], kind='info', ident=row['id'])
    sub = mesh.bus.subscribe()

    pump = NodeEventPump.for_mesh(mesh)
    steps = (pump.run_due(), pump.run_due())

    assert all(step.operation == 'events' and step.records == 1 and not step.gap
               for step in steps)
    received = list(sub.drain())
    assert [(event.type, event.chat_id, event.data) for event in received] == [
        (eventbus.READ_MODEL, chat, {'scope': 'chat'}),
        (eventbus.READ_MODEL, chat, {'scope': 'chat'}),
    ]
    assert 'private-name' not in str(received)
    assert 'secret-emoji' not in str(received)
    assert node_event_journal.peek_event_identities(mesh.store, kind='info') == ()


def test_info_invalidation_rotates_with_continuous_message_work(world):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    info = {'id': 'info', 'ns': base + 2, 'from': 'alice', 'kind': 'info',
            'event': {'type': 'reaction', 'msg_id': 'one', 'emoji': 'x'}}
    mesh.store.upsert_messages(chat, (info,))
    _enqueue(mesh, chat, info['ns'], kind='info', ident=info['id'])
    _enqueue(mesh, chat, base)
    worker = NodeMessageEventPreparation(mesh)

    first = worker.run_one()
    second = worker.run_one()

    assert first.status == 'consumed' and first.messages[0].id == 'one'
    assert second.status == 'consumed' and second.records == 1 \
        and second.messages == () and not second.gap


def test_same_chat_info_invalidation_preserves_retained_message_retry(world, monkeypatch):
    mesh, _mirror, _provider, chat, base, _reader, _publisher = world
    info = {'id': 'info', 'ns': base + 2, 'from': 'alice', 'kind': 'info',
            'event': {'type': 'reaction', 'msg_id': 'one', 'emoji': 'x'}}
    mesh.store.upsert_messages(chat, (info,))
    _enqueue(mesh, chat, info['ns'], kind='info', ident=info['id'])
    _enqueue(mesh, chat, base)
    monkeypatch.setattr(
        'agentbridge.mesh.node_event_preparation.ExactMessageProjectionOperation.prepare',
        lambda *_args: PageWorkResult('work', 'overlay_proofs', ()),
    )
    worker = NodeMessageEventPreparation(mesh, clock=lambda: 10.0)

    assert worker.run_one().status == 'pending'
    retained = worker._jobs[chat]
    retry_due = retained.next_due
    info_step = worker.run_one()

    assert info_step.status == 'consumed' and info_step.messages == ()
    assert worker._jobs[chat] is retained
    assert retained.next_due == retry_due
    assert tuple(item.message_id for item in retained.identities) == ('one',)


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


def test_unknown_kind_gap_does_not_starve_message_or_info_invalidation(world):
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
    assert node_event_journal.peek_event_identities(mesh.store, kind='info') == ()


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
