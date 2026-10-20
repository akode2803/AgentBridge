"""Inactive admission wire: exact bounded plans and pinned RPC replies."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from uuid import UUID

import pytest

from agentbridge.transport.provider_admission import (
    AdmissionLeg, AdmissionPlan, ProviderAdmission, ProviderAdmissionError,
    prepare_admission_plan,
)
from agentbridge.transport.typed_mutations import (
    TypedMutationRPC, TypedMutationRPCError, prepare_mutation,
)

ROOT = "team"
ACTOR = "12345678-1234-5678-9234-567812345673"
EPOCH = "12345678-1234-5678-9234-567812345671"
OWNER = "12345678-1234-5678-9234-567812345675"
OP1 = "12345678-1234-5678-9234-567812345672"
OP2 = "12345678-1234-5678-9234-567812345676"
URL = "https://example.supabase.co"


class _Query:
    def __init__(self, client, name, params):
        self.client, self.name, self.params = client, name, params
        self.request = SimpleNamespace(headers=dict(client.headers))

    def execute(self):
        self.client.calls.append((self.name, self.params))
        result = self.client.on_execute(self.name, self.params)
        return SimpleNamespace(data=result)


class _Client:
    def __init__(self):
        self.calls = []
        self.on_execute = lambda name, params: None
        self.session = SimpleNamespace(
            user=SimpleNamespace(id=ACTOR, role="authenticated"),
            access_token="token-1",
        )
        self.auth = SimpleNamespace(
            get_session=lambda: self.session,
            get_user=lambda *, jwt: SimpleNamespace(
                user=SimpleNamespace(id=ACTOR, role="authenticated")),
        )
        self.postgrest = self
        self.base_url = URL + "/rest/v1"
        self.headers = {"Authorization": "Bearer token-1"}

    def rpc(self, name, params):
        return _Query(self, name, params)


class _Provider:
    def __init__(self, client):
        self._client = client
        self._env = {"SUPABASE_URL": URL}
        self.root = ROOT
        self.auth_mode = "member:alice"
        self.revision = 1

    def prepare_node_identity(self):
        return self.revision

    def node_identity_revision(self):
        return self.revision

    def _sb(self):
        return self._client


def _requests():
    return (
        prepare_mutation(ROOT, EPOCH, OP1, ACTOR, "doc_put",
                         {"p_path": "accounts/alice.json", "p_data": {"v": 1}}),
        prepare_mutation(ROOT, EPOCH, OP2, ACTOR, "member_leave", {}),
    )


def _wire():
    client = _Client()
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root=ROOT, endpoint=URL)
    return ProviderAdmission(rpc), client


def _admission_reply(plan, status="admitted"):
    return {
        "schema_version": 1, "root": plan.root, "actor_id": plan.actor_id,
        "protocol_epoch": plan.protocol_epoch, "owner_id": plan.owner_id,
        "status": status, "plan": plan.wire_plan(),
    }


def _cancel_reply(plan, leg, status="cancelled"):
    return {
        "schema_version": 1, "root": plan.root, "actor_id": plan.actor_id,
        "protocol_epoch": plan.protocol_epoch, "owner_id": plan.owner_id,
        "operation_id": leg.operation_id, "kind": leg.kind,
        "client_digest": leg.client_digest, "status": status,
    }


def test_plan_from_exact_prepared_requests_and_immutable_wire():
    requests = _requests()
    plan = prepare_admission_plan(OWNER, requests)
    assert plan == AdmissionPlan.from_prepared(OWNER, requests)
    assert plan.legs == tuple(AdmissionLeg.from_prepared(r) for r in requests)
    assert b'"p_data"' not in plan.request_json
    assert b'"v"' not in plan.request_json
    assert len(plan.request_json) < 512 * 1024
    wire = plan.rpc_parameters()
    assert set(wire) == {"p_root", "p_protocol_epoch", "p_owner_id", "p_plan"}
    assert wire["p_plan"] == plan.wire_plan()
    wire["p_plan"][0]["kind"] = "doc_delete"
    assert plan.rpc_parameters()["p_plan"][0]["kind"] == "doc_put"
    with pytest.raises(FrozenInstanceError):
        plan.owner_id = OP1


def test_plan_rejects_mixed_duplicate_unsupported_and_oversized_count():
    a, b = _requests()
    with pytest.raises(ValueError, match="duplicate"):
        AdmissionPlan.from_prepared(OWNER, (a, a))
    other = prepare_mutation("other", EPOCH, OP2, ACTOR, "member_leave", {})
    with pytest.raises(ValueError, match="mixed"):
        AdmissionPlan.from_prepared(OWNER, (a, other))
    with pytest.raises(ValueError, match="owner"):
        AdmissionPlan.from_prepared("not-a-uuid", (a,))
    with pytest.raises(ValueError, match="kind"):
        AdmissionLeg(OP1, "arbitrary_sql", a.expectation.client_digest)
    with pytest.raises(ValueError, match="digest"):
        AdmissionLeg(OP1, "doc_put", "A" * 64)
    with pytest.raises(ValueError, match="operation"):
        AdmissionLeg("12345678-1234-5678-ABCD-567812345672", "doc_put",
                     a.expectation.client_digest)
    legs = tuple(AdmissionLeg(str(UUID(int=i + 1)), "member_leave", "a" * 64)
                 for i in range(1025))
    assert len(AdmissionPlan(ROOT, ACTOR, EPOCH, OWNER, legs).legs) == 1025
    with pytest.raises(ValueError, match="legs"):
        AdmissionPlan(ROOT, ACTOR, EPOCH, OWNER, legs + (AdmissionLeg(OP1, "doc_put", "b" * 64),))


def test_plan_revalidates_forged_prepared_mutation():
    request = _requests()[0]
    object.__setattr__(request, "arguments_json", b"{}")
    with pytest.raises(ValueError, match="arguments"):
        prepare_admission_plan(OWNER, (request,))


@pytest.mark.parametrize("status", ["admitted", "full"])
def test_admit_exact_response_and_no_automatic_mutation(status):
    plan = prepare_admission_plan(OWNER, _requests())
    wire, client = _wire()
    client.on_execute = lambda name, params: _admission_reply(plan, status)
    result = wire.admit(plan)
    assert result.status == status
    assert client.calls == [("ab_admit_mutation_owner", plan.rpc_parameters())]


@pytest.mark.parametrize("change", [
    lambda r: {**r, "schema_version": True},
    lambda r: {**r, "actor_id": OP1},
    lambda r: {**r, "owner_id": OP1},
    lambda r: {**r, "status": "allowed"},
    lambda r: {**r, "private_key": "bad"},
    lambda r: {**r, "plan": list(reversed(r["plan"]))},
    lambda r: {**r, "plan": [{**r["plan"][0], "client_digest": "b" * 64}, r["plan"][1]]},
    lambda r: {**r, "plan": [{**r["plan"][0], "extra": 1}, r["plan"][1]]},
    lambda r: {**r, "plan": r["plan"] + [{}] * 50000},
])
def test_admit_rejects_mismatched_or_unbounded_response(change):
    plan = prepare_admission_plan(OWNER, _requests())
    wire, client = _wire()
    client.on_execute = lambda name, params: change(_admission_reply(plan))
    with pytest.raises(ProviderAdmissionError, match="response"):
        wire.admit(plan)
    assert len(client.calls) == 1


def test_lost_admission_reply_is_one_call_and_replay_is_exact():
    plan = prepare_admission_plan(OWNER, _requests())
    wire, client = _wire()
    attempts = 0

    def execute(name, params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("reply lost")
        return _admission_reply(plan)

    client.on_execute = execute
    with pytest.raises(TypedMutationRPCError, match="RPC failed"):
        wire.admit(plan)
    assert attempts == 1
    assert wire.admit(plan).status == "admitted"
    assert client.calls[0] == client.calls[1]


@pytest.mark.parametrize("status", ["cancelled", "executed"])
def test_cancel_exact_response_then_strict_ack(status):
    plan = prepare_admission_plan(OWNER, _requests())
    leg = plan.legs[0]
    wire, client = _wire()
    client.on_execute = lambda name, params: (
        _cancel_reply(plan, leg, status)
        if name == "ab_cancel_mutation_admission" else True
    )
    assert wire.cancel(plan, leg).status == status
    assert wire.acknowledge_cancel(plan, leg) is True
    assert client.calls[0] == (
        "ab_cancel_mutation_admission", {
            "p_root": ROOT, "p_protocol_epoch": EPOCH, "p_owner_id": OWNER,
            "p_operation_id": OP1, "p_kind": "doc_put",
            "p_client_digest": leg.client_digest,
        },
    )
    assert client.calls[1][0] == "ab_ack_mutation_admission_cancel"
    assert client.calls[1][1] == client.calls[0][1]
    client.on_execute = lambda name, params: 1
    with pytest.raises(ProviderAdmissionError, match="acknowledgment"):
        wire.acknowledge_cancel(plan, leg)


def test_cancellation_mismatch_and_unknown_leg_fail_closed():
    plan = prepare_admission_plan(OWNER, _requests())
    wire, client = _wire()
    leg = plan.legs[0]
    client.on_execute = lambda name, params: {**_cancel_reply(plan, leg), "kind": "doc_delete"}
    with pytest.raises(ProviderAdmissionError, match="response"):
        wire.cancel(plan, leg)
    assert len(client.calls) == 1
    alien = AdmissionLeg(str(UUID(int=12345)), "member_leave", "a" * 64)
    with pytest.raises(ValueError, match="not in"):
        wire.cancel(plan, alien)
    assert len(client.calls) == 1


def test_lost_cancellation_reply_replays_exact_same_leg():
    plan = prepare_admission_plan(OWNER, _requests())
    leg = plan.legs[1]
    wire, client = _wire()
    attempts = 0

    def execute(name, params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("reply lost")
        return _cancel_reply(plan, leg, "executed")

    client.on_execute = execute
    with pytest.raises(TypedMutationRPCError, match="RPC failed"):
        wire.cancel(plan, leg)
    assert attempts == 1
    assert wire.cancel(plan, leg).status == "executed"
    assert client.calls[0] == client.calls[1]


def test_admission_identity_drift_blocks_send():
    plan = prepare_admission_plan(OWNER, _requests())
    wire, client = _wire()
    client.session.access_token = "other-token"
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        wire.admit(plan)
    assert not client.calls
    client.session.access_token = "token-1"
    wrong = AdmissionPlan("other", ACTOR, EPOCH, OWNER, plan.legs)
    with pytest.raises(ProviderAdmissionError, match="identity"):
        wire.admit(wrong)
    assert not client.calls
