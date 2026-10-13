"""Bound account patches, exact lifecycle records and keyed agent adoption."""

from __future__ import annotations

import pytest

import test_identity_dispatch as dispatch
from agentbridge.core.errors import ValidationError
from agentbridge.mesh import lifecycle
from agentbridge.mesh.directory import Directory
from agentbridge.transport.local_mutations import LocalMutationTransport
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = dispatch.world
identity_world = dispatch.identity_world
accounts = dispatch.accounts


@pytest.fixture
def bound(accounts, monkeypatch):
    service, provider, successes = accounts
    monkeypatch.setattr(LocalMutationTransport, "list_docs",
                        lambda self, prefix: provider.docs.list_docs(prefix))
    # As in Mesh, the directory reads and writes through the bound transport.
    service.directory = Directory(service.tx, pins=service.directory.pins)
    service.create_agent("helper")
    return service, provider, service.tx._bound_writer().driver.journal


def _calls(provider, name):
    return [op for rpc, op in provider.calls if rpc == name]


def test_keyed_adoption_moves_host_through_exact_record_and_swap(bound):
    service, provider, journal = bound
    service.machine = "new-box"
    adopted = service.adopt_agent("helper")
    assert adopted.agent.machine == "new-box"
    assert provider.docs.docs["users/helper.json"]["agent"]["machine"] == "new-box"
    head = lifecycle.resolve_lifecycle(service.directory, "helper")
    assert (head["action"], head["machine"], head["actor"]) == ("host", "new-box", "alice")
    assert lifecycle.canonical_bootstrap_id(service.directory, "helper") == head["previous_id"]
    assert journal.pending_keys() == ()


def test_profile_setter_swaps_the_exact_account_document(bound):
    service, provider, journal = bound
    assert service.set_display("Helper Two", agent="helper").display == "Helper Two"
    assert provider.docs.docs["users/helper.json"]["display"] == "Helper Two"
    assert provider.docs.docs["users/helper.json"]["agent"]["owner"] == "alice"
    assert journal.pending_keys() == ()


def test_lost_record_reply_settles_before_the_next_change(bound):
    service, provider, journal = bound
    service.machine = "new-box"
    provider.lose.add(dispatch.CREATE_RPC)
    with pytest.raises(TypedMutationRPCError):
        service.adopt_agent("helper")
    (pending,) = journal.pending_keys()
    lost = pending[1]
    assert lost in _calls(provider, dispatch.CREATE_RPC)
    # The retry first replays the exact earlier record, then extends it.
    service.adopt_agent("helper")
    assert _calls(provider, dispatch.CREATE_RPC).count(lost) == 2
    assert journal.pending_keys() == ()
    head = lifecycle.resolve_lifecycle(service.directory, "helper")
    previous = provider.docs.get_doc(f"lifecycle/helper/{head['previous_id']}.json")
    assert previous["record"]["action"] == "host"
    assert provider.docs.docs["users/helper.json"]["agent"]["machine"] == "new-box"


def test_keyless_adoption_still_requires_a_durable_identity_owner(bound):
    service, provider, _ = bound
    provider.docs.docs["users/ghost.json"] = {
        "name": "ghost", "kind": "agent", "display": "Ghost", "active": True,
        "created": "2026-10-13T00:00:00Z", "keys": {},
        "agent": {"owner": "alice", "machine": "migrated", "harness": {}}}
    before = len(provider.calls)
    with pytest.raises(ValidationError, match="durable identity publication owner"):
        service.adopt_agent("ghost")
    assert not [rpc for rpc, _ in provider.calls[before:]
                if rpc in (dispatch.CREATE_RPC, dispatch.COMPARE_RPC)]
    assert service.keystore.load("ghost") is None
