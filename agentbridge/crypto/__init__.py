"""Policy-free crypto primitives (R9, D4/D5).

There is no transport, service, storage-authority, or persistent state here.
The module retains only a private bounded process-local memo of successful
Ed25519 verification digests; trust and key selection remain with callers.

Validated end-to-end by spikes/r1/smoke_crypto.py before a line of this
shipped. Everything here is bytes-in/bytes-out; the mesh layer (keyring,
sealer) owns storage and policy.

Key hierarchy:
  identity bundle (64B = Ed25519 sign priv || X25519 agree priv)
    ├─ published:   sign_pub + agree_pub (account doc)
    ├─ at rest:     password-wrapped (scrypt→ChaCha20Poly1305) + recovery-code
    │               wrapped copies in the account doc (humans; D5)
    └─ unlocked:    bundle in ~/.agentbridge/keys/ — DPAPI-wrapped on Windows
                    (R31.5, crypto/dpapi.py); plain (OS-user boundary) elsewhere
  chat epoch key (32B random per epoch)
    └─ wrapped per member: ephemeral X25519 ECDH → HKDF → ChaCha20Poly1305
  message envelope: ChaCha20Poly1305 under the epoch key, AAD binds
    (chat|id|ns|from|epoch), Ed25519 signature over AAD+ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
from collections import OrderedDict

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

__all__ = [
    "b64e", "b64d", "generate_identity", "identity_pubs", "wrap_bundle",
    "unwrap_bundle", "new_recovery_code", "new_chat_key", "wrap_key_for",
    "unwrap_key_with", "seal_bytes", "unseal_bytes", "seal_raw", "unseal_raw",
    "sign", "verify", "CryptoFail",
]

_KEYWRAP_INFO = b"agentbridge.keywrap.v2"
_VERIFY_CACHE_CAPACITY = 8192
_VERIFY_CACHE_MAX_PAYLOAD = 1024 * 1024
_VERIFY_CACHE_DOMAIN = b"AgentBridge.Ed25519.verify.positive.v1\0"


def _new_verify_cache_secret() -> bytes | None:
    try:
        return secrets.token_bytes(32)
    except Exception:  # secure randomness unavailable: verification still works
        return None


_verify_cache_lock = threading.Lock()
_verify_cache: OrderedDict[bytes, None] = OrderedDict()
_verify_cache_secret = _new_verify_cache_secret()


def _reset_verify_cache_after_fork() -> None:
    """A child must inherit neither a possibly held mutex nor parent digests."""
    global _verify_cache_lock, _verify_cache, _verify_cache_secret
    _verify_cache_lock = threading.Lock()
    _verify_cache = OrderedDict()
    _verify_cache_secret = _new_verify_cache_secret()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_verify_cache_after_fork)


class CryptoFail(Exception):
    """Deliberately generic — callers treat any failure as 'cannot open'."""


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


# ------------------------------------------------------------------ identity

def generate_identity() -> bytes:
    """64-byte bundle: Ed25519 signing priv || X25519 agreement priv."""
    sign = Ed25519PrivateKey.generate()
    agree = X25519PrivateKey.generate()
    return _raw(sign) + _raw_x(agree)


def _raw(k: Ed25519PrivateKey) -> bytes:
    return k.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _raw_x(k: X25519PrivateKey) -> bytes:
    return k.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def identity_pubs(bundle: bytes) -> tuple[str, str]:
    """(sign_pub_b64, agree_pub_b64) derived from the private bundle."""
    sign = Ed25519PrivateKey.from_private_bytes(bundle[:32])
    agree = X25519PrivateKey.from_private_bytes(bundle[32:])
    return (
        b64e(sign.public_key().public_bytes_raw()),
        b64e(agree.public_key().public_bytes_raw()),
    )


def _pw_key(secret: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(secret.encode())


def wrap_bundle(bundle: bytes, secret: str) -> dict:
    """Password/recovery-code wrap of the identity bundle (D5)."""
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = ChaCha20Poly1305(_pw_key(secret, salt)).encrypt(nonce, bundle, None)
    return {"salt": b64e(salt), "nonce": b64e(nonce), "ct": b64e(ct)}


def unwrap_bundle(wrapped: dict, secret: str) -> bytes:
    try:
        key = _pw_key(secret, b64d(wrapped["salt"]))
        return ChaCha20Poly1305(key).decrypt(
            b64d(wrapped["nonce"]), b64d(wrapped["ct"]), None
        )
    except (KeyError, ValueError, InvalidTag) as e:
        raise CryptoFail("cannot unlock the account key") from e


def new_recovery_code() -> str:
    """Shown ONCE at account creation; losing it + the password = history
    unreadable (D5 — the honest cost of real E2EE)."""
    raw = secrets.token_hex(10)  # 80 bits
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


# ------------------------------------------------------------- chat epoch keys

def new_chat_key() -> bytes:
    return os.urandom(32)


def wrap_key_for(agree_pub_b64: str, key: bytes) -> dict:
    """Wrap a chat key for one member: ephemeral ECDH → HKDF → AEAD."""
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(b64d(agree_pub_b64)))
    kek = HKDF(algorithm=SHA256(), length=32, salt=None, info=_KEYWRAP_INFO).derive(shared)
    nonce = os.urandom(12)
    return {
        "eph": b64e(eph.public_key().public_bytes_raw()),
        "nonce": b64e(nonce),
        "ct": b64e(ChaCha20Poly1305(kek).encrypt(nonce, key, None)),
    }


def unwrap_key_with(bundle: bytes, wrapped: dict) -> bytes:
    try:
        agree = X25519PrivateKey.from_private_bytes(bundle[32:])
        shared = agree.exchange(X25519PublicKey.from_public_bytes(b64d(wrapped["eph"])))
        kek = HKDF(algorithm=SHA256(), length=32, salt=None, info=_KEYWRAP_INFO).derive(shared)
        return ChaCha20Poly1305(kek).decrypt(
            b64d(wrapped["nonce"]), b64d(wrapped["ct"]), None
        )
    except (KeyError, ValueError, InvalidTag) as e:
        raise CryptoFail("cannot unwrap the chat key") from e


# ----------------------------------------------------------------- envelopes

def seal_bytes(chat_key: bytes, aad: bytes, plaintext: bytes) -> tuple[str, str]:
    """(nonce_b64, ct_b64) — AAD authenticates the routing metadata."""
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(chat_key).encrypt(nonce, plaintext, aad)
    return b64e(nonce), b64e(ct)


def unseal_bytes(chat_key: bytes, aad: bytes, nonce_b64: str, ct_b64: str) -> bytes:
    try:
        return ChaCha20Poly1305(chat_key).decrypt(b64d(nonce_b64), b64d(ct_b64), aad)
    except (ValueError, InvalidTag) as e:
        raise CryptoFail("cannot open the envelope") from e


# --------------------------------------------------------------------- blobs
# Raw-binary variant for file attachments (R13): no b64 bloat on disk.

def seal_raw(chat_key: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """``nonce(12B) + ciphertext`` — same AEAD as envelopes, binary layout."""
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(chat_key).encrypt(nonce, plaintext, aad)


def unseal_raw(chat_key: bytes, aad: bytes, sealed: bytes) -> bytes:
    try:
        return ChaCha20Poly1305(chat_key).decrypt(sealed[:12], sealed[12:], aad)
    except (ValueError, InvalidTag) as e:
        raise CryptoFail("cannot open the blob") from e


def sign(bundle: bytes, data: bytes) -> str:
    return b64e(Ed25519PrivateKey.from_private_bytes(bundle[:32]).sign(data))


def _verify_uncached(sign_pub_b64: str, sig_b64: str, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(sign_pub_b64)).verify(b64d(sig_b64), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def _verify_cache_digest(
    sign_pub_b64: str, sig_b64: str, data: bytes,
) -> bytes | None:
    secret = _verify_cache_secret
    if secret is None or type(sign_pub_b64) is not str \
            or type(sig_b64) is not str or type(data) is not bytes:
        return None
    if len(sign_pub_b64) != 44 or len(sig_b64) != 88 \
            or len(data) > _VERIFY_CACHE_MAX_PAYLOAD \
            or not sign_pub_b64.isascii() or not sig_b64.isascii():
        return None
    pub = sign_pub_b64.encode("ascii")
    sig = sig_b64.encode("ascii")
    digest = hashlib.blake2b(key=secret, digest_size=32)
    digest.update(_VERIFY_CACHE_DOMAIN)
    for value in (pub, sig, data):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.digest()


def _verify_cache_hit(digest: bytes) -> bool:
    with _verify_cache_lock:
        if digest not in _verify_cache:
            return False
        _verify_cache.move_to_end(digest)
        return True


def _remember_positive_verification(digest: bytes) -> None:
    with _verify_cache_lock:
        _verify_cache[digest] = None
        _verify_cache.move_to_end(digest)
        while len(_verify_cache) > _VERIFY_CACHE_CAPACITY:
            _verify_cache.popitem(last=False)


def verify(sign_pub_b64: str, sig_b64: str, data: bytes) -> bool:
    """Verify exactly as before, memoizing only bounded positive predicates."""
    digest = None
    try:
        digest = _verify_cache_digest(sign_pub_b64, sig_b64, data)
        if digest is not None and _verify_cache_hit(digest):
            return True
    except Exception:
        # Cache preparation/lookup is optional. Run the original primitive once.
        digest = None

    verified = _verify_uncached(sign_pub_b64, sig_b64, data)
    if verified and digest is not None:
        try:
            _remember_positive_verification(digest)
        except Exception:
            pass  # the mathematical result is already established
    return verified
