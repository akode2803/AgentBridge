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


# ------------------------------------------- migrated history after sealing

def _legacy_line(sender, ns, body):
    """An epoch-0 message line the way the migrator writes them."""
    return {
        "id": f"m-{ns}-feedbeef", "ns": ns, "ts": "2026-07-01T00:00:00Z",
        "from": sender, "kind": "message", "epoch": 0, "nonce": "",
        "ct": json.dumps({"body": body}), "sig": "",
    }


def test_migrated_history_survives_first_sealed_post(world):
    """A legacy (migrated) chat keeps its epoch-0 history — messages AND plain
    v1 file blobs — readable after the room's first sealed post mints an
    epoch. Plaintext minted AFTER that moment is still refused."""
    from agentbridge.core.models import MsgKind
    from agentbridge.core.timekit import next_ns

    meshes, _, _, root = world
    aryan, fable = meshes["aryan"], meshes["fable"]
    tx = FolderTransport(root)
    chat_id = "team-room"  # migrated v1 id: no -g genesis marker

    # migrated shape: meta snapshot + legacy genesis + epoch-0 message lines
    tx.put_doc(f"chats/{chat_id}/meta.json", {
        "id": chat_id, "kind": "group", "name": "Team room",
        "members": {"aryan": {"role": "admin", "joined_ns": 1},
                    "fable": {"role": "member", "joined_ns": 1}},
    })
    genesis = {"id": "m-1-00000001", "ns": 1, "ts": "2026-07-01T00:00:00Z",
               "from": "aryan", "kind": "info",
               "event": {"type": "created", "kind": "group",
                         "name": "Team room", "creator": "aryan",
                         "members": {"aryan": "admin", "fable": "member"}}}
    tx.append_log(chat_id, "aryan@migrated", genesis)
    tx.append_log(chat_id, "aryan@migrated", _legacy_line("aryan", 1000, "hello from v1"))
    tx.append_log(chat_id, "fable@migrated", _legacy_line("fable", 2000, "old reply"))
    tx.put_blob(f"chats/{chat_id}/files/report.csv", b"a,b\n1,2\n")  # v1 file

    for m in (aryan, fable):
        m.sync.sync_once([chat_id])
    assert [m.body for m in fable.messages_for(chat_id)
            if m.kind is MsgKind.MESSAGE] == ["hello from v1", "old reply"]

    # the first sealed post mints the chat's first epoch
    aryan.post(chat_id, "sealed now")
    ripple(aryan, chat_id, fable)
    assert aryan.keys.first_epoch(chat_id) is not None
    assert [m.body for m in fable.messages_for(chat_id)
            if m.kind is MsgKind.MESSAGE] == ["hello from v1", "old reply",
                                              "sealed now"]

    # the plain v1 blob (no v2 id) still opens; a plain blob whose id is
    # minted after sealing does not
    assert fable.sealer.open_blob(chat_id, "report.csv", b"a,b\n1,2\n") \
        == b"a,b\n1,2\n"
    assert fable.sealer.open_blob(chat_id, f"f-{next_ns()}-abcd.bin", b"x") is None

    # plaintext minted AFTER the room sealed is refused, not displayed
    late = _legacy_line("fable", next_ns(), "post-seal plaintext")
    tx.append_log(chat_id, "fable@migrated", late)
    aryan.sync.sync_once([chat_id])
    sneaky = [m for m in aryan.messages_for(chat_id) if m.id == late["id"]][0]
    assert sneaky.body == ""


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
