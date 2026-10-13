"""Recovery-driven agent identity creation through one admitted owner."""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import pytest

import test_operation_journal as base
import test_provider_admission_closure as closure
import test_provider_admission_owner as cases
import test_provider_admission_settlement as settlement
from agentbridge import crypto
from agentbridge.core.errors import ValidationError
from agentbridge.core.jsonkit import canonical_json_bytes
from agentbridge.mesh import identity_publication as identity
from agentbridge.mesh import lifecycle
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.identity_dispatch import (
    CANONICAL_PENDING, CREATED, FULL, NAME_TAKEN, AgentIdentityDispatcher,
)
from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.pins import key_fingerprint
from agentbridge.store import local_source
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = base.world


class _Docs:
    """The provider's document table, as the directory reads it."""

    def __init__(self):
        self.docs = {}

    def get_doc(self, path):
        value = self.docs.get(path)
        return None if value is None else json.loads(json.dumps(value))

    def list_docs(self, prefix):
        return sorted(path for path in self.docs if path.startswith(prefix + "/"))


class _Pins:
    def __init__(self):
        self.pinned, self.verified = {}, set()

    def pin(self, name, sign_pub, agree_pub):
        self.pinned.setdefault(name, (sign_pub, agree_pub))

    def fingerprint(self, name, sign_pub="", agree_pub=""):
        return key_fingerprint(name, *self.pinned.get(name, (sign_pub, agree_pub)))

    def mark_verified(self, name):
        self.verified.add(name)

    def trusted(self, name, sign_pub, agree_pub, history=None):
        return self.pinned.get(name, (sign_pub, agree_pub))


class _Provider(closure._Provider):
    """Conditional creates land in the shared document table."""

    def __init__(self, client, docs):
        super().__init__(client)
        self.docs, self.full, self.dropped, self.lose_ops = docs, False, set(), set()

    def _handle(self, name, params):
        op = params.get("p_operation_id")
        if name == "ab_admit_mutation_owner" and self.full:
            return cases._reply(self.plans[params["p_owner_id"]], "full")
        request = self.requests.get(op)
        if (request is not None and request.expectation.kind == "doc_compare_put"
                and name == request.rpc_name and op not in self.receipts and op not in self.cancelled
                and not (self.rotated and params["p_protocol_epoch"] == base.EPOCH)):
            args = json.loads(request.arguments_json)
            if args["p_path"] in self.docs.docs:
                self.conflicts.add(op)
            elif args["p_path"] not in self.dropped:
                self.docs.docs[args["p_path"]] = args["p_data"]
        reply = super()._handle(name, params)
        if op in self.lose_ops and name == getattr(request, "rpc_name", None):
            self.lose_ops.discard(op)
            raise TimeoutError("reply lost after provider commit")
        return reply


@pytest.fixture
def identity_world(world, tmp_path):
    owners, client = cases._owner(world)
    docs = _Docs()
    provider = _Provider(client, docs)
    owner_bundle = crypto.generate_identity()
    owner_sign, owner_agree = crypto.identity_pubs(owner_bundle)
    docs.docs["users/alice.json"] = {
        "name": "alice", "kind": "human", "display": "Alice", "active": True,
        "created": "2026-10-13T00:00:00Z",
        "keys": {"sign_pub": owner_sign, "agree_pub": owner_agree}}
    artifact = identity.prepare_agent_identity(
        owner_id=str(uuid4()), root="mesh", actor_id=base.ACTOR, name="worker",
        owner="alice", machine="laptop", owner_bundle=owner_bundle,
        owner_sign_pub=owner_sign)
    record = identity.freeze_agent_identity(owners, artifact, protocol_epoch=base.EPOCH,
                                            source_epoch=str(uuid4()))
    provider.owner(record)
    pins = _Pins()
    directory = Directory(docs, pins=pins)
    keystore = KeyStore(tmp_path / "identity-home")
    dispatcher = AgentIdentityDispatcher(
        owners, settlement._driver(owners), keystore, directory)
    return dispatcher, provider, artifact, record, owner_bundle


def _rpc(request):
    return request.rpc_name, request.expectation.operation_id


def test_creation_publishes_account_then_bootstrap_and_reaps_owner(identity_world):
    dispatcher, provider, artifact, record, _ = identity_world
    outcome = dispatcher.run(artifact.owner_id)
    assert (outcome.state, outcome.name) == (CREATED, "worker")
    account, bootstrap = (path for path, _ in artifact.documents())
    assert provider.docs.docs[account] == artifact.account
    assert provider.docs.docs[bootstrap] == artifact.bootstrap
    executed = [call for call in provider.calls if call[0] == record.requests[0].rpc_name]
    assert executed == [_rpc(record.requests[0]), _rpc(record.requests[1])]
    assert dispatcher.keystore.load("worker") == artifact.bundle
    assert dispatcher.directory.pins.verified == {"worker"}
    assert dispatcher.owners.get(artifact.owner_id) is None
    assert provider.closable(record.plan)
    assert lifecycle.canonical_bootstrap_id(dispatcher.directory, "worker") == \
        artifact.bootstrap["record"]["id"]
    with pytest.raises(KeyError):
        dispatcher.run(artifact.owner_id)


@pytest.mark.parametrize("lost", [0, 1])
def test_lost_execution_reply_resumes_the_exact_leg(identity_world, lost):
    dispatcher, provider, artifact, record, _ = identity_world
    provider.lose_ops.add(record.requests[lost].expectation.operation_id)
    with pytest.raises(TypedMutationRPCError):
        dispatcher.run(artifact.owner_id)
    assert dispatcher.owners.get(artifact.owner_id).linked[lost]
    assert dispatcher.run(artifact.owner_id).state == CREATED
    assert len(provider.receipts) == 2
    assert dispatcher.owners.get(artifact.owner_id) is None


def test_taken_name_cancels_bootstrap_and_never_pins_or_publishes(identity_world):
    dispatcher, provider, artifact, record, _ = identity_world
    other = {"name": "worker", "kind": "human", "active": True}
    provider.docs.docs["users/worker.json"] = other
    outcome = dispatcher.run(artifact.owner_id)
    assert outcome.state == NAME_TAKEN
    assert provider.docs.docs["users/worker.json"] == other
    assert not provider.docs.list_docs("lifecycle/worker")
    assert record.requests[1].expectation.operation_id in provider.cancel_acked
    assert _rpc(record.requests[1]) not in provider.calls
    assert dispatcher.directory.pins.pinned == {}
    # The never-published key is retained, never deleted.
    assert dispatcher.keystore.load("worker") == artifact.bundle
    assert dispatcher.owners.get(artifact.owner_id) is None
    assert provider.closable(record.plan)


def test_different_local_key_fails_before_any_provider_call(identity_world):
    dispatcher, provider, artifact, _, _ = identity_world
    dispatcher.keystore.install_if_absent_or_identical("worker", crypto.generate_identity())
    with pytest.raises(Exception):
        dispatcher.run(artifact.owner_id)
    assert provider.calls == []
    assert dispatcher.owners.get(artifact.owner_id).state == "prepared"
    assert dispatcher.owners.abandon(artifact.owner_id) is True
    assert dispatcher.owners.get(artifact.owner_id) is None


def test_full_admission_keeps_draft_until_rotation_then_creates(identity_world):
    dispatcher, provider, artifact, record, _ = identity_world
    provider.full = True
    assert dispatcher.run(artifact.owner_id).state == FULL
    assert dispatcher.owners.get(artifact.owner_id).state == "full"
    assert dispatcher.keystore.load("worker") == artifact.bundle
    provider.full = False
    provider.rotate()  # a full owner holds no provider legs
    dispatcher.owners.journal._adopt_epoch(closure.CLOSED)
    rebased = dispatcher.owners.get(artifact.owner_id)
    assert rebased.plan.protocol_epoch == cases.NEXT
    provider.owner(rebased)
    assert dispatcher.run(artifact.owner_id).state == CREATED


def test_unverified_canonical_lifecycle_keeps_owner_and_keys(identity_world):
    dispatcher, provider, artifact, record, _ = identity_world
    bootstrap_path = artifact.documents()[1][0]
    provider.dropped.add(bootstrap_path)
    assert dispatcher.run(artifact.owner_id).state == CANONICAL_PENDING
    current = dispatcher.owners.get(artifact.owner_id)
    assert current.outcome == ("applied", "applied")
    assert current.private_payload == artifact.private_payload
    provider.docs.docs[bootstrap_path] = artifact.bootstrap
    assert dispatcher.run(artifact.owner_id).state == CREATED
    assert dispatcher.owners.get(artifact.owner_id) is None


def test_conflicting_local_pin_stops_before_bootstrap(identity_world):
    dispatcher, provider, artifact, record, _ = identity_world
    stale = crypto.identity_pubs(crypto.generate_identity())
    dispatcher.directory.pins.pinned["worker"] = stale
    with pytest.raises(ValidationError, match="conflicting local identity keys"):
        dispatcher.run(artifact.owner_id)
    assert _rpc(record.requests[1]) not in provider.calls
    assert dispatcher.owners.get(artifact.owner_id).outcome == ("applied", None)
    assert dispatcher.keystore.load("worker") == artifact.bundle


def test_canonical_bootstrap_survives_later_accepted_change(identity_world):
    dispatcher, provider, artifact, record, owner_bundle = identity_world
    dispatcher.run(artifact.owner_id)
    first = artifact.bootstrap["record"]
    ns = first["ns"] + 1
    change = {**first, "id": f"life-{ns}-0000abcd", "ns": ns, "action": "state",
              "active": False, "previous_id": first["id"]}
    signed = canonical_json_bytes(change)
    provider.docs.docs[f"lifecycle/worker/{change['id']}.json"] = {
        "record": change, "sig": crypto.sign(owner_bundle, signed), "subject_sig": ""}
    assert lifecycle.resolve_lifecycle(dispatcher.directory, "worker")["id"] == change["id"]
    assert lifecycle.canonical_bootstrap_id(dispatcher.directory, "worker") == first["id"]
    # A head (e.g. retained locally) whose ancestor is unreadable fails closed.
    del provider.docs.docs[artifact.documents()[1][0]]
    original = lifecycle.resolve_lifecycle
    try:
        lifecycle.resolve_lifecycle = lambda *a, **k: change
        with pytest.raises(lifecycle.LifecycleUnavailable):
            lifecycle.canonical_bootstrap_id(dispatcher.directory, "worker")
    finally:
        lifecycle.resolve_lifecycle = original
    # Without its bootstrap no chain is valid, so nothing descends from ours.
    assert lifecycle.canonical_bootstrap_id(dispatcher.directory, "worker") is None


def test_reap_requires_the_exact_retained_private_material(identity_world):
    dispatcher, provider, artifact, record, _ = identity_world
    dispatcher.directory.pins.pinned["worker"] = crypto.identity_pubs(
        crypto.generate_identity())
    with pytest.raises(ValidationError):
        dispatcher.run(artifact.owner_id)
    owners = dispatcher.owners
    with pytest.raises(local_source.SourceChanged, match="owner_unsettled"):
        owners.reap(artifact.owner_id, private_payload=artifact.private_payload)
    with pytest.raises(local_source.SourceChanged, match="owner_not_abandonable"):
        owners.abandon(artifact.owner_id)
    with sqlite3.connect(owners.journal.coordinator.path) as conn:
        assert conn.execute("SELECT count(*) FROM typed_admission_owners").fetchone() == (1,)


def test_owner_is_kept_when_the_keystore_no_longer_holds_the_exact_key(identity_world):
    from agentbridge.mesh.identity_dispatch import IdentityDispatchError

    dispatcher, provider, artifact, record, _ = identity_world
    dispatcher.keystore.load = lambda name, **kwargs: None
    with pytest.raises(IdentityDispatchError, match="no longer retained"):
        dispatcher.run(artifact.owner_id)
    kept = dispatcher.owners.get(artifact.owner_id)
    assert kept.private_payload == artifact.private_payload
    assert kept.outcome == ("applied", "applied")
