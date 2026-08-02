"""Adversarial coverage for signed runtime stop, timer, and pause controls."""

from __future__ import annotations

import json

import pytest

from agentbridge.harness.runtime.controls import (
    consume_owner_command,
    owner_command_path,
    owner_commands,
    pause_prefix,
    publish_owner_command,
    publish_pause,
    read_pause,
)
from agentbridge.mesh.service import Mesh


@pytest.fixture
def controls(tmp_path):
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home,
                 store_path=home / "helper-controls.sqlite")
    yield owner, agent
    agent.close()
    owner.close()


def test_owner_command_is_pairwise_encrypted_bound_and_one_use(controls):
    owner, agent = controls
    record = publish_owner_command(
        owner, target="helper", action="timer_cancel", chat_id="chat-secret",
        timer_id="timer-secret", timer_at_ns=123, timeout_s=60,
    )
    raw = owner.tx.get_doc(owner_command_path("helper", record["id"]))
    encoded = json.dumps(raw, sort_keys=True)
    assert "chat-secret" not in encoded and "timer-secret" not in encoded

    opened = owner_commands(agent, target="helper")
    assert len(opened) == 1 and opened[0]["timer_id"] == "timer-secret"
    assert consume_owner_command(
        agent, target="helper", action="timer_cancel",
    )["id"] == record["id"]
    assert consume_owner_command(
        agent, target="helper", action="timer_cancel",
    ) is None


def test_forged_legacy_and_tampered_owner_controls_are_inert(controls):
    owner, agent = controls
    owner.tx.put_doc("status/helper_stop.json", {
        "ns": 2**62, "by": "aryan", "chat_id": "",
    })
    assert consume_owner_command(agent, target="helper", action="stop") is None

    record = publish_owner_command(
        owner, target="helper", action="stop", timeout_s=60,
    )
    path = owner_command_path("helper", record["id"])
    raw = owner.tx.get_doc(path)
    raw["header"]["action"] = "timer_cancel"
    owner.tx.put_doc(path, raw)
    assert owner_commands(agent, target="helper") == []


def test_owner_command_invalidates_when_policy_changes(controls):
    owner, agent = controls
    publish_owner_command(owner, target="helper", action="stop", timeout_s=60)
    owner.directory.patch(
        "helper",
        lambda doc: doc.setdefault("agent", {}).setdefault("harness", {}).update(
            catchup="none"
        ),
    )
    assert owner_commands(agent, target="helper") == []


def test_expired_owner_command_is_inert(controls, monkeypatch):
    owner, agent = controls
    record = publish_owner_command(
        owner, target="helper", action="stop", timeout_s=1,
    )
    import agentbridge.harness.runtime.controls as controls_module

    monkeypatch.setattr(
        controls_module.time, "time_ns", lambda: record["expires_ns"] + 1,
    )
    assert owner_commands(agent, target="helper") == []


def test_stop_chat_binding_does_not_consume_other_room(controls):
    owner, agent = controls
    record = publish_owner_command(
        owner, target="helper", action="stop", chat_id="c1", timeout_s=60,
    )
    assert consume_owner_command(
        agent, target="helper", action="stop", chat_id="c2",
    ) is None
    assert consume_owner_command(
        agent, target="helper", action="stop", chat_id="c1",
    )["id"] == record["id"]


def test_signed_global_pause_resume_and_legacy_inert(controls):
    owner, _ = controls
    owner.tx.put_doc("control.json", {"paused": True, "by": "mallory"})
    assert read_pause(owner.directory, owner.tx) is False
    publish_pause(owner, paused=True)
    assert read_pause(owner.directory, owner.tx) is True
    publish_pause(owner, paused=False)
    assert read_pause(owner.directory, owner.tx) is False


def test_inactive_pause_actor_has_no_authority(controls):
    owner, _ = controls
    publish_pause(owner, paused=True)
    owner.directory.patch("aryan", lambda doc: doc.update(active=False))
    assert read_pause(owner.directory, owner.tx) is False


def test_tampered_pause_and_wrong_path_are_inert(controls):
    owner, _ = controls
    record = publish_pause(owner, paused=True)
    path = f"{pause_prefix()}/{record['id']}.json"
    doc = owner.tx.get_doc(path)
    doc["record"]["paused"] = False
    owner.tx.put_doc(path, doc)
    assert read_pause(owner.directory, owner.tx) is False

    valid = publish_pause(owner, paused=True)
    valid_path = f"{pause_prefix()}/{valid['id']}.json"
    owner.tx.put_doc(f"{pause_prefix()}/wrong.json", owner.tx.get_doc(valid_path))
    owner.tx.delete_doc(valid_path)
    assert read_pause(owner.directory, owner.tx) is False


def test_room_pause_requires_current_member(controls):
    owner, _ = controls
    bob = Mesh(owner.tx.root, "bob", "otherbox", encrypt=True, home=owner.home,
               store_path=owner.home / "bob.sqlite")
    try:
        bob.accounts.create_human("bob", "bob-password")
        snap = owner.create_chat("Ops", members=["helper", "bob"])
        bob.sync.sync_once([snap.id])
        publish_pause(bob, paused=True, chat_id=snap.id)
        assert read_pause(
            owner.directory, owner.tx, chat_id=snap.id,
            snapshot=owner.snapshot(snap.id),
        ) is True

        owner.remove_member(snap.id, "bob")
        assert read_pause(
            owner.directory, owner.tx, chat_id=snap.id,
            snapshot=owner.snapshot(snap.id),
        ) is False
    finally:
        bob.close()


def test_pause_transport_failure_is_not_mistaken_for_resume(controls, monkeypatch):
    owner, _ = controls
    publish_pause(owner, paused=True)

    def unavailable(_prefix):
        raise OSError("offline")

    monkeypatch.setattr(owner.tx, "list_docs", unavailable)
    with pytest.raises(OSError, match="offline"):
        read_pause(owner.directory, owner.tx)
