"""V63 / R65: the storage janitor — verified-redaction blob reclamation
(grace + undo + forgery safe) and terminal-chat purges, over the real
Supabase transport with E2EE on.
"""

from __future__ import annotations

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.mesh.janitor import Janitor
from agentbridge.mesh.overlays import ChatOverlays
from agentbridge.mesh.service import Mesh

from conftest import install_key, seed_account


@pytest.fixture
def world(clouds, tmp_path):
    root = clouds.root(tmp_path / "mesh2")
    tx_seed = clouds.bare(root)
    bundles = {n: seed_account(tx_seed, n) for n in ("aryan", "fable")}

    def mk(user):
        home = tmp_path / f"home-{user}"
        install_key(home, user, bundles[user])

        return Mesh(clouds.bare(root), user, "mach1", home=home,
                    encrypt=True)

    meshes = {u: mk(u) for u in ("aryan", "fable")}
    yield meshes
    for m in meshes.values():
        m.close()


def _post_with_blob(mesh, chat_id, name="doc.txt", body=b"blob bytes"):
    blob_id = f"f-{name}"
    sealed = mesh.sealer.seal_blob(chat_id, blob_id, body)
    mesh.tx.put_blob(f"chats/{chat_id}/files/{blob_id}", sealed)
    env = mesh.post(chat_id, f"sharing {name}", files=[{
        "id": blob_id, "name": name, "bytes": len(body)}])
    mesh.outbox.flush_once()
    # the janitor reads the message envelope from the LOCAL STORE (populated by
    # sync); ingest it now so the sweep is deterministic — in production a
    # grace-eligible (>=7d old) message is always long since synced (fixes a
    # pre-existing flake where the author's own post hadn't been cached yet)
    mesh.sync.sync_once([chat_id])
    return env, f"chats/{chat_id}/files/{blob_id}"


def test_reclaims_verified_redactions_only(world):
    aryan = world["aryan"]
    chat = aryan.create_chat("Sweep", members=["fable"])
    doomed, doomed_path = _post_with_blob(aryan, chat.id, "old.txt")
    kept, kept_path = _post_with_blob(aryan, chat.id, "keep.txt")

    aryan.redact(chat.id, [doomed.id])       # delete for everyone (signed)
    aryan.outbox.flush_once()

    # inside the grace window: nothing moves
    out = Janitor(aryan).sweep(grace_days=7)
    assert out == {"chats": 0, "blobs": 0, "bytes": 0}
    assert aryan.tx.blob_size(doomed_path) is not None

    # past the grace: exactly the redacted blob goes. Same grace-0 boundary
    # as the chat-purge test below — the redaction's ns can sit a coarse
    # clock tick AHEAD of the sweep's horizon on py3.12/Windows, so poll the
    # idempotent sweep until the tick rolls (never a sleep).
    import time as _time
    deadline = _time.time() + 2
    out = Janitor(aryan).sweep(grace_days=0)
    while out["blobs"] == 0 and _time.time() < deadline:
        out = Janitor(aryan).sweep(grace_days=0)
    assert out["blobs"] == 1 and out["bytes"] > 0 and out["chats"] == 0
    assert aryan.tx.blob_size(doomed_path) is None
    assert aryan.tx.blob_size(kept_path) is not None
    # the log + tombstone are untouched — history still folds
    msgs = aryan.messages_for(chat.id)
    assert any(m.id == doomed.id and m.deleted for m in msgs)
    assert any(m.id == kept.id and not m.deleted for m in msgs)
    # idempotent
    assert Janitor(aryan).sweep(grace_days=0)["blobs"] == 0


def test_undo_and_forgery_reclaim_nothing(world):
    aryan = world["aryan"]
    chat = aryan.create_chat("Safe", members=["fable"])
    undone, undone_path = _post_with_blob(aryan, chat.id, "undo.txt")
    target, target_path = _post_with_blob(aryan, chat.id, "forged.txt")

    # a validly voided redaction (R44 Undo) must keep its attachment
    aryan.redact(chat.id, [undone.id])
    aryan.unredact(chat.id, undone.id)
    aryan.outbox.flush_once()

    # a FORGED redaction dropped on the transport (unsigned) reclaims nothing
    ChatOverlays(aryan.tx, chat.id).put_redaction(target.id, by="fable")

    out = Janitor(aryan).sweep(grace_days=0)
    assert out["blobs"] == 0
    assert aryan.tx.blob_size(undone_path) is not None
    assert aryan.tx.blob_size(target_path) is not None


def test_deleted_group_purges_after_grace(world):
    aryan = world["aryan"]
    live = aryan.create_chat("Alive", members=["fable"])
    dead = aryan.create_chat("Doomed", members=["fable"])
    _post_with_blob(aryan, dead.id, "gone.txt")
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()

    # inside grace: still there
    assert Janitor(aryan).sweep(grace_days=7)["chats"] == 0
    assert dead.id in aryan.tx.list_chat_ids()

    # grace-0 boundary: on py3.12/Windows time.time_ns() ticks ~15.6ms and
    # the monotonic ns guard can stamp the deletion a hair AHEAD of the wall
    # clock, so a same-tick sweep correctly says "not older than the grace
    # yet". Poll the idempotent sweep until the tick rolls — never a sleep.
    import time as _time
    deadline = _time.time() + 2
    out = Janitor(aryan).sweep(grace_days=0)
    while out["chats"] == 0 and _time.time() < deadline:
        out = Janitor(aryan).sweep(grace_days=0)
    assert out["chats"] == 1
    assert dead.id not in aryan.tx.list_chat_ids()
    assert live.id in aryan.tx.list_chat_ids()
    # fable's janitor can also verify + purge (idempotent across members)
    assert Janitor(world["fable"]).sweep(grace_days=0)["chats"] == 0


def test_deleted_group_reclaims_every_object_of_its_own_folders_only(world):
    aryan = world["aryan"]
    dead = aryan.create_chat("Folder cleanup", members=["fable"])
    live = aryan.create_chat("Neighbour", members=["fable"])
    _post_with_blob(aryan, dead.id, "owned.txt")
    # Objects no message body names (nobody could decrypt one, or none
    # references it), past Storage's 100-name page, the group photo and a
    # staged attachment all belong to the dead chat's folders.
    for n in range(105):
        aryan.tx.put_blob(f"chats/{dead.id}/files/f-orphan-{n:03d}", b"x")
    aryan.tx.put_blob(f"chats/{dead.id}/avatar.jpg", b"photo")
    aryan.tx.put_blob(f"blob-stage/{dead.id}/f2-staged", b"sealed")
    # A body cannot choose a path, and a sibling folder sharing the id as a
    # prefix, or another chat, is never listed.
    aryan.post(dead.id, "bad path", files=[{
        "id": "../not-owned", "name": "bad.txt", "bytes": 1,
    }])
    aryan.outbox.flush_once()
    kept = [f"chats/{dead.id}x/files/f-sibling", f"chats/{live.id}/files/f-live",
            f"blob-stage/{dead.id}x/f2-sibling", "chats/not-owned"]
    for path in kept:
        aryan.tx.put_blob(path, b"keep")

    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    import time as _time
    deadline = _time.time() + 2
    out = Janitor(aryan).sweep(grace_days=0)
    while out["chats"] == 0 and _time.time() < deadline:
        out = Janitor(aryan).sweep(grace_days=0)
    assert out["chats"] == 1
    assert aryan.tx.list_blobs(f"chats/{dead.id}") == []
    assert aryan.tx.list_blobs(f"blob-stage/{dead.id}") == []
    assert all(aryan.tx.blob_size(path) == 4 for path in kept)
    assert dead.id not in aryan.tx.list_chat_ids()


def test_folder_reclaim_does_not_depend_on_storage_reporting_subfolders(
        world, monkeypatch):
    import fake_cloud

    aryan = world["aryan"]
    dead = aryan.create_chat("No folder rows", members=["fable"])
    _, path = _post_with_blob(aryan, dead.id, "deep.txt")
    avatar, staged = f"chats/{dead.id}/avatar.jpg", f"blob-stage/{dead.id}/f2-staged"
    nested = f"chats/{dead.id}/x/y"  # not attachment-shaped: a member uploaded it
    for extra in (avatar, staged, nested):
        aryan.tx.put_blob(extra, b"z")
    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    janitor = Janitor(aryan)
    assert janitor._chat_objects(dead.id) == sorted([path, avatar, staged, nested])
    # Were Storage to stop reporting sub-folders, the known folders are still
    # listed by name: the files are not missed and the room not purged early.
    real_list = fake_cloud.FakeBucketApi.list
    monkeypatch.setattr(fake_cloud.FakeBucketApi, "list", lambda self, parent, options=None: [
        row for row in real_list(self, parent, options) if row["id"] is not None])
    assert janitor._chat_objects(dead.id) == sorted([path, avatar, staged])


def test_a_transport_that_cannot_list_never_purges(world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Unlistable", members=["fable"])
    _, path = _post_with_blob(aryan, dead.id, "kept.txt")
    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()

    def cannot_list(_prefix):
        raise NotImplementedError("this transport cannot list blobs")

    monkeypatch.setattr(aryan.tx, "list_blobs", cannot_list)
    purged = []
    monkeypatch.setattr(aryan.tx, "delete_chat", purged.append)
    assert Janitor(aryan).sweep(grace_days=0)["chats"] == 0 and purged == []
    assert Janitor(aryan).reclaim_deleted_chat_attachments(dead.id) == 0
    assert aryan.tx.blob_size(path) is not None


def test_typed_sweep_first_settles_purges_whose_reply_was_lost(world, monkeypatch):
    from agentbridge.transport.base import Transport

    aryan = world["aryan"]
    janitor, settled = Janitor(aryan), []

    def settle():
        settled.append(True)
        raise OSError("provider unavailable")  # never stops the sweep

    monkeypatch.setattr(aryan.tx, "settle_chat_purges", settle, raising=False)
    janitor.sweep(grace_days=0)
    assert settled == []  # unbound: nothing to settle
    monkeypatch.setattr(janitor, "_typed", lambda: True)
    assert janitor.sweep(grace_days=0) == {"chats": 0, "blobs": 0, "bytes": 0}
    assert settled == [True]
    # A transport without listing says so instead of answering "empty".
    with pytest.raises(NotImplementedError):
        Transport.list_blobs(aryan.tx, "chats/x")


def test_deleted_group_is_purged_only_once_its_folders_are_empty(world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Stubborn", members=["fable"])
    _post_with_blob(aryan, dead.id, "stuck.txt")
    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    # Storage answers the delete but keeps the object (e.g. a new upload
    # raced in): the purge would end every read and orphan it, so it waits.
    monkeypatch.setattr(aryan.tx, "delete_blobs", lambda _paths: None)
    purged = []
    monkeypatch.setattr(aryan.tx, "delete_chat", purged.append)
    assert Janitor(aryan).sweep(grace_days=0)["chats"] == 0 and purged == []


def test_typed_janitor_waits_for_a_terminal_provider_meta(world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Lagging meta", members=["fable"])
    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    janitor = Janitor(aryan)
    monkeypatch.setattr(janitor, "_typed", lambda: True)
    purged = []
    monkeypatch.setattr(aryan.tx, "delete_chat", purged.append)
    meta_path, real_get = f"chats/{dead.id}/meta.json", aryan.tx.get_doc
    provider = {"meta": dict(real_get(meta_path), deleted=False, members={"aryan": {}})}
    monkeypatch.setattr(aryan.tx, "get_doc", lambda path, default=None: (
        provider["meta"] if path == meta_path else real_get(path, default)))
    assert janitor._deletion(dead.id)  # the signed fold says deleted
    assert janitor.sweep(grace_days=0)["chats"] == 0 and purged == []
    provider["meta"] = dict(provider["meta"], deleted=True, members={})
    import time as _time
    deadline = _time.time() + 2
    while not purged and _time.time() < deadline:
        janitor.sweep(grace_days=0)
    assert purged == [dead.id]


def test_deleted_group_keeps_evidence_when_a_blob_delete_fails(
        world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Retry cleanup", members=["fable"])
    _, first_path = _post_with_blob(aryan, dead.id, "a-first.txt")
    _, second_path = _post_with_blob(aryan, dead.id, "z-second.txt")
    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    original_delete = aryan.tx.delete_blobs
    failed = False

    def fail_once(paths):
        nonlocal failed
        if not failed:
            assert sorted(paths) == sorted([first_path, second_path])  # one batch
            failed = True
            raise OSError("storage unavailable")
        original_delete(paths)

    monkeypatch.setattr(aryan.tx, "delete_blobs", fail_once)
    import time as _time
    deadline = _time.time() + 2
    while not failed and _time.time() < deadline:
        assert Janitor(aryan).sweep(grace_days=0)["chats"] == 0
    assert failed
    assert aryan.tx.blob_size(second_path) is not None
    assert dead.id in aryan.tx.list_chat_ids()

    deadline = _time.time() + 2
    out = Janitor(aryan).sweep(grace_days=0)
    while out["chats"] == 0 and _time.time() < deadline:
        out = Janitor(aryan).sweep(grace_days=0)
    assert out["chats"] == 1
    assert aryan.tx.blob_size(first_path) is None
    assert aryan.tx.blob_size(second_path) is None
    assert dead.id not in aryan.tx.list_chat_ids()


def test_terminal_reclaim_needs_no_readable_message_body(world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Unreadable cleanup", members=["fable"])
    _, path = _post_with_blob(aryan, dead.id, "sealed.txt")
    terminal = aryan.messaging.build_event(
        dead.id, {"type": "chat_deleted", "by": "aryan"})
    aryan.tx.append_log(dead.id, "aryan@mach1.jsonl", terminal.to_dict())
    monkeypatch.setattr(aryan.sealer, "unseal", lambda *_args: None)
    assert Janitor(aryan).reclaim_deleted_chat_attachments(dead.id) == 1
    assert aryan.tx.blob_size(path) is None


def test_terminal_reclaim_treats_unreadable_terminal_event_as_transient(
        world, monkeypatch):
    aryan = world["aryan"]
    chat = aryan.create_chat("Terminal read lag", members=["fable"])
    monkeypatch.setattr(aryan.tx, "list_logs", lambda _chat_id: [])

    with pytest.raises(TransportError, match="not yet readable"):
        Janitor(aryan).reclaim_deleted_chat_attachments(chat.id)
