from types import SimpleNamespace
import pytest
import test_operation_journal as cases
from test_receipt_retirement import _epoch, NEXT
from agentbridge.store import local_source
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = cases.world


def test_full_rejection_then_rotation_strands_attempted_ticket(world):
    journal = world[-1]
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket(
        "log_append", {"p_chat": "room", "p_log": "alice.jsonl", "p_record": {"id": "m", "ns": 1}}
    )

    def full(_request):
        raise TypedMutationRPCError("typed mutation receipt epoch is full")

    rpc = SimpleNamespace(
        endpoint=journal.endpoint, root=journal.root, actor_id=journal.actor_id, execute=full
    )
    with pytest.raises(TypedMutationRPCError, match="epoch is full"):
        JournaledMutations(journal, rpc).resume(ticket.protocol_epoch, ticket.operation_id)
    assert journal.get_ticket(ticket).state == "attempted"
    assert journal.get_ticket(ticket).receipt is None
    with pytest.raises(local_source.SourceChanged, match="unresolved_epoch"):
        journal._adopt_epoch(_epoch(1, NEXT))
    assert journal.get_ticket(ticket).state == "attempted"
