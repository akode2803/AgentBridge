"""Durable orchestration failures must never mint a second operation."""

import json

import pytest

from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.mutation_receipts import decode_mutation_receipt
from agentbridge.transport.typed_mutations import prepare_mutation


ACTOR = "12345678-1234-5678-9234-567812345673"
EPOCH = "12345678-1234-5678-9234-567812345671"
OP = "12345678-1234-5678-9234-567812345672"
URL = "https://example.supabase.co"


def request():
    return prepare_mutation("team", EPOCH, OP, ACTOR, "doc_put", {
        "p_path": "chats/room/meta.json", "p_data": {"v": 1},
    })


class Wire:
    """Receipt-retaining provider double, including response-loss injection."""

    actor_id, root, endpoint = ACTOR, "team", URL

    def __init__(self):
        self.requests = []
        self.effects = {}
        self.acks = []
        self.lose_reply = False
        self.lose_ack = False

    def execute(self, prepared):
        self.requests.append(prepared.request_json)
        e = prepared.expectation
        key = (e.protocol_epoch, e.operation_id)
        raw = self.effects.setdefault(key, {
            "schema_version": 2, "root": e.root,
            "protocol_epoch": e.protocol_epoch, "operation_id": e.operation_id,
            "actor_id": e.actor_id, "kind": e.kind,
            "client_digest": e.client_digest, "provider_digest": "a" * 64,
            "result": {"path": "chats/room/meta.json", "updated": True},
        })
        if self.lose_reply:
            raise OSError("reply lost")
        return decode_mutation_receipt(raw, e)

    def acknowledge(self, receipt):
        self.acks.append(receipt)
        if self.lose_ack:
            raise OSError("ack lost")
        return True


@pytest.fixture
def world(tmp_path):
    root = MutationCoordinator(tmp_path / "home", json.dumps(
        ["local-root-v1", "supabase", URL, "team"], separators=(",", ":")))
    node = NodeStore(tmp_path / "node" / "replica.sqlite", ReplicaIdentity(
        URL, "supabase://team", ACTOR, "test-machine"))
    root.register_node(node)
    journal = OperationJournal(root, node, actor_id=ACTOR)
    return root, node, journal, Wire()


def test_success_then_repeat_does_not_send_again(world):
    root, node, journal, rpc = world
    result = JournaledMutations(journal, rpc).submit(request())
    assert result.state == "acknowledged"
    reopened = OperationJournal(root, node, actor_id=ACTOR)
    assert JournaledMutations(reopened, rpc).resume(EPOCH, OP) == result
    assert len(rpc.requests) == len(rpc.acks) == len(rpc.effects) == 1


def test_lost_initial_response_replays_exact_durable_request(world):
    root, node, journal, rpc = world
    rpc.lose_reply = True
    with pytest.raises(OSError, match="reply lost"):
        JournaledMutations(journal, rpc).submit(request())
    assert journal.get(EPOCH, OP).state == "attempted"
    rpc.lose_reply = False
    reopened = OperationJournal(root, node, actor_id=ACTOR)
    assert JournaledMutations(reopened, rpc).resume(EPOCH, OP).state == "acknowledged"
    assert rpc.requests == [request().request_json] * 2
    assert len(rpc.effects) == len(rpc.acks) == 1


@pytest.mark.parametrize("method,state,sends", [
    ("mark_attempted", "prepared", 1),
    ("accept", "attempted", 2),
    ("complete", "accepted", 1),
    ("mark_acknowledged", "completed", 1),
])
def test_local_interruption_resumes_at_durable_boundary(world, monkeypatch,
                                                       method, state, sends):
    root, node, journal, rpc = world

    def fail(*args, **kwargs):
        raise OSError("local interruption")

    monkeypatch.setattr(journal, method, fail)
    with pytest.raises(OSError, match="local interruption"):
        JournaledMutations(journal, rpc).submit(request())
    assert journal.get(EPOCH, OP).state == state
    reopened = OperationJournal(root, node, actor_id=ACTOR)
    assert JournaledMutations(reopened, rpc).resume(EPOCH, OP).state == "acknowledged"
    assert len(rpc.requests) == sends
    assert len(rpc.effects) == 1
    assert len(rpc.acks) == (2 if method == "mark_acknowledged" else 1)


def test_lost_ack_retries_only_ack(world):
    root, node, journal, rpc = world
    rpc.lose_ack = True
    with pytest.raises(OSError, match="ack lost"):
        JournaledMutations(journal, rpc).submit(request())
    assert journal.get(EPOCH, OP).state == "completed"
    rpc.lose_ack = False
    reopened = OperationJournal(root, node, actor_id=ACTOR)
    assert JournaledMutations(reopened, rpc).resume(EPOCH, OP).state == "acknowledged"
    assert len(rpc.requests) == 1
    assert len(rpc.acks) == 2


@pytest.mark.parametrize("field", ["actor_id", "root", "endpoint"])
def test_wrong_rpc_owner_fails_before_journaling(world, field):
    _root, _node, journal, rpc = world
    setattr(rpc, field, "other")
    with pytest.raises(ValueError, match="owner mismatch"):
        JournaledMutations(journal, rpc)
    assert not rpc.requests


def test_late_concurrent_acceptance_cannot_reopen_operation(world):
    _root, _node, journal, rpc = world
    operation = request()
    journal.prepare(operation)
    journal.mark_attempted(operation)
    late_receipt = rpc.execute(operation)
    result = JournaledMutations(journal, rpc).resume(EPOCH, OP)
    assert result.state == "acknowledged"
    assert journal.accept(operation, late_receipt) == result
    assert journal.mark_attempted(operation) == result
    assert journal.complete(operation) == result


def test_false_ack_cannot_be_recorded(world):
    _root, _node, journal, rpc = world
    rpc.acknowledge = lambda receipt: False
    with pytest.raises(ValueError, match="acknowledgment failed"):
        JournaledMutations(journal, rpc).submit(request())
    assert journal.get(EPOCH, OP).state == "completed"
