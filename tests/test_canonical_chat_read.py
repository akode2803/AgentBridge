"""R189 request-owned canonical read regression tests."""

from __future__ import annotations

import pytest

from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.core.timekit import next_ns
from agentbridge.gui.projection_perf import ProjectionObservation
from agentbridge.mesh import messaging
from agentbridge.mesh.overlays import UserState
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


CHAT = "room1"


@pytest.fixture
def world(tmp_path):
    """Three plaintext viewers on a disposable shared room."""
    root = tmp_path / "mesh"
    tx = FolderTransport(root)
    tx.put_doc(P.meta(CHAT), ChatSnapshot(
        id=CHAT,
        kind=ChatKind.GROUP,
        name="Room",
        members={
            "ann": Member(role=Role.ADMIN, joined_ns=1),
            "bob": Member(role=Role.MEMBER, joined_ns=2),
        },
    ).to_dict())
    meshes = {
        name: Mesh(FolderTransport(root), name, "m1", home=tmp_path / f"{name}-home")
        for name in ("ann", "bob", "eve")
    }
    try:
        yield meshes
    finally:
        for mesh in meshes.values():
            mesh.close()


def _flush(sender, *receivers):
    sender.outbox.flush_once()
    for receiver in (sender, *receivers):
        receiver.sync.sync_once([CHAT])


def test_projection_reuses_one_verified_overlay_cut_and_one_fold(world, monkeypatch):
    """A writer after the captured state cannot split transcript from summary."""
    ann, bob = world["ann"], world["bob"]
    envelope = ann.post(CHAT, "visible before the overlay change")
    _flush(ann, bob)

    original_get = UserState.get
    reads = 0
    folds = 0

    def changed_after_capture(state):
        nonlocal reads
        value = original_get(state)
        if state.chat_id == CHAT and state.user == "bob":
            reads += 1
            if reads == 1:
                # A separate summary/state read would now observe this cut.
                bob.tx.put_doc(P.state(CHAT, "bob"), {"deleted": envelope.ns})
        return value

    original_fold = messaging.build_messages

    def counted_fold(*args, **kwargs):
        nonlocal folds
        folds += 1
        return original_fold(*args, **kwargs)

    monkeypatch.setattr(UserState, "get", changed_after_capture)
    monkeypatch.setattr(messaging, "build_messages", counted_fold)
    observation = ProjectionObservation("chat")
    projection = bob.conversation_projection(CHAT, observer=observation)

    assert [message.id for message in projection.messages] == [envelope.id]
    assert projection.overview["deleted"] is False
    assert projection.viewer_state["read_ns"] == 0
    assert reads == folds == 1
    assert observation.counts["fold_calls"] == 1

    # The next request is allowed to observe the subsequent overlay version.
    next_projection = bob.conversation_projection(CHAT)
    assert next_projection.messages == ()
    assert next_projection.overview["deleted"] is True


def test_projection_matches_existing_view_and_deeply_detaches_public_state(world):
    ann, bob = world["ann"], world["bob"]
    first = ann.post(CHAT, "first")
    second = ann.post(CHAT, "second")
    _flush(ann, bob)
    bob.star(CHAT, [first.id])
    bob.mark_read(CHAT)
    bob.set_chat_flag(CHAT, "archived", True)
    bob.set_chat_flag(CHAT, "mute", True)

    projection = bob.conversation_projection(CHAT)
    assert list(projection.messages) == bob.messages_for(CHAT)
    assert projection.overview == bob.chat_overview(CHAT)
    assert projection.viewer_state == bob.my_state(CHAT)

    # Public nested state/overview copies cannot poison a later canonical read.
    projection.viewer_state["starred"].clear()
    assert projection.overview["last"] is not projection.messages[-1]
    projection.overview["last"].body = "corrupted public copy"
    assert projection.messages[-1].body == "second"
    again = bob.conversation_projection(CHAT)
    assert again.viewer_state["starred"] == [first.id]
    assert again.overview["last"].id == second.id
    assert again.overview["last"].body == "second"


def test_projection_rejects_breadcrumbs_and_membership_but_keeps_reaction_fold(world):
    ann, bob, eve = world["ann"], world["bob"], world["eve"]
    envelope = ann.post(CHAT, "ordinary")
    _flush(ann, bob)
    bob.react(CHAT, envelope.id, "👍")
    _flush(bob, ann)

    projection = ann.conversation_projection(CHAT)
    assert not any((message.event or {}).get("type") == "reaction"
                   for message in projection.messages)
    assert projection.messages[-1].reactions == {"👍": ["bob"]}

    try:
        ann.conversation_projection(CHAT, breadcrumbs=True)
    except TypeError:
        pass
    else:  # API deliberately has no harness-only breadcrumbs switch.
        raise AssertionError("conversation projection accepted breadcrumbs")

    try:
        eve.conversation_projection(CHAT)
    except Exception as exc:
        assert type(exc).__name__ == "NotAMember"
    else:
        raise AssertionError("nonmember received a canonical projection")


def test_overview_does_not_sanitize_unused_hidden_runtime_field(world):
    """Overview keeps its direct private-fold path for legacy malformed state."""
    ann, bob = world["ann"], world["bob"]
    ann.post(CHAT, "still previewable")
    _flush(ann, bob)
    bob.tx.put_doc(P.state(CHAT, "bob"), {"hidden_runtime": None})

    overview = bob.chat_overview(CHAT)
    assert overview["last"] is not None
    assert overview["deleted"] is False


def test_projection_uses_verified_state_not_forged_overlay(tmp_path):
    """The new public state view must not bypass UserState's signature gate."""
    root = tmp_path / "root"
    homes = {name: tmp_path / f"home-{name}" for name in ("aryan", "fable")}

    def open_mesh(name):
        return Mesh(FolderTransport(root), name, "m1", encrypt=True, home=homes[name])

    for name in homes:
        mesh = open_mesh(name)
        try:
            mesh.accounts.create_human(name, f"{name}-password")
        finally:
            mesh.close()

    aryan, fable = open_mesh("aryan"), open_mesh("fable")
    try:
        chat = aryan.create_chat("Signed state", members=["fable"])
        envelope = aryan.post(chat.id, "must remain visible")
        aryan.outbox.flush_once()
        fable.sync.sync_once([chat.id])
        fable.tx.put_doc(P.state(chat.id, "fable"), {
            "cleared": {"ns": next_ns()}, "hidden": [envelope.id],
            "read_ns": envelope.ns, "starred": [envelope.id], "mute": True,
        })

        projection = fable.conversation_projection(chat.id)
        assert envelope.id in [message.id for message in projection.messages]
        assert projection.viewer_state == {
            "starred": [], "read_ns": 0, "hidden_runtime": [],
            "archived": False, "pinned": False, "forced_unread": False,
            "mute": False,
        }
        assert projection.overview["deleted"] is False
    finally:
        aryan.close()
        fable.close()
