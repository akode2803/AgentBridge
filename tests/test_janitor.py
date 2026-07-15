"""V63 / R65: the storage janitor — verified-redaction blob reclamation
(grace + undo + forgery safe) and terminal-chat purges, over the real
folder transport with E2EE on.
"""

from __future__ import annotations

import pytest

from agentbridge.mesh.janitor import Janitor
from agentbridge.mesh.overlays import ChatOverlays
from agentbridge.mesh.paths import P
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
    aryan, fable = world["aryan"], world["fable"]
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
    aryan, fable = world["aryan"], world["fable"]
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
