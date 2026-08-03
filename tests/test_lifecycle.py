"""Adversarial contracts for signed account lifecycle authority."""

from __future__ import annotations

import pytest

from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.lifecycle import LifecycleError, publish_change, resolve_lifecycle
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh"
    aryan = Mesh(FolderTransport(root), "aryan", "workstation",
                 home=tmp_path / "home-aryan")
    aryan.accounts.create_human("aryan", "aryan-pass")
    aryan.accounts.create_human("fable", "fable-pass")
    fable_bundle = aryan.keystore.load("fable")
    aryan.accounts.create_agent("claude")
    yield aryan, root, fable_bundle
    aryan.close()


def _paths(mesh, subject):
    return mesh.tx.list_docs(f"lifecycle/{subject}")


def test_unsigned_directory_rewrite_cannot_move_or_stop_agent(world):
    mesh, _, _ = world
    raw = mesh.tx.get_doc(P.user("claude"))
    raw["active"] = False
    raw["deactivated"] = "forged"
    raw["agent"].update(owner="fable", machine="attacker-box")
    mesh.tx.put_doc(P.user("claude"), raw)

    acc = mesh.directory.get("claude")
    assert acc.active is True and acc.deactivated == ""
    assert acc.agent.owner == "aryan" and acc.agent.machine == "workstation"


def test_tampered_or_misrouted_lifecycle_record_is_ignored(world):
    mesh, _, _ = world
    bootstrap_path = _paths(mesh, "claude")[0]
    signed = mesh.tx.get_doc(bootstrap_path)
    forged = {**signed, "record": {**signed["record"], "owner": "fable"}}
    mesh.tx.put_doc("lifecycle/claude/forged.json", forged)
    mesh.tx.put_doc("lifecycle/fable/copied.json", signed)

    state = resolve_lifecycle(mesh.directory, "claude", store=mesh.store)
    assert state["owner"] == "aryan" and state["action"] == "bootstrap"


def test_signed_state_survives_transport_rollback_via_local_head(world):
    mesh, _, _ = world
    mesh.accounts.set_machine_agents_active(False)
    latest = resolve_lifecycle(mesh.directory, "claude", store=mesh.store)
    assert latest["active"] is False

    mesh.tx.delete_doc(f"lifecycle/claude/{latest['id']}.json")
    raw = mesh.tx.get_doc(P.user("claude"))
    raw["active"] = True
    mesh.tx.put_doc(P.user("claude"), raw)

    assert mesh.directory.get("claude").active is False
    assert resolve_lifecycle(mesh.directory, "claude", store=mesh.store)["id"] == latest["id"]
    with pytest.raises(LifecycleError, match="verified head is unavailable"):
        publish_change(
            mesh.directory, mesh.keystore, "claude", actor="aryan",
            action="state", active=True,
        )


def test_host_and_transfer_require_agent_private_key(world, tmp_path):
    mesh, root, fable_bundle = world
    home = tmp_path / "home-fable"
    KeyStore(home).save("fable", fable_bundle)
    fable = Mesh(FolderTransport(root), "fable", "workstation", home=home)
    try:
        with pytest.raises(LifecycleError, match="identity key for @claude"):
            publish_change(
                fable.directory, fable.keystore, "claude", actor="fable",
                action="transfer", owner="fable", machine="workstation",
            )
        fable.keystore.save("claude", mesh.keystore.load("claude"))
        changed = publish_change(
            fable.directory, fable.keystore, "claude", actor="fable",
            action="transfer", owner="fable", machine="workstation",
        )
        assert changed["owner"] == "fable"
        assert fable.directory.get("claude").agent.owner == "fable"
    finally:
        fable.close()


def test_deactivation_is_terminal(world):
    mesh, _, _ = world
    mesh.accounts.delete_agent("claude")
    assert mesh.directory.get("claude").active is False
    with pytest.raises(LifecycleError, match="terminal"):
        publish_change(
            mesh.directory, mesh.keystore, "claude", actor="aryan",
            action="state", active=True,
        )


def test_lifecycle_fold_is_transport_order_independent(world, monkeypatch):
    mesh, _, _ = world
    mesh.accounts.set_machine_agents_active(False)
    expected = resolve_lifecycle(mesh.directory, "claude", store=mesh.store)
    original = mesh.tx.list_docs

    def reversed_docs(prefix):
        return list(reversed(original(prefix)))

    monkeypatch.setattr(mesh.tx, "list_docs", reversed_docs)
    assert resolve_lifecycle(mesh.directory, "claude", store=mesh.store)["id"] == expected["id"]
