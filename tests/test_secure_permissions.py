from __future__ import annotations

import json

import pytest

from agentbridge.harness.runtime.envelope import EnvelopeError
from agentbridge.harness.runtime.permissions import (
    PermissionLane,
    PermissionRecordError,
    answer,
    ask_path,
    list_owner_asks,
    open_ask,
)
from agentbridge.mesh.service import Mesh


@pytest.fixture()
def secure_meshes(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "box", encrypt=True, home=home,
                 store_path=tmp_path / "agent.sqlite")
    chat = owner.create_chat("Secure", members=["helper"])
    other = owner.create_chat("Other", members=["helper"])
    owner.outbox.flush_once()
    agent.sync.sync_once([chat.id, other.id])
    try:
        yield owner, agent, chat.id, other.id
    finally:
        agent.close()
        owner.close()


def publish(agent, chat_id, *, timeout_s=30.0):
    return PermissionLane(agent, "helper").publish_ask(
        chat_id=chat_id, kind="permission", tool="Write",
        detail="/Users/owner/secret.txt", input_digest="a" * 64,
        timeout_s=timeout_s, run_id="r-live", call_id="call-7",
    )


def test_pairwise_encrypted_roundtrip_and_durable_one_use(secure_meshes):
    owner, agent, chat_id, _ = secure_meshes
    lane = PermissionLane(agent, "helper")
    ask = publish(agent, chat_id)

    raw = agent.tx.get_doc(ask_path(chat_id, "helper", ask.id))
    encoded = json.dumps(raw, sort_keys=True)
    assert "/Users/owner/secret.txt" not in encoded
    assert "Write" not in encoded

    visible = list_owner_asks(owner, chat_id=chat_id)
    assert [(a["id"], a["run_id"], a["call_id"]) for a in visible] == [
        (ask.id, "r-live", "call-7")
    ]
    answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
           verdict="allow")
    answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
           verdict="allow")  # two owner windows, same decision: idempotent
    assert lane.read_decision(ask)["verdict"] == "allow"
    assert lane.read_decision(ask)["verdict"] == "deny"

    # Consumption survives a fresh Mesh/Store connection to the same DB.
    reopened = Mesh(agent.tx, "helper", "box", encrypt=True, home=agent.home,
                    store_path=agent.store.path)
    try:
        assert PermissionLane(reopened, "helper").read_decision(ask)["verdict"] == "deny"
    finally:
        reopened.close()


def test_tamper_cross_room_and_wrong_audience_fail_closed(secure_meshes):
    owner, agent, chat_id, other_id = secure_meshes
    ask = publish(agent, chat_id)
    path = ask_path(chat_id, "helper", ask.id)
    raw = agent.tx.get_doc(path)

    tampered = dict(raw)
    tampered["ct"] = ("A" if raw["ct"][:1] != "A" else "B") + raw["ct"][1:]
    agent.tx.put_doc(path, tampered)
    assert list_owner_asks(owner, chat_id=chat_id) == []
    with pytest.raises((EnvelopeError, PermissionRecordError)):
        open_ask(owner, chat_id=chat_id, agent="helper", ask_id=ask.id)

    agent.tx.put_doc(path, raw)
    agent.tx.put_doc(ask_path(other_id, "helper", ask.id), raw)
    with pytest.raises((EnvelopeError, PermissionRecordError)):
        open_ask(owner, chat_id=other_id, agent="helper", ask_id=ask.id)


def test_conflicting_valid_owner_decisions_deny(secure_meshes):
    owner, agent, chat_id, _ = secure_meshes
    lane = PermissionLane(agent, "helper")
    ask = publish(agent, chat_id)
    answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
           verdict="allow")
    answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
           verdict="deny", text="changed my mind")
    decision = lane.read_decision(ask)
    assert decision == {
        "verdict": "deny", "text": "conflicting owner decisions; denied"
    }


def test_membership_and_policy_epochs_invalidate_stale_asks(secure_meshes):
    owner, agent, chat_id, _ = secure_meshes
    stale_policy = publish(agent, chat_id)
    owner.set_agent_harness("helper", {"aux": {"read": False, "web": False}})
    with pytest.raises(PermissionRecordError, match="policy_revision"):
        answer(owner, chat_id=chat_id, agent="helper", ask_id=stale_policy.id,
               verdict="allow")

    current = publish(agent, chat_id)
    owner.remove_member(chat_id, "helper")
    owner.outbox.flush_once()
    with pytest.raises((PermissionRecordError, PermissionError)):
        answer(owner, chat_id=chat_id, agent="helper", ask_id=current.id,
               verdict="allow")


def test_always_uses_verified_ask_binding_not_frontend_fields(secure_meshes):
    owner, agent, chat_id, _ = secure_meshes
    lane = PermissionLane(agent, "helper")
    ask = publish(agent, chat_id)
    answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
           verdict="always")
    decision = lane.read_decision(ask)
    assert decision["verdict"] == "always"
    approvals = owner.directory.get("helper").agent.harness["approvals"]
    assert approvals == [{"tool": "Write", "chat": chat_id}]


def test_outside_scope_never_becomes_standing_grant(secure_meshes):
    owner, agent, chat_id, _ = secure_meshes
    ask = PermissionLane(agent, "helper").publish_ask(
        chat_id=chat_id, kind="permission", tool="Read",
        detail="/Downloads/private", input_digest="b" * 64,
        timeout_s=30, run_id="r", call_id="c", scope="outside",
    )
    with pytest.raises(PermissionRecordError, match="only be allowed once"):
        answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
               verdict="always")
    assert owner.directory.get("helper").agent.harness.get("approvals") in (None, [])
