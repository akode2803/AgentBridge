"""P2.2 A regressions for authenticated E2EE unseal-cache admission."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge import crypto
from agentbridge.core.models import BodyRecord, Envelope
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def encrypted_pair(tmp_path):
    """Two identities with separate keystores over one real folder mesh."""
    root = tmp_path / "mesh"

    def make(user: str) -> Mesh:
        return Mesh(
            FolderTransport(root),
            user,
            "cache-test",
            encrypt=True,
            home=tmp_path / f"home-{user}",
        )

    for user in ("alice", "bob"):
        mesh = make(user)
        mesh.accounts.create_human(user, "scratch-password")
        mesh.close()

    alice, bob = make("alice"), make("bob")
    yield alice, bob
    alice.close()
    bob.close()


def _seed(encrypted_pair, **post_fields):
    alice, bob = encrypted_pair
    chat = alice.create_chat("Sealer cache", members=["bob"])
    posted = alice.post(chat.id, "authentic body", **post_fields)
    alice.outbox.flush_once()
    bob.sync.sync_once([chat.id])
    rows = bob.store.messages(chat.id)
    env = Envelope.from_dict(next(row for row in rows if row["id"] == posted.id))
    return chat, env, rows


def _assert_body(bob: Mesh, chat_id: str, env: Envelope) -> BodyRecord:
    record = bob.sealer.unseal(chat_id, env)
    assert record is not None
    assert record.body == "authentic body"
    return record


def test_sender_substitution_is_rejected_cold_and_after_cache_warm(encrypted_pair):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    forged = replace(env, from_="bob")

    assert bob.sealer.unseal(chat.id, forged) is None
    _assert_body(bob, chat.id, env)
    assert bob.sealer.unseal(chat.id, forged) is None


def test_messages_for_rejects_sender_substitution_after_store_rebuild(encrypted_pair):
    _, bob = encrypted_pair
    chat, env, original_rows = _seed(encrypted_pair)
    assert next(m for m in bob.messages_for(chat.id) if m.id == env.id).body \
        == "authentic body"

    bob.store.forget_chat(chat.id)
    bob.store.upsert_messages(chat.id, [
        {**row, "from": "bob"} if row["id"] == env.id else row
        for row in original_rows
    ])

    projected = next(m for m in bob.messages_for(chat.id) if m.id == env.id)
    assert projected.from_ == "bob"
    assert projected.body == ""
    assert projected.undecrypted is True


@pytest.mark.parametrize(
    "field",
    ["chat_id", "id", "ns", "epoch", "nonce", "ct", "sig"],
)
def test_authenticated_input_substitution_is_rejected_cold_and_warm(
    encrypted_pair, field,
):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    forged_chat_id = chat.id
    forged = env
    if field == "chat_id":
        forged_chat_id = f"{chat.id}-substituted"
    elif field == "id":
        forged = replace(env, id=f"{env.id}-substituted")
    elif field == "ns":
        forged = replace(env, ns=env.ns + 1)
    elif field == "epoch":
        forged = replace(env, epoch=env.epoch + 1)
    elif field == "nonce":
        forged = replace(env, nonce=crypto.b64e(b"\x00" * 12))
    elif field == "ct":
        forged = replace(
            env, ct=crypto.b64e(b"\x00" * len(crypto.b64d(env.ct))),
        )
    else:
        forged = replace(
            env, sig=crypto.b64e(b"\x00" * len(crypto.b64d(env.sig))),
        )

    assert bob.sealer.unseal(forged_chat_id, forged) is None
    _assert_body(bob, chat.id, env)
    assert bob.sealer.unseal(forged_chat_id, forged) is None


@pytest.mark.parametrize("malformed_sig", [None, 7])
def test_malformed_signature_fails_closed_cold_and_warm(
    encrypted_pair, malformed_sig,
):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    forged = replace(env, sig=malformed_sig)

    assert bob.sealer.unseal(chat.id, forged) is None
    _assert_body(bob, chat.id, env)
    assert bob.sealer.unseal(chat.id, forged) is None


def test_identity_loss_respects_resident_key_then_recovers_without_negative_cache(
    encrypted_pair,
):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    _assert_body(bob, chat.id, env)
    slot = (chat.id, env.epoch)
    assert slot in bob.keys._cache
    bundle = bob.keystore.load("bob")
    assert bundle is not None

    bob.keystore.forget("bob")
    _assert_body(bob, chat.id, env)
    bob.sealer._cache.clear()
    _assert_body(bob, chat.id, env)

    bob.keys._cache.pop(slot)
    assert bob.sealer.unseal(chat.id, env) is None
    bob.sealer._cache.clear()
    assert bob.sealer.unseal(chat.id, env) is None

    bob.keystore.save("bob", bundle)
    _assert_body(bob, chat.id, env)


@pytest.mark.parametrize("doc", [
    [], {"wrapped": []}, {"wrapped": None}, {"wrapped": {"bob": 7}},
    {"wrapped": {"bob": {"eph": None, "nonce": "", "ct": ""}}},
])
def test_malformed_key_document_fails_closed_and_recovers(encrypted_pair, doc):
    from agentbridge.mesh.paths import P

    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    _assert_body(bob, chat.id, env)
    path = P.keys(chat.id, env.epoch)
    original = bob.tx.get_doc(path)
    bob.keys._cache.clear()
    bob.tx.put_doc(path, doc)
    forged = replace(env, sig=crypto.b64e(b"\x00" * 64))
    assert bob.sealer.unseal(chat.id, forged) is None
    assert bob.sealer.unseal(chat.id, env) is None
    bob.sealer._cache.clear()
    assert bob.sealer.unseal(chat.id, env) is None
    bob.tx.put_doc(path, original)
    _assert_body(bob, chat.id, env)


def test_effective_epoch_key_change_invalidates_hit_and_recovery_retries(
    encrypted_pair,
):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    _assert_body(bob, chat.id, env)
    slot = (chat.id, env.epoch)
    genuine_key = bob.keys._cache[slot]

    bob.keys._cache[slot] = crypto.new_chat_key()
    assert bob.sealer.unseal(chat.id, env) is None
    bob.sealer._cache.clear()
    assert bob.sealer.unseal(chat.id, env) is None

    bob.keys._cache[slot] = genuine_key
    _assert_body(bob, chat.id, env)


@pytest.mark.parametrize("trust_change", ["missing", "changed_pub"])
def test_current_signer_trust_is_rechecked_before_hit(
    encrypted_pair, monkeypatch, trust_change,
):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    _assert_body(bob, chat.id, env)
    original_get = bob.directory.get
    alice = original_get("alice")
    bob_account = original_get("bob")
    assert alice is not None and bob_account is not None

    replacement = None
    if trust_change == "changed_pub":
        replacement = replace(
            alice,
            keys=replace(alice.keys, sign_pub=bob_account.keys.sign_pub),
        )

    def changed_get(name: str):
        if name == "alice":
            return replacement
        return original_get(name)

    monkeypatch.setattr(bob.directory, "get", changed_get)
    assert bob.sealer.unseal(chat.id, env) is None
    bob.sealer._cache.clear()
    assert bob.sealer.unseal(chat.id, env) is None


def test_inactive_historical_sender_still_verifies_on_cold_path(
    encrypted_pair, monkeypatch,
):
    _, bob = encrypted_pair
    chat, env, _ = _seed(encrypted_pair)
    sender = bob.directory.get("alice")
    assert sender is not None
    inactive_sender = replace(
        sender, active=False, deactivated="2026-09-06T00:00:00Z",
    )
    original_get = bob.directory.get

    monkeypatch.setattr(
        bob.directory,
        "get",
        lambda name: inactive_sender if name == "alice" else original_get(name),
    )
    bob.sealer._cache.clear()
    _assert_body(bob, chat.id, env)


def test_unseal_results_are_deeply_detached_on_miss_and_hit(encrypted_pair):
    _, bob = encrypted_pair
    metadata = {
        "tags": ["alice"],
        "files": [{"name": "notes.txt", "meta": {"pages": [1, 2]}}],
        "reply_to": {"id": "parent", "preview": {"tags": ["original"]}},
        "fwd": {"from": "carol", "trail": [{"id": "source"}]},
    }
    chat, env, _ = _seed(encrypted_pair, **metadata)
    expected = {"body": "authentic body", **metadata}

    bob.sealer._cache.clear()
    first = _assert_body(bob, chat.id, env)
    assert first.to_dict() == expected
    first.tags.append("mutated")
    first.files[0]["meta"]["pages"].append(3)
    first.reply_to["preview"]["tags"].append("mutated")
    first.fwd["trail"][0]["id"] = "mutated"

    hit = _assert_body(bob, chat.id, env)
    assert hit.to_dict() == expected
    hit.tags.clear()
    hit.files[0]["meta"]["pages"].clear()
    hit.reply_to["preview"]["tags"].clear()
    hit.fwd["trail"].clear()

    assert _assert_body(bob, chat.id, env).to_dict() == expected
