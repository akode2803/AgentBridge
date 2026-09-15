"""Adversarial contracts for signed account lifecycle authority."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agentbridge.harness.runtime.authority import responsible_authority
from agentbridge.mesh import lifecycle
from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.lifecycle import (
    LifecycleError, LifecycleUnavailable, _write, publish_change, resolve_lifecycle,
)
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store
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


def _append_state(mesh, *, active: bool) -> dict:
    """Append a valid remote successor without resolving/publishing it locally."""
    current = resolve_lifecycle(mesh.directory, "claude", store=mesh.store)
    ns = time.time_ns()
    record = {
        **current, "id": f"life-test-{ns}", "ns": ns, "actor": "aryan",
        "action": "state", "previous_id": current["id"], "active": active,
        "deactivated": "",
    }
    return _write(mesh.directory, mesh.keystore, record, subject_proof=False)


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


def test_conflicted_resolver_recollects_and_cannot_overwrite_newer_retained_head(
        world, monkeypatch):
    mesh, _, _ = world
    delayed = _append_state(mesh, active=False)
    retained_before = mesh.store.observe_lifecycle_head("claude")
    winner = {**delayed, "id": "life-retained-winner", "ns": delayed["ns"] + 1,
              "active": True}
    original_publish = mesh.store.publish_lifecycle_head
    calls = []
    entered, release = Event(), Event()

    def delayed_publish(expected, proposed):
        if expected.subject != "claude":
            return original_publish(expected, proposed)
        calls.append((expected, proposed))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original_publish(expected, proposed)

    monkeypatch.setattr(mesh.store, "publish_lifecycle_head", delayed_publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resolve_lifecycle, mesh.directory, "claude", store=mesh.store)
        assert entered.wait(5)
        competing = Store(mesh.store.path)
        try:
            current = competing.observe_lifecycle_head("claude")
            assert current == retained_before
            assert competing.publish_lifecycle_head(current, winner)
        finally:
            competing.close()
        release.set()
        resolved = future.result(timeout=5)
    assert len(calls) == 1
    assert calls[0][1]["id"] == delayed["id"]
    assert resolved == winner
    assert mesh.store.observe_lifecycle_head("claude").payload_json
    assert mesh.directory.get("claude").active is True


def test_persistent_lifecycle_conflict_propagates_without_raw_directory_fallback(
        world, monkeypatch):
    mesh, _, _ = world
    _append_state(mesh, active=False)
    raw = mesh.tx.get_doc(P.user("claude"))
    raw.update(active=True, deactivated="")
    raw["agent"].update(owner="fable", machine="raw-machine")
    mesh.tx.put_doc(P.user("claude"), raw)
    calls = 0

    def always_conflict(_expected, _proposed):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(mesh.store, "publish_lifecycle_head", always_conflict)
    with pytest.raises(LifecycleUnavailable, match="changed repeatedly"):
        resolve_lifecycle(mesh.directory, "claude", store=mesh.store)
    assert calls == 2
    with pytest.raises(LifecycleUnavailable, match="changed repeatedly"):
        mesh.directory.get("claude")


def test_authority_consumer_propagates_lifecycle_unavailable(world, monkeypatch):
    mesh, _, _ = world

    def unavailable(_subject):
        raise LifecycleUnavailable("injected retained-head failure")

    monkeypatch.setattr(mesh.store, "observe_lifecycle_head", unavailable)
    with pytest.raises(LifecycleUnavailable, match="injected retained-head failure"):
        responsible_authority(mesh, "claude")


def test_malformed_retained_head_and_nested_owner_failure_propagate(world):
    mesh, _, _ = world
    mesh.store.cache_doc("lifecycle/head/claude", {"not": "a record"})
    with pytest.raises(LifecycleUnavailable, match="retained lifecycle authority"):
        mesh.directory.get("claude")

    # Restore Claude's trusted head, then make the prospective transfer owner's
    # retained authority malformed.  Authorization must re-raise that nested
    # failure instead of skipping the transfer or reading Fable's raw account.
    claude = resolve_lifecycle(mesh.directory, "claude", store=None)
    position = mesh.store.observe_lifecycle_head("claude")
    assert mesh.store.publish_lifecycle_head(position, claude)
    ns = time.time_ns()
    transfer = {
        **claude, "id": f"life-transfer-{ns}", "ns": ns, "actor": "fable",
        "action": "transfer", "owner": "fable", "machine": "workstation",
        "previous_id": claude["id"],
    }
    _write(mesh.directory, mesh.keystore, transfer, subject_proof=True)
    mesh.store.cache_doc("lifecycle/head/fable", {"not": "a record"})
    with pytest.raises(LifecycleUnavailable, match="retained lifecycle authority"):
        resolve_lifecycle(mesh.directory, "claude", store=mesh.store)


def test_retained_future_record_survives_clock_rollback_but_remote_future_is_skipped(
        world, monkeypatch):
    mesh, _, _ = world
    current = resolve_lifecycle(mesh.directory, "claude", store=mesh.store)
    real_now = time.time_ns
    accepted_now = real_now() + 60 * 60 * 1_000_000_000
    future = {**current, "id": f"life-future-{accepted_now}", "ns": accepted_now,
              "actor": "aryan", "action": "state", "previous_id": current["id"],
              "active": False, "deactivated": ""}
    _write(mesh.directory, mesh.keystore, future, subject_proof=False)
    # B is admitted and retained while this machine's clock is ahead.
    monkeypatch.setattr(lifecycle.time, "time_ns", lambda: accepted_now)
    assert resolve_lifecycle(mesh.directory, "claude", store=mesh.store) == future
    # After a clock rollback, B is too far in the future as remote evidence,
    # but remains a previously accepted structural retained head.
    monkeypatch.setattr(lifecycle.time, "time_ns", real_now)
    assert resolve_lifecycle(mesh.directory, "claude", store=mesh.store) == future
