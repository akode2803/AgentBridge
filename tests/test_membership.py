"""Membership & groups: event fold, multi-admin, owner pull-in, permissions."""

import pytest

from agentbridge.core.errors import PermissionDenied, ValidationError
from agentbridge.core.models import ChatKind, Role, UserKind
from agentbridge.mesh import events
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


def put_account(tx, name, kind, owner=None):
    doc = {"name": name, "kind": kind, "display": name.title()}
    if owner:
        doc["agent"] = {"owner": owner, "machine": "m1"}
    tx.put_doc(P.user(name), doc)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    put_account(tx, "aryan", "human")
    put_account(tx, "fable", "human")
    put_account(tx, "sudhir", "human")
    put_account(tx, "claude", "agent", owner="aryan")
    put_account(tx, "coco", "agent", owner="fable")

    def mk(user):
        return Mesh(FolderTransport(root), user, "mach1", home=tmp_path / f"home-{user}")

    meshes = {u: mk(u) for u in ("aryan", "fable", "sudhir", "claude", "coco")}
    yield meshes
    for m in meshes.values():
        m.close()


def ripple(sender: Mesh, chat_id: str, *others: Mesh):
    """Flush the sender's outbox, then everyone syncs the chat."""
    sender.outbox.flush_once()
    for m in (sender, *others):
        m.sync.sync_once([chat_id])


# ------------------------------------------------------------------ creation

def test_create_group_creator_is_admin(world):
    aryan = world["aryan"]
    snap = aryan.create_chat("QA Room", members=["fable"])
    assert snap.kind is ChatKind.GROUP
    assert snap.members["aryan"].role is Role.ADMIN
    assert snap.members["fable"].role is Role.MEMBER
    # genesis pill is in the creator's transcript immediately (optimistic)
    pills = aryan.messages_for(snap.id)
    assert pills[0].event["type"] == "created"


def test_empty_group_name_falls_back(world):
    snap = world["aryan"].create_chat("  ")
    assert snap.name == "New Group"  # v1 UX kept


def test_owner_pull_in_on_create(world):
    aryan = world["aryan"]
    snap = aryan.create_chat("With CoCo", members=["coco"])
    assert set(snap.members) == {"aryan", "coco", "fable"}  # fable pulled in
    genesis = aryan.messages_for(snap.id)[0].event
    assert genesis["pulled"] == {"fable": "coco"}


def test_dm_with_own_agent_stays_dm(world):
    snap = world["aryan"].create_dm("claude")
    assert snap.kind is ChatKind.DM and set(snap.members) == {"aryan", "claude"}


def test_dm_with_foreign_agent_births_auto_group_symmetric(world):
    # fable DMs aryan's agent -> group with aryan pulled in
    s1 = world["fable"].create_dm("claude")
    assert s1.kind is ChatKind.GROUP and s1.auto_dm
    assert set(s1.members) == {"fable", "claude", "aryan"}
    # GENESIS ADMIN RULE: every human at genesis is admin, the agent never
    assert s1.members["fable"].role is Role.ADMIN
    assert s1.members["aryan"].role is Role.ADMIN
    assert s1.members["claude"].role is Role.MEMBER
    # symmetric direction: aryan DMs fable's agent (the v0.24.1 verified case)
    s2 = world["aryan"].create_dm("coco")
    assert set(s2.members) == {"aryan", "coco", "fable"}
    assert s2.members["fable"].role is Role.ADMIN  # pulled owner = admin too
    # auto-groups dedupe on the same roster
    world["fable"].sync.sync_once([s1.id])
    again = world["fable"].create_dm("claude")
    assert again.id == s1.id


def test_agent_initiated_dm_all_humans_admin(world):
    """claude DMs fable: 'Aryan joined as Claude's responsible member' and
    both humans hold admin — the agent acts only under oversight."""
    snap = world["claude"].create_dm("fable")
    assert snap.auto_dm and set(snap.members) == {"claude", "fable", "aryan"}
    assert snap.members["aryan"].role is Role.ADMIN
    assert snap.members["fable"].role is Role.ADMIN
    assert snap.members["claude"].role is Role.MEMBER
    genesis = world["claude"].messages_for(snap.id)[0].event
    assert genesis["pulled"] == {"aryan": "claude"}  # the info-pill data


def test_agent_to_agent_dm_both_owners_admin(world):
    snap = world["claude"].create_dm("coco")
    assert set(snap.members) == {"claude", "coco", "aryan", "fable"}
    assert snap.members["aryan"].role is Role.ADMIN
    assert snap.members["fable"].role is Role.ADMIN
    assert snap.members["claude"].role is Role.MEMBER
    assert snap.members["coco"].role is Role.MEMBER


def test_plain_dm_and_self_chat_dedupe(world):
    aryan = world["aryan"]
    dm1 = aryan.create_dm("fable")
    assert dm1.kind is ChatKind.DM
    assert aryan.create_dm("fable").id == dm1.id
    self1 = aryan.create_self_chat()
    assert self1.kind is ChatKind.SELF and set(self1.members) == {"aryan"}
    assert aryan.create_self_chat().id == self1.id


def test_create_rejects_unknown_users_and_self_dm(world):
    with pytest.raises(ValidationError):
        world["aryan"].create_chat("x", members=["nobody"])
    with pytest.raises(ValidationError):
        world["aryan"].create_dm("aryan")


# ------------------------------------------------------------------ mutations

def test_add_members_pulls_owner_and_folds_everywhere(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Grow", members=[])
    aryan.add_members(snap.id, ["coco"])
    ripple(aryan, snap.id, fable)
    healed = fable.membership.refold(snap.id)
    assert set(healed.members) == {"aryan", "coco", "fable"}
    assert healed.members["fable"].role is Role.MEMBER


def test_add_members_respects_permission_level(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Locked", members=["fable"])
    aryan.set_permissions(snap.id, {"add_members": "admins"})
    ripple(aryan, snap.id, fable)
    with pytest.raises(PermissionDenied):
        fable.add_members(snap.id, ["sudhir"])
    aryan.add_members(snap.id, ["sudhir"])  # admin still can


def test_remove_member_cascades_ownerless_agent(world):
    aryan = world["aryan"]
    snap = aryan.create_chat("Cascade", members=["coco"])  # pulls fable
    assert set(snap.members) == {"aryan", "coco", "fable"}
    healed = aryan.remove_member(snap.id, "fable")
    # fable removed -> coco has no responsible member left -> cascades out
    assert set(healed.members) == {"aryan"}


def test_remove_requires_admin_and_not_self(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Rules", members=["fable", "sudhir"])
    ripple(aryan, snap.id, fable)
    with pytest.raises(PermissionDenied):
        fable.remove_member(snap.id, "sudhir")
    with pytest.raises(ValidationError):
        aryan.remove_member(snap.id, "aryan")


def test_leave_auto_promotes_longest_standing_human(world):
    aryan, fable, sudhir = world["aryan"], world["fable"], world["sudhir"]
    snap = aryan.create_chat("Succession", members=["fable"])
    ripple(aryan, snap.id, fable, sudhir)
    aryan.add_members(snap.id, ["sudhir"])   # joins later than fable
    ripple(aryan, snap.id, fable, sudhir)

    healed = aryan.leave(snap.id)
    assert "aryan" not in healed.members
    assert healed.members["fable"].role is Role.ADMIN   # earliest human wins
    assert healed.members["sudhir"].role is Role.MEMBER


def test_admin_grant_revoke_and_agent_refusal(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Admins", members=["fable", "coco"])
    healed = aryan.grant_admin(snap.id, "fable")
    assert healed.members["fable"].role is Role.ADMIN

    with pytest.raises(ValidationError):
        aryan.grant_admin(snap.id, "coco")  # agents can never be admins

    healed = aryan.revoke_admin(snap.id, "fable")
    assert healed.members["fable"].role is Role.MEMBER

    ripple(aryan, snap.id, fable)
    with pytest.raises(PermissionDenied):
        fable.grant_admin(snap.id, "fable")  # non-admin cannot self-promote


def test_edit_settings_permission_gates_rename(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Old Name", members=["fable"])
    ripple(aryan, snap.id, fable)
    fable.rename(snap.id, "Fable Was Here")  # default edit_settings=all
    ripple(fable, snap.id, aryan)            # fable's event must flush first
    assert aryan.membership.refold(snap.id).name == "Fable Was Here"

    aryan.set_permissions(snap.id, {"edit_settings": "admins"})
    ripple(aryan, snap.id, fable)
    with pytest.raises(PermissionDenied):
        fable.rename(snap.id, "Locked Out")
    with pytest.raises(PermissionDenied):
        fable.set_description(snap.id, "nope")


def test_send_messages_admins_only_blocks_post(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Announcements", members=["fable"])
    aryan.set_permissions(snap.id, {"send_messages": "admins"})
    ripple(aryan, snap.id, fable)
    with pytest.raises(PermissionDenied):
        fable.post(snap.id, "can I talk?")
    aryan.post(snap.id, "admins only in here")  # fine


def test_set_permissions_validates_keys_and_actor(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Perm", members=["fable"])
    ripple(aryan, snap.id, fable)
    with pytest.raises(ValidationError):
        aryan.set_permissions(snap.id, {"teleport": True})
    with pytest.raises(PermissionDenied):
        fable.set_permissions(snap.id, {"send_messages": "admins"})


# ------------------------------------------------------------ history-on-join

def test_history_on_join_toggle(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("History")
    old = aryan.post(snap.id, "before fable joined")
    aryan.set_permissions(snap.id, {"send_history": False})
    aryan.add_members(snap.id, ["fable"])
    fresh = aryan.post(snap.id, "after fable joined")
    ripple(aryan, snap.id, fable)

    seen = [m.id for m in fable.messages_for(snap.id) if m.kind.value == "message"]
    assert seen == [fresh.id]  # pre-join history hidden
    assert old.id in [m.id for m in aryan.messages_for(snap.id)]  # aryan unaffected

    aryan.set_permissions(snap.id, {"send_history": True})
    ripple(aryan, snap.id, fable)
    fable.membership.refold(snap.id)
    seen2 = [m.id for m in fable.messages_for(snap.id) if m.kind.value == "message"]
    assert seen2 == [old.id, fresh.id]  # flipping it on reveals the past


# --------------------------------------------------------------- fold healing

def test_meta_clobber_self_heals(world):
    aryan = world["aryan"]
    snap = aryan.create_chat("Healme", members=["fable", "coco"])
    aryan.grant_admin(snap.id, "fable")
    good = aryan.membership.refold(snap.id)

    # simulate the v1 last-writer-wins disaster: meta wrecked by a stale writer
    aryan.tx.put_doc(P.meta(snap.id), {"id": snap.id, "kind": "group",
                                       "name": "WRECKED", "members": {}})
    healed = aryan.membership.refold(snap.id)
    assert healed.to_dict() == good.to_dict()  # bit-for-bit reconstruction


def test_forged_events_ignored_by_fold(world):
    aryan = world["aryan"]
    snap = aryan.create_chat("Fortress", members=["fable"])
    aryan.outbox.flush_once()

    # eve (not a member) writes forged events straight into the transport;
    # ns must sort AFTER the real genesis to exercise the authority checks
    # (a backdated ns would be dropped by the before-genesis rule instead)
    eve_events = [
        {"id": "f1", "ns": 2 * 10**18, "ts": "t", "from": "sudhir", "kind": "info",
         "event": {"type": "member_added", "who": "sudhir", "by": "sudhir"}},
        {"id": "f2", "ns": 2 * 10**18 + 1, "ts": "t", "from": "fable", "kind": "info",
         "event": {"type": "admin_granted", "who": "fable", "by": "fable"}},
    ]
    for ev in eve_events:
        aryan.tx.append_log(snap.id, "sudhir@rogue", ev)
    aryan.sync.sync_once([snap.id])

    healed = aryan.membership.refold(snap.id)
    assert "sudhir" not in healed.members            # outsider add ignored
    assert healed.members["fable"].role is Role.MEMBER  # self-grant ignored


def test_fold_deterministic_across_log_distribution(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Det", members=["fable"])
    aryan.grant_admin(snap.id, "fable")
    ripple(aryan, snap.id, fable)
    fable.rename(snap.id, "Determinism")
    ripple(fable, snap.id, aryan)

    a = events.fold(snap.id, aryan.store.messages(snap.id), aryan.directory)
    b = events.fold(snap.id, fable.store.messages(snap.id), fable.directory)
    assert a.to_dict() == b.to_dict()


def test_chats_for_lists_only_my_chats(world):
    aryan, fable = world["aryan"], world["fable"]
    mine = aryan.create_chat("Mine only")
    both = aryan.create_chat("Shared", members=["fable"])
    assert {s.id for s in aryan.membership.chats_for()} == {mine.id, both.id}
    assert {s.id for s in fable.membership.chats_for()} == {both.id}


# --------------------------------------------------- agent oversight (R6.1)

def agent_group(world):
    """aryan's group with coco in it (fable pulled as plain MEMBER — pull-ins
    into deliberate groups get no auto-admin) — synced to coco's mesh."""
    aryan, coco = world["aryan"], world["coco"]
    snap = aryan.create_chat("Team", members=["coco"])
    assert snap.members["fable"].role is Role.MEMBER  # pulled, not admin
    aryan.outbox.flush_once()
    coco.sync.sync_once([snap.id])
    return snap


def test_agent_add_toggle1_needs_owner_admin(world):
    aryan, coco = world["aryan"], world["coco"]
    snap = agent_group(world)
    # default: agents_add_if_owner_admin=True, but fable is NOT admin here
    with pytest.raises(PermissionDenied) as e:
        coco.add_members(snap.id, ["sudhir"])
    assert "agent" in str(e.value)

    aryan.grant_admin(snap.id, "fable")   # owner gains admin -> agent may add
    aryan.outbox.flush_once()
    coco.sync.sync_once([snap.id])
    healed = coco.add_members(snap.id, ["sudhir"])
    assert "sudhir" in healed.members
    assert healed.members["sudhir"].role is Role.MEMBER  # never auto-admin


def test_agent_add_toggle2_rides_members_can_add(world):
    aryan, coco = world["aryan"], world["coco"]
    snap = agent_group(world)
    aryan.set_permissions(snap.id, {"agents_add_if_owner_admin": False,
                                    "agents_add_if_members_can": True})
    aryan.outbox.flush_once()
    coco.sync.sync_once([snap.id])
    healed = coco.add_members(snap.id, ["sudhir"])  # add_members=all (default)
    assert "sudhir" in healed.members

    # but toggle 2 dies when general members lose the add right
    aryan.remove_member(snap.id, "sudhir")
    aryan.set_permissions(snap.id, {"add_members": "admins"})
    aryan.outbox.flush_once()
    coco.sync.sync_once([snap.id])
    with pytest.raises(PermissionDenied):
        coco.add_members(snap.id, ["sudhir"])


def test_agent_add_both_toggles_off(world):
    aryan, coco = world["aryan"], world["coco"]
    snap = agent_group(world)
    aryan.grant_admin(snap.id, "fable")  # even WITH the owner as admin
    aryan.set_permissions(snap.id, {"agents_add_if_owner_admin": False,
                                    "agents_add_if_members_can": False})
    aryan.outbox.flush_once()
    coco.sync.sync_once([snap.id])
    with pytest.raises(PermissionDenied):
        coco.add_members(snap.id, ["sudhir"])


def test_agents_can_never_remove_members(world):
    aryan, coco = world["aryan"], world["coco"]
    snap = agent_group(world)
    with pytest.raises(PermissionDenied) as e:
        coco.remove_member(snap.id, "aryan")
    assert "never remove" in str(e.value)

    # forged agent-authored removal dies in the fold too (post-genesis ns —
    # a backdated one would be dropped before the authority check even ran)
    aryan.tx.append_log(snap.id, "coco@rogue", {
        "id": "forge1", "ns": 2 * 10**18, "ts": "t", "from": "coco", "kind": "info",
        "event": {"type": "member_removed", "who": "aryan", "by": "coco"},
    })
    aryan.sync.sync_once([snap.id])
    healed = aryan.membership.refold(snap.id)
    assert "aryan" in healed.members


def test_agent_kind_lookup(world):
    d = world["aryan"].directory
    assert d.kind("claude") is UserKind.AGENT
    assert d.owner_of("claude") == "aryan"
    assert d.owner_of("aryan") is None
    assert d.missing_owners(["aryan", "coco"]) == {"fable": "coco"}
    assert d.missing_owners(["aryan", "claude"]) == {}


# --------------------------------------------------------- R13: delete + photo

def test_delete_chat_terminal_and_admin_only(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Doomed", members=["fable"])
    ripple(aryan, snap.id, fable)

    # a plain member may not delete
    with pytest.raises(PermissionDenied):
        fable.delete_chat(snap.id)

    dead = aryan.delete_chat(snap.id)
    assert dead.deleted is True and dead.members == {}
    ripple(aryan, snap.id, fable)
    assert all(s.id != snap.id for s in fable.chats_for())
    with pytest.raises(Exception):
        fable.post(snap.id, "anyone home?")

    # a forged later 'created' cannot resurrect it (terminal in the fold).
    # NOTE: a BACKDATED forged genesis is a separate, real gap (it would win
    # "first created wins") — tracked as the R13.5 fold-integrity round.
    aryan.tx.append_log(snap.id, "fable@rogue", {
        "id": "res1", "ns": 2 * 10**18 + 5, "ts": "t", "from": "fable",
        "kind": "info",
        "event": {"type": "created", "name": "Zombie",
                  "members": {"fable": "admin"}},
    })
    aryan.sync.sync_once([snap.id])
    refolded = aryan.membership.refold(snap.id)
    assert refolded.deleted is True and refolded.members == {}


def test_delete_chat_refused_for_dm(world):
    aryan = world["aryan"]
    dm = aryan.create_dm("fable")
    with pytest.raises(ValidationError):
        aryan.delete_chat(dm.id)


def test_group_avatar_folds_and_gates(world):
    aryan, fable = world["aryan"], world["fable"]
    snap = aryan.create_chat("Pic", members=["fable"],
                             permissions={"edit_settings": "admins"})
    ripple(aryan, snap.id, fable)

    updated = aryan.set_avatar(snap.id, b"jpegbytes")
    assert len(updated.avatar) == 64  # sha256 marker in the FOLD
    assert aryan.tx.get_blob(P.chat_avatar(snap.id)) == b"jpegbytes"

    # a non-admin's avatar event is refused at write AND ignored at fold
    ripple(aryan, snap.id, fable)
    with pytest.raises(PermissionDenied):
        fable.set_avatar(snap.id, b"sneaky")
    aryan.tx.append_log(snap.id, "fable@rogue", {
        "id": "av1", "ns": 2 * 10**18 + 9, "ts": "t", "from": "fable",
        "kind": "info", "event": {"type": "avatar", "sha": "f" * 64},
    })
    aryan.sync.sync_once([snap.id])
    refolded = aryan.membership.refold(snap.id)
    assert refolded.avatar == updated.avatar  # forged change didn't stick

    cleared = aryan.clear_avatar(snap.id)
    assert cleared.avatar == ""


def test_pin_expiry_is_lazy(world):
    aryan = world["aryan"]
    snap = aryan.create_chat("Pins")
    env = aryan.post(snap.id, "pin me")
    aryan.pin(snap.id, env.id, hours=-1)  # until_ns firmly in the past
    assert aryan.pins(snap.id) == {}
    aryan.pin(snap.id, env.id)  # forever
    assert env.id in aryan.pins(snap.id)


def test_refold_survives_transient_meta_write_failure(world, monkeypatch):
    """meta.json is a rebuildable CACHE (tenet 3): if its write is transiently
    blocked (scanner/sync lock — the Windows-CI burn), the mutation that
    triggered the refold still succeeds with the correct fold, and the next
    write heals the snapshot on disk."""
    from agentbridge.core.errors import TransportError

    aryan = world["aryan"]
    snap = aryan.create_chat("Cache", members=["fable"])

    real_put = aryan.tx.put_doc
    blocked = {"n": 0}

    def flaky_put(path, data):
        if path.endswith("meta.json") and blocked["n"] > 0:
            blocked["n"] -= 1
            raise TransportError("atomic write failed after 6 attempts")
        return real_put(path, data)

    monkeypatch.setattr(aryan.tx, "put_doc", flaky_put)
    blocked["n"] = 1
    healed = aryan.grant_admin(snap.id, "fable")   # must NOT raise
    assert set(healed.admins()) == {"aryan", "fable"}  # fold result correct

    # the cache write was skipped — the next mutation rewrites it
    aryan.rename(snap.id, "Cache 2")
    on_disk = aryan.snapshot(snap.id)
    assert set(on_disk.admins()) == {"aryan", "fable"}
    assert on_disk.name == "Cache 2"
