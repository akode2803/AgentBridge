"""R1 spike: validate the D4/D5 E2EE design end-to-end with `cryptography`.

Prototypes the real R9 flow, not just imports:
  1. account identity = Ed25519 (signing) + X25519 (agreement) keypairs
  2. private keys wrapped at rest with a password-derived key (scrypt)
  3. per-chat symmetric key, wrapped for each member via X25519 ECDH -> HKDF
     -> ChaCha20Poly1305 AEAD
  4. message envelope: body encrypted under the chat key, signed by sender
  5. membership-change rotation: new chat key; removed member can't unwrap it
"""

import os
import sys

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def kdf_password(password: bytes, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password)


def wrap_for(recipient_pub: X25519PublicKey, plaintext: bytes) -> dict:
    """Wrap bytes for a recipient: ephemeral X25519 ECDH -> HKDF -> AEAD."""
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(recipient_pub)
    kek = HKDF(algorithm=SHA256(), length=32, salt=None, info=b"ab.keywrap.v1").derive(shared)
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(kek).encrypt(nonce, plaintext, None)
    return {
        "eph_pub": eph.public_key().public_bytes_raw(),
        "nonce": nonce,
        "ct": ct,
    }


def unwrap_with(recipient_priv: X25519PrivateKey, wrapped: dict) -> bytes:
    shared = recipient_priv.exchange(X25519PublicKey.from_public_bytes(wrapped["eph_pub"]))
    kek = HKDF(algorithm=SHA256(), length=32, salt=None, info=b"ab.keywrap.v1").derive(shared)
    return ChaCha20Poly1305(kek).decrypt(wrapped["nonce"], wrapped["ct"], None)


def main() -> None:
    # 1. Identity keys for three members (alice, bob, eve-the-removed).
    ids = {}
    for name in ("alice", "bob", "eve"):
        ids[name] = {
            "sign": Ed25519PrivateKey.generate(),
            "agree": X25519PrivateKey.generate(),
        }

    # 2. Password-wrap alice's agreement key at rest (the D5 recovery model).
    salt = os.urandom(16)
    pw_key = kdf_password(b"alice-password", salt)
    raw_priv = ids["alice"]["agree"].private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    nonce = os.urandom(12)
    stored = ChaCha20Poly1305(pw_key).encrypt(nonce, raw_priv, None)
    # ... sign-in on a new device: derive the same key, unwrap.
    recovered = ChaCha20Poly1305(kdf_password(b"alice-password", salt)).decrypt(nonce, stored, None)
    assert recovered == raw_priv, "password unwrap mismatch"
    try:  # wrong password must fail hard
        ChaCha20Poly1305(kdf_password(b"wrong", salt)).decrypt(nonce, stored, None)
        raise AssertionError("wrong password unwrapped the key!")
    except InvalidTag:
        pass

    # 3. Chat key v1 wrapped for all three members.
    chat_key_v1 = os.urandom(32)
    wrapped_v1 = {
        n: wrap_for(ids[n]["agree"].public_key(), chat_key_v1) for n in ("alice", "bob", "eve")
    }
    for n in ("alice", "bob", "eve"):
        assert unwrap_with(ids[n]["agree"], wrapped_v1[n]) == chat_key_v1

    # 4. Envelope: alice posts a message under the chat key, signed.
    body = "the quarterly numbers are in the shared sheet".encode()
    n1 = os.urandom(12)
    aad = b"chat:general|from:alice|ns:1234"  # routing metadata is authenticated, not secret
    env_ct = ChaCha20Poly1305(chat_key_v1).encrypt(n1, body, aad)
    sig = ids["alice"]["sign"].sign(env_ct)
    # bob verifies + decrypts
    ids["alice"]["sign"].public_key().verify(sig, env_ct)
    assert ChaCha20Poly1305(chat_key_v1).decrypt(n1, env_ct, aad) == body
    # tampered signature must fail
    try:
        ids["alice"]["sign"].public_key().verify(b"\x00" * 64, env_ct)
        raise AssertionError("bad signature verified!")
    except InvalidSignature:
        pass

    # 5. Eve is removed -> rotate: new chat key wrapped only for alice+bob.
    chat_key_v2 = os.urandom(32)
    wrapped_v2 = {n: wrap_for(ids[n]["agree"].public_key(), chat_key_v2) for n in ("alice", "bob")}
    assert "eve" not in wrapped_v2
    # eve still holds v1 (old history readable - WhatsApp semantics), but a new
    # message under v2 is opaque to her:
    n2 = os.urandom(12)
    post_removal = ChaCha20Poly1305(chat_key_v2).encrypt(n2, b"eve is gone", aad)
    try:
        ChaCha20Poly1305(chat_key_v1).decrypt(n2, post_removal, aad)  # her best guess
        raise AssertionError("removed member read a post-rotation message!")
    except InvalidTag:
        pass
    assert ChaCha20Poly1305(unwrap_with(ids["bob"]["agree"], wrapped_v2["bob"])).decrypt(
        n2, post_removal, aad
    ) == b"eve is gone"

    print("OK smoke_crypto: identity keys, password wrap+recovery, per-member")
    print("   chat-key wrap, signed envelope, rotation-on-removal all verified")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_crypto: {type(e).__name__}: {e}")
        sys.exit(1)
