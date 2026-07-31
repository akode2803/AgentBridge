"""MessagingService via the Mesh facade — two identities on one shared root.

This is the R4 integration surface: post -> outbox flush -> transport ->
other identity syncs -> reads through the choke point. Membership gates on
EVERY operation (the v0.24.1 lesson).
"""

import time

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


def test_attachment_manifest_is_durable_before_remote_upload(world):
    ann, bob = world["ann"], world["bob"]
    prepared = ann.prepare_attachment(CHAT, "report.txt", b"durable report")
    blob_id = prepared.record["id"]
    spool = ann.attachments.root / blob_id
    env = ann.post(CHAT, "attached", attachments=[prepared])

    assert spool.is_file()
    assert ann.tx.get_blob(P.file(CHAT, blob_id)) is None
    assert ann.open_attachment(CHAT, blob_id) == b"durable report"
    payload = ann.store.outbox_payloads()[0]
    assert payload["envelope"]["id"] == env.id
    assert payload["attachments"][0]["blob_id"] == blob_id

    flush_and_sync(ann, bob)
    assert ann.tx.get_blob(P.file(CHAT, blob_id)) is not None
    assert not spool.exists()
    assert bob.messages_for(CHAT)[0].files[0]["id"] == blob_id


def test_attachment_spool_is_cancelled_when_local_commit_fails(world, monkeypatch):
    ann = world["ann"]
    prepared = ann.prepare_attachment(CHAT, "rollback.txt", b"not committed")
    spool = ann.attachments.root / prepared.record["id"]
    monkeypatch.setattr(
        ann.store, "cache_and_outbox_add",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        ann.post(CHAT, "rollback", attachments=[prepared])

    assert not spool.exists()
    assert ann.store.outbox_counts() == {}
    assert ann.messages_for(CHAT) == []


def test_attachment_retry_reuses_manifest_after_upload_then_append_failure(
        world, monkeypatch):
    ann, bob = world["ann"], world["bob"]
    prepared = ann.prepare_attachment(CHAT, "retry.bin", b"same bytes")
    blob_id = prepared.record["id"]
    ann.post(CHAT, "retry me", attachments=[prepared])
    original_put = ann.tx.put_blob
    original_append = ann.tx.append_log
    writes = []

    def track_put(path, data):
        writes.append(path)
        original_put(path, data)

    def fail_append(*_args, **_kwargs):
        raise OSError("append unavailable")

    monkeypatch.setattr(ann.tx, "put_blob", track_put)
    monkeypatch.setattr(ann.tx, "append_log", fail_append)
    ann.outbox.base_delay = ann.outbox.max_delay = 0.001
    assert ann.outbox.flush_once() == 0
    assert ann.store.outbox_counts() == {"pending": 1}
    assert ann.tx.get_blob(P.file(CHAT, blob_id)) is not None
    assert (ann.attachments.root / blob_id).is_file()

    monkeypatch.setattr(ann.tx, "append_log", original_append)
    deadline = time.monotonic() + 2.0
    delivered = 0
    while not delivered and time.monotonic() < deadline:
        delivered = ann.outbox.flush_once()
        if not delivered:
            time.sleep(0.002)
    assert delivered == 1
    bob.sync.sync_once([CHAT])
    assert writes == [P.file(CHAT, blob_id), P.file(CHAT, blob_id)]
    assert [m.body for m in bob.messages_for(CHAT)] == ["retry me"]
    assert not (ann.attachments.root / blob_id).exists()


def test_crash_after_append_retries_same_ids_and_projects_once(world, monkeypatch):
    ann, bob = world["ann"], world["bob"]
    prepared = ann.prepare_attachment(CHAT, "acked.txt", b"ack boundary")
    blob_id = prepared.record["id"]
    env = ann.post(CHAT, "ack crash", attachments=[prepared])
    original_done = ann.store.outbox_done
    monkeypatch.setattr(
        ann.store, "outbox_done",
        lambda _seq: (_ for _ in ()).throw(OSError("crash before ack")))
    with pytest.raises(OSError, match="crash before ack"):
        ann.outbox.flush_once()
    assert ann.tx.get_blob(P.file(CHAT, blob_id)) is not None
    assert (ann.attachments.root / blob_id).is_file()

    monkeypatch.setattr(ann.store, "outbox_done", original_done)
    ann.store._conn().execute("UPDATE outbox SET lease_ns=0")
    assert ann.outbox.flush_once() == 1
    bob.sync.sync_once([CHAT])
    assert [m.id for m in bob.messages_for(CHAT)] == [env.id]
    records, _ = ann.tx.read_log(CHAT, "ann@mach1", 0)
    assert [r["id"] for r in records] == [env.id, env.id]
    assert not (ann.attachments.root / blob_id).exists()


def test_pending_message_cannot_recreate_a_deleted_chat(world):
    ann = world["ann"]
    prepared = ann.prepare_attachment(CHAT, "doomed.txt", b"doomed")
    blob_id = prepared.record["id"]
    ann.post(CHAT, "must not return", attachments=[prepared])
    ann.tx.delete_chat(CHAT)

    assert ann.outbox.flush_once() == 0
    assert CHAT not in ann.tx.list_chat_ids()
    assert ann.tx.get_blob(P.file(CHAT, blob_id)) is None
    assert ann.store.outbox_counts() == {"dead": 1}
    assert (ann.attachments.root / blob_id).is_file()

    ann.store._conn().execute("UPDATE outbox SET created_ns=0 WHERE state='dead'")
    ann.outbox.flush_once()
    assert ann.store.outbox_counts() == {}
    assert not (ann.attachments.root / blob_id).exists()


def test_terminal_event_stops_retrying_after_chat_is_physically_reclaimed(world):
    ann = world["ann"]
    terminal = ann.build_event(CHAT, {"type": "chat_deleted", "by": "ann"})
    ann.commit_envelope(CHAT, terminal)
    ann.tx.delete_chat(CHAT)

    assert ann.outbox.flush_once() == 0
    assert ann.store.outbox_counts() == {"dead": 1}
    assert CHAT not in ann.tx.list_chat_ids()


def test_already_applied_terminal_event_acknowledges_without_reappend(
        world, monkeypatch):
    ann = world["ann"]
    terminal = ann.build_event(CHAT, {"type": "chat_deleted", "by": "ann"})
    ann.commit_envelope(CHAT, terminal)
    meta = ann.tx.get_doc(P.meta(CHAT))
    meta["deleted"] = True
    meta["members"] = {}
    ann.tx.put_doc(P.meta(CHAT), meta)
    monkeypatch.setattr(
        ann.tx, "append_log",
        lambda *_args, **_kwargs: pytest.fail("terminal event was re-appended"))

    assert ann.outbox.flush_once() == 1
    assert ann.store.outbox_counts() == {}


def test_terminal_followup_does_not_bypass_a_live_send_restriction(world):
    bob = world["bob"]
    blocked = bob.post(CHAT, "queued before restriction")
    meta = bob.tx.get_doc(P.meta(CHAT))
    meta.setdefault("permissions", {})["send_messages"] = "admins"
    bob.tx.put_doc(P.meta(CHAT), meta)
    leave = bob.build_event(CHAT, {"type": "member_left"})
    bob.commit_envelope(CHAT, leave)

    assert bob.outbox.flush_once() == 1
    assert bob.store.outbox_counts() == {"dead": 1}
    records, _ = bob.tx.read_log(CHAT, "bob@mach1", 0)
    assert [record["id"] for record in records] == [leave.id]
    assert blocked.id not in {record["id"] for record in records}


def test_nonmember_cannot_prepare_target_attachment(world):
    eve = world["eve"]
    with pytest.raises(NotAMember):
        eve.prepare_attachment(CHAT, "leak.txt", b"never sealed")
    assert list(eve.attachments.root.iterdir()) == []


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


def test_delete_chat_for_me_hides_then_reappears(world):
    """WhatsApp 'Delete chat' (Q25): the transcript empties for me only, the
    sidebar row hides, a NEW message brings the chat back (new messages only),
    and undo restores everything."""
    ann, bob = world["ann"], world["bob"]
    ann.post(CHAT, "before the delete")
    flush_and_sync(ann, bob)

    bob.delete_chat_for_me(CHAT)
    assert bob.messages_for(CHAT) == []                       # empty for me
    assert bob.chat_overview(CHAT)["deleted"] is True         # row hides
    assert len(ann.messages_for(CHAT)) == 1                   # per-user only
    assert ann.chat_overview(CHAT)["deleted"] is False

    later = ann.post(CHAT, "after the delete")
    flush_and_sync(ann, bob)
    # the chat reappears with ONLY the new message
    assert [m.id for m in bob.messages_for(CHAT)] == [later.id]
    assert bob.chat_overview(CHAT)["deleted"] is False

    bob.set_chat_flag(CHAT, "deleted", False)                 # undo
    assert len(bob.messages_for(CHAT)) == 2                   # everything back


def test_delete_chat_for_me_is_membership_gated(world):
    with pytest.raises(NotAMember):
        world["eve"].delete_chat_for_me(CHAT)


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
