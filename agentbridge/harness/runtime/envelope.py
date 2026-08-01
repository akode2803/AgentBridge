"""Signed pairwise-encrypted envelopes for owner-control records."""

from __future__ import annotations

from dataclasses import dataclass

from ... import crypto
from .models import RuntimeContractError, canonical_json_bytes


class EnvelopeError(RuntimeContractError):
    """An envelope is malformed, forged, stale, or cannot be opened."""


@dataclass(frozen=True, slots=True)
class OpenedEnvelope:
    header: dict
    payload: dict


def _strict(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise EnvelopeError(f"invalid {name}")
    return value


def _agree_pub(mesh, name: str) -> str:
    acc = mesh.directory.get(name)
    value = acc.keys.agree_pub if acc else ""
    if not value:
        raise EnvelopeError(f"no trusted agreement key for @{name}")
    return value


def seal(mesh, *, kind: str, record_id: str, ns: int, sender: str,
         recipient: str, agent: str, chat_id: str, expires_ns: int,
         key_epoch: int, membership_epoch: int, ownership_epoch: int,
         policy_revision: int, payload: dict) -> dict:
    bundle = mesh.keystore.load(sender)
    if not bundle:
        raise EnvelopeError(f"identity key for @{sender} is locked or unavailable")
    header = {
        "v": 1, "kind": kind, "id": record_id, "ns": int(ns),
        "sender": sender, "recipient": recipient, "agent": agent,
        "chat_id": chat_id, "expires_ns": int(expires_ns),
        "key_epoch": int(key_epoch),
        "membership_epoch": int(membership_epoch),
        "ownership_epoch": int(ownership_epoch),
        "policy_revision": int(policy_revision),
    }
    key = crypto.new_chat_key()
    wrapped = {
        sender: crypto.wrap_key_for(_agree_pub(mesh, sender), key),
        recipient: crypto.wrap_key_for(_agree_pub(mesh, recipient), key),
    }
    aad = canonical_json_bytes({"header": header, "wrapped": wrapped})
    nonce, ct = crypto.seal_bytes(key, aad, canonical_json_bytes(payload))
    signed = {"header": header, "wrapped": wrapped, "nonce": nonce, "ct": ct}
    return {**signed, "sig": crypto.sign(bundle, canonical_json_bytes(signed))}


def open_envelope(mesh, raw: object, *, expected_kind: str,
                  expected_sender: str, expected_recipient: str,
                  expected_agent: str, expected_chat: str) -> OpenedEnvelope:
    env = _strict(raw, {"header", "wrapped", "nonce", "ct", "sig"}, "envelope")
    header = _strict(env["header"], {
        "v", "kind", "id", "ns", "sender", "recipient", "agent",
        "chat_id", "expires_ns", "membership_epoch", "ownership_epoch",
        "policy_revision", "key_epoch",
    }, "header")
    expected = {
        "v": 1, "kind": expected_kind, "sender": expected_sender,
        "recipient": expected_recipient, "agent": expected_agent,
        "chat_id": expected_chat,
    }
    if any(header.get(k) != v for k, v in expected.items()):
        raise EnvelopeError("envelope routing does not match the expected authority")
    for name in ("id",):
        if not isinstance(header.get(name), str) or not header[name]:
            raise EnvelopeError(f"invalid header {name}")
    for name in ("ns", "expires_ns", "key_epoch", "membership_epoch",
                 "ownership_epoch", "policy_revision"):
        if isinstance(header.get(name), bool) or not isinstance(header.get(name), int):
            raise EnvelopeError(f"invalid header {name}")
    wrapped = env["wrapped"]
    if not isinstance(wrapped, dict) or set(wrapped) != {expected_sender, expected_recipient}:
        raise EnvelopeError("envelope audience is not exactly sender and recipient")
    signed = {k: env[k] for k in ("header", "wrapped", "nonce", "ct")}
    sign_pub = mesh.directory.sign_pub(expected_sender)
    if not sign_pub or not crypto.verify(sign_pub, str(env["sig"]),
                                         canonical_json_bytes(signed)):
        raise EnvelopeError("invalid envelope signature")
    bundle = mesh.keystore.load(expected_recipient)
    if not bundle:
        raise EnvelopeError(f"identity key for @{expected_recipient} is locked or unavailable")
    try:
        key = crypto.unwrap_key_with(bundle, wrapped[expected_recipient])
        aad = canonical_json_bytes({"header": header, "wrapped": wrapped})
        plain = crypto.unseal_bytes(key, aad, str(env["nonce"]), str(env["ct"]))
        import json
        payload = json.loads(plain)
    except (crypto.CryptoFail, ValueError, TypeError) as exc:
        raise EnvelopeError("cannot open envelope") from exc
    if not isinstance(payload, dict):
        raise EnvelopeError("envelope payload must be an object")
    return OpenedEnvelope(header=header, payload=payload)
