"""Sealer — the crypto seam (D4). The messaging service never touches crypto
directly: it hands a BodyRecord to a Sealer and gets envelope fields back.

``PlainSealer``: format-v2 without encryption (epoch 0) — tests, and the
migration era. ``E2EESealer`` (R9): per-chat epoch keys, ChaCha20Poly1305
with AAD-bound routing metadata, Ed25519 signatures — same envelope shape,
zero caller changes (the whole point of the seam).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from .. import crypto
from ..core.models import BodyRecord, ChatSnapshot, Envelope
from ..transport.base import Transport
from .directory import Directory
from .keyring import ChatKeyService
from .paths import P

__all__ = ["Sealer", "PlainSealer", "E2EESealer"]


class Sealer(ABC):
    @abstractmethod
    def seal(self, chat_id: str, env_id: str, ns: int, body: BodyRecord) -> dict:
        """Return the envelope fields ``{epoch, nonce, ct, sig}``. The caller
        mints ``env_id``/``ns`` FIRST so they can be cryptographically bound
        (replay-proofing: a member can't re-post someone's old ciphertext
        under a fresh id)."""

    @abstractmethod
    def unseal(self, chat_id: str, env: Envelope) -> BodyRecord | None:
        """Decrypt an envelope's body-record; None if it cannot be opened."""

    @abstractmethod
    def seal_blob(self, chat_id: str, blob_id: str, data: bytes) -> bytes:
        """File bytes -> at-rest bytes. Provenance rides the SIGNED message
        that names the blob (its ``files[].sha256``) — readers verify there."""

    @abstractmethod
    def open_blob(self, chat_id: str, blob_id: str, data: bytes) -> bytes | None:
        """At-rest bytes -> file bytes; None if they cannot be opened."""


class PlainSealer(Sealer):
    """Format-v2 envelopes without encryption (epoch 0) — pre-R9 and also the
    honest representation for migrated-but-unencrypted test roots."""

    def seal(self, chat_id: str, env_id: str, ns: int, body: BodyRecord) -> dict:
        return {
            "epoch": 0,
            "nonce": "",
            "ct": json.dumps(body.to_dict(), ensure_ascii=False),
            "sig": "",
        }

    def unseal(self, chat_id: str, env: Envelope) -> BodyRecord | None:
        if env.epoch != 0:
            return None  # encrypted envelope, and I'm the plain sealer
        try:
            data = json.loads(env.ct) if env.ct else {}
        except json.JSONDecodeError:
            return None
        return BodyRecord.from_dict(data if isinstance(data, dict) else {})

    def seal_blob(self, chat_id: str, blob_id: str, data: bytes) -> bytes:
        return data

    def open_blob(self, chat_id: str, blob_id: str, data: bytes) -> bytes | None:
        if data.startswith(_BLOB_MAGIC):
            return None  # sealed blob, and I'm the plain sealer
        return data


def _aad(chat_id: str, env_id: str, ns: int, sender: str, epoch: int) -> bytes:
    """Authenticated routing metadata — swap ANY of these fields on disk and
    the envelope simply refuses to open (no mis-attribution, no replay)."""
    return f"{chat_id}|{env_id}|{ns}|{sender}|{epoch}".encode()


# sealed-blob layout: magic + 8-byte BE epoch + nonce(12B) + ciphertext
_BLOB_MAGIC = b"AB2E"


def _blob_aad(chat_id: str, blob_id: str, epoch: int) -> bytes:
    return f"{chat_id}|blob|{blob_id}|{epoch}".encode()


class E2EESealer(Sealer):
    """The real thing (R9). Seal: ensure a correct epoch (rotating after any
    membership drift — the race heal), encrypt with AAD-bound metadata, sign
    with the sender's identity key. Unseal: verify signature, unwrap my epoch
    copy, decrypt — ANY failure returns None (show nothing rather than lie).

    Epoch-0 plaintext is never accepted: an encrypted mesh has no legitimate
    plaintext envelopes or plain file blobs (the migrated-history era ended
    with the R16.5 purge), so anything unsealed reads as nothing rather than
    as an injectable "plaintext from @aryan".
    """

    def __init__(
        self,
        tx: Transport,
        directory: Directory,
        keys: ChatKeyService,
        user: str,
        keystore_bundle,  # callable () -> bytes | None (lazy: login may follow init)
    ) -> None:
        self.tx = tx
        self.directory = directory
        self.keys = keys
        self.user = user
        self._bundle = keystore_bundle

    def seal(self, chat_id: str, env_id: str, ns: int, body: BodyRecord) -> dict:
        bundle = self._bundle()
        if bundle is None:
            raise crypto.CryptoFail("identity keys are locked — sign in first")
        snap_doc = self.tx.get_doc(P.meta(chat_id))
        if not isinstance(snap_doc, dict):
            raise crypto.CryptoFail(f"unknown chat {chat_id}")
        epoch, key = self.keys.ensure(chat_id, ChatSnapshot.from_dict(snap_doc))
        aad = _aad(chat_id, env_id, ns, self.user, epoch)
        nonce, ct = crypto.seal_bytes(
            key, aad, json.dumps(body.to_dict(), ensure_ascii=False).encode()
        )
        sig = crypto.sign(bundle, aad + b"|" + nonce.encode() + b"|" + ct.encode())
        return {"epoch": epoch, "nonce": nonce, "ct": ct, "sig": sig}

    def unseal(self, chat_id: str, env: Envelope) -> BodyRecord | None:
        if env.epoch == 0:
            return None  # v2 has no plaintext envelopes (R16.5)
        sender = self.directory.get(env.from_)
        if sender is None or not sender.keys.sign_pub:
            return None
        aad = _aad(chat_id, env.id, env.ns, env.from_, env.epoch)
        signed = aad + b"|" + env.nonce.encode() + b"|" + env.ct.encode()
        if not crypto.verify(sender.keys.sign_pub, env.sig, signed):
            return None
        key = self.keys.my_key(chat_id, env.epoch)
        if key is None:
            return None  # not my epoch (removed member / pre-join, no history)
        try:
            raw = crypto.unseal_bytes(key, aad, env.nonce, env.ct)
        except crypto.CryptoFail:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return BodyRecord.from_dict(data if isinstance(data, dict) else {})

    def seal_blob(self, chat_id: str, blob_id: str, data: bytes) -> bytes:
        snap_doc = self.tx.get_doc(P.meta(chat_id))
        if not isinstance(snap_doc, dict):
            raise crypto.CryptoFail(f"unknown chat {chat_id}")
        epoch, key = self.keys.ensure(chat_id, ChatSnapshot.from_dict(snap_doc))
        sealed = crypto.seal_raw(key, _blob_aad(chat_id, blob_id, epoch), data)
        return _BLOB_MAGIC + epoch.to_bytes(8, "big") + sealed

    def open_blob(self, chat_id: str, blob_id: str, data: bytes) -> bytes | None:
        if not data.startswith(_BLOB_MAGIC):
            return None  # plain bytes are never chat files in v2 (R16.5)
        epoch = int.from_bytes(data[4:12], "big")
        key = self.keys.my_key(chat_id, epoch)
        if key is None:
            return None  # not my epoch (removed member / pre-join)
        try:
            return crypto.unseal_raw(
                key, _blob_aad(chat_id, blob_id, epoch), data[12:]
            )
        except crypto.CryptoFail:
            return None
