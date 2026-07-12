"""MessagingService via the Mesh facade — two identities on one shared root.

This is the R4 integration surface: post -> outbox flush -> transport ->
other identity syncs -> reads through the choke point. Membership gates on
EVERY operation (the v0.24.1 lesson).
"""

import pytest

from agentbridge.core.errors import NotAMember, PermissionDenied, ValidationError
from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport

CHAT = "room1"


@pytest.fixture
def world(tmp_path):
    """Shared folder root + meshes for ann, bob (members) and eve (not)."""
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    snap = ChatSnapshot(
        id=CHAT, kind=ChatKind.GROUP, name="Room",
        members={
            "ann": Member(role=Role.ADMIN, joined_ns=1),
            "bob": Member(role=Role.MEMBER, joined_ns=2),
        },
    )
    tx.put_doc(P.meta(CHAT), snap.to_dict())

    def mk(user):
        return Mesh(FolderTransport(root), user, "mach1", home=tmp_path / f"home-{user}")

    meshes = {u: mk(u) for u in ("ann", "bob", "eve")}
    yield meshes
    for m in meshes.values():
        m.close()


def flush_and_sync(sender: Mesh, *receivers: Mesh):
    sender.outbox.flush_once()
    for r in (sender, *receivers):
        r.sync.sync_once([CHAT])


def test_post_flows_to_other_member(world):
    ann, bob = world["ann"], world["bob"]
    env = ann.post(CHAT, "hello @bob")
    # optimistic: ann sees it instantly, before any flush
    assert [m.id for m in ann.messages_for(CHAT)] == [env.id]
    # bob sees nothing until the outbox flushes and he syncs
    assert bob.messages_for(CHAT) == []
    flush_and_sync(ann, bob)
    msgs = bob.messages_for(CHAT)
    assert len(msgs) == 1 and msgs[0].body == "hello @bob"
    assert msgs[0].tags == ["bob"]


def test_every_endpoint_membership_gated(world):
    eve = world["eve"]
    with pytest.raises(NotAMember):
        eve.messages_for(CHAT)
    with pytest.raises(NotAMember):
        eve.post(CHAT, "let me in")
    for call in (
        lambda: eve.edit(CHAT, "m1", "x"),
        lambda: eve.redact(CHAT, ["m1"]),
        lambda: eve.react(CHAT, "m1", "👍"),
        lambda: eve.pin(CHAT, "m1"),
        lambda: eve.unpin(CHAT, "m1"),
        lambda: eve.star(CHAT, ["m1"]),
        lambda: eve.hide(CHAT, ["m1"]),
        lambda: eve.clear_chat(CHAT),
        lambda: eve.mark_read(CHAT),
        lambda: eve.set_chat_flag(CHAT, "pinned", True),
        lambda: eve.pins(CHAT),
        lambda: eve.starred(CHAT),
    ):
        with pytest.raises(NotAMember):
            call()
    with pytest.raises(NotAMember):
        eve.post("no-such-chat", "hi")


def test_empty_post_rejected(world):
    with pytest.raises(ValidationError):
        world["ann"].post(CHAT, "   ")


def test_edit_rules(world):
    ann, bob = world["ann"], world["bob"]
    env = ann.post(CHAT, "orignal")
    flush_and_sync(ann, bob)

    with pytest.raises(PermissionDenied):
        bob.edit(CHAT, env.id, "hijack")
    with pytest.raises(ValidationError):
        ann.edit(CHAT, env.id, "  ")
    with pytest.raises(ValidationError):
        ann.edit(CHAT, "no-such-id", "x")

    ann.edit(CHAT, env.id, "original, fixed")
    for viewer in (ann, bob):
        m = viewer.messages_for(CHAT)[0]
        assert m.body == "original, fixed" and m.edited is not None

    # editing a deleted message is impossible
    ann.redact(CHAT, [env.id])
    with pytest.raises(ValidationError):
        ann.edit(CHAT, env.id, "zombie edit")


def test_redact_sender_only_and_tombstone(world):
    ann, bob = world["ann"], world["bob"]
    env = ann.post(CHAT, "delete me")
    flush_and_sync(ann, bob)

    with pytest.raises(PermissionDenied):
        bob.redact(CHAT, [env.id])
    ann.pin(CHAT, env.id)
    ann.redact(CHAT, [env.id])

    for viewer in (ann, bob):
        m = viewer.messages_for(CHAT)[0]
        assert m.deleted and m.body == ""
    assert ann.pins(CHAT) == {}  # pin purged with the redaction


def test_reactions_one_per_user(world):
    ann, bob = world["ann"], world["bob"]
    env = ann.post(CHAT, "react to me")
    flush_and_sync(ann, bob)

    bob.react(CHAT, env.id, "👍")
    ann.react(CHAT, env.id, "👍")
    bob.react(CHAT, env.id, "❤️")  # replaces bob's earlier one
    m = ann.messages_for(CHAT)[0]
    assert m.reactions == {"👍": ["ann"], "❤️": ["bob"]}
    bob.react(CHAT, env.id, None)  # remove
    assert ann.messages_for(CHAT)[0].reactions == {"👍": ["ann"]}


def test_hide_is_private(world):
    ann, bob = world["ann"], world["bob"]
    env = ann.post(CHAT, "visible")
    flush_and_sync(ann, bob)
    bob.hide(CHAT, [env.id])
    assert bob.messages_for(CHAT) == []          # hidden for bob
    assert len(ann.messages_for(CHAT)) == 1       # unaffected for ann
    bob.unhide(CHAT, [env.id])
    assert len(bob.messages_for(CHAT)) == 1


def test_clear_chat_keep_starred(world):
    ann, bob = world["ann"], world["bob"]
    kept = ann.post(CHAT, "star me")
    ann.post(CHAT, "clear me")
    flush_and_sync(ann, bob)
    bob.star(CHAT, [kept.id])
    bob.clear_chat(CHAT, keep_starred=True)
    assert [m.id for m in bob.messages_for(CHAT)] == [kept.id]
    assert len(ann.messages_for(CHAT)) == 2  # clear is for-me-only

    later = ann.post(CHAT, "after the clear")
    flush_and_sync(ann, bob)
    assert later.id in [m.id for m in bob.messages_for(CHAT)]


def test_starred_resolves_live_not_snapshot(world):
    ann, bob = world["ann"], world["bob"]
    env = ann.post(CHAT, "the secret number is 42")
    flush_and_sync(ann, bob)
    bob.star(CHAT, [env.id])
    ann.redact(CHAT, [env.id])
    starred = bob.starred(CHAT)
    assert len(starred) == 1 and starred[0].deleted and starred[0].body == ""


def test_mark_read_and_unread_derivation(world):
    ann, bob = world["ann"], world["bob"]
    first = ann.post(CHAT, "one")
    ann.post(CHAT, "two")
    flush_and_sync(ann, bob)

    assert bob.unread(CHAT)["unread"] == 2
    bob.mark_read(CHAT)
    assert bob.unread(CHAT)["unread"] == 0

    # edit-marks-unread: ann edits an already-read message
    ann.edit(CHAT, first.id, "one, corrected")
    info = bob.unread(CHAT)
    assert info["unread"] == 1 and info["first_unread_ns"] == first.ns

    bob.set_chat_flag(CHAT, "forced_unread", True)
    assert bob.unread(CHAT)["forced_unread"] is True
    bob.mark_read(CHAT)
    assert bob.unread(CHAT)["forced_unread"] is False


def test_outbox_retry_never_loses_the_post(world, monkeypatch):
    ann, bob = world["ann"], world["bob"]
    fails = {"left": 2}
    real_append = ann.tx.append_log

    def flaky(chat_id, log_name, record):
        if fails["left"] > 0:
            fails["left"] -= 1
            raise OSError("sync storm")
        real_append(chat_id, log_name, record)

    monkeypatch.setattr(ann.tx, "append_log", flaky)
    ann.outbox.base_delay = 0.001
    env = ann.post(CHAT, "survives failure")

    import time
    for _ in range(30):
        if ann.outbox.flush_once():
            break
        time.sleep(0.005)
    bob.sync.sync_once([CHAT])
    assert [m.id for m in bob.messages_for(CHAT)] == [env.id]
    assert ann.store.outbox_counts() == {}
