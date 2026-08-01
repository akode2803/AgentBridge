"""V63 / R65: the storage janitor — verified-redaction blob reclamation
(grace + undo + forgery safe) and terminal-chat purges, over the real
folder transport with E2EE on.
"""

from __future__ import annotations

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.mesh.janitor import Janitor
from agentbridge.mesh.overlays import ChatOverlays
from agentbridge.mesh.service import Mesh

from conftest import install_key, seed_account


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx_seed = __import__("agentbridge.transport.folder",
                         fromlist=["FolderTransport"]).FolderTransport(root)
    bundles = {n: seed_account(tx_seed, n) for n in ("aryan", "fable")}

    def mk(user):
        home = tmp_path / f"home-{user}"
        install_key(home, user, bundles[user])
        from agentbridge.transport.folder import FolderTransport
        return Mesh(FolderTransport(root), user, "mach1", home=home,
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


def test_deleted_group_derives_only_exact_verified_attachment_paths(
        world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Exact cleanup", members=["fable"])
    valid, valid_path = _post_with_blob(aryan, dead.id, "owned.txt")

    # A signed body with a malformed id is still not allowed to choose a path.
    aryan.post(dead.id, "bad path", files=[{
        "id": "../not-owned", "name": "bad.txt", "bytes": 1,
    }])
    aryan.outbox.flush_once()

    # A transport-forged copy cannot become ownership evidence: changing the
    # AAD-bound id invalidates the original sender's signature.
    forged = valid.to_dict()
    forged["id"] = "m-forged-attachment-owner"
    forged["ns"] += 1
    aryan.tx.append_log(dead.id, "attacker@box", forged)

    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    deleted_paths = []
    deleted_chats = []
    monkeypatch.setattr(aryan.tx, "delete_blob", deleted_paths.append)
    monkeypatch.setattr(aryan.tx, "delete_chat", deleted_chats.append)

    assert Janitor(aryan).sweep(grace_days=0)["chats"] == 1
    assert deleted_paths == [valid_path]
    assert deleted_chats == [dead.id]


def test_deleted_group_keeps_evidence_when_exact_blob_delete_fails(
        world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Retry cleanup", members=["fable"])
    _, first_path = _post_with_blob(aryan, dead.id, "a-first.txt")
    _, second_path = _post_with_blob(aryan, dead.id, "z-second.txt")
    aryan.messaging.set_terminal_reclaimer(lambda _chat_id: None)
    aryan.membership.delete_chat(dead.id)
    aryan.outbox.flush_once()
    original_delete = aryan.tx.delete_blob
    failed = False

    def fail_second_once(path):
        nonlocal failed
        if path == second_path and not failed:
            failed = True
            raise OSError("storage unavailable")
        original_delete(path)

    monkeypatch.setattr(aryan.tx, "delete_blob", fail_second_once)
    import time as _time
    deadline = _time.time() + 2
    while not failed and _time.time() < deadline:
        assert Janitor(aryan).sweep(grace_days=0)["chats"] == 0
    assert failed
    assert aryan.tx.blob_size(first_path) is None
    assert aryan.tx.blob_size(second_path) is not None
    assert dead.id in aryan.tx.list_chat_ids()

    deadline = _time.time() + 2
    out = Janitor(aryan).sweep(grace_days=0)
    while out["chats"] == 0 and _time.time() < deadline:
        out = Janitor(aryan).sweep(grace_days=0)
    assert out["chats"] == 1
    assert aryan.tx.blob_size(second_path) is None
    assert dead.id not in aryan.tx.list_chat_ids()


def test_owned_paths_reject_out_of_tenure_and_malformed_envelopes(
        world, monkeypatch):
    aryan = world["aryan"]
    janitor = Janitor(aryan)
    opened = []

    def unseal(_chat_id, env):
        opened.append(env.id)
        return type("Body", (), {"files": [{"id": "f-owned.txt"}]})()

    monkeypatch.setattr(aryan.sealer, "unseal", unseal)
    records = [
        {"id": "m-late", "from": "fable", "ns": 20, "kind": "message"},
        {"id": "m-bad-ns", "from": "fable", "ns": "bad", "kind": "message"},
        {"id": "m-valid", "from": "fable", "ns": 5, "kind": "message"},
    ]
    paths = janitor._owned_attachment_paths(
        "c-exact", records, {"fable": [[1, 10]]})
    assert paths == ["chats/c-exact/files/f-owned.txt"]
    assert opened == ["m-valid"]


def test_terminal_reclaim_merges_signed_local_message_when_remote_read_lags(
        world, monkeypatch):
    aryan = world["aryan"]
    dead = aryan.create_chat("Lagged cleanup", members=["fable"])
    message, path = _post_with_blob(aryan, dead.id, "lagged.txt")
    terminal = aryan.messaging.build_event(
        dead.id, {"type": "chat_deleted", "by": "aryan"})
    aryan.tx.append_log(dead.id, "aryan@mach1.jsonl", terminal.to_dict())
    original_read = aryan.tx.read_log

    def lagged_read(chat_id, log_name, offset=0):
        rows, head = original_read(chat_id, log_name, offset)
        return [r for r in rows if r.get("id") != message.id], head

    monkeypatch.setattr(aryan.tx, "read_log", lagged_read)
    assert Janitor(aryan).reclaim_deleted_chat_attachments(dead.id) == 1
    assert aryan.tx.blob_size(path) is None


def test_terminal_reclaim_treats_unreadable_terminal_event_as_transient(
        world, monkeypatch):
    aryan = world["aryan"]
    chat = aryan.create_chat("Terminal read lag", members=["fable"])
    monkeypatch.setattr(aryan.tx, "list_logs", lambda _chat_id: [])

    with pytest.raises(TransportError, match="not yet readable"):
        Janitor(aryan).reclaim_deleted_chat_attachments(chat.id)
