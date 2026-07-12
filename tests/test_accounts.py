"""Accounts v2: creation, auth, handle-vs-identity, deletion cascade."""

import pytest

from agentbridge.core.errors import PermissionDenied, ValidationError
from agentbridge.core.models import Role, UserKind
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"

    def mk(user, machine="mach1"):
        return Mesh(FolderTransport(root), user, machine,
                    home=tmp_path / f"home-{user}-{machine}")

    boot = mk("aryan")
    boot.accounts.create_human("aryan", "aryan-pass")
    boot.accounts.create_human("fable", "fable-pass")
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
