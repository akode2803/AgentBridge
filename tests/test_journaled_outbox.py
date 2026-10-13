"""Application outbox recovery uses durable identities before fresh-send checks."""

import json
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import NotAMember
from agentbridge.mesh.events import EV_DELETED
from agentbridge.mesh.messaging import MessagingService
from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.store.outbox import OutboxWorker
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.mutation_receipts import decode_mutation_receipt


ACTOR = '12345678-1234-5678-9234-567812345673'
EPOCH = '12345678-1234-5678-9234-567812345671'
NEXT_EPOCH = '12345678-1234-5678-9234-567812345675'
URL = 'https://example.supabase.co'


class Wire:
    actor_id, root, endpoint = ACTOR, 'team', URL

    def __init__(self):
        self.effects = {}
        self.calls = []
        self.lose_reply = False
        self.lose_ack = False
        self.acks = []

    def execute(self, request):
        e = request.expectation
        self.calls.append(request)
        raw = self.effects.setdefault((e.protocol_epoch, e.operation_id), {
            'schema_version': 2, 'root': e.root, 'protocol_epoch': e.protocol_epoch,
            'operation_id': e.operation_id, 'actor_id': e.actor_id, 'kind': e.kind,
            'client_digest': e.client_digest, 'provider_digest': 'a' * 64,
            'result': {**dict(e.targets), 'row_id': len(self.effects) + 1},
        })
        if self.lose_reply:
            raise OSError('reply lost')
        return decode_mutation_receipt(raw, e)

    def acknowledge(self, receipt):
        self.acks.append(receipt)
        if self.lose_ack:
            raise OSError('ack lost')
        return True


@pytest.fixture
def world(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', json.dumps(
        ['local-root-v1', 'supabase', URL, 'team'], separators=(',', ':')))
    node = NodeStore(tmp_path / 'node' / 'replica.sqlite', ReplicaIdentity(
        URL, 'supabase://team', ACTOR, 'test-machine'))
    root.register_node(node)
    store = Store(tmp_path / 'outbox.sqlite')
    rpc = Wire()

    def open_writer(epoch=EPOCH):
        journal = OperationJournal(root, node, actor_id=ACTOR)
        return ApplicationMutationWriter(JournaledMutations(journal, rpc), epoch)

    try:
        yield store, rpc, open_writer
    finally:
        store.close()


def service(store, writer):
    tx = SimpleNamespace(append_log=writer.append_log,
                         recover_append_log=writer.recover_append_log)
    result = MessagingService(tx, store, None, 'alice', 'machine')
    result.require_send = lambda *args, **kwargs: None
    return result


def due(store):
    with store._conn() as conn:
        conn.execute('UPDATE outbox SET next_ns=0')


@pytest.mark.parametrize('loss', ['lose_reply', 'lose_ack'])
def test_restart_recovers_before_removed_membership_and_preserves_epoch(world, loss):
    store, rpc, open_writer = world
    envelope = {'id': 'message-one', 'kind': 'message', 'body': 'immutable'}
    store.outbox_add('append_log', 'room|alice.jsonl', envelope)
    original = open_writer()
    first = service(store, original)
    setattr(rpc, loss, True)
    assert OutboxWorker(store, first.outbox_handlers()).flush_once() == 0
    assert store.outbox_counts() == {'pending': 1}
    assert len(rpc.effects) == 1

    setattr(rpc, loss, False)
    restored = service(store, open_writer(NEXT_EPOCH))

    def revoked(*args, **kwargs):
        pytest.fail('fresh membership must not precede retained receipt recovery')

    restored.require_send = revoked
    due(store)
    assert OutboxWorker(store, restored.outbox_handlers()).flush_once() == 1
    assert store.outbox_counts() == {}
    assert len(rpc.effects) == 1
    assert {item.expectation.protocol_epoch for item in rpc.calls} == {EPOCH}
    assert len(rpc.calls) == (2 if loss == 'lose_reply' else 1)


def test_new_send_still_requires_current_application_authority(world):
    store, rpc, open_writer = world
    app = service(store, open_writer())
    app.require_send = lambda *a, **kw: (_ for _ in ()).throw(NotAMember('removed'))
    app.snapshot = lambda *a: (_ for _ in ()).throw(NotAMember('removed'))
    store.outbox_add('append_log', 'room|alice.jsonl', {'id': 'new', 'kind': 'message'})
    assert OutboxWorker(store, app.outbox_handlers()).flush_once() == 0
    assert store.outbox_counts() == {'dead': 1}
    assert not rpc.calls


def test_terminal_replay_cannot_escape_via_applied_snapshot_shortcut(world):
    store, rpc, open_writer = world
    envelope = {'id': 'terminal-one', 'kind': 'info', 'event': {'type': EV_DELETED}}
    writer = open_writer()
    rpc.lose_reply = True
    with pytest.raises(OSError):
        writer.append_log('room', 'alice.jsonl', envelope)
    rpc.lose_reply = False
    app = service(store, open_writer())
    app._materialized_snapshot = lambda *_: pytest.fail('terminal shortcut preceded recovery')
    events = []
    app._reclaim_terminal = lambda chat: events.append(('reclaim', chat))
    app._apply_terminal = lambda chat: events.append(('apply', chat))
    store.outbox_add('append_log', 'room|alice.jsonl', envelope)
    assert OutboxWorker(store, app.outbox_handlers()).flush_once() == 1
    assert events == [('reclaim', 'room'), ('apply', 'room')]
    assert len(rpc.effects) == 1


def test_same_message_id_with_changed_payload_never_reaches_provider(world):
    store, rpc, open_writer = world
    writer = open_writer()
    writer.append_log('room', 'alice.jsonl', {'id': 'same', 'kind': 'message', 'body': 'A'})
    store.outbox_add('append_log', 'room|alice.jsonl',
                     {'id': 'same', 'kind': 'message', 'body': 'B'})
    assert OutboxWorker(store, service(store, writer).outbox_handlers()).flush_once() == 0
    assert store.outbox_counts() == {'pending': 1}
    assert len(rpc.calls) == 1
