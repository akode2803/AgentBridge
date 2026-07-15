"""End-to-end encryption (R9): sealed envelopes, epochs, rotation, recovery.

These exercise the real E2EESealer over the folder transport with per-identity
keystores — the closest unit-level analogue to two machines sharing a folder.
"""

import json

import pytest

from agentbridge import crypto
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def world(tmp_path):
    """Each identity gets its OWN home (its own keystore) — a faithful stand-in
    for separate machines syncing one folder."""
    root = tmp_path / "mesh2"
    homes: dict[str, object] = {}

    def mk(user, machine="m1"):
        home = tmp_path / f"home-{user}"
        homes[user] = home
        return Mesh(FolderTransport(root), user, machine, encrypt=True, home=home)

    # bootstrap accounts, each on its own mesh so keys land in its own keystore
    recovery: dict[str, str] = {}
    for u in ("aryan", "fable", "sudhir"):
        m = mk(u)
        _, code = m.accounts.create_human(u, f"{u}-pass")
        recovery[u] = code
        m.close()

    meshes = {u: mk(u) for u in ("aryan", "fable", "sudhir")}
    yield meshes, mk, recovery, root
    for m in meshes.values():
        m.close()


def ripple(sender, chat_id, *others):
    sender.outbox.flush_once()
    for m in (sender, *others):
        m.sync.sync_once([chat_id])


# --------------------------------------------------------------- ciphertext

def test_body_is_ciphertext_on_disk(world):
    meshes, _, _, root = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    chat = aryan.create_chat("Secret", members=["fable"])
    aryan.post(chat.id, "the launch code is hunter2")
    aryan.outbox.flush_once()

    # read the raw jsonl straight off the transport — no plaintext anywhere
    raw = FolderTransport(root).read_log(chat.id, "aryan@m1")[0]
    blob = json.dumps(raw)
    assert "hunter2" not in blob and "launch code" not in blob
    msg = next(r for r in raw if r.get("kind") == "message")
    assert msg["epoch"] > 0 and msg["ct"] and msg["sig"]

    # but a member decrypts it fine
    ripple(aryan, chat.id, fable)
    assert fable.messages_for(chat.id)[-1].body == "the launch code is hunter2"


def test_roundtrip_and_signature_tamper_detected(world):
    meshes, _, _, root = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    chat = aryan.create_chat("Signed", members=["fable"])
    env = aryan.post(chat.id, "authentic message")
    ripple(aryan, chat.id, fable)
    assert fable.messages_for(chat.id)[-1].body == "authentic message"

    # forge the ciphertext in place -> AEAD/sig fails -> reader shows nothing,
    # never a wrong plaintext
    tx = FolderTransport(root)
    recs = tx.read_log(chat.id, "aryan@m1")[0]
    for r in recs:
        if r["id"] == env.id:
            r["ct"] = crypto.b64e(b"\x00" * len(crypto.b64d(r["ct"])))
    p = tx.local_path(f"chats/{chat.id}/msgs/aryan@m1.jsonl")
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    fable.store.forget_chat(chat.id)
    fable.sync.sync_once([chat.id])
    tampered = [m for m in fable.messages_for(chat.id) if m.id == env.id][0]
    assert tampered.body == ""  # unopenable -> blank, not the old text


def test_non_member_cannot_decrypt(world):
    meshes, _, _, root = world
    aryan, sudhir = meshes["aryan"], meshes["sudhir"]
    chat = aryan.create_chat("Members only", members=["fable"])
    env = aryan.post(chat.id, "not for sudhir")
    aryan.outbox.flush_once()

    # sudhir reads raw ciphertext off the shared folder and has his own keys,
    # but the epoch key was never wrapped for him -> cannot open
    recs = FolderTransport(root).read_log(chat.id, "aryan@m1")[0]
    epoch = next(r["epoch"] for r in recs if r["id"] == env.id)
    key = sudhir.keys.my_key(chat.id, epoch)
    assert key is None


# ------------------------------------------------------------ epoch rotation

def test_removed_member_keeps_history_loses_future(world):
    """WhatsApp/Signal crypto semantics. Note: at the APP layer a removed
    member can't read the chat at all (visibility=membership), so this is a
    KEY-level assertion — the ex-member holds the old epoch key but not the
    new one, so old ciphertext stays openable to them while new does not."""
    meshes, _, _, _ = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    group = aryan.create_chat("Rotation", members=["fable", "sudhir"])
    aryan.post(group.id, "before fable is removed")
    ripple(aryan, group.id, fable)
    fable.sync.sync_once([group.id])
    old_epoch = aryan.keys.latest(group.id)[0]
    assert fable.keys.my_key(group.id, old_epoch) is not None  # fable holds it

    aryan.remove_member(group.id, "fable")     # rotates the epoch
    aryan.post(group.id, "after fable is gone")
    new_epoch = aryan.keys.latest(group.id)[0]
    assert new_epoch != old_epoch
    fable.keys._cache.clear()
    assert fable.keys.my_key(group.id, old_epoch) is not None  # history: kept
    assert fable.keys.my_key(group.id, new_epoch) is None      # future: opaque


def test_leave_rotates_the_epoch_away_from_the_leaver(world):
    """R69: leaving rotates the chat key away from the departing member, so a
    message posted after they leave is sealed under an epoch they were never
    wrapped into — E2EE no longer leans on the app-level membership gate.
    The leaver's own rotation is created ``by`` them, so ensure() re-keys on
    a remaining member's next post (departed-creator distrust)."""
    meshes, _, _, _ = world
    aryan, fable, sudhir = meshes["aryan"], meshes["fable"], meshes["sudhir"]
    group = aryan.create_chat("Exit", members=["fable", "sudhir"])
    aryan.post(group.id, "before fable leaves")
    ripple(aryan, group.id, fable, sudhir)
    old_epoch = aryan.keys.latest(group.id)[0]
    assert fable.keys.my_key(group.id, old_epoch) is not None   # fable held it

    fable.sync.sync_once([group.id])
    fable.leave(group.id)                        # R69: rotates on the way out
    ripple(fable, group.id, aryan, sudhir)
    # the leave-rotation already excludes fable and is stamped by the leaver
    left_epoch, left_doc = aryan.keys.latest(group.id)
    assert left_epoch != old_epoch and "fable" not in left_doc["wrapped"]
    assert left_doc["by"] == "fable"

    env = aryan.post(group.id, "after fable left")  # ensure() re-keys (by gone)
    ripple(aryan, group.id, sudhir)
    new_epoch, new_doc = aryan.keys.latest(group.id)
    assert new_epoch != left_epoch and new_doc["by"] == "aryan"
    assert "fable" not in new_doc["wrapped"]
    fable.keys._cache.clear()
    assert fable.keys.my_key(group.id, env.epoch) is None       # future: opaque
    assert fable.keys.my_key(group.id, old_epoch) is not None   # history: kept
    assert sudhir.keys.my_key(group.id, env.epoch) is not None  # remaining reads


def test_ensure_distrusts_an_epoch_from_a_departed_creator(world):
    """R69 defense-in-depth: even when the newest epoch's wrapped set matches
    the current members, ensure() re-keys if its CREATOR is no longer a
    member — so a key a leaver minted (and might have kept) is never trusted
    as the current epoch once a remaining member posts."""
    meshes, _, _, root = world
    aryan, fable, sudhir = meshes["aryan"], meshes["fable"], meshes["sudhir"]
    group = aryan.create_chat("Distrust", members=["fable", "sudhir"])
    aryan.post(group.id, "seed")
    ripple(aryan, group.id, fable, sudhir)
    ep, doc = aryan.keys.latest(group.id)
    # forge the newest epoch to look member-exact but authored by (soon to be
    # removed) fable — only the departed-creator check can catch this
    aryan.remove_member(group.id, "fable")
    ep2, doc2 = aryan.keys.latest(group.id)      # remove_member rotated
    forged = dict(doc2); forged["by"] = "fable"  # a departed member's stamp
    FolderTransport(root).put_doc(P.keys(group.id, ep2), forged)
    aryan.keys._cache.clear()
    env = aryan.post(group.id, "after the plant")  # ensure() must re-key
    latest_epoch, latest_doc = aryan.keys.latest(group.id)
    assert env.epoch == latest_epoch and latest_epoch != ep2
    assert latest_doc["by"] == "aryan"


def test_ensure_heals_after_clobbered_rotation(world):
    """Two removals could race on the epoch file; ensure() guarantees the
    NEXT message is sealed under a members-only key regardless."""
    meshes, _, _, root = world
    aryan = meshes["aryan"]
    group = aryan.create_chat("Race", members=["fable", "sudhir"])
    aryan.post(group.id, "seed")             # mints epoch 1 for all three
    aryan.remove_member(group.id, "sudhir")

    # simulate a stale writer clobbering the latest epoch back to the old
    # 3-member wrap (the race): copy epoch1's doc over the newest epoch
    tx = FolderTransport(root)
    eps = aryan.keys.epochs(group.id)
    first = tx.get_doc(P.keys(group.id, eps[0][0]))
    tx.put_doc(P.keys(group.id, eps[-1][0]), first)   # newest now wraps sudhir

    env = aryan.post(group.id, "post-race message")   # ensure() must re-rotate
    latest_epoch, doc = aryan.keys.latest(group.id)
    assert env.epoch == latest_epoch
    assert "sudhir" not in doc["wrapped"]             # healed to members only


def test_history_on_join_off_is_cryptographic(world):
    meshes, _, _, _ = world
    aryan, sudhir = meshes["aryan"], meshes["sudhir"]
    group = aryan.create_chat("NoHistory")
    old = aryan.post(group.id, "pre-join secret")
    aryan.set_permissions(group.id, {"send_history": False})
    aryan.add_members(group.id, ["sudhir"])   # rotates; sudhir gets only new
    fresh = aryan.post(group.id, "post-join visible")
    ripple(aryan, group.id, sudhir)

    bodies = {m.id: m.body for m in sudhir.messages_for(group.id)
              if m.kind.value == "message"}
    # app-layer filter hides pre-join; even if it didn't, sudhir has no key
    old_epoch = next(e for e, _ in aryan.keys.epochs(group.id))
    assert sudhir.keys.my_key(group.id, old_epoch) is None
    assert bodies.get(fresh.id) == "post-join visible"
    assert old.id not in bodies


# ---------------------------------------------------------- keys & recovery

def test_locked_identity_cannot_send(world):
    meshes, _, _, root = world
    aryan = meshes["aryan"]
    chat = aryan.create_chat("Locked test", members=["fable"])
    # a fresh device for aryan that never unlocked (no bundle in its keystore)
    fresh = Mesh(FolderTransport(root), "aryan", "phone",
                 encrypt=True, home=_new_home(aryan))
    try:
        with pytest.raises(crypto.CryptoFail):
            fresh.post(chat.id, "should fail — keys locked")
    finally:
        fresh.close()


def test_password_change_and_recovery_reunlock(world):
    meshes, mk, recovery, root = world
    aryan = meshes["aryan"]

    # change password: identity re-wraps, a fresh device unlocks with the new
    aryan.accounts.change_password("aryan-pass", "brand-new-pass")
    dev2 = Mesh(FolderTransport(root), "aryan", "dev2", encrypt=True,
                home=_new_home(aryan))
    try:
        assert dev2.accounts.unlock("brand-new-pass") is True
        assert dev2.accounts.unlock("aryan-pass") is False
    finally:
        dev2.close()

    # recovery code still works (untouched by the password change) and
    # unlocks on a brand-new device
    dev3 = Mesh(FolderTransport(root), "aryan", "dev3", encrypt=True,
                home=_new_home(aryan))
    try:
        assert dev3.accounts.unlock_with_recovery(recovery["aryan"]) is True
        assert dev3.accounts.unlock_with_recovery("wrong-code") is False
    finally:
        dev3.close()


# --- a fresh home = a fresh device with an empty keystore ------------------

_HOME_SEQ = {"n": 0}


def _new_home(mesh):
    _HOME_SEQ["n"] += 1
    return mesh.home.parent / f"fresh-device-{_HOME_SEQ['n']}"


# -------------------------------------------------------------- blobs (R13)

def test_blob_seal_roundtrip_and_injection_rules(world):
    meshes, mk, recovery, root = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    snap = aryan.create_chat("Files", members=["fable"])
    ripple(aryan, snap.id, fable)

    data = b"attachment bytes " * 100
    sealed = aryan.sealer.seal_blob(snap.id, "f-1.bin", data)
    assert sealed.startswith(b"AB2E") and data not in sealed

    # both members open it; an id swap refuses (AAD binds the blob id)
    assert aryan.sealer.open_blob(snap.id, "f-1.bin", sealed) == data
    assert fable.sealer.open_blob(snap.id, "f-1.bin", sealed) == data
    assert fable.sealer.open_blob(snap.id, "f-2.bin", sealed) is None

    # a PLAIN blob dropped into a sealed room is refused (injection rule)
    assert aryan.sealer.open_blob(snap.id, "f-3.bin", b"plain bytes") is None

    # a non-member can't open the blob at all (no wrapped epoch copy)
    assert meshes["sudhir"].sealer.open_blob(snap.id, "f-1.bin", sealed) is None


# ---------------------------------------------- no plaintext, no legacy ids

def _plain_line(sender, ns, body):
    """An epoch-0 message line (the retired migration era's shape)."""
    return {
        "id": f"m-{ns}-feedbeef", "ns": ns, "ts": "2026-07-01T00:00:00Z",
        "from": sender, "kind": "message", "epoch": 0, "nonce": "",
        "ct": json.dumps({"body": body}), "sig": "",
    }


def test_plaintext_and_plain_blobs_never_open(world):
    """R16.5: the migrated era is over — an epoch-0 envelope reads as
    nothing everywhere (even in a chat with no epochs yet), and plain bytes
    are never served as chat files."""
    from agentbridge.core.timekit import next_ns

    meshes, _, _, root = world
    aryan = meshes["aryan"]
    tx = FolderTransport(root)
    chat = aryan.create_chat("Sealed room", members=["fable"])

    line = _plain_line("fable", next_ns(), "plaintext line")
    tx.append_log(chat.id, "fable@m1", line)
    aryan.sync.sync_once([chat.id])
    injected = [m for m in aryan.messages_for(chat.id) if m.id == line["id"]][0]
    assert injected.body == ""

    tx.put_blob(f"chats/{chat.id}/files/report.csv", b"a,b\n1,2\n")
    assert aryan.sealer.open_blob(chat.id, "report.csv", b"a,b\n1,2\n") is None


# ----------------------------------------------- R13.5 signed info events

def test_signature_blocks_impersonated_admin_grant(world):
    """A member cannot self-promote by forging an admin_granted attributed to
    a real admin: it lands (from==log-owner passes ingestion) but the fold
    demands that admin's signature, which the forger cannot produce."""
    meshes, _, _, root = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    snap = aryan.create_chat("Keys", members=["fable", "sudhir"])
    ripple(aryan, snap.id, fable)

    forged = {"id": "forge-1", "ns": 10**18, "ts": "t", "from": "aryan",
              "kind": "info",
              "event": {"type": "admin_granted", "who": "fable", "by": "aryan"}}
    # from==owner so ingestion passes; NO valid sig (fable can't sign as aryan)
    aryan.tx.append_log(snap.id, "aryan@rogue", forged)
    aryan.sync.sync_once([snap.id])
    healed = aryan.membership.refold(snap.id)
    assert healed.members["fable"].role.value == "member"  # forged grant ignored


def test_tampered_signed_event_rejected(world):
    """Aryan's genuine signed grant, cloned with the target flipped, keeps a
    now-stale signature — the fold ignores it, the genuine one stands."""
    meshes, _, _, root = world
    aryan = meshes["aryan"]
    snap = aryan.create_chat("Tamper", members=["fable", "sudhir"])
    aryan.grant_admin(snap.id, "fable")   # a REAL signed admin_granted
    ripple(aryan, snap.id)

    genuine = next(r for r in aryan.store.messages(snap.id)
                   if (r.get("event") or {}).get("type") == "admin_granted")
    assert genuine.get("sig")   # it really is signed
    tampered = {**genuine, "id": "tampered-1",
                "event": {**genuine["event"], "who": "sudhir"}}
    aryan.tx.append_log(snap.id, "aryan@rogue", tampered)
    aryan.sync.sync_once([snap.id])
    healed = aryan.membership.refold(snap.id)
    assert healed.members["sudhir"].role.value == "member"  # tamper ignored
    assert healed.members["fable"].role.value == "admin"    # genuine held


def test_signed_event_cannot_replay_into_another_chat(world):
    """A signature binds to ONE chat (chat id is in the signed bytes): aryan's
    grant in chat A copied into chat B fails B's verification."""
    meshes, _, _, root = world
    aryan = meshes["aryan"]
    a = aryan.create_chat("A", members=["fable", "sudhir"])
    b = aryan.create_chat("B", members=["fable", "sudhir"])
    aryan.grant_admin(a.id, "fable")
    ripple(aryan, a.id)

    grant = next(r for r in aryan.store.messages(a.id)
                 if (r.get("event") or {}).get("type") == "admin_granted")
    aryan.tx.append_log(b.id, "aryan@rogue", {**grant, "id": "replay-1"})
    aryan.sync.sync_once([b.id])
    healed = aryan.membership.refold(b.id)
    assert healed.members["fable"].role.value == "member"  # replay rejected in B


def test_genuine_signed_events_fold_normally(world):
    """Sanity: with real keys, the ordinary signed path still works end to end
    (a rename + a grant both land) — the integrity gate isn't over-tight."""
    meshes, _, _, root = world
    aryan = meshes["aryan"]
    snap = aryan.create_chat("Normal", members=["fable"])
    aryan.rename(snap.id, "Renamed")
    aryan.grant_admin(snap.id, "fable")
    healed = aryan.membership.refold(snap.id)
    assert healed.name == "Renamed"
    assert healed.members["fable"].role.value == "admin"
