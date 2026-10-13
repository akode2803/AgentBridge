"""Option B: authenticated closure proves a ticketed attempt never executed."""

from __future__ import annotations

import sqlite3

import pytest

import test_operation_journal as cases
import test_outbox_mutation_owner as outbox_cases
import test_user_state_actions as state_cases
from agentbridge.store import local_source
from agentbridge.store.outbox import OutboxWorker
from agentbridge.transport.receipt_retirement import ProviderEpoch
from agentbridge.transport.typed_mutations import TypedMutationRPCError
from test_receipt_retirement import NEXT, _epoch

world = cases.world


def _count(root, table):
    with sqlite3.connect(root.path) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.mark.parametrize("state", ["prepared", "attempted"])
def test_ticketed_attempt_without_receipt_retires_at_closure(world, state):
    root, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket("doc_put", {"p_path": "presence/a.json", "p_data": {}})
    request = journal.get_ticket(ticket).request
    if state == "attempted":
        journal.mark_attempted(request)
    assert _count(root, "mutation_intents") == 1
    journal._adopt_epoch(_epoch(1, NEXT))
    with pytest.raises(local_source.SourceChanged, match="missing"):
        journal.get_ticket(ticket)
    with pytest.raises(local_source.SourceChanged, match="missing"):
        journal.mark_attempted(request)  # the stale request can never be re-sent
    assert _count(root, "mutation_intents") == 0
    assert _count(root, "typed_operation_tickets") == 0
    # New work proceeds in the successor epoch.
    fresh = journal.allocate_ticket("doc_put", {"p_path": "presence/a.json", "p_data": {}})
    assert fresh.protocol_epoch == NEXT


def test_untracked_pre_ticket_attempt_still_blocks_closure(world):
    _, _, _, _, journal = world
    request = cases._request(31, arguments={"p_path": "presence/b.json", "p_data": {}})
    journal.prepare(request)
    journal.mark_attempted(request)
    journal._adopt_epoch(_epoch())  # same epoch: the opaque row is retained
    with pytest.raises(local_source.SourceChanged, match="unresolved_epoch"):
        journal._adopt_epoch(_epoch(1, NEXT))
    assert journal.get(cases.EPOCH, request.expectation.operation_id).state == "attempted"


def test_closure_does_not_resolve_without_newer_authenticated_generation(world):
    _, _, _, _, journal = world
    journal._adopt_epoch(_epoch())
    ticket = journal.allocate_ticket("doc_put", {"p_path": "presence/c.json", "p_data": {}})
    journal.mark_attempted(journal.get_ticket(ticket).request)
    assert journal._adopt_epoch(_epoch()) == 0  # same generation proves nothing
    assert journal.get_ticket(ticket).state == "attempted"


def test_linked_state_action_requeues_and_runs_once_in_the_new_epoch(tmp_path):
    coordinator, provider, reopen = state_cases.world.__wrapped__(tmp_path)

    def full(_args):
        raise TypedMutationRPCError("typed mutation receipt epoch is full")

    provider.before_write = full
    with pytest.raises(TypedMutationRPCError):
        state_cases._write(reopen(), "star", {"msg_ids": ["m"]})
    assert provider.effects == []
    queue = reopen()
    queue.journal._adopt_epoch(ProviderEpoch(
        state_cases.URL, "mesh", state_cases.ACTOR, state_cases.LIFECYCLE, NEXT, 1))
    (pending,) = queue.pending_actions(state_cases.USER)
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute(
            "SELECT state,attempt,ticket_epoch FROM user_state_actions").fetchall() == [
            ("queued", 1, None)]
    provider.before_write = None
    document, changed = reopen().run(state_cases.CHAT, state_cases.USER, pending.action_id,
                                     signer=state_cases.signer,
                                     verifier=state_cases.verifier)
    assert changed and document["starred"] == ["m"]
    assert len(provider.effects) == 1
    assert provider.requests[-1].expectation.protocol_epoch == NEXT


def test_outbox_leg_relinks_in_the_new_epoch_without_a_second_effect(world):
    store, journal, rpc, owner, fresh = outbox_cases.setup(world)
    store.outbox_add("append_log", "room|alice.jsonl", {"id": "m", "ns": 1})
    item = store.outbox_claim_due()[0]
    original = rpc.execute

    def full(request):
        raise TypedMutationRPCError("typed mutation receipt epoch is full")

    rpc.execute = full
    with pytest.raises(TypedMutationRPCError):
        owner.dispatch(item)
    assert rpc.effects == {}
    root = world[0]
    assert _count(root, "typed_operation_consumers") == 1
    journal._adopt_epoch(ProviderEpoch(cases.ENDPOINT, "mesh", cases.ACTOR,
                                       "cccccccc-cccc-4ccc-8ccc-cccccccccccc", NEXT, 1))
    assert _count(root, "typed_operation_consumers") == 0
    assert _count(root, "typed_outbox_reservations") == 0
    rpc.execute = original
    owner.dispatch(item)
    (key,) = rpc.effects
    assert key[0] == NEXT
    assert store.outbox_mark_sent(item)
    assert OutboxWorker(store, {}, typed_owner=owner, base_delay=0).flush_once() == 1
    assert journal.pending_keys() == ()
