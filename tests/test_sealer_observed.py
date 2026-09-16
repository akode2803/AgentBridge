"""R216 crypto-only observed unseal parity and isolation tests."""
from __future__ import annotations

import json

import pytest

from agentbridge import crypto
from agentbridge.core.models import BodyRecord, Envelope
from agentbridge.mesh.sealer import E2EESealer


def _sealed(body=None):
    bundle = crypto.generate_identity()
    sign_pub, _agree = crypto.identity_pubs(bundle)
    key = crypto.new_chat_key()
    env = Envelope(id="m1", ns=9, from_="alice", epoch=3)
    aad = f"room|{env.id}|{env.ns}|{env.from_}|{env.epoch}".encode()
    payload = body or BodyRecord(
        body="secret", tags=["x"], files=[{"name": "a", "size": 1}],
        reply_to={"id": "p"},
    )
    env.nonce, env.ct = crypto.seal_bytes(
        key, aad, json.dumps(payload.to_dict()).encode(),
    )
    env.sig = crypto.sign(
        bundle, aad + b"|" + env.nonce.encode() + b"|" + env.ct.encode(),
    )
    return env, sign_pub, key, payload


def test_observed_crypto_parity_has_no_live_lookups_and_detaches_cache():
    env, sign_pub, key, expected = _sealed()

    class Forbidden:
        def __getattr__(self, name):
            pytest.fail(f"observed core attempted live I/O: {name}")

    sealer = E2EESealer(Forbidden(), Forbidden(), Forbidden(), "viewer", Forbidden())
    first = sealer.unseal_observed("room", env, sign_pub, key)
    assert first == expected
    first.tags.append("mutated")
    first.files[0]["name"] = "changed"
    second = sealer.unseal_observed("room", env, sign_pub, key)
    assert second == expected
    assert second is not first and second.files is not first.files


def test_live_unseal_preserves_directory_then_key_lookup_order():
    env, sign_pub, key, expected = _sealed(BodyRecord(body="ordered"))
    calls = []

    class Directory:
        def get(self, name):
            calls.append(("directory", name))
            return type("Account", (), {
                "keys": type("Keys", (), {"sign_pub": sign_pub})(),
            })()

    class Keys:
        def my_key(self, chat, epoch):
            calls.append(("key", chat, epoch))
            return key

    sealer = E2EESealer(None, Directory(), Keys(), "viewer", lambda: None)
    assert sealer.unseal("room", env) == expected
    assert calls == [("directory", "alice"), ("key", "room", 3)]
    # A crypto-cache hit still performs both live authority lookups.
    assert sealer.unseal("room", env) == expected
    assert calls[-2:] == [("directory", "alice"), ("key", "room", 3)]


@pytest.mark.parametrize("change", ["signature", "ciphertext", "nonce", "key", "signer"])
def test_observed_rejects_tampered_crypto_inputs(change):
    env, sign_pub, key, _expected = _sealed()
    if change == "signature":
        env.sig = env.sig[:-1] + ("A" if env.sig[-1] != "A" else "B")
    elif change == "ciphertext":
        env.ct = env.ct[:-1] + ("A" if env.ct[-1] != "A" else "B")
    elif change == "nonce":
        env.nonce = env.nonce[:-1] + ("A" if env.nonce[-1] != "A" else "B")
    elif change == "key":
        key = crypto.new_chat_key()
    else:
        sign_pub = crypto.identity_pubs(crypto.generate_identity())[0]
    sealer = E2EESealer(None, None, None, "viewer", lambda: None)
    assert sealer.unseal_observed("room", env, sign_pub, key) is None


@pytest.mark.parametrize("field,value", [
    ("epoch", 0), ("nonce", None), ("ct", 1), ("sig", False),
])
def test_observed_shape_gates_before_crypto(monkeypatch, field, value):
    env, sign_pub, key, _expected = _sealed()
    setattr(env, field, value)
    monkeypatch.setattr(
        crypto, "verify", lambda *a: pytest.fail("malformed envelope reached crypto"),
    )
    sealer = E2EESealer(None, None, None, "viewer", lambda: None)
    assert sealer.unseal_observed("room", env, sign_pub, key) is None


def test_observed_invalid_json_matches_canonical_empty_body_semantics():
    bundle = crypto.generate_identity()
    sign_pub, _agree = crypto.identity_pubs(bundle)
    key = crypto.new_chat_key()
    env = Envelope(id="bad-json", ns=4, from_="alice", epoch=2)
    aad = b"room|bad-json|4|alice|2"
    env.nonce, env.ct = crypto.seal_bytes(key, aad, b"not-json")
    env.sig = crypto.sign(bundle, aad + b"|" + env.nonce.encode() + b"|" + env.ct.encode())
    sealer = E2EESealer(None, None, None, "viewer", lambda: None)
    assert sealer.unseal_observed("room", env, sign_pub, key) is None
