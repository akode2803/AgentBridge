"""Durable cancellation and exact domain settlement of admitted owner legs."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import test_operation_journal as base
import test_provider_admission_owner as cases
from agentbridge.store import local_source
from agentbridge.store.provider_admission_owner import (
    ProviderAdmissionOwnerJournal, reservation_capacity,
)
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = base.world
OTHER = "ffffffff-ffff-4fff-8fff-ffffffffffff"
CANCEL = "ab_cancel_mutation_admission"
CANCEL_ACK = "ab_ack_mutation_admission_cancel"


def _put(index):
    return base._request(index, arguments={
        "p_path": f"presence/{index}.json", "p_data": {"value": index}})


def _compare(index):
    return base._request(index, kind="doc_compare_put", arguments={
        "p_path": f"presence/{index}.json", "p_data": {"value": index},
        "p_expected_source_epoch": cases.NEXT, "p_expected_seq": 0,
        "p_expected_deleted": False})


def _raw_receipt(request, *, applied=True):
    e = request.expectation
    result = dict(e.targets)
    if e.kind == "doc_put":
        result["updated"] = True
    if e.kind == "doc_compare_put":
        result["applied"] = applied
    if e.kind == "doc_create":
        result["created"] = True
    raw = {name: getattr(e, name) for name in (
        "root", "protocol_epoch", "operation_id", "actor_id", "kind", "client_digest")}
    raw.update(schema_version=2, provider_digest="c" * 64, result=result)
    return raw


class _Provider:
    """Exact provider rules: one receipt per operation; replay precedes
    authority; cancellation and execution serialize on one operation lock."""

    def __init__(self, client):
        self.plans, self.requests = {}, {}
        self.receipts, self.cancelled = {}, set()
        self.cancel_acked, self.receipt_acked = set(), set()
        self.conflicts, self.revoked = set(), False
        self.lose, self.before, self.after = set(), {}, {}
        self.calls = []
        self.lock = threading.Lock()
        self.bind(client)

    def bind(self, client):
        client.on_execute = self

    def owner(self, record):
        self.plans[record.plan.owner_id] = record.plan
        self.requests.update((r.expectation.operation_id, r) for r in record.requests)

    def __call__(self, name, params):
        self.calls.append((name, params.get("p_operation_id")))
        hook = self.before.pop(name, None)
        if hook is not None:
            hook()
        with self.lock:
            reply = self._handle(name, params)
        hook = self.after.pop(name, None)
        if hook is not None:
            hook()
        if name in self.lose:
            self.lose.discard(name)
            raise TimeoutError("reply lost after provider commit")
        return reply

    def _handle(self, name, params):
        op = params.get("p_operation_id")
        if name == "ab_admit_mutation_owner":
            return cases._reply(self.plans[params["p_owner_id"]], "admitted")
        if name == CANCEL:
            if op in self.receipts:
                status = "executed"
            else:
                self.cancelled.add(op)
                status = "cancelled"
            plan = self.plans[params["p_owner_id"]]
            leg = next(leg for leg in plan.legs if leg.operation_id == op)
            return {"schema_version": 1, "root": plan.root, "actor_id": plan.actor_id,
                    "protocol_epoch": plan.protocol_epoch, "owner_id": plan.owner_id,
                    "operation_id": op, "kind": leg.kind,
                    "client_digest": leg.client_digest, "status": status}
        if name == CANCEL_ACK:
            if op not in self.cancelled:
                raise RuntimeError("mutation admission leg is not cancelled")
            self.cancel_acked.add(op)
            return True
        if name == "ab_ack_mutation_receipt":
            if op not in self.receipts:
                raise RuntimeError("mutation receipt is unavailable")
            self.receipt_acked.add(op)
            return True
        request = self.requests[op]
        assert name == request.rpc_name
        if op in self.receipts:
            return self.receipts[op]
        if op in self.cancelled:
            raise RuntimeError("typed mutation requires matching uncancelled admission")
        if self.revoked:
            raise PermissionError("typed document put is unauthorized")
        self.receipts[op] = _raw_receipt(request, applied=op not in self.conflicts)
        return self.receipts[op]

    def closable(self, plan):
        """The provider's epoch-closure predicate for every leg of ``plan``."""
        return all(
            (leg.operation_id in self.cancelled and leg.operation_id in self.cancel_acked
             and leg.operation_id not in self.receipts)
            or (leg.operation_id not in self.cancelled and leg.operation_id in self.receipts
                and leg.operation_id in self.receipt_acked)
            for leg in plan.legs)


def _admitted(world, requests, *, owner_id=cases.OWNER, private_payload=b""):
    owner, client = cases._owner(world)
    provider = _Provider(client)
    record = owner.prepare(owner_id, requests, private_payload=private_payload)
    provider.owner(record)
    assert owner.admit(owner_id).status == "admitted"
    return owner, provider, record


def _driver(owner):
    return JournaledMutations(owner.journal, owner.wire._rpc)


def _op(request):
    return request.expectation.operation_id


def _rows(world, sql, *args):
    with sqlite3.connect(world[0].path) as conn:
        return conn.execute(sql, args).fetchall()


def _restart(world, provider):
    restarted = ProviderAdmissionOwnerJournal(base._reload(world), cases._wire()[0])
    provider.bind(restarted.wire._rpc._client)
    return restarted


def test_unused_leg_cancels_before_binding_and_owner_settles_for_closure(world):
    used, unused = _put(81), _put(82)
    owner, provider, record = _admitted(world, (used, unused))
    with sqlite3.connect(world[0].path) as conn:
        before = reservation_capacity(conn)

    def request_is_durable():
        # The RPC runs outside the root cut, after the request committed.
        assert _rows(world, "SELECT cancel FROM typed_admission_legs WHERE operation_id=?",
                     _op(unused)) == [("requested",)]
        with pytest.raises(local_source.SourceChanged, match="leg_cancelling"):
            owner.bind_ticket(cases.OWNER, _op(unused))

    provider.before[CANCEL] = request_is_durable
    assert owner.cancel(cases.OWNER, _op(unused)).status == "cancelled"
    assert not provider.before
    current = owner.get(cases.OWNER)
    assert (current.linked, current.cancel, current.outcome) == (
        (False, False), ("none", "cancelled"), (None, "cancelled"))
    with sqlite3.connect(world[0].path) as conn:
        # The cancelled leg can never link; only its own replay copy remains.
        assert reservation_capacity(conn) == (before[0] - 1, before[1] - len(unused.request_json))
    with pytest.raises(local_source.SourceChanged, match="leg_cancelling"):
        owner.bind_ticket(cases.OWNER, _op(unused))
    assert owner.cancel(cases.OWNER, _op(unused)).status == "cancelled"
    assert provider.calls.count((CANCEL, _op(unused))) == 1

    ticket = owner.bind_ticket(cases.OWNER, _op(used))
    assert _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id).state == "acknowledged"
    with pytest.raises(local_source.SourceChanged, match="admission_owner_unconsumed"):
        owner.journal.release_ticket(ticket)
    with pytest.raises(local_source.SourceChanged, match="owner_unsettled"):
        owner.reap(cases.OWNER)
    assert owner.settle(cases.OWNER, _op(used)) == "applied"
    assert _rows(world, "SELECT released FROM typed_operation_tickets") == [(1,)]
    with pytest.raises(local_source.SourceChanged, match="owner_unsettled"):
        owner.reap(cases.OWNER)  # cancellation not yet acknowledged
    assert not provider.closable(record.plan)
    with pytest.raises(local_source.SourceChanged, match="admission_owner_unresolved_epoch"):
        owner.journal._adopt_epoch(cases._epoch(1, cases.NEXT))

    assert owner.acknowledge_cancel(cases.OWNER, _op(unused)) is True
    assert owner.acknowledge_cancel(cases.OWNER, _op(unused)) is True
    assert provider.calls.count((CANCEL_ACK, _op(unused))) == 1
    assert provider.closable(record.plan)
    assert owner.reap(cases.OWNER) is True
    assert owner.get(cases.OWNER) is None and owner.reap(cases.OWNER) is False
    with sqlite3.connect(world[0].path) as conn:
        assert reservation_capacity(conn) == (0, 0)
    # Released evidence persists until authenticated closure, then prunes.
    assert owner.journal.get_ticket(ticket).state == "acknowledged"
    assert owner.journal._adopt_epoch(cases._epoch(1, cases.NEXT)) == 1


def test_lost_cancellation_reply_keeps_request_durable_across_restart(world):
    request = _put(83)
    owner, provider, record = _admitted(world, (request,))
    provider.lose.add(CANCEL)
    with pytest.raises(TypedMutationRPCError):
        owner.cancel(cases.OWNER, _op(request))
    assert owner.get(cases.OWNER).cancel == ("requested",)
    restarted = _restart(world, provider)
    with pytest.raises(local_source.SourceChanged, match="leg_cancelling"):
        restarted.bind_ticket(cases.OWNER, _op(request))
    with pytest.raises(local_source.SourceChanged, match="cancellation_unsettled"):
        restarted.acknowledge_cancel(cases.OWNER, _op(request))
    assert (CANCEL_ACK, _op(request)) not in provider.calls
    # The provider already committed; the exact retry returns the same answer.
    assert restarted.cancel(cases.OWNER, _op(request)).status == "cancelled"
    provider.lose.add(CANCEL_ACK)
    with pytest.raises(TypedMutationRPCError):
        restarted.acknowledge_cancel(cases.OWNER, _op(request))
    assert restarted.get(cases.OWNER).cancel == ("cancelled",)
    assert restarted.acknowledge_cancel(cases.OWNER, _op(request)) is True
    assert provider.closable(record.plan)
    assert restarted.reap(cases.OWNER) is True


def test_revoked_linked_attempt_cancels_and_retires_ticket_and_intent_atomically(world):
    request = _put(84)
    owner, provider, record = _admitted(world, (request,))
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    provider.revoked = True
    with pytest.raises(TypedMutationRPCError):
        _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    assert owner.journal.get_ticket(ticket).state == "attempted"
    assert _rows(world, "SELECT count(*) FROM mutation_intents") == [(1,)]

    # Cancellation needs no fresh authority: it is how revoked work settles.
    assert owner.cancel(cases.OWNER, _op(request)).status == "cancelled"
    assert _rows(world, "SELECT count(*) FROM typed_operations") == [(0,)]
    assert _rows(world, "SELECT count(*) FROM typed_operation_tickets") == [(0,)]
    assert _rows(world, "SELECT count(*) FROM mutation_intents") == [(0,)]
    current = owner.get(cases.OWNER)
    assert (current.linked, current.cancel, current.outcome) == (
        (False,), ("cancelled",), ("cancelled",))
    with pytest.raises(local_source.SourceChanged, match="missing"):
        owner.journal.mark_attempted(request)
    with pytest.raises(local_source.SourceChanged, match="leg_cancelling"):
        owner.bind_ticket(cases.OWNER, _op(request))
    assert owner.acknowledge_cancel(cases.OWNER, _op(request)) is True
    assert owner.reap(cases.OWNER) is True
    assert provider.closable(record.plan)
    assert owner.journal._adopt_epoch(cases._epoch(1, cases.NEXT)) == 0


def test_executed_cancellation_recovers_exact_receipt_never_no_effect(world):
    request = _put(85)
    owner, provider, record = _admitted(world, (request,))
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    provider.lose.add(request.rpc_name)  # effect committed, reply lost
    with pytest.raises(TypedMutationRPCError):
        _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    provider.lose.add(CANCEL)
    with pytest.raises(TypedMutationRPCError):
        owner.cancel(cases.OWNER, _op(request))
    attempts = provider.calls.count((request.rpc_name, _op(request)))
    # A durable request blocks a fresh attempt until the provider answers.
    with pytest.raises(local_source.SourceChanged, match="admission_cancel_requested"):
        _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    assert provider.calls.count((request.rpc_name, _op(request))) == attempts

    restarted = _restart(world, provider)
    assert restarted.cancel(cases.OWNER, _op(request)).status == "executed"
    current = restarted.get(cases.OWNER)
    assert (current.linked, current.cancel, current.outcome) == ((True,), ("executed",), (None,))
    with pytest.raises(local_source.SourceChanged, match="receipt_unacknowledged"):
        restarted.settle(cases.OWNER, _op(request))
    with pytest.raises(local_source.SourceChanged, match="cancellation_unsettled"):
        restarted.acknowledge_cancel(cases.OWNER, _op(request))
    assert restarted.cancel(cases.OWNER, _op(request)).status == "executed"
    record_after = _driver(restarted).resume(ticket.protocol_epoch, ticket.operation_id)
    assert record_after.state == "acknowledged"
    assert record_after.receipt == base._receipt(request)
    assert len(provider.receipts) == 1
    assert restarted.settle(cases.OWNER, _op(request)) == "applied"
    assert restarted.reap(cases.OWNER) is True
    assert provider.closable(record.plan)


def test_executed_reply_for_unlinked_leg_relinks_only_for_exact_replay(world):
    request = _put(86)
    owner, provider, record = _admitted(world, (request,))
    # Provider evidence that some exact attempt executed without a local link.
    provider.receipts[_op(request)] = _raw_receipt(request)
    assert owner.cancel(cases.OWNER, _op(request)).status == "executed"
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    assert _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id).state == "acknowledged"
    assert owner.settle(cases.OWNER, _op(request)) == "applied"
    assert owner.reap(cases.OWNER) is True
    assert provider.closable(record.plan)


def test_settlement_outcome_comes_from_receipt_and_is_idempotent(world):
    request = _compare(87)
    owner, provider, record = _admitted(world, (request,))
    provider.conflicts.add(_op(request))
    with pytest.raises(local_source.SourceChanged, match="leg_unlinked"):
        owner.settle(cases.OWNER, _op(request))
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    assert owner.settle(cases.OWNER, _op(request)) == "conflict"
    assert owner.settle(cases.OWNER, _op(request)) == "conflict"
    with pytest.raises(local_source.SourceChanged, match="leg_settled"):
        owner.cancel(cases.OWNER, _op(request))
    with sqlite3.connect(world[0].path) as conn:
        conn.execute("UPDATE typed_admission_legs SET outcome='applied'")
    with pytest.raises(local_source.SourceChanged, match="material_changed"):
        owner.get(cases.OWNER)
    with sqlite3.connect(world[0].path) as conn:
        conn.execute("UPDATE typed_admission_legs SET outcome='conflict'")
    assert owner.reap(cases.OWNER) is True


def test_receipt_already_durable_refuses_new_cancellation(world):
    request = _put(88)
    owner, provider, _ = _admitted(world, (request,))
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    provider.lose.add("ab_ack_mutation_receipt")
    with pytest.raises(TypedMutationRPCError):
        _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    assert owner.journal.get_ticket(ticket).state == "completed"
    with pytest.raises(local_source.SourceChanged, match="leg_has_receipt"):
        owner.cancel(cases.OWNER, _op(request))
    assert CANCEL not in {name for name, _ in provider.calls}
    with pytest.raises(local_source.SourceChanged, match="receipt_unacknowledged"):
        owner.settle(cases.OWNER, _op(request))
    _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    assert owner.settle(cases.OWNER, _op(request)) == "applied"


def test_cancelled_reply_contradicting_a_local_receipt_fails_closed(world):
    request = _put(89)
    owner, provider, _ = _admitted(world, (request,))
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    owner.journal.mark_attempted(request)
    provider.lose.add(CANCEL)
    with pytest.raises(TypedMutationRPCError):
        owner.cancel(cases.OWNER, _op(request))
    # An attempt already in flight lands its exact receipt after the request.
    owner.journal.accept(request, base._receipt(request))
    with pytest.raises(local_source.SourceChanged, match="cancelled_operation_changed"):
        owner.cancel(cases.OWNER, _op(request))  # provider (wrongly) says cancelled
    assert owner.get(cases.OWNER).cancel == ("requested",)
    assert owner.journal.get_ticket(ticket).state == "accepted"
    provider.cancelled.discard(_op(request))
    provider.receipts[_op(request)] = _raw_receipt(request)
    assert owner.cancel(cases.OWNER, _op(request)).status == "executed"
    assert _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id).state == "acknowledged"
    assert owner.settle(cases.OWNER, _op(request)) == "applied"
    assert owner.reap(cases.OWNER) is True


def test_private_material_and_unadmitted_owners_are_never_reaped(world):
    request = _put(90)
    owner, provider, _ = _admitted(world, (request,), private_payload=b"private material")
    owner.cancel(cases.OWNER, _op(request))
    owner.acknowledge_cancel(cases.OWNER, _op(request))
    with pytest.raises(local_source.SourceChanged, match="private_material_unsettled"):
        owner.reap(cases.OWNER)
    draft = _put(91)
    owner.prepare(OTHER, (draft,))
    with pytest.raises(local_source.SourceChanged, match="owner_unsettled"):
        owner.reap(OTHER)
    with pytest.raises(local_source.SourceChanged, match="positive_reply_required"):
        owner.cancel(OTHER, _op(draft))
    assert (CANCEL, _op(draft)) not in provider.calls


@pytest.mark.parametrize("sql", [
    "UPDATE typed_admission_legs SET outcome='applied' WHERE ordinal=1",
    "UPDATE typed_admission_legs SET outcome=NULL WHERE ordinal=1",
    "UPDATE typed_admission_legs SET cancel='acknowledged' WHERE ordinal=0",
    "UPDATE typed_admission_legs SET linked=1 WHERE ordinal=1",
    "UPDATE typed_admission_legs SET outcome=NULL WHERE ordinal=0",
    "UPDATE typed_admission_legs SET cancel='executed' WHERE ordinal=1",
    "UPDATE typed_admission_owners SET state='full'",
    "UPDATE typed_operation_tickets SET released=0",
])
def test_impossible_leg_states_fail_closed(world, sql):
    used, unused = _put(92), _put(93)
    owner, provider, _ = _admitted(world, (used, unused))
    ticket = owner.bind_ticket(cases.OWNER, _op(used))
    _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)
    owner.settle(cases.OWNER, _op(used))
    owner.cancel(cases.OWNER, _op(unused))
    assert owner.get(cases.OWNER).outcome == ("applied", "cancelled")
    with sqlite3.connect(world[0].path) as conn:
        conn.execute(sql)
    with pytest.raises(local_source.SourceChanged):
        owner.get(cases.OWNER)


@pytest.mark.parametrize("first", ["execution", "cancellation"])
def test_in_flight_attempt_and_cancellation_resolve_to_one_exact_outcome(world, first):
    request = _put(94)
    owner, provider, record = _admitted(world, (request,))
    ticket = owner.bind_ticket(cases.OWNER, _op(request))
    in_flight, requested, decided = (threading.Event() for _ in range(3))

    def attempt_waits():
        in_flight.set()
        assert requested.wait(5)
        if first == "cancellation":
            assert decided.wait(5)

    def cancellation_waits():
        requested.set()  # the local request committed before this RPC
        if first == "execution":
            assert decided.wait(5)

    provider.before[request.rpc_name] = attempt_waits
    provider.before[CANCEL] = cancellation_waits
    if first == "execution":
        provider.after[request.rpc_name] = decided.set
    else:
        provider.after[CANCEL] = decided.set

    def attempt():
        return _driver(owner).resume(ticket.protocol_epoch, ticket.operation_id)

    def cancel():
        assert in_flight.wait(5)
        return owner.cancel(cases.OWNER, _op(request))

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempted, cancelled = pool.submit(attempt), pool.submit(cancel)
        status = cancelled.result(timeout=10).status
        if first == "execution":
            assert status == "executed"
            assert attempted.result(timeout=10).state == "acknowledged"
        else:
            assert status == "cancelled"
            with pytest.raises(TypedMutationRPCError):
                attempted.result(timeout=10)
    if first == "execution":
        assert owner.settle(cases.OWNER, _op(request)) == "applied"
        assert len(provider.receipts) == 1
    else:
        assert not provider.receipts
        assert _rows(world, "SELECT count(*) FROM typed_operations") == [(0,)]
        assert _rows(world, "SELECT count(*) FROM mutation_intents") == [(0,)]
        owner.acknowledge_cancel(cases.OWNER, _op(request))
    assert owner.reap(cases.OWNER) is True
    assert provider.closable(record.plan)
