"""Accounts v2: creation, auth, handle-vs-identity, deletion cascade."""

import pytest

from agentbridge.core.errors import PermissionDenied, ValidationError
from agentbridge.core.models import MsgKind, Role, UserKind
from agentbridge.mesh.paths import P
from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    bundles = {}

    def mk(user, machine="mach1"):
        home = tmp_path / f"home-{user}-{machine}"
        if user in bundles:
            KeyStore(home).save(user, bundles[user])
        return Mesh(FolderTransport(root), user, machine, home=home)

    boot = mk("aryan")
    boot.accounts.create_human("aryan", "aryan-pass")
    boot.accounts.create_human("fable", "fable-pass")
    bundles.update({name: boot.keystore.load(name) for name in ("aryan", "fable")})
    boot.close()

    meshes = {"aryan": mk("aryan"), "fable": mk("fable")}
    yield meshes, mk
    for m in meshes.values():
        m.close()


# ------------------------------------------------------------------ creation

def test_create_human_shape_and_uniqueness(world):
    meshes, _ = world
    acc = meshes["aryan"].directory.get("fable")
    assert acc.kind is UserKind.HUMAN and acc.active
    assert acc.display == "Fable"
    with pytest.raises(ValidationError):
        meshes["aryan"].accounts.create_human("fable", "again")  # taken
    for bad in ("A", "3abc", "has space", "x", "all", "everyone"):
        with pytest.raises(ValidationError):
            meshes["aryan"].accounts.create_human(bad, "password")
    with pytest.raises(ValidationError):
        meshes["aryan"].accounts.create_human("shortpw", "12345")


def test_create_agent_machine_login_ownership(world):
    meshes, _ = world
    aryan = meshes["aryan"]
    acc = aryan.accounts.create_agent("claude", display="Claude")
    assert acc.kind is UserKind.AGENT
    assert acc.agent.owner == "aryan" and acc.agent.machine == "mach1"
    assert acc.about == "Aryan's Claude on mach1"   # the default about
    assert acc.auth is None                          # agents never authenticate
    assert aryan.directory.owner_of("claude") == "aryan"


def test_create_agent_prepares_keys_before_publish_and_rolls_back(
    world, monkeypatch
):
    meshes, _ = world
    aryan = meshes["aryan"]
    original_put = aryan.tx.put_doc
    observed = {"ready": False}

    def refuse_agent(path, doc):
        if path == P.user("helper"):
            keys = doc["keys"]
            assert aryan.keystore.load("helper") is not None
            assert aryan.key_pins.fingerprint(
                "helper", keys["sign_pub"], keys["agree_pub"])
            observed["ready"] = True
            raise PermissionError("simulated RLS denial")
        return original_put(path, doc)

    monkeypatch.setattr(aryan.tx, "put_doc", refuse_agent)
    with pytest.raises(PermissionError, match="RLS denial"):
        aryan.accounts.create_agent("helper")

    assert observed["ready"]
    assert aryan.keystore.load("helper") is None
    assert aryan.key_pins.fingerprint("helper") == ""
    assert aryan.directory.get("helper") is None

    # The failed attempt leaves the name and local identity state reusable.
    monkeypatch.setattr(aryan.tx, "put_doc", original_put)
    assert aryan.accounts.create_agent("helper").active


# ---------------------------------------------------------------------- auth

def test_password_verify_and_change(world):
    meshes, _ = world
    aryan = meshes["aryan"].accounts
    assert aryan.verify_password("aryan", "aryan-pass")
    assert not aryan.verify_password("aryan", "wrong")
    assert not aryan.verify_password("nobody", "x")

    with pytest.raises(PermissionDenied):
        aryan.change_password("wrong-old", "new-pass-1")
    aryan.change_password("aryan-pass", "new-pass-1")
    assert aryan.verify_password("aryan", "new-pass-1")
    assert not aryan.verify_password("aryan", "aryan-pass")


# ---------------------------------------------------------- handle vs identity

def test_handle_change_keeps_identity_and_history(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    chat = aryan.create_chat("Before rename", members=["fable"])
    aryan.post(chat.id, "sent under the old handle")

    aryan.accounts.set_handle("aryan-kumar")
    acc = aryan.directory.get("aryan")
    assert acc.name == "aryan" and acc.handle == "aryan-kumar"
    assert acc.handle_or_name() == "aryan-kumar"

    # identity untouched: membership, messages, resolution all still work
    assert "aryan" in aryan.snapshot(chat.id).members
    assert aryan.messages_for(chat.id)[-1].from_ == "aryan"
    assert fable.directory.resolve("aryan-kumar") == "aryan"
    assert fable.directory.resolve("aryan") == "aryan"  # id always resolves


def test_handle_collisions_and_reserved(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    with pytest.raises(ValidationError):
        aryan.accounts.set_handle("fable")      # collides with an id
    fable.accounts.set_handle("storyteller")
    with pytest.raises(ValidationError):
        aryan.accounts.set_handle("storyteller")  # collides with a handle
    with pytest.raises(ValidationError):
        aryan.accounts.set_handle("all")        # reserved (@all mention)
    aryan.accounts.set_handle("aryan")          # your own id is always fine


def test_agent_profile_owner_gated(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.create_agent("claude")
    aryan.accounts.set_display("Claude 4.8", agent="claude")
    aryan.accounts.set_about("Dev agent on the work laptop", agent="claude")
    assert aryan.directory.get("claude").display == "Claude 4.8"
    with pytest.raises(PermissionDenied):
        fable.accounts.set_display("Hijacked", agent="claude")
    with pytest.raises(ValidationError):
        aryan.accounts.set_display("X", agent="fable")  # not an agent


# ------------------------------------------------------------------ lifecycle

def test_machine_signout_flips_only_that_machines_agents(world):
    meshes, mk = world
    aryan = meshes["aryan"]
    aryan.accounts.create_agent("claude")            # on mach1
    laptop = mk("aryan", machine="laptop")
    laptop.accounts.create_agent("claude-mini")      # on laptop

    changed = aryan.accounts.set_machine_agents_active(False)  # sign out mach1
    assert changed == ["claude"]
    assert aryan.directory.get("claude").active is False
    assert aryan.directory.get("claude-mini").active is True   # untouched

    aryan.accounts.set_machine_agents_active(True)   # sign back in
    assert aryan.directory.get("claude").active is True
    laptop.close()


def test_delete_account_cascades(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.create_agent("claude")
    group = aryan.create_chat("Doomed", members=["fable", "claude"])
    dm = fable.create_dm("aryan")
    fable.outbox.flush_once()
    aryan.sync.sync_once([dm.id])

    with pytest.raises(PermissionDenied):
        aryan.accounts.delete_account("wrong-password")
    aryan.accounts.delete_account("aryan-pass")

    # account + owned agents soft-deactivated, names still resolvable
    assert aryan.directory.get("aryan").active is False
    assert aryan.directory.get("claude").active is False
    assert aryan.directory.display("aryan") == "Aryan"  # grey-out, not gone

    # left the group; the fold cascaded the ownerless agent out with him
    aryan.outbox.flush_once()
    fable.sync.sync_once([group.id])
    healed = fable.membership.refold(group.id)
    assert set(healed.members) == {"fable"}
    assert healed.members["fable"].role is Role.ADMIN  # auto-promoted

    # DMing the deleted account is refused without leaking specifics
    with pytest.raises(PermissionDenied) as e:
        fable.post(dm.id, "hello?")
    assert "not available" in str(e.value)
    # and the profile shows inactive so the GUI can grey + disable fields
    assert fable.visible_profile("aryan")["active"] is False


def test_deleted_account_dm_gate_on_create_too(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.delete_account("aryan-pass")
    with pytest.raises(PermissionDenied):
        fable.create_dm("aryan")


# ------------------------------------------------- R7.1 agent lifecycle (D19)

def test_owner_removes_own_agent_without_admin(world):
    """The oversight rule: a member may always remove THEIR agent from any
    room — admin or not; other non-admins still can't."""
    meshes, mk = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.create_agent("claude")
    group = fable.create_chat("Fables room", members=["aryan", "claude"])
    fable.outbox.flush_once()
    aryan.sync.sync_once([group.id])
    assert group.members["aryan"].role.value == "member"  # aryan NOT admin

    sudhir = mk("sudhir")
    try:
        with pytest.raises(Exception):  # noqa: B017 — non-member/non-owner path
            sudhir.remove_member(group.id, "claude")
        healed = aryan.remove_member(group.id, "claude")  # owner: allowed
        assert "claude" not in healed.members
    finally:
        sudhir.close()


def test_delete_agent_full_lifecycle(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.create_agent("claude")
    room = aryan.create_chat("Working room", members=["fable", "claude"])

    with pytest.raises(PermissionDenied):
        fable.accounts.delete_agent("claude")   # not the responsible member

    aryan.accounts.delete_agent("claude")
    assert aryan.directory.get("claude").active is False
    assert aryan.keystore.load("claude") is None            # local keys gone
    healed = aryan.membership.refold(room.id)
    assert "claude" not in healed.members                   # out of the room
    assert aryan.directory.display("claude") == "Claude"    # name resolvable


def test_forged_agent_removal_by_non_owner_ignored_in_fold(world):
    meshes, _ = world
    aryan = meshes["aryan"]
    aryan.accounts.create_agent("claude")
    room = aryan.create_chat("Hold", members=["fable", "claude"])
    aryan.outbox.flush_once()
    # fable is a plain member, not admin, not the owner — her removal event
    # must not take effect in the fold
    aryan.tx.append_log(room.id, "fable@else", {
        "id": "x1", "ns": 10**18, "ts": "t", "from": "fable", "kind": "info",
        "event": {"type": "member_removed", "who": "claude", "by": "fable"},
    })
    aryan.sync.sync_once([room.id])
    healed = aryan.membership.refold(room.id)
    assert "claude" in healed.members


def test_agents_cannot_self_manage_account(world):
    """D19 with the R38 carve-out: status + about are the agent's OWN to keep
    current (owner and agent both write; most recent wins) — every other
    account surface still refuses the agent identity."""
    meshes, mk = world
    aryan = meshes["aryan"]
    aryan.accounts.create_agent("claude")
    claude = mk("claude")
    try:
        for call in (
            lambda: claude.accounts.set_display("Self Named"),
            lambda: claude.accounts.set_handle("sneaky"),
            lambda: claude.privacy.set_privacy({"messaging": "nobody"}),
            lambda: claude.privacy.block("fable"),
        ):
            with pytest.raises(PermissionDenied):
                call()
        # the carve-out: its own status + about, last writer wins
        claude.accounts.set_status("busy", "indexing the repo")
        claude.accounts.set_about("Aryan's code helper")
        acc = claude.directory.get("claude")
        assert acc.status.state == "busy" and acc.status.text == "indexing the repo"
        assert acc.about == "Aryan's code helper"
        aryan.accounts.set_status("available", agent="claude")   # owner overwrites
        assert aryan.directory.get("claude").status.state == "available"
    finally:
        claude.close()


def test_claim_machine_agents_requires_agent_key(world):
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.create_agent("claude")               # on mach1, owner aryan
    room = aryan.create_chat("Only aryan here", members=["claude"])

    assert fable.accounts.claimable_agents() == []
    assert fable.accounts.claim_machine_agents() == []
    assert fable.directory.owner_of("claude") == "aryan"

    # A matching machine label is not authority and has no membership fallout.
    healed = aryan.membership.refold(room.id)
    assert "claude" in healed.members


def test_claim_posts_owner_changed_departures(world):
    """V69: the FACADE claim makes the fallout visible — in every group the
    new responsible member isn't in, the agent leaves AS ITSELF with reason
    ``owner_changed`` (a real ns-stamped departure, not a silent heal);
    rooms the new owner shares keep the agent, no pill."""
    from conftest import install_key

    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.accounts.create_agent("claude")
    lost = aryan.create_chat("Aryan only", members=["claude"])
    kept = aryan.create_chat("Both here", members=["claude", "fable"])
    # the machine-claim premise: the agent's keys live on this machine —
    # the test world splits homes per user, so mirror reality explicitly
    install_key(fable.home, "claude", aryan.keystore.load("claude"))

    assert fable.claim_machine_agents() == ["claude"]
    assert fable.directory.owner_of("claude") == "fable"

    # the lost room: a REAL departure event authored by the agent
    aryan.sync.sync_once([lost.id])
    snap = aryan.membership.refold(lost.id)
    assert "claude" not in snap.members
    pills = [m for m in aryan.messages_for(lost.id)
             if m.kind is MsgKind.INFO
             and (m.event or {}).get("type") == "member_left"
             and (m.event or {}).get("reason") == "owner_changed"]
    assert len(pills) == 1 and pills[0].from_ == "claude"
    # tenure closed at the event's ns — the departure is log-real (R25)
    assert pills[0].ns > 0

    # the kept room: fable is present, claude stays, and no pill appears
    aryan.sync.sync_once([kept.id])
    snap2 = aryan.membership.refold(kept.id)
    assert "claude" in snap2.members
    assert not [m for m in aryan.messages_for(kept.id)
                if m.kind is MsgKind.INFO
                and (m.event or {}).get("reason") == "owner_changed"]
