"""Sealed-root self-claim through the typed, receipt-bearing member RPC."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from postgrest.exceptions import APIError

from agentbridge.transport import member_claim
from agentbridge.transport.member_claim import (
    ALREADY_MEMBER, CLAIMED, UNSEALED, MemberClaimError, claim_member,
    member_claim_operation_id,
)
from agentbridge.transport.supabase import SupabaseTransport
from agentbridge.transport.typed_mutations import TypedMutationRPC, TypedMutationRPCError

ACTOR = "12345678-1234-5678-9234-567812345673"
EPOCH = "12345678-1234-5678-9234-567812345671"
NEXT_EPOCH = "12345678-1234-5678-9234-567812345674"
LIFECYCLE = "12345678-1234-5678-9234-567812345679"
URL = "https://example.supabase.co"


class _Query:
    def __init__(self, client, name, parameters):
        self.client, self.name, self.parameters = client, name, parameters
        self.request = SimpleNamespace(headers=dict(client.headers))

    def execute(self):
        client, args = self.client, self.parameters
        client.calls.append((self.name, args))
        fault = client.faults.get(self.name)
        if fault:
            error = fault.pop(0)
            if error is not None and not callable(error):
                raise error
            if callable(error):
                return error(self)
        if self.name == "ab_mutation_epoch_state":
            return SimpleNamespace(data={
                "schema_version": 1, "root": args["p_root"], "actor_id": ACTOR,
                "role": "authenticated", "lifecycle_id": LIFECYCLE,
                "protocol_epoch": client.epoch, "generation": client.generation,
                "sealed": True,
            })
        if self.name == "ab_ack_mutation_receipt":
            client.acked.append(args["p_operation_id"])
            return SimpleNamespace(data=True)
        assert self.name == "ab_mutation_member_claim"
        return SimpleNamespace(data=client.claim(args))


class _Table:
    def __init__(self, client):
        self.client, self.filters = client, {}

    def select(self, _columns):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def execute(self):
        rows = [{"username": name} for (root, name), uid in self.client.members.items()
                if root == self.filters["root"] and uid == self.filters["uid"]]
        return SimpleNamespace(data=rows)


class _Client:
    """One authenticated member session with a fake receipt store."""

    def __init__(self):
        self.session = SimpleNamespace(
            user=SimpleNamespace(id=ACTOR, role="authenticated"), access_token="token-1")
        self.auth = SimpleNamespace(
            get_session=lambda: self.session,
            get_user=lambda *, jwt: SimpleNamespace(
                user=SimpleNamespace(id=ACTOR, role="authenticated")))
        self.postgrest = self
        self.base_url = URL + "/rest/v1"
        self.headers = {"Authorization": "Bearer token-1"}
        self.calls, self.acked, self.faults = [], [], {}
        self.epoch, self.generation = EPOCH, 1
        self.members: dict[tuple[str, str], str] = {}
        self.receipts: dict[tuple[str, str], dict] = {}
        self.effects = 0

    def rpc(self, name, parameters):
        return _Query(self, name, parameters)

    def from_(self, name):
        assert name == "ab_members"
        return _Table(self)

    def claim(self, args):
        key = (args["p_protocol_epoch"], args["p_operation_id"])
        if key in self.receipts:  # exact replay, like ab_mutation_begin
            return self.receipts[key]
        if args["p_protocol_epoch"] != self.epoch:
            raise APIError({"code": "55000", "message": "typed mutation epoch is stale"})
        if (args["p_root"], args["p_username"]) in self.members:
            raise APIError({"code": "55000", "message": "conflicts with existing identity"})
        self.members[(args["p_root"], args["p_username"])] = ACTOR
        self.effects += 1
        self.receipts[key] = {
            "schema_version": 2, "root": args["p_root"],
            "protocol_epoch": args["p_protocol_epoch"],
            "operation_id": args["p_operation_id"], "actor_id": ACTOR,
            "kind": "member_claim", "client_digest": args["p_payload_digest"],
            "provider_digest": "a" * 64,
            "result": {"username": args["p_username"], "claimed": True},
        }
        return self.receipts[key]


@pytest.fixture
def world():
    client = _Client()
    provider = SupabaseTransport("team", env={"SUPABASE_URL": URL}, client=client)
    provider.auth_mode = "member:ben"
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    return client, rpc


def _claims(client):
    return [args for name, args in client.calls if name == "ab_mutation_member_claim"]


def _lost_after_effect(query):
    """The provider commits, then the reply is lost (no provider code)."""
    query.client.claim(query.parameters)
    raise ConnectionError("reply lost")


def test_sealed_claim_is_receipted_and_acknowledged(world):
    client, rpc = world
    assert claim_member(rpc, "ben", sleep=lambda _s: None) == CLAIMED
    operation = member_claim_operation_id("team", ACTOR, "ben")
    assert [c["p_operation_id"] for c in _claims(client)] == [operation]
    assert client.acked == [operation]
    assert client.members == {("team", "ben"): ACTOR}


def test_operation_key_is_deterministic_per_root_account_and_name():
    key = member_claim_operation_id("team", ACTOR, "ben")
    assert key == member_claim_operation_id("team", ACTOR, "ben")
    assert len({key, member_claim_operation_id("team", ACTOR, "bea"),
                member_claim_operation_id("other", ACTOR, "ben"),
                member_claim_operation_id("team", NEXT_EPOCH, "ben")}) == 4


def test_lost_reply_replays_the_exact_operation_once(world):
    client, rpc = world
    client.faults["ab_mutation_member_claim"] = [_lost_after_effect]
    assert claim_member(rpc, "ben", sleep=lambda _s: None) == CLAIMED
    claims = _claims(client)
    assert len(claims) == 2 and claims[0] == claims[1]
    assert client.effects == 1
    assert client.acked == [claims[0]["p_operation_id"]]


def test_unknown_acknowledgment_is_resolved_by_a_rerun(world):
    client, rpc = world
    client.faults["ab_ack_mutation_receipt"] = [ConnectionError("lost")] * 4
    with pytest.raises(MemberClaimError) as exc:
        claim_member(rpc, "ben", sleep=lambda _s: None)
    assert exc.value.reason == "unknown"
    assert client.acked == []
    # A rerun replays the same operation in the same epoch and finishes it.
    assert claim_member(rpc, "ben", sleep=lambda _s: None) == CLAIMED
    assert client.effects == 1
    assert client.acked == [member_claim_operation_id("team", ACTOR, "ben")]


@pytest.mark.parametrize("code", ["55000", "PGRST202", "42883"])
def test_unsealed_or_older_root_falls_back_to_the_legacy_claim(world, code):
    client, rpc = world
    client.faults["ab_mutation_epoch_state"] = [APIError({"code": code, "message": "x"})]
    assert claim_member(rpc, "ben") == UNSEALED
    assert _claims(client) == []


def test_unreadable_epoch_fails_closed(world):
    client, rpc = world
    client.faults["ab_mutation_epoch_state"] = [ConnectionError("offline")]
    with pytest.raises(MemberClaimError) as exc:
        claim_member(rpc, "ben")
    assert exc.value.reason == "unknown"
    assert _claims(client) == []


def test_name_held_by_someone_else_is_taken(world):
    client, rpc = world
    client.members[("team", "ben")] = "someone-else"
    with pytest.raises(MemberClaimError) as exc:
        claim_member(rpc, "ben", sleep=lambda _s: None)
    assert exc.value.reason == "taken"
    assert client.acked == []


def test_name_already_held_by_this_account_is_success(world):
    client, rpc = world
    # e.g. claimed in an earlier, already rotated epoch or before the seal
    client.members[("team", "ben")] = ACTOR
    assert claim_member(rpc, "ben", sleep=lambda _s: None) == ALREADY_MEMBER
    assert client.acked == []


def test_epoch_rotation_between_read_and_claim_retries_in_the_new_epoch(world):
    client, rpc = world

    def rotate(query):
        query.client.epoch, query.client.generation = NEXT_EPOCH, 2
        return query.client.claim(query.parameters)

    client.faults["ab_mutation_member_claim"] = [rotate]
    assert claim_member(rpc, "ben", sleep=lambda _s: None) == CLAIMED
    epochs = [c["p_protocol_epoch"] for c in _claims(client)]
    assert epochs == [EPOCH, NEXT_EPOCH]
    assert len({c["p_operation_id"] for c in _claims(client)}) == 1
    assert client.effects == 1


def test_full_epoch_is_reported_without_retrying(world):
    client, rpc = world
    client.faults["ab_mutation_member_claim"] = [
        APIError({"code": "54000", "message": "typed mutation receipt epoch is full"})]
    with pytest.raises(MemberClaimError) as exc:
        claim_member(rpc, "ben", sleep=lambda _s: None)
    assert exc.value.reason == "full"
    assert len(_claims(client)) == 1


def test_contended_slot_replays_the_same_operation(world):
    client, rpc = world
    client.faults["ab_mutation_member_claim"] = [
        APIError({"code": "40001", "message": "contended"})]
    assert claim_member(rpc, "ben", sleep=lambda _s: None) == CLAIMED
    claims = _claims(client)
    assert len(claims) == 2 and claims[0] == claims[1]


def test_rpc_error_keeps_only_a_bounded_provider_code(world):
    client, rpc = world
    client.faults["ab_mutation_epoch_state"] = [
        APIError({"code": "55000", "message": "secret detail"}),
        APIError({"code": "not a code", "message": "x"}),
        ConnectionError("offline"),
    ]
    codes = []
    for _ in range(3):
        with pytest.raises(TypedMutationRPCError) as exc:
            rpc._call("ab_mutation_epoch_state", {"p_root": "team"})
        assert "secret detail" not in str(exc.value)
        codes.append(exc.value.code)
    assert codes == ["55000", None, None]


def test_open_member_rpc_pins_the_member_session(monkeypatch):
    client = _Client()
    seen = {}

    class _Transport(SupabaseTransport):
        def __init__(self, root, *, env):
            seen["env"] = env
            super().__init__(root, env=env, client=client)
            self.auth_mode = "member:ben"

    monkeypatch.setattr("agentbridge.transport.supabase.SupabaseTransport", _Transport)
    rpc = member_claim.open_member_rpc({"SUPABASE_URL": URL + "/"}, "team", ACTOR)
    assert (rpc.root, rpc.actor_id, rpc.endpoint) == ("team", ACTOR, URL)
