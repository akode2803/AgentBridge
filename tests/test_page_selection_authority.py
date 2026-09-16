"""Encrypted authority parity between bounded selection and the full fold."""
from __future__ import annotations

from copy import deepcopy

import pytest

from agentbridge.core.models import BodyRecord
from agentbridge.core.timekit import next_ns
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.overlays import ChatOverlays
from agentbridge.mesh.page_selection import select_message_page
from agentbridge.mesh.paths import P
from agentbridge.mesh.readmodel import build_messages, transcript_visible
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def encrypted_world(tmp_path):
    root = tmp_path / "mesh"

    def opened(user, home=None):
        return Mesh(
            FolderTransport(root), user, "m1", encrypt=True,
            home=home or tmp_path / f"home-{user}",
        )

    for user in ("aryan", "fable"):
        mesh = opened(user)
        mesh.accounts.create_human(user, f"{user}-pass")
        mesh.close()
    aryan, fable = opened("aryan"), opened("fable")
    aryan.accounts.create_agent("helper")
    helper = opened("helper", aryan.home)
    chat = aryan.create_chat("Authority", members=["fable", "helper"])

    def ripple(sender, *receivers):
        sender.outbox.flush_once()
        for mesh in (sender, *receivers):
            mesh.sync.sync_once([chat.id])

    ripple(aryan, fable, helper)
    agent_message = helper.post(chat.id, "agent original")
    human_message = aryan.post(chat.id, "human original")
    ripple(helper, aryan, fable)
    ripple(aryan, helper, fable)
    fable.store.prepare_page_input_index()
    source = fable.store.publish_document_batch(
        fable.store.capture_document_position("authority-source"), {},
        cursor=1, full=True, retain_tombstones=False,
    )
    index = fable.store.publish_overlay_index(prepare_overlay_index(
        fable.store.capture_document_observation(source.source_id), chat.id,
    ))
    try:
        yield {
            "aryan": aryan, "fable": fable, "helper": helper, "chat": chat,
            "agent_message": agent_message, "human_message": human_message,
            "index": index, "ripple": ripple,
        }
    finally:
        helper.close()
        fable.close()
        aryan.close()


def _fold_inputs(mesh, chat_id, **overrides):
    snapshot = mesh._require_member(chat_id)
    overlays = ChatOverlays(mesh.tx, chat_id)
    values = {
        "edits": overlays.edits(),
        "redactions": overlays.redactions(),
        "reactions": mesh._verified_reactions(chat_id, snapshot, overlays),
        "state": deepcopy(mesh._state(chat_id).get()),
        "history_from_ns": 0,
        "tenure": snapshot.tenure,
        "verify_redaction": mesh._redaction_verifier(chat_id),
        "owner_of": mesh.directory.owner_of,
    }
    values.update(overrides)
    return values


def _assert_page_matches_full(world, **overrides):
    viewer, chat, index = world["fable"], world["chat"], world["index"]
    records = viewer.store.messages(chat.id)
    inputs = viewer.store.capture_page_inputs(index, raw_limit=len(records))
    fold = _fold_inputs(viewer, chat.id, **overrides)
    expected = [
        message for message in build_messages(
            chat.id, viewer.user, records, viewer.sealer, **fold,
        )
        if transcript_visible(message, viewer.user)
    ]
    selected = select_message_page(
        inputs, viewer.user, viewer.sealer, limit=max(1, len(records)), **fold,
    )
    assert selected.messages == tuple(expected)
    return list(selected.messages)


def _message(messages, ident):
    return next(message for message in messages if message.id == ident)


def test_authorized_forged_owner_and_void_overlays_match_full_fold(encrypted_world):
    world = encrypted_world
    aryan, fable, helper = world["aryan"], world["fable"], world["helper"]
    chat, agent = world["chat"], world["agent_message"]

    assert _message(_assert_page_matches_full(world), agent.id).body == "agent original"

    helper.edit(chat.id, agent.id, "author edit")
    assert _message(_assert_page_matches_full(world), agent.id).body == "author edit"

    # A correctly encrypted non-owner edit is rejected by actor authority.
    bad_ns = next_ns()
    bad_seal = fable.sealer.seal(
        chat.id, agent.id, bad_ns, BodyRecord(body="non-owner edit"),
    )
    ChatOverlays(fable.tx, chat.id).put_edit(
        agent.id, bad_seal, by="fable", ns=bad_ns,
    )
    assert _message(_assert_page_matches_full(world), agent.id).body == "agent original"

    # Claiming the owner cannot make ciphertext sealed by another actor open.
    ChatOverlays(fable.tx, chat.id).put_edit(
        agent.id, bad_seal, by="aryan", ns=bad_ns,
    )
    assert _message(_assert_page_matches_full(world), agent.id).body == "agent original"

    aryan.edit(chat.id, agent.id, "owner edit")
    assert _message(_assert_page_matches_full(world), agent.id).body == "owner edit"

    aryan.redact(chat.id, [agent.id])
    assert _message(_assert_page_matches_full(world), agent.id).deleted is True
    valid_redaction = aryan.tx.get_doc(P.redaction(chat.id, agent.id))

    forged = dict(valid_redaction)
    forged["sig"] = "invalid"
    fable.tx.put_doc(P.redaction(chat.id, agent.id), forged)
    assert _message(_assert_page_matches_full(world), agent.id).deleted is False

    forged_void = dict(valid_redaction)
    forged_void["void"] = {"by": "aryan", "ns": next_ns(), "sig": "invalid"}
    fable.tx.put_doc(P.redaction(chat.id, agent.id), forged_void)
    assert _message(_assert_page_matches_full(world), agent.id).deleted is True

    fable.tx.put_doc(P.redaction(chat.id, agent.id), valid_redaction)
    aryan.unredact(chat.id, agent.id)
    restored = _message(_assert_page_matches_full(world), agent.id)
    assert not restored.deleted and restored.body == "owner edit"


def test_history_tenure_and_unseal_failures_match_full_fold(encrypted_world):
    world = encrypted_world
    fable, chat = world["fable"], world["chat"]
    agent, human = world["agent_message"], world["human_message"]

    records = fable.store.messages(chat.id)
    original = next(record for record in records if record["id"] == human.id)
    unreadable = dict(original)
    unreadable.update({"id": "bad-ciphertext", "ns": human.ns + 1, "ct": "invalid"})
    fable.store.upsert_messages(chat.id, [unreadable])
    messages = _assert_page_matches_full(world)
    failed = _message(messages, "bad-ciphertext")
    assert failed.undecrypted and failed.body == ""

    history = _assert_page_matches_full(world, history_from_ns=human.ns)
    assert agent.id not in {message.id for message in history}
    assert human.id in {message.id for message in history}

    tenure = deepcopy(fable._require_member(chat.id).tenure)
    tenure["helper"] = [[1, agent.ns]]
    bounded = _assert_page_matches_full(world, tenure=tenure)
    assert agent.id not in {message.id for message in bounded}


def test_authority_callback_exception_propagates_without_partial_selection(
        encrypted_world):
    world = encrypted_world
    aryan, fable = world["aryan"], world["fable"]
    chat, agent = world["chat"], world["agent_message"]
    aryan.redact(chat.id, [agent.id])
    records = fable.store.messages(chat.id)
    inputs = fable.store.capture_page_inputs(world["index"], raw_limit=len(records))
    calls = []

    def tripwire(*args):
        calls.append(args)
        raise RuntimeError("authority unavailable")

    with pytest.raises(RuntimeError, match="authority unavailable"):
        select_message_page(
            inputs, fable.user, fable.sealer, limit=len(records),
            **_fold_inputs(fable, chat.id, verify_redaction=tripwire),
        )
    assert calls and calls[0][0] == agent.id
