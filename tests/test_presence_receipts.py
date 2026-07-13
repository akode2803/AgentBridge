"""Presence heartbeats + the Sent/Delivered/Read receipt ladder (R8)."""

import time

import pytest

from agentbridge.core.errors import PermissionDenied, ValidationError
from agentbridge.mesh import presence as presence_mod
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


from conftest import install_key, seed_account


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    bundles = {n: seed_account(tx, n) for n in ("aryan", "fable", "sudhir")}
    bundles["claude"] = seed_account(tx, "claude", "agent", owner="aryan")

    def mk(user, machine="mach1"):
        home = tmp_path / f"home-{user}-{machine}"
        install_key(home, user, bundles[user])
        return Mesh(FolderTransport(root), user, machine, home=home)

    meshes = {u: mk(u) for u in ("aryan", "fable", "sudhir", "claude")}
    yield meshes, mk
    for m in meshes.values():
        m.close()


def ripple(sender, chat_id, *others):
    sender.outbox.flush_once()
    for m in (sender, *others):
        m.sync.sync_once([chat_id])


# ------------------------------------------------------------------ presence

def test_heartbeat_writes_and_throttles(world):
    meshes, _ = world
    fable = meshes["fable"]
    assert fable.presence.heartbeat() is True          # first beat writes
    assert fable.presence.heartbeat() is False         # throttled
    assert fable.presence.heartbeat(online=False) is True  # flag flip writes
    assert fable.presence.heartbeat(online=False) is False
    raw = meshes["aryan"].presence.presence_of("fable")
    assert raw["online"] is False and raw["last_seen_ns"] > 0


def test_multi_device_merge_any_fresh_online_wins(world):
    meshes, mk = world
    fable_desktop, fable_phone = meshes["fable"], mk("fable", machine="phone")
    fable_desktop.presence.heartbeat(online=False)
    fable_phone.presence.heartbeat(online=True)
    assert meshes["aryan"].presence.presence_of("fable")["online"] is True
    fable_phone.presence.heartbeat(online=False)
    assert meshes["aryan"].presence.presence_of("fable")["online"] is False
    fable_phone.close()


def test_stale_device_stops_counting(world, monkeypatch):
    meshes, _ = world
    meshes["fable"].presence.heartbeat(online=True)
    # jump the clock past the staleness window: the online flag alone
    # doesn't count if the device stopped beating (crash without offline)
    real_ns = time.time_ns
    monkeypatch.setattr(time, "time_ns",
                        lambda: real_ns() + int((presence_mod.STALE_S + 5) * 1e9))
    assert meshes["aryan"].presence.presence_of("fable")["online"] is False


def test_presence_matrix_gating(world):
    meshes, _ = world
    fable = meshes["fable"]
    fable.presence.heartbeat(online=True)
    fable.set_privacy({"last_seen": "nobody", "online": "members"})
    view = meshes["sudhir"].presence.visible_presence("fable")
    assert view["last_seen"] is None            # hidden from everyone
    assert view["online"] is None               # no shared chat yet
    meshes["sudhir"].create_chat("Shared", members=["fable"])
    view2 = meshes["sudhir"].presence.visible_presence("fable")
    assert view2["online"] is True              # members-only now passes
    assert view2["last_seen"] is None           # still nobody


# -------------------------------------------------------------------- status

def test_status_one_logical_value_owner_gated(world):
    meshes, _ = world
    aryan = meshes["aryan"]
    aryan.set_status("dnd", "deep in the rewrite")
    acc = aryan.directory.get("aryan")
    assert acc.status.state == "dnd" and "rewrite" in acc.status.text

    aryan.set_status("busy", agent="claude")     # owner sets the agent's
    assert aryan.directory.get("claude").status.state == "busy"
    with pytest.raises(PermissionDenied):
        meshes["fable"].set_status("free", agent="claude")
    with pytest.raises(ValidationError):
        aryan.set_status("")
    # agents read status before deciding to message (matrix-gated surface)
    assert meshes["claude"].visible_profile("aryan")["status"]["state"] == "dnd"


# ------------------------------------------------------------------ receipts

def test_receipt_ladder_sent_delivered_read(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    dm = aryan.create_dm("fable")
    env = aryan.post(dm.id, "climb the ladder")
    ripple(aryan, dm.id, fable)

    # fable has no presence and no read cursor yet -> Sent
    assert aryan.receipts_for(dm.id)[env.id]["state"] == "sent"

    # fable's client comes online AFTER the message -> Delivered
    fable.presence.heartbeat(online=True)
    rec = aryan.receipts_for(dm.id)[env.id]
    assert rec["state"] == "delivered" and rec["delivered_to"] == ["fable"]

    # fable reads -> Read
    fable.mark_read(dm.id)
    rec = aryan.receipts_for(dm.id)[env.id]
    assert rec["state"] == "read" and rec["read_by"] == ["fable"]


def test_group_receipts_lowest_tier_wins(world):
    meshes, _ = world
    aryan, fable, sudhir = meshes["aryan"], meshes["fable"], meshes["sudhir"]
    group = aryan.create_chat("Ticks", members=["fable", "sudhir"])
    env = aryan.post(group.id, "who has seen this?")
    ripple(aryan, group.id, fable, sudhir)

    fable.presence.heartbeat(online=True)
    fable.mark_read(group.id)                    # fable: read
    sudhir.presence.heartbeat(online=True)       # sudhir: delivered only
    rec = aryan.receipts_for(group.id)[env.id]
    assert rec["state"] == "delivered"           # lowest tier rules the tick
    assert rec["read_by"] == ["fable"]
    assert rec["delivered_to"] == ["sudhir"]
    assert rec["total"] == 2

    sudhir.mark_read(group.id)
    assert aryan.receipts_for(group.id)[env.id]["state"] == "read"


def test_receipts_privacy_gates_both_tiers(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    dm = aryan.create_dm("fable")
    env = aryan.post(dm.id, "private ticks")
    ripple(aryan, dm.id, fable)
    fable.presence.heartbeat(online=True)
    fable.mark_read(dm.id)

    fable.set_privacy({"read_receipts": False})  # fable stops emitting
    rec = aryan.receipts_for(dm.id)[env.id]
    assert rec["state"] == "sent"                # not even Delivered leaks

    fable.set_privacy({"read_receipts": True})
    aryan.set_privacy({"view_read_receipts": False})  # viewer opts out
    assert aryan.receipts_for(dm.id)[env.id]["state"] == "sent"


def test_message_info_shapes(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    dm = aryan.create_dm("fable")
    env = aryan.post(dm.id, "inspect me")
    ripple(aryan, dm.id, fable)

    mine = aryan.message_info(dm.id, env.id)
    assert mine["dm"] is True and mine["state"] == "sent"
    theirs = fable.message_info(dm.id, env.id)   # someone else's message
    assert theirs["from"] == "aryan" and "state" not in theirs
    with pytest.raises(ValidationError):
        aryan.message_info(dm.id, "m-nope")


def test_self_chat_reads_trivially(world):
    meshes, _ = world
    aryan = meshes["aryan"]
    note = aryan.create_self_chat()
    env = aryan.post(note.id, "note to self")
    assert aryan.receipts_for(note.id)[env.id]["state"] == "read"


def test_deactivated_account_never_delivers(world):
    meshes, _ = world
    aryan, sudhir = meshes["aryan"], meshes["sudhir"]
    # sudhir needs an auth record to self-delete
    aryan.tx.put_doc(P.user("sudhir"), {
        **aryan.tx.get_doc(P.user("sudhir")),
        "auth": None,
    })
    dm = aryan.create_dm("sudhir")
    env = aryan.post(dm.id, "into the void")
    ripple(aryan, dm.id, sudhir)
    # no heartbeat ever, then account deactivated directly (no password set)
    sudhir.directory.patch("sudhir", lambda d: d.update(active=False))
    assert aryan.receipts_for(dm.id)[env.id]["state"] == "sent"  # forever