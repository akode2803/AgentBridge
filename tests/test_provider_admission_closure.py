"""Authenticated epoch closure: draft rebase and certification of lost ACKs."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

import test_operation_journal as base
import test_provider_admission_owner as cases
import test_provider_admission_settlement as settlement
from agentbridge.mesh import identity_publication as identity
from agentbridge.store import local_source
from agentbridge.store.provider_admission_owner import reservation_capacity
from agentbridge.transport.receipt_retirement import ProviderEpoch
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = base.world
CLOSED = cases._epoch(1, cases.NEXT)


class _Provider(settlement._Provider):
    """Adds administrative rotation: only a closable epoch rotates, after
    which every old-epoch call is rejected as unavailable."""

    def __init__(self, client):
        super().__init__(client)
        self.rotated = False

    def rotate(self, *plans):
        assert all(self.closable(plan) for plan in plans)
        self.rotated = True

    def _handle(self, name, params):
        if name == "ab_mutation_epoch_state":
            return {"schema_version": 1, "root": "mesh", "actor_id": base.ACTOR,
                    "role": "authenticated", "lifecycle_id": cases.LIFECYCLE,
                    "protocol_epoch": cases.NEXT if self.rotated else base.EPOCH,
                    "generation": int(self.rotated), "sealed": True}
        if self.rotated and params.get("p_protocol_epoch") == base.EPOCH:
            raise RuntimeError("mutation admission epoch unavailable")
        return super()._handle(name, params)


def _world(world, requests, *, private_payload=b""):
    owner, client = cases._owner(world)
    provider = _Provider(client)
    record = owner.prepare(cases.OWNER, requests, private_payload=private_payload)
    provider.owner(record)
    return owner, provider, record


def _capacity(world):
    with sqlite3.connect(world[0].path) as conn:
        return reservation_capacity(conn)


@pytest.mark.parametrize("draft", ["prepared", "full", "admission_attempted"])
def test_never_admitted_draft_rebases_at_authenticated_adoption(world, draft):
    requests = (settlement._put(101), settlement._put(102))
    owner, provider, record = _world(world, requests, private_payload=b"retained private")
    if draft == "full":
        owner.wire._rpc._client.on_execute = lambda name, params: cases._reply(record.plan, "full")
        assert owner.admit(cases.OWNER).status == "full"
    elif draft == "admission_attempted":
        # Ambiguous locally. Had the provider admitted it, the unsettled legs
        # would have made the closure below impossible.
        def unreachable():
            raise TimeoutError("admission request did not reach the provider")
        provider.before["ab_admit_mutation_owner"] = unreachable
        with pytest.raises(TypedMutationRPCError):
            owner.admit(cases.OWNER)
    assert owner.get(cases.OWNER).state == draft
    before = _capacity(world)

    assert owner.journal._adopt_epoch(CLOSED) == 0
    rebased = owner.get(cases.OWNER)
    assert rebased.state == "prepared"
    assert rebased.plan.protocol_epoch == cases.NEXT
    assert rebased.private_payload == b"retained private"
    assert rebased.owner_kind == record.owner_kind
    for old, new in zip(record.requests, rebased.requests):
        assert new.expectation.protocol_epoch == cases.NEXT
        assert (new.expectation.operation_id, new.expectation.kind,
                new.expectation.client_digest, new.arguments_json) == (
            old.expectation.operation_id, old.expectation.kind,
            old.expectation.client_digest, old.arguments_json)
    assert _capacity(world) == before
    with sqlite3.connect(world[0].path) as conn:
        assert conn.execute("SELECT count(*) FROM typed_operations").fetchone() == (0,)

    # The rebased draft admits and dispatches only in the successor epoch.
    owner.wire._rpc._client.on_execute = lambda name, params: cases._reply(rebased.plan, "admitted")
    assert owner.admit(cases.OWNER).status == "admitted"
    ticket = owner.bind_ticket(cases.OWNER, settlement._op(requests[0]))
    assert ticket.protocol_epoch == cases.NEXT


def test_late_old_epoch_admission_reply_cannot_attach_to_rebased_draft(world):
    request = settlement._put(103)
    owner, provider, record = _world(world, (request,))
    in_flight, release = threading.Event(), threading.Event()

    def late(name, params):
        in_flight.set()
        assert release.wait(5)
        return cases._reply(record.plan, "admitted")

    owner.wire._rpc._client.on_execute = late
    with ThreadPoolExecutor(max_workers=1) as pool:
        admitting = pool.submit(owner.admit, cases.OWNER)
        assert in_flight.wait(5)
        owner.journal._adopt_epoch(CLOSED)
        release.set()
        with pytest.raises(local_source.SourceChanged, match="plan_changed"):
            admitting.result(timeout=10)
    current = owner.get(cases.OWNER)
    assert (current.state, current.plan.protocol_epoch) == ("prepared", cases.NEXT)


def test_identity_draft_survives_closure_without_regenerating_material(world, monkeypatch):
    from agentbridge import crypto
    from test_identity_publication import prepare as prepare_identity

    owners, _ = cases._owner(world)
    artifact = prepare_identity(root="mesh", actor_id=base.ACTOR)
    identity.freeze_agent_identity(owners, artifact, protocol_epoch=base.EPOCH,
                                   source_epoch=str(uuid4()))
    for name in ("generate_identity", "sign"):
        monkeypatch.setattr(crypto, name, lambda *a, **k: pytest.fail("regenerated"))
    owners.journal._adopt_epoch(CLOSED)
    recovered = identity.recover_agent_identity(owners, artifact.owner_id)
    assert recovered == artifact and recovered.bundle == artifact.bundle
    assert {r.expectation.protocol_epoch for r in owners.get(artifact.owner_id).requests} == {
        cases.NEXT}


def test_closure_certifies_lost_cancellation_ack_before_reap_and_adoption(world):
    request = settlement._put(104)
    owner, provider, record = _world(world, (request,))
    owner.admit(cases.OWNER)
    assert owner.cancel(cases.OWNER, settlement._op(request)).status == "cancelled"
    provider.lose.add(settlement.CANCEL_ACK)
    with pytest.raises(TypedMutationRPCError):
        owner.acknowledge_cancel(cases.OWNER, settlement._op(request))
    provider.rotate(record.plan)
    with pytest.raises(TypedMutationRPCError):
        owner.acknowledge_cancel(cases.OWNER, settlement._op(request))
    with pytest.raises(local_source.SourceChanged, match="owner_unsettled"):
        owner.reap(cases.OWNER)
    with pytest.raises(local_source.SourceChanged, match="admission_owner_unresolved_epoch"):
        owner.journal._adopt_epoch(CLOSED)

    assert owner.certify_closure() == 1  # authenticated epoch read
    assert owner.get(cases.OWNER).cancel == ("acknowledged",)
    assert owner.certify_closure() == 0
    assert owner.reap(cases.OWNER) is True
    owner.journal._adopt_epoch(CLOSED)


def test_closure_certifies_lost_receipt_ack_so_the_leg_can_settle(world):
    request = settlement._compare(105)
    owner, provider, record = _world(world, (request,))
    provider.conflicts.add(settlement._op(request))
    owner.admit(cases.OWNER)
    ticket = owner.bind_ticket(cases.OWNER, settlement._op(request))
    provider.lose.add("ab_ack_mutation_receipt")
    driver = settlement._driver(owner)
    with pytest.raises(TypedMutationRPCError):
        driver.resume(ticket.protocol_epoch, ticket.operation_id)
    assert owner.journal.get_ticket(ticket).state == "completed"
    provider.rotate(record.plan)
    with pytest.raises(TypedMutationRPCError):
        driver.resume(ticket.protocol_epoch, ticket.operation_id)
    with pytest.raises(local_source.SourceChanged, match="receipt_unacknowledged"):
        owner.settle(cases.OWNER, settlement._op(request))

    assert owner._certify_closure(CLOSED) == 1
    assert owner.journal.get_ticket(ticket).state == "acknowledged"
    assert owner.settle(cases.OWNER, settlement._op(request)) == "conflict"
    assert owner.reap(cases.OWNER) is True
    assert owner.journal._adopt_epoch(CLOSED) == 1


def test_certification_needs_newer_generation_and_leaves_other_work_to_adoption(world):
    request = settlement._put(106)
    owner, provider, record = _world(world, (request,))
    owner.admit(cases.OWNER)
    generic = owner.journal.allocate_ticket(
        "doc_put", {"p_path": "presence/generic.json", "p_data": {}})
    retained = owner.journal.get_ticket(generic).request
    owner.journal.mark_attempted(retained)
    owner.journal.accept(retained, base._receipt(retained))
    owner.journal.complete(retained)

    assert owner._certify_closure(cases._epoch()) == 0
    with pytest.raises(local_source.SourceChanged, match="epoch_changed"):
        owner._certify_closure(cases._epoch(0, cases.NEXT))
    with pytest.raises(local_source.SourceChanged, match="epoch_regressed"):
        owner._certify_closure(ProviderEpoch(
            base.ENDPOINT, "mesh", base.ACTOR, str(uuid4()), cases.NEXT, 1))
    assert owner._certify_closure(CLOSED) == 0
    assert owner.journal.get_ticket(generic).state == "completed"


def test_owner_outside_the_lifecycle_epoch_fails_closed(world):
    owner, provider, record = _world(world, (settlement._put(107),))
    with sqlite3.connect(world[0].path) as conn:
        conn.execute("UPDATE typed_admission_owners SET protocol_epoch=?", (cases.NEXT,))
        conn.execute("UPDATE typed_admission_legs SET protocol_epoch=?", (cases.NEXT,))
    with pytest.raises(local_source.SourceChanged, match="owner_epoch_changed"):
        owner.get(cases.OWNER)


def test_rebase_refuses_tampered_draft_material_and_keeps_old_epoch(world):
    owner, provider, record = _world(world, (settlement._put(108),))
    with sqlite3.connect(world[0].path) as conn:
        raw = json.loads(conn.execute("SELECT request FROM typed_admission_legs").fetchone()[0])
        raw["arguments"]["p_data"] = {"value": "forged"}
        forged = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        conn.execute("UPDATE typed_admission_legs SET request=?", (forged,))
    with pytest.raises(local_source.SourceChanged, match="material_changed"):
        owner.journal._adopt_epoch(CLOSED)
    with sqlite3.connect(world[0].path) as conn:
        assert conn.execute(
            "SELECT protocol_epoch FROM typed_operation_lifecycle").fetchone() == (base.EPOCH,)
