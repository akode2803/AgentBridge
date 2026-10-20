"""Root-atomic local ownership of full effect-free provider admission plans."""

from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest

import test_operation_journal as journal_cases
from agentbridge.store import local_source
from agentbridge.store.provider_admission_owner import (
    AdmissionOwnerChanged, ProviderAdmissionOwnerJournal, reservation_capacity,
)
from agentbridge.store.operation_journal import MAX_RECEIPT_BYTES, _bytes_used
from agentbridge.transport.provider_admission import ProviderAdmission
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.receipt_retirement import ProviderEpoch
from agentbridge.transport.typed_mutations import (
    TypedMutationRPC, TypedMutationRPCError, prepare_mutation,
)
from test_operation_journal import ACTOR, ENDPOINT, EPOCH, _reload

world = journal_cases.world
LIFECYCLE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
NEXT = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


class _Query:
    def __init__(self, client, name, params):
        self.client, self.name, self.params = client, name, params
        self.request = SimpleNamespace(headers=dict(client.headers))

    def execute(self):
        self.client.calls.append((self.name, self.params))
        return SimpleNamespace(data=self.client.on_execute(self.name, self.params))


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
        self.base_url = ENDPOINT + "/rest/v1"
        self.headers = {"Authorization": "Bearer token-1"}

    def rpc(self, name, params):
        return _Query(self, name, params)


class _Provider:
    def __init__(self, client):
        self._client = client
        self._env = {"SUPABASE_URL": ENDPOINT}
        self.root = "mesh"
        self.auth_mode = "member:alice"
        self.revision = 1

    def prepare_node_identity(self):
        return self.revision

    def node_identity_revision(self):
        return self.revision

    def _sb(self):
        return self._client


def _wire():
    client = _Client()
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR,
                           root="mesh", endpoint=ENDPOINT)
    return ProviderAdmission(rpc), client


def _requests():
    return (
        prepare_mutation("mesh", EPOCH, str(uuid.UUID(int=11)), ACTOR,
                         "doc_create", {"p_path": "accounts/alice.json",
                                        "p_data": {"private": "exact"}}),
        prepare_mutation("mesh", EPOCH, str(uuid.UUID(int=12)), ACTOR,
                         "member_leave", {}),
    )


def _epoch(generation=0, epoch=EPOCH):
    return ProviderEpoch(ENDPOINT, "mesh", ACTOR, LIFECYCLE, epoch, generation)


def _owner(world):
    journal = world[-1]
    journal._adopt_epoch(_epoch())
    wire, client = _wire()
    return ProviderAdmissionOwnerJournal(journal, wire), client


def _reply(plan, status):
    return {"schema_version": 1, "root": plan.root,
            "actor_id": plan.actor_id, "protocol_epoch": plan.protocol_epoch,
            "owner_id": plan.owner_id, "status": status,
            "plan": plan.wire_plan()}


def test_freeze_full_private_plan_before_admission_and_restart(world):
    root, _, _, _, journal = world
    owner, client = _owner(world)
    requests = _requests()
    record = owner.prepare(OWNER, iter(requests))
    assert record.state == "prepared"
    assert record.requests == requests
    assert not client.calls
    assert owner.admitted_request(OWNER, requests[0].expectation.operation_id) is None
    with sqlite3.connect(root.path) as conn:
        assert conn.execute("SELECT format FROM mutation_root").fetchone() == (9,)
        assert conn.execute("SELECT state FROM typed_admission_owners").fetchone() == ("prepared",)
        replay = conn.execute("SELECT request FROM typed_admission_legs ORDER BY ordinal").fetchall()
        assert [raw for (raw,) in replay] == [request.request_json for request in requests]
        assert conn.execute("SELECT count(*) FROM typed_operations").fetchone() == (0,)
    reopened = ProviderAdmissionOwnerJournal(_reload(world), _wire()[0])
    assert reopened.get(OWNER) == record
    assert reopened.prepare(OWNER, requests) == record
    assert journal.get_operation(requests[0].expectation.operation_id) is None


def test_ambiguous_reply_stays_nondispatchable_then_exact_replay(world):
    owner, client = _owner(world)
    requests = _requests()
    frozen = owner.prepare(OWNER, requests)
    attempts = 0

    def respond(name, params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("admission reply lost")
        return _reply(frozen.plan, "admitted")

    client.on_execute = respond
    with pytest.raises(TypedMutationRPCError, match="RPC failed"):
        owner.admit(OWNER)
    assert owner.get(OWNER).state == "admission_attempted"
    assert owner.admitted_request(OWNER, requests[0].expectation.operation_id) is None
    restarted = ProviderAdmissionOwnerJournal(_reload(world), _wire()[0])
    # A new authenticated client can replay only the same owner and payload.
    restarted.wire._rpc._client.on_execute = lambda name, params: _reply(frozen.plan, "admitted")
    assert restarted.admit(OWNER).status == "admitted"
    assert restarted.get(OWNER).state == "admitted"
    assert restarted.admitted_request(OWNER, requests[0].expectation.operation_id) is None
    ticket = restarted.bind_ticket(OWNER, requests[0].expectation.operation_id)
    assert ticket.protocol_epoch == EPOCH
    assert restarted.bind_ticket(OWNER, requests[0].expectation.operation_id) == ticket
    assert restarted.admitted_request(OWNER, requests[0].expectation.operation_id) == requests[0]
    assert restarted.journal.get_ticket(ticket).request == requests[0]
    assert attempts == 1


def test_full_reply_and_divergent_owner_never_dispatch(world):
    owner, client = _owner(world)
    requests = _requests()
    record = owner.prepare(OWNER, requests)
    client.on_execute = lambda name, params: _reply(record.plan, "full")
    assert owner.admit(OWNER).status == "full"
    assert owner.get(OWNER).state == "full"
    assert owner.admitted_request(OWNER, requests[0].expectation.operation_id) is None
    changed = prepare_mutation("mesh", EPOCH, requests[0].expectation.operation_id,
                               ACTOR, "doc_create", {"p_path": "accounts/alice.json",
                                                      "p_data": {"different": True}})
    with pytest.raises(AdmissionOwnerChanged, match="plan changed"):
        owner.prepare(OWNER, (changed, requests[1]))
    with pytest.raises(local_source.SourceChanged, match="operation_already_owned"):
        owner.prepare(str(uuid.uuid4()), requests)


def test_same_operation_cannot_enter_old_journal_or_rotate_epoch(world):
    owner, client = _owner(world)
    requests = _requests()
    record = owner.prepare(OWNER, requests)
    journal = world[-1]
    with pytest.raises(local_source.SourceChanged, match="admission_leg"):
        journal.prepare_operation(requests[0])
    client.on_execute = lambda name, params: _reply(record.plan, "admitted")
    owner.admit(OWNER)
    with pytest.raises(local_source.SourceChanged, match="admission_owner_unresolved_epoch"):
        journal._adopt_epoch(_epoch(1, NEXT))


def test_bad_provider_reply_does_not_commit_positive(world):
    owner, client = _owner(world)
    record = owner.prepare(OWNER, _requests())
    client.on_execute = lambda name, params: {**_reply(record.plan, "admitted"),
                                              "protocol_epoch": NEXT}
    with pytest.raises(Exception, match="response"):
        owner.admit(OWNER)
    assert owner.get(OWNER).state == "admission_attempted"


def test_tampered_replay_material_fails_targeted_lookup(world):
    root, _, _, _, _ = world
    owner, _ = _owner(world)
    owner.prepare(OWNER, _requests())
    with sqlite3.connect(root.path) as conn:
        conn.execute("UPDATE typed_admission_legs SET request=? WHERE ordinal=0", (b"{}",))
    with pytest.raises(local_source.SourceChanged, match="material_changed"):
        owner.get(OWNER)


def test_binding_transfers_exact_reservation_and_resume_is_only_driver_entry(world):
    root, _, _, _, journal = world
    owner, client = _owner(world)
    requests = _requests()
    record = owner.prepare(OWNER, requests, private_payload=b"private owner material")
    assert b"private owner material" not in repr(record).encode()
    client.on_execute = lambda name, params: _reply(record.plan, "admitted")
    owner.admit(OWNER)

    def budget():
        with sqlite3.connect(root.path) as conn:
            reserved, owner_bytes = reservation_capacity(conn)
            pending = conn.execute(
                "SELECT count(*) FROM typed_operations WHERE receipt IS NULL"
            ).fetchone()[0]
            used = _bytes_used(conn)
            return used + owner_bytes + (pending + reserved) * MAX_RECEIPT_BYTES

    before = budget()
    ticket = owner.bind_ticket(OWNER, requests[0].expectation.operation_id)
    assert budget() == before
    with pytest.raises(local_source.SourceChanged, match="admission_leg"):
        journal.prepare(requests[0])
    driver = JournaledMutations(journal, owner.wire._rpc)
    driver._advance = lambda saved: saved
    assert driver.resume(ticket.protocol_epoch, ticket.operation_id).request == requests[0]
    with pytest.raises(local_source.SourceChanged, match="admission_owner_unconsumed"):
        journal.release_ticket(ticket)
    assert owner.get(OWNER).linked == (True, False)


def test_bind_rolls_back_ticket_intent_and_link_as_one_cut(world, monkeypatch):
    root, _, _, _, journal = world
    owner, client = _owner(world)
    requests = _requests()
    record = owner.prepare(OWNER, requests)
    client.on_execute = lambda name, params: _reply(record.plan, "admitted")
    owner.admit(OWNER)
    original = root._retire_nodes

    def interrupted(*args, **kwargs):
        raise OSError("interrupted before root commit")

    monkeypatch.setattr(root, "_retire_nodes", interrupted)
    with pytest.raises(OSError, match="interrupted before root commit"):
        owner.bind_ticket(OWNER, requests[0].expectation.operation_id)
    monkeypatch.setattr(root, "_retire_nodes", original)
    with sqlite3.connect(root.path) as conn:
        assert conn.execute("SELECT count(*) FROM typed_operations").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM typed_operation_tickets").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (0,)
    assert owner.get(OWNER).linked == (False, False)
    ticket = owner.bind_ticket(OWNER, requests[0].expectation.operation_id)
    assert journal.get_ticket(ticket).request == requests[0]


def test_queued_user_state_action_remains_recoverable_after_format9_upgrade(tmp_path):
    import test_user_state_actions as state_cases

    coordinator, _, reopen = state_cases.world.__wrapped__(tmp_path)
    queue = reopen()
    action_id = queue.enqueue(state_cases.CHAT, state_cases.USER,
                              "star", {"msg_ids": ["kept"]})
    ProviderAdmissionOwnerJournal(queue.journal, _wire()[0])
    assert reopen().pending_actions(state_cases.USER)[0].action_id == action_id
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute("SELECT format FROM mutation_root").fetchone() == (9,)
