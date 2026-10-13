"""Real root journal and Store tombstones around explicit outbox dispatch."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_operation_journal as cases

from agentbridge.store.outbox import OutboxWorker
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.mutation_receipts import decode_mutation_receipt
from agentbridge.transport.outbox_mutations import OutboxMutationOwner
from agentbridge.transport.receipt_retirement import ProviderEpoch

world = cases.world


class Provider:
    endpoint, root, actor_id = cases.ENDPOINT, 'mesh', cases.ACTOR

    def __init__(self):
        self.effects = {}
        self.lose = False

    def execute(self, request):
        e = request.expectation
        key = e.protocol_epoch, e.operation_id
        if key not in self.effects:
            raw = {name: getattr(e, name) for name in (
                'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
            raw.update(schema_version=2, provider_digest='c' * 64,
                       result={**dict(e.targets), 'row_id': 1})
            self.effects[key] = decode_mutation_receipt(raw, e)
        if self.lose:
            raise OSError('reply lost')
        return self.effects[key]

    def acknowledge(self, _receipt):
        return True


def setup(world):
    _, _, store, _, journal = world
    journal._adopt_epoch(ProviderEpoch(cases.ENDPOINT, 'mesh', cases.ACTOR,
                                     'cccccccc-cccc-4ccc-8ccc-cccccccccccc', cases.EPOCH, 0))
    rpc = Provider()
    writer = ApplicationMutationWriter(JournaledMutations(journal, rpc), cases.EPOCH)
    tx = SimpleNamespace(_bound_writer=lambda: writer)
    fresh = []

    def dispatch(target, payload, *, _context):
        chat, log = target.split('|')
        if not _context.recover_append_log(chat, log, payload):
            fresh.append(1)
            _context.append_log(chat, log, payload)

    owner = OutboxMutationOwner(tx, store, dispatch)
    return store, journal, rpc, owner, fresh


def test_lost_append_reply_recovers_then_sent_release_prunes_owner(world):
    store, journal, rpc, owner, fresh = setup(world)
    store.outbox_add('append_log', 'room|alice.jsonl', {'id': 'm', 'ns': 1})
    worker = OutboxWorker(store, {}, typed_owner=owner, base_delay=0)
    # One explicit claim avoids the worker's retry-until-not-due loop with zero delay.
    item = store.outbox_claim_due()[0]
    rpc.lose = True
    with pytest.raises(OSError):
        owner.dispatch(item)
    rpc.lose = False
    owner.dispatch(item)
    assert len(rpc.effects) == 1 and fresh == [1]
    assert store.outbox_mark_sent(item)
    assert worker.flush_once() == 1
    assert store.outbox_counts().get('pending', 0) == 0
    assert journal.pending_keys() == ()
    with pytest.raises(ValueError, match='no longer current'):
        owner.dispatch(item)
    assert len(rpc.effects) == 1


def test_sent_cleanup_failure_never_redispatches_and_retries_after_root_cleanup(world):
    store, journal, rpc, owner, _ = setup(world)
    store.outbox_add('append_log', 'room|alice.jsonl', {'id': 'm', 'ns': 1})
    attempts = []

    def cleanup(_target, _payload):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('cleanup interrupted')

    worker = OutboxWorker(store, {}, typed_owner=owner,
                          success_hooks={'append_log': cleanup})
    assert worker.flush_once() == 0
    assert len(store.outbox_sent_items()) == 1
    assert worker.flush_once() == 1
    assert not store.outbox_sent_items()
    assert len(rpc.effects) == 1
    assert journal.pending_keys() == ()


def test_root_release_then_epoch_prune_then_store_cleanup_recovery(world, monkeypatch):
    store, journal, rpc, owner, _ = setup(world)
    store.outbox_add('append_log', 'room|alice.jsonl', {'id':'m','ns':1})
    claim=store.outbox_claim_due()[0]
    owner.dispatch(claim)
    assert store.outbox_mark_sent(claim)
    sent=store.outbox_sent_items()[0]
    owner.release(sent)
    # Crash boundary: root consumed+released, Store sent row has not been deleted.
    journal._adopt_epoch(ProviderEpoch(cases.ENDPOINT, 'mesh', cases.ACTOR,
                         'cccccccc-cccc-4ccc-8ccc-cccccccccccc', str(uuid4()), 1))
    with journal.coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
        assert conn.execute('SELECT consumed FROM typed_operation_consumers').fetchall() == [(1,)]
    monkeypatch.setattr(rpc, 'execute', lambda _request: pytest.fail('redispatch after consumption'))
    assert OutboxWorker(store, {}, typed_owner=owner).flush_once() == 1
    assert store.outbox_sent_items() == []
    assert len(rpc.effects) == 1


def test_stale_lease_and_new_worker_share_exact_ticket_without_duplicate_effect(world):
    store, journal, rpc, owner, fresh = setup(world)
    store.outbox_add('append_log', 'room|alice.jsonl', {'id':'m','ns':1})
    old=store.outbox_claim_due(lease_s=0)[0]
    current=store.outbox_claim_due()[0]
    owner.dispatch(old)
    owner.dispatch(current)
    assert len(rpc.effects) == 1 and fresh == [1]
    assert not store.outbox_mark_sent(old)
    assert not store.outbox_retry_claim(old, 'late failure', 1)
    assert store.outbox_mark_sent(current)
    assert OutboxWorker(store, {}, typed_owner=owner).flush_once() == 1


@pytest.mark.parametrize('lost_reply', [False, True])
def test_pre_consumer_append_cannot_be_reallocated(world, lost_reply):
    _, _, store, _, journal = world
    rpc = Provider()
    writer = ApplicationMutationWriter(JournaledMutations(journal, rpc), cases.EPOCH)
    envelope = {'id': 'pre-consumer', 'ns': 1}
    store.outbox_add('append_log', 'room|alice.jsonl', envelope)
    rpc.lose = lost_reply
    if lost_reply:
        with pytest.raises(OSError):
            writer.append_log('room', 'alice.jsonl', envelope)
    else:
        writer.append_log('room', 'alice.jsonl', envelope)
    rpc.lose = False
    # Finish the old replay before adoption if its reply was lost; keep the
    # durable outbox row to model a crash before Store cleanup.
    assert writer.recover_append_log('room', 'alice.jsonl', envelope)
    journal._adopt_epoch(ProviderEpoch(cases.ENDPOINT, 'mesh', cases.ACTOR,
                         'cccccccc-cccc-4ccc-8ccc-cccccccccccc', cases.EPOCH, 0))
    fresh = []
    owner = OutboxMutationOwner(SimpleNamespace(_bound_writer=lambda: writer),
                                store, lambda *_args, **_kwargs: fresh.append(1))
    item = store.outbox_claim_due()[0]
    with pytest.raises(ValueError, match='drain pre-consumer'):
        owner.dispatch(item)
    assert len(rpc.effects) == 1
    assert not fresh
    assert journal.get_outbox_leg(store, item.seq, 'append', item.owner_digest) is None
    # Prior owner can still recover the exact committed append and drain its row.
    assert owner.writer.recover_append_log('room', 'alice.jsonl', envelope)
    store.outbox_done(item.seq)
    assert len(rpc.effects) == 1
