"""Shared sealing and verification for immutable room-runtime records."""

from __future__ import annotations

import json
from typing import Callable, TypeVar

from ... import crypto
from ...core.errors import ValidationError
from ...mesh.readmodel import member_at
from .models import (
    RecordKind, RecordMeta, RuntimeContractError, canonical_json_bytes,
)

__all__ = [
    "ENVELOPE_FIELDS", "deliver_immutable", "open_record", "seal_record",
]

ENVELOPE_FIELDS = {"meta", "nonce", "ct", "sig"}
RecordT = TypeVar("RecordT")


def _aad(meta: RecordMeta) -> bytes:
    return canonical_json_bytes({"meta": meta.to_dict()})


def _signed(meta: RecordMeta, nonce: str, ciphertext: str) -> bytes:
    return canonical_json_bytes({
        "meta": meta.to_dict(), "nonce": nonce, "ct": ciphertext,
    })


def seal_record(mesh, record) -> dict:
    """Encrypt one record with its room epoch and sign its routed envelope."""
    meta = record.meta
    bundle = mesh.keystore.load(mesh.user)
    if not bundle:
        raise RuntimeContractError("runtime signer identity key is unavailable")
    key = mesh.keys.my_key(meta.chat_id, meta.key_epoch)
    if key is None:
        raise RuntimeContractError("runtime chat epoch key is unavailable")
    nonce, ciphertext = crypto.seal_bytes(
        key, _aad(meta), record.canonical_bytes(),
    )
    return {
        "meta": meta.to_dict(), "nonce": nonce, "ct": ciphertext,
        "sig": crypto.sign(bundle, _signed(meta, nonce, ciphertext)),
    }


def deliver_immutable(tx, path: str, doc: dict) -> None:
    """Create an immutable document idempotently; conflicts are structural."""
    if bool(getattr(tx, "supports_exclusive_create", False)):
        # An exclusive transport owns idempotent retry/conflict detection.
        # Avoid a redundant cloud read before every globally unique event.
        try:
            tx.create_doc(path, doc)
        except Exception:
            # A response may be lost after the create committed. Accept only
            # exact bytes; conflicting or unavailable readback stays failed.
            if tx.get_doc(path, default=None) != doc:
                raise
        return
    current = tx.get_doc(path, default=None)
    if current is not None and current != doc:
        raise ValidationError("immutable runtime path already differs")
    if current is None:
        try:
            tx.create_doc(path, doc)
        except Exception:
            if tx.get_doc(path, default=None) != doc:
                raise


def open_record(
    mesh, snap, doc: dict, expected_kind: RecordKind,
    parser: Callable[[dict], RecordT],
) -> RecordT:
    """Verify, decrypt and strictly parse one room-runtime envelope."""
    if not isinstance(doc, dict) or set(doc) != ENVELOPE_FIELDS:
        raise RuntimeContractError("invalid runtime envelope fields")
    meta = RecordMeta.from_dict(doc["meta"])
    if meta.kind is not expected_kind:
        raise RuntimeContractError("runtime envelope kind mismatch")
    if meta.actor != meta.signer:
        raise RuntimeContractError("runtime actor and signer differ")
    pub = mesh.directory.sign_pub(meta.signer)
    nonce = str(doc["nonce"])
    ciphertext = str(doc["ct"])
    if not pub or not crypto.verify(
            pub, str(doc["sig"]), _signed(meta, nonce, ciphertext)):
        raise RuntimeContractError("invalid runtime signature")
    if not member_at(snap.tenure.get(meta.actor), meta.ns):
        raise RuntimeContractError("runtime signer was outside room tenure")
    key = mesh.keys.my_key(meta.chat_id, meta.key_epoch)
    if key is None:
        raise RuntimeContractError("runtime epoch is unavailable")
    try:
        plain = crypto.unseal_bytes(key, _aad(meta), nonce, ciphertext)
        record = parser(json.loads(plain))
    except RuntimeContractError:
        raise
    except (crypto.CryptoFail, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeContractError("runtime ciphertext is invalid") from exc
    if record.meta != meta:
        raise RuntimeContractError("runtime ciphertext metadata mismatch")
    return record
