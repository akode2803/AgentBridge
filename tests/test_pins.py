"""R27 key-pinning tests — the directory root of trust.

The account doc publishes the keys every signature check and epoch-key wrap
relies on, but the doc sits on a writable transport. These tests craft hostile
doc rewrites directly on the transport and assert the pin layer keeps every
established relationship on the keys it already knows.
"""

from __future__ import annotations

import pytest

from agentbridge import crypto
from agentbridge.core.timekit import next_ns, utcnow_iso
from agentbridge.mesh.events import signing_bytes
from agentbridge.mesh.paths import P
from agentbridge.mesh.pins import KeyPinStore, rekey_signing_bytes
from agentbridge.mesh.service import Mesh


@pytest.fixture
def world(clouds, tmp_path):
    """aryan / fable on their OWN homes (own keystores + own pin files) —
    the stand-in for two machines syncing one shared provider."""
    root = clouds.root(tmp_path / "mesh2")

    def mk(user):
        return Mesh(clouds.bare(root), user, "m1", encrypt=True,
                    home=tmp_path / f"home-{user}")

    for u in ("aryan", "fable"):
        m = mk(u)
        m.accounts.create_human(u, f"{u}-pass")
        m.close()

    meshes = {u: mk(u) for u in ("aryan", "fable")}
    yield meshes, root
    for m in meshes.values():
        m.close()


def ripple(sender, chat_id, *others):
    sender.outbox.flush_once()
    for m in (sender, *others):
        m.sync.sync_once([chat_id])


def keypair():
    bundle = crypto.generate_identity()
    return (bundle, *crypto.identity_pubs(bundle))


# ================================ KeyPinStore units =========================

def test_auto_verify_local_marks_only_matching_bundles(tmp_path):
    """R54 (V31): a pin whose PRIVATE bundle lives here (and matches) marks
    itself Verified; a foreign pin and a stale bundle mark nothing."""
    store = KeyPinStore(tmp_path, "rootX")
    a_bundle, a_sign, a_agree = keypair()
    b_bundle, b_sign, b_agree = keypair()
    _, c_sign, c_agree = keypair()
    store.trusted("mine", a_sign, a_agree)      # local bundle matches
    store.trusted("theirs", b_sign, b_agree)    # bundle lives elsewhere
    store.trusted("stale", c_sign, c_agree)     # local bundle != pinned keys
    bundles = {"mine": a_bundle, "stale": b_bundle}
    marked = store.auto_verify_local(bundles.get, crypto.identity_pubs)
    assert marked == ["mine"]
    assert store.verified("mine")
    assert not store.verified("theirs")
    assert not store.verified("stale")
    # idempotent: a second sweep marks nothing new
    assert store.auto_verify_local(bundles.get, crypto.identity_pubs) == []


def test_first_sight_pins_and_persists(tmp_path):
    store = KeyPinStore(tmp_path, "rootX")
    _, sign, agree = keypair()
    assert store.trusted("kim", sign, agree) == (sign, agree)
    # a fresh instance over the same file sees the pin
    again = KeyPinStore(tmp_path, "rootX")
    _, sign2, agree2 = keypair()
    assert again.trusted("kim", sign2, agree2) == (sign, agree)
    assert again.alerts()[0]["name"] == "kim"


def test_keyless_account_pins_nothing(tmp_path):
    store = KeyPinStore(tmp_path, "rootX")
    assert store.trusted("kim", "", "") == ("", "")
    _, sign, agree = keypair()   # keys published later (login upgrade shape)
    assert store.trusted("kim", sign, agree) == (sign, agree)
    assert store.alerts() == []  # keyless -> keyed is a first sight, no alert


def test_wiped_keys_fall_back_to_pin(tmp_path):
    store = KeyPinStore(tmp_path, "rootX")
    _, sign, agree = keypair()
    store.trusted("kim", sign, agree)
    assert store.trusted("kim", "", "") == (sign, agree)
    assert store.alerts()[0]["pinned_sign_pub"] == sign


def test_signed_history_advances_pin(tmp_path):
    store = KeyPinStore(tmp_path, "rootX")
    a_bundle, a_sign, a_agree = keypair()
    _, b_sign, b_agree = keypair()
    store.trusted("kim", a_sign, a_agree)
    ns = next_ns()
    hist = [{
        "old_sign_pub": a_sign, "sign_pub": b_sign, "agree_pub": b_agree,
        "ns": ns,
        "sig": crypto.sign(
            a_bundle, rekey_signing_bytes("kim", a_sign, b_sign, b_agree, ns)),
    }]
    assert store.trusted("kim", b_sign, b_agree, hist) == (b_sign, b_agree)
    assert store.alerts() == []
    # the pin MOVED: a further unsigned change now alerts against the new key
    _, c_sign, c_agree = keypair()
    assert store.trusted("kim", c_sign, c_agree) == (b_sign, b_agree)
    assert store.alerts()[0]["pinned_sign_pub"] == b_sign


def test_unsigned_history_never_advances(tmp_path):
    store = KeyPinStore(tmp_path, "rootX")
    _, a_sign, a_agree = keypair()
    b_bundle, b_sign, b_agree = keypair()
    store.trusted("kim", a_sign, a_agree)
    ns = next_ns()
    hist = [{  # signed by the NEW key, not the retiring one -> invalid
        "old_sign_pub": a_sign, "sign_pub": b_sign, "agree_pub": b_agree,
        "ns": ns,
        "sig": crypto.sign(
            b_bundle, rekey_signing_bytes("kim", a_sign, b_sign, b_agree, ns)),
    }]
    assert store.trusted("kim", b_sign, b_agree, hist) == (a_sign, a_agree)
    assert store.alerts()[0]["name"] == "kim"


def test_concurrent_stores_merge_pins(tmp_path):
    one = KeyPinStore(tmp_path, "rootX")
    two = KeyPinStore(tmp_path, "rootX")
    _, s1, a1 = keypair()
    _, s2, a2 = keypair()
    one.trusted("kim", s1, a1)
    two.trusted("lee", s2, a2)   # two's memory has no "kim" yet
    third = KeyPinStore(tmp_path, "rootX")
    _, sx, ax = keypair()
    assert third.trusted("kim", sx, ax) == (s1, a1)
    assert third.trusted("lee", sx, ax) == (s2, a2)


def test_ack_clears_alert_keeps_pin(tmp_path):
    store = KeyPinStore(tmp_path, "rootX")
    _, sign, agree = keypair()
    _, s2, a2 = keypair()
    store.trusted("kim", sign, agree)
    store.trusted("kim", s2, a2)
    assert store.alerts(unacked_only=True)
    store.ack("kim")
    assert store.alerts(unacked_only=True) == []
    assert store.trusted("kim", s2, a2) == (sign, agree)  # pin unmoved


# ============================== mesh integration ============================

def test_overwritten_keys_are_neutralized(world):
    """A provider writer replaces fable's published keys with their own pair.
    For aryan (who already knows fable) nothing changes: the pinned keys keep
    verifying fable's real messages, the attacker's signed-as-fable info event
    never folds, and an alert surfaces."""
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    chat = aryan.create_chat("Pins", members=["fable"])
    aryan.post(chat.id, "hello")
    ripple(aryan, chat.id, fable)
    assert fable.messages_for(chat.id)[-1].body == "hello"

    mallory, m_sign, m_agree = keypair()
    doc = aryan.tx.get_doc(P.user("fable"))
    doc["keys"] = {"sign_pub": m_sign, "agree_pub": m_agree}
    aryan.tx.put_doc(P.user("fable"), doc)

    # the directory keeps the pinned pair, not the published one
    acc = aryan.directory.get("fable")
    assert acc.keys.sign_pub != m_sign and acc.keys.agree_pub != m_agree

    # an info event signed with the REPLACEMENT key as fable never folds
    ns = next_ns()
    forged = {"id": "forge-ev", "ns": ns, "ts": utcnow_iso(), "from": "fable",
              "kind": "info",
              "event": {"type": "renamed", "name": "Owned", "by": "fable"}}
    forged["sig"] = crypto.sign(mallory, signing_bytes(chat.id, forged))
    aryan.tx.append_log(chat.id, "fable@rogue", forged)
    aryan.sync.sync_once([chat.id])
    assert aryan.membership.refold(chat.id).name == "Pins"

    # fable's genuine key still works end to end
    fable.post(chat.id, "still me")
    ripple(fable, chat.id, aryan)
    assert aryan.messages_for(chat.id)[-1].body == "still me"

    alerts = aryan.key_alerts()
    assert alerts and alerts[0]["name"] == "fable"


def test_epoch_wrap_uses_pinned_agree_key(world):
    """New epoch keys are wrapped to the PINNED agreement key: after a doc
    rewrite the real member still reads new messages and the replacement
    keypair cannot unwrap its copy."""
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    aryan.directory.get("fable")   # establish the pin before the rewrite

    mallory, m_sign, m_agree = keypair()
    doc = aryan.tx.get_doc(P.user("fable"))
    doc["keys"] = {"sign_pub": m_sign, "agree_pub": m_agree}
    aryan.tx.put_doc(P.user("fable"), doc)

    chat = aryan.create_chat("Sealed", members=["fable"])
    aryan.post(chat.id, "secret")
    ripple(aryan, chat.id, fable)
    assert fable.messages_for(chat.id)[-1].body == "secret"

    epoch_doc = aryan.keys.latest(chat.id)[1]
    wrapped = epoch_doc["wrapped"]["fable"]
    with pytest.raises(crypto.CryptoFail):
        crypto.unwrap_key_with(mallory, wrapped)


def test_provisioning_pins_immediately(world):
    """Signup pins the fresh keys on the creating machine before any read —
    and an account's own machine flags a rewrite of its own doc."""
    meshes, _ = world
    fable = meshes["fable"]
    assert fable.key_pins.alerts() == []

    _, m_sign, m_agree = keypair()
    doc = fable.tx.get_doc(P.user("fable"))
    doc["keys"] = {"sign_pub": m_sign, "agree_pub": m_agree}
    fable.tx.put_doc(P.user("fable"), doc)

    acc = fable.directory.get("fable")
    assert acc.keys.sign_pub != m_sign          # own pin holds
    assert fable.key_alerts()[0]["name"] == "fable"


# ================================= GUI surface ==============================

def test_state_carries_key_alerts_and_ack(rig):
    rig.signup("aryan")
    rig.peer_account("fable")
    assert rig.get("/api/mesh/state")["key_alerts"] == []

    # rewrite fable's published keys behind the GUI's back
    tx = rig.app.mesh.tx
    rig.app.mesh.directory.get("fable")   # pin first sight
    _, m_sign, m_agree = keypair()
    doc = tx.get_doc(P.user("fable"))
    doc["keys"] = {"sign_pub": m_sign, "agree_pub": m_agree}
    tx.put_doc(P.user("fable"), doc)
    rig.app.mesh.directory.get("fable")   # a read notices the change

    alerts = rig.get("/api/mesh/state")["key_alerts"]
    assert alerts and alerts[0]["name"] == "fable"
    assert rig.post("/api/mesh/key_alert_ack", name="fable")["ok"]
    assert rig.get("/api/mesh/state")["key_alerts"] == []


# ============================ R31: fingerprints =============================

def test_fingerprint_same_on_both_machines(world):
    """Both devices derive the same code for the same account, from the pin —
    the out-of-band comparison only works if the values agree."""
    meshes, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    # each side resolves the OTHER through its own pin store
    a_view = aryan.key_fingerprint("fable")["fingerprint"]
    f_view = fable.key_fingerprint("fable")["fingerprint"]
    assert a_view and a_view == f_view
    # 8 groups of 4 hex chars, human-readable
    groups = a_view.split(" ")
    assert len(groups) == 8 and all(len(g) == 4 for g in groups)


def test_fingerprint_tracks_the_pin_not_the_doc(world):
    """A hostile doc rewrite must not move the fingerprint a device shows —
    what you read aloud is what you actually trust."""
    meshes, _ = world
    aryan = meshes["aryan"]
    before = aryan.key_fingerprint("fable")["fingerprint"]

    _, sign, agree = keypair()
    doc = aryan.tx.get_doc(P.user("fable"))
    doc["keys"] = {"sign_pub": sign, "agree_pub": agree}
    aryan.tx.put_doc(P.user("fable"), doc)

    assert aryan.key_fingerprint("fable")["fingerprint"] == before


def test_mark_verified_round_trip(world):
    meshes, _ = world
    aryan = meshes["aryan"]
    assert aryan.key_fingerprint("fable")["verified"] == ""
    aryan.mark_key_verified("fable")
    assert aryan.key_fingerprint("fable")["verified"] != ""
    # a fresh store over the same file keeps it (machine-local, durable)
    fresh = KeyPinStore(aryan.home, str(aryan.tx.cache_key))
    assert fresh.verified("fable") != ""
