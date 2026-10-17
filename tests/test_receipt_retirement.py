"""Explicit epoch tickets free bounded storage without UUID rebinding."""
from dataclasses import replace
import sqlite3
import uuid

import pytest
import test_operation_journal as journal_cases

from agentbridge.store import local_source
from agentbridge.transport.receipt_retirement import (
    OperationTicket, ProviderEpoch, decode_provider_epoch, observe_provider_epoch,
)
from agentbridge.transport.typed_mutations import TypedMutationRPC, TypedMutationRPCError
from test_operation_journal import (
    ACTOR, ENDPOINT, EPOCH, _receipt, _reload, _request,
)

world = journal_cases.world

LIFECYCLE = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
NEXT = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


def _epoch(generation=0, epoch=EPOCH):
    return ProviderEpoch(ENDPOINT, 'mesh', ACTOR, LIFECYCLE, epoch, generation)


def _finish(journal, ticket, *, acknowledge=True, release=True):
    request = journal.get_ticket(ticket).request
    journal.mark_attempted(request)
    journal.accept(request, _receipt(request))
    journal.complete(request)
    if acknowledge:
        journal.mark_acknowledged(request)
    if release:
        journal.release_ticket(ticket)
    return request


def test_closed_ticket_is_pruned_and_never_reprepared_or_rebound(world):
    root, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {'ns': 1}})
    original = _finish(journal, ticket)
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 1
    restored = _reload(world)
    with pytest.raises(local_source.SourceChanged, match='missing'):
        restored.get_ticket(ticket)
    with pytest.raises(local_source.SourceChanged, match='ticket_required'):
        restored.prepare(original)
    with pytest.raises(local_source.SourceChanged, match='ticket_required'):
        restored.prepare_operation(replace(original, expectation=replace(original.expectation, protocol_epoch=NEXT)))
    newer = restored.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {'ns': 2}})
    assert newer.protocol_epoch == NEXT and newer.operation_id != ticket.operation_id
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (1,)


def test_rotation_recovers_lost_ack_reply_but_keeps_opaque_records(world):
    _, _, _, _, journal = world
    legacy = _request()
    journal.prepare_operation(legacy)
    journal.mark_attempted(legacy)
    journal.accept(legacy, _receipt(legacy))
    journal.complete(legacy)
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    _finish(journal, ticket, acknowledge=False)
    assert journal._adopt_epoch(_epoch(3, NEXT)) == 1
    assert journal.get_operation(legacy.expectation.operation_id).state == 'acknowledged'
    assert journal.prepare_operation(legacy).state == 'acknowledged'
    assert journal._adopt_epoch(_epoch(3, NEXT)) == 0


# A durable receipt never acknowledged contradicts closure; ticketed attempts
# without receipts are proven unexecuted (tests/test_closure_resolution.py).
@pytest.mark.parametrize('state', ['accepted'])
def test_ambiguous_or_uncompleted_ticket_prevents_retirement(world, state):
    _, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    request = journal.get_ticket(ticket).request
    if state != 'prepared':
        journal.mark_attempted(request)
    if state == 'accepted':
        journal.accept(request, _receipt(request))
    with pytest.raises(local_source.SourceChanged, match='unresolved_epoch'):
        journal._adopt_epoch(_epoch(1, NEXT))
    assert _reload(world).get_ticket(ticket).state == state


def test_retirement_and_generation_floor_rollback_together(world, monkeypatch):
    root, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    _finish(journal, ticket, acknowledge=False)
    with monkeypatch.context() as patch:
        patch.setattr(root, '_advance_root_revision', lambda conn: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError, match='crash'):
            journal._adopt_epoch(_epoch(1, NEXT))
    assert _reload(world).get_ticket(ticket).state == 'completed'
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 1


@pytest.mark.parametrize('change', [
    {'generation': -1}, {'generation': True}, {'actor_id': str(uuid.uuid4())},
    {'lifecycle_id': str(uuid.uuid4())}, {'root': 'other'},
    {'protocol_epoch': NEXT}, {'generation': 1},
])
def test_bad_or_regressed_epoch_never_changes_lifecycle(world, change):
    _, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    with pytest.raises((ValueError, local_source.SourceChanged)):
        journal._adopt_epoch(replace(_epoch(), **change))
    assert journal._adopt_epoch(_epoch()) == 0


def test_retirement_capacity_reused_across_many_generations(world, monkeypatch):
    from agentbridge.store import operation_journal

    _, _, _, _, journal = world
    monkeypatch.setattr(operation_journal, 'MAX_RECORDS', 2)
    current = _epoch()
    journal._adopt_epoch(current)
    for _ in range(5):
        for value in range(2):
            ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {'ns': value}})
            _finish(journal, ticket)
        with pytest.raises(local_source.SourceChanged, match='record_budget'):
            journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
        current = replace(current, generation=current.generation + 1, protocol_epoch=str(uuid.uuid4()))
        assert journal._adopt_epoch(current) == 2


def test_missing_ticket_cannot_allocate_and_format_six_stays_node_only(world):
    root, _, store, definition, journal = world
    journal._adopt_epoch(_epoch())
    with pytest.raises(local_source.SourceChanged, match='missing'):
        journal.get_ticket(OperationTicket(EPOCH, str(uuid.uuid4())))
    with pytest.raises(local_source.SourceChanged, match='node_publication_required'):
        with root.publication_gate(store, definition):
            pytest.fail('legacy admission became possible')


def test_epoch_observation_is_strict_and_checks_identity_after_network():
    from test_typed_mutations import _Client, _Provider, ACTOR as RPC_ACTOR, URL

    client = _Client()
    provider = _Provider(client)
    provider.root = 'mesh'
    rpc = TypedMutationRPC(provider, actor_id=RPC_ACTOR, root='mesh', endpoint=URL)
    raw = dict(schema_version=1, root='mesh', actor_id=RPC_ACTOR, role='authenticated',
               lifecycle_id=LIFECYCLE, protocol_epoch=EPOCH, generation=0, sealed=True)
    client.result = raw
    assert observe_provider_epoch(rpc).generation == 0
    assert client.calls[-1] == ('ab_mutation_epoch_state', {'p_root': 'mesh'})
    for change in ({'generation': True}, {'sealed': False}, {'extra': 1}, {'actor_id': ACTOR}):
        with pytest.raises(ValueError):
            decode_provider_epoch({**raw, **change}, endpoint=URL, root='mesh', actor_id=RPC_ACTOR)

    def swap(name, parameters):
        client.headers['Authorization'] = 'Bearer other'
        return raw

    client.on_execute = swap
    with pytest.raises(TypedMutationRPCError, match='identity changed'):
        observe_provider_epoch(rpc)


def test_public_adoption_fetches_authenticated_evidence_and_rejects_external_object(world):
    from test_typed_mutations import _Client, _Provider

    journal = world[-1]
    with pytest.raises(TypeError, match='observer'):
        journal.adopt_epoch(_epoch())
    client = _Client()
    client.session.user.id = client.jwt_user.id = ACTOR
    provider = _Provider(client)
    provider.root = 'mesh'
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root='mesh', endpoint=ENDPOINT)
    client.result = dict(schema_version=1, root='mesh', actor_id=ACTOR, role='authenticated',
                         lifecycle_id=LIFECYCLE, protocol_epoch=EPOCH, generation=0, sealed=True)
    assert journal.adopt_epoch(rpc) == 0
    assert client.calls == [('ab_mutation_epoch_state', {'p_root': 'mesh'})]
    client.result = {**client.result, 'generation': 1, 'protocol_epoch': NEXT}
    assert journal.adopt_epoch(rpc) == 0
    assert len(client.calls) == 2


def test_lifecycle_schema_corruption_and_missing_ticket_record_fail_closed(world):
    root, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    with sqlite3.connect(root.path) as conn:
        conn.execute('INSERT INTO typed_operation_tickets VALUES(?,?,0,0)', (EPOCH, str(uuid.uuid4())))
    with pytest.raises(local_source.SourceChanged, match='ticket_invalid'):
        journal.pending_keys()


def test_unconsumed_outcome_survives_closure_and_remains_discoverable(world):
    _, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {'ns': 1}})
    request = _finish(journal, ticket, acknowledge=False, release=False)
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 0
    restored = _reload(world)
    retained = restored.get_ticket(ticket)
    assert retained.state == 'acknowledged' and retained.receipt == _receipt(request)
    assert restored.pending_document_keys('presence/alice.json') == ((EPOCH, ticket.operation_id),)
    assert restored.pending_keys() == ((EPOCH, ticket.operation_id),)
    assert restored.operation_ticket(EPOCH, ticket.operation_id) == ticket
    assert restored.release_ticket(ticket) is True
    assert restored.pending_document_keys('presence/alice.json') == ()
    assert restored.pending_keys() == ()
    assert restored.operation_ticket(EPOCH, ticket.operation_id) is None
    with pytest.raises(local_source.SourceChanged, match='missing'):
        restored.get_ticket(ticket)


def test_unconsumed_old_tickets_survive_multiple_closures(world):
    journal = world[-1]
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    _finish(journal, ticket, release=False)
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 0
    assert journal._adopt_epoch(_epoch(2, str(uuid.uuid4()))) == 0
    assert journal.get_ticket(ticket).state == 'acknowledged'
    assert journal.release_ticket(ticket)


def test_ticket_release_requires_local_terminal_completion(world):
    journal = world[-1]
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    with pytest.raises(local_source.SourceChanged, match='ticket_uncompleted'):
        journal.release_ticket(ticket)


def test_release_cleanup_is_idempotent_but_missing_ticket_is_not_a_result(world):
    journal = world[-1]
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    _finish(journal, ticket, release=False)
    journal._adopt_epoch(_epoch(1, NEXT))
    assert journal.release_ticket(ticket) is True
    assert journal.release_ticket(ticket) is False
    unknown = OperationTicket(EPOCH, str(uuid.uuid4()))
    assert journal.release_ticket(unknown) is False
    for missing in (ticket, unknown):
        with pytest.raises(local_source.SourceChanged, match='missing'):
            journal.get_ticket(missing)


def test_corrupt_released_receipt_cannot_be_pruned(world):
    root, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket('doc_put', {'p_path': 'presence/alice.json', 'p_data': {}})
    _finish(journal, ticket)
    with sqlite3.connect(root.path) as conn:
        conn.execute('UPDATE typed_operations SET receipt=? WHERE operation_id=?', (b'{}', ticket.operation_id))
    with pytest.raises(local_source.SourceChanged, match='record_invalid'):
        journal._adopt_epoch(_epoch(1, NEXT))
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT generation FROM typed_operation_lifecycle').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (1,)


def test_linked_action_cannot_release_its_unsettled_result_before_or_after_closure(world):
    from agentbridge.mesh.paths import P

    root, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    action_id = str(uuid.uuid4())
    path = P.state('room', 'alice')
    with sqlite3.connect(root.path) as conn:
        conn.execute('INSERT INTO user_state_actions(action_id,actor_id,path,chat,user,kind,args,attempt,state) '
                     "VALUES(?,?,?,'room','alice','star',?,0,'queued')",
                     (action_id, ACTOR, path, b'{"msg_ids":["m"]}'))
    ticket = journal.allocate_action_ticket(action_id, 0, {
        'p_path': path, 'p_data': {}, 'p_expected_source_epoch': str(uuid.uuid4()),
        'p_expected_seq': 0, 'p_expected_deleted': False,
    })
    _finish(journal, ticket, release=False)
    with pytest.raises(local_source.SourceChanged, match='consumer_unsettled'):
        journal.release_ticket(ticket)
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 0
    with pytest.raises(local_source.SourceChanged, match='consumer_unsettled'):
        journal.release_ticket(ticket)
    with sqlite3.connect(root.path) as conn:
        conn.execute("UPDATE user_state_actions SET state='applied' WHERE action_id=?", (action_id,))
    assert journal.release_ticket(ticket)
    assert journal.pending_keys() == ()
