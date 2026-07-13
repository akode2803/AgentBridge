"""Privacy & permission layer: the matrix, blocks, and the public gates."""

import pytest

from agentbridge.core.errors import PermissionDenied, ValidationError
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


from conftest import install_key, seed_account


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    bundles = {
        "aryan": seed_account(tx, "aryan"),               # owns claude
        "fable": seed_account(tx, "fable"),               # owns coco
        "sudhir": seed_account(tx, "sudhir"),             # owns nothing
        "claude": seed_account(tx, "claude", "agent", owner="aryan",
                               about="Aryan's Claude on Work Lenovo"),
        "coco": seed_account(tx, "coco", "agent", owner="fable"),
    }

    def mk(user):
        home = tmp_path / f"home-{user}"
        install_key(home, user, bundles[user])
        return Mesh(FolderTransport(root), user, "mach1", home=home)

    meshes = {u: mk(u) for u in ("aryan", "fable", "sudhir", "claude", "coco")}
    yield meshes
    for m in meshes.values():
        m.close()


# ----------------------------------------------------------------- write side

def test_set_own_privacy_and_validation(world):
    aryan = world["aryan"]
    aryan.set_privacy({"about": "members", "read_receipts": False})
    acc = aryan.directory.get("aryan")
    assert acc.privacy.about.value == "members" and acc.privacy.read_receipts is False

    with pytest.raises(ValidationError):
        aryan.set_privacy({"about": "vips"})           # bad audience
    with pytest.raises(ValidationError):
        aryan.set_privacy({"photo": "members"})        # photo: everyone|nobody
    with pytest.raises(ValidationError):
        aryan.set_privacy({"hologram": "everyone"})    # unknown field
    with pytest.raises(ValidationError):
        aryan.set_privacy({"read_receipts": "yes"})    # bool required


def test_agent_settings_owner_only(world):
    aryan, fable = world["aryan"], world["fable"]
    aryan.set_privacy({"status": "members"}, agent="claude")
    assert aryan.directory.get("claude").privacy.status.value == "members"
    aryan.set_agent_rules("claude", {"messaging": "members"})
    assert aryan.directory.get("claude").rules().messaging.value == "members"

    with pytest.raises(PermissionDenied):
        fable.set_privacy({"status": "nobody"}, agent="claude")
    with pytest.raises(PermissionDenied):
        fable.set_agent_rules("claude", {"messaging": "nobody"})
    with pytest.raises(ValidationError):
        aryan.set_agent_rules("aryan", {"messaging": "nobody"})  # not an agent


# ------------------------------------------------------------------- blocking

def test_block_kills_dms_both_directions_without_leaking(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    dm = aryan.create_dm("sudhir")
    aryan.block("sudhir")

    with pytest.raises(PermissionDenied) as e1:
        aryan.post(dm.id, "I blocked you")   # blocker can't message either
    with pytest.raises(PermissionDenied) as e2:
        sudhir.create_dm("aryan")            # and the blocked can't reach out
    for e in (e1, e2):
        assert "block" not in str(e.value).lower()  # never leaks the block

    aryan.unblock("sudhir")
    aryan.post(dm.id, "we're good again")


def test_block_leaves_common_groups_untouched(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    group = aryan.create_chat("Team", members=["sudhir"])
    aryan.block("sudhir")
    aryan.outbox.flush_once()
    sudhir.sync.sync_once([group.id])
    sudhir.post(group.id, "group still works")  # WhatsApp semantics


# ------------------------------------------------------------ messaging gates

def test_messaging_audience_members_only(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    aryan.set_privacy({"messaging": "members"})
    with pytest.raises(PermissionDenied) as e:
        sudhir.create_dm("aryan")            # stranger: no shared chat
    assert "members only" in str(e.value)

    sudhir.create_chat("Icebreaker", members=["aryan"])  # now they share one
    dm = sudhir.create_dm("aryan")
    assert set(dm.members) == {"aryan", "sudhir"}


def test_messaging_audience_agents_only_owner_rides_along(world):
    """'Agents only' controls who may KNOCK; the owner always rides along
    into the room with admin oversight (Aryan's correction 2026-07-12)."""
    sudhir, fable, coco = world["sudhir"], world["fable"], world["coco"]
    sudhir.set_privacy({"messaging": "agents"})
    with pytest.raises(PermissionDenied):
        fable.create_dm("sudhir")            # humans can't initiate directly
    dm = coco.create_dm("sudhir")            # the agent can...
    assert set(dm.members) == {"coco", "sudhir", "fable"}  # ...owner rides in
    assert dm.members["fable"].role.value == "admin"       # with oversight
    assert dm.members["sudhir"].role.value == "admin"      # equal rights
    assert dm.members["coco"].role.value == "member"


def test_messaging_audience_nobody(world):
    world["aryan"].set_privacy({"messaging": "nobody"})
    with pytest.raises(PermissionDenied):
        world["sudhir"].create_dm("aryan")


def test_agent_outbound_rules_gate_the_agent(world):
    aryan, claude = world["aryan"], world["claude"]
    aryan.set_agent_rules("claude", {"messaging": "nobody"})
    with pytest.raises(PermissionDenied) as e:
        claude.create_dm("sudhir")
    assert "responsible member" in str(e.value)

    aryan.set_agent_rules("claude", {"messaging": "agents"})
    with pytest.raises(PermissionDenied):
        claude.create_dm("sudhir")           # sudhir is human
    dm = claude.create_dm("coco")            # agent-to-agent fine
    assert {"claude", "coco"} <= set(dm.members)


def test_public_gates_readable_by_anyone(world):
    world["aryan"].set_privacy({"messaging": "members", "add_to_group": "nobody"})
    gates = world["sudhir"].public_gates("aryan")
    assert gates == {"messaging": "members", "add_to_group": "nobody"}


# --------------------------------------------------------- add-to-group gates

def test_add_to_group_gate_blocks_direct_add(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    sudhir.set_privacy({"add_to_group": "nobody"})
    group = aryan.create_chat("Wants Sudhir")
    with pytest.raises(PermissionDenied) as e:
        aryan.add_members(group.id, ["sudhir"])
    assert "nobody only" in str(e.value)
    with pytest.raises(PermissionDenied):
        aryan.create_chat("With Sudhir", members=["sudhir"])


def test_pulled_owner_gate_fails_the_whole_chat(world):
    """fable won't be added to groups -> chatting coco is impossible for
    non-owners: the responsible-member invariant always wins."""
    aryan, fable = world["aryan"], world["fable"]
    fable.set_privacy({"add_to_group": "nobody"})
    with pytest.raises(PermissionDenied):
        aryan.create_dm("coco")              # needs to pull fable -> refused
    with pytest.raises(PermissionDenied):
        aryan.create_chat("CoCo room", members=["coco"])
    # fable herself can still chat her own agent (no pull-in needed)
    dm = fable.create_dm("coco")
    assert set(dm.members) == {"fable", "coco"}


# ------------------------------------------------------------ profile matrix

def test_profile_about_members_only(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    aryan.set_privacy({"about": "members"})
    assert "about" not in sudhir.visible_profile("aryan")
    sudhir.create_chat("Now we share", members=["aryan"])
    assert "about" in sudhir.visible_profile("aryan")


def test_profile_agents_audience_admits_agent_owners(world):
    aryan = world["aryan"]
    aryan.set_privacy({"status": "agents"})
    assert "status" in world["coco"].visible_profile("aryan")     # an agent
    assert "status" in world["fable"].visible_profile("aryan")    # owns coco
    assert "status" not in world["sudhir"].visible_profile("aryan")  # plain human


def test_agent_profile_owner_always_visible_and_owner_sees_all(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    aryan.set_privacy({"about": "nobody", "status": "nobody"}, agent="claude")
    prof = sudhir.visible_profile("claude")
    assert prof["owner"] == "aryan"          # product rule: owner always shown
    assert "about" not in prof
    own = aryan.visible_profile("claude")    # the responsible member sees all
    assert own["about"].startswith("Aryan's Claude")


def test_photo_gate_and_self_view(world):
    aryan, sudhir = world["aryan"], world["sudhir"]
    aryan.set_privacy({"photo": "nobody", "last_seen": "nobody"})
    prof = sudhir.visible_profile("aryan")
    assert prof["photo_visible"] is False and prof["may_see_last_seen"] is False
    own = aryan.visible_profile("aryan")
    assert own["photo_visible"] is True and own["may_see_last_seen"] is True


# ------------------------------------------------------------- read receipts

def test_receipt_visibility_needs_both_toggles(world):
    aryan, fable = world["aryan"], world["fable"]
    assert aryan.may_see_receipts_of("fable") is True
    fable.set_privacy({"read_receipts": False})   # fable stops emitting
    assert aryan.may_see_receipts_of("fable") is False
    fable.set_privacy({"read_receipts": True})
    aryan.set_privacy({"view_read_receipts": False})  # aryan stops looking
    assert aryan.may_see_receipts_of("fable") is False
