"""Generic signed pairwise-encrypted envelope primitive."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .. import crypto
from ..core.jsonkit import canonical_json_bytes


class EnvelopeError(ValueError):
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


def seal_pairwise(mesh, *, header: dict, sender: str, recipient: str,
                  payload: dict) -> dict:
    """Seal an exact protocol header to one signer and one recipient."""
    bundle = mesh.keystore.load(sender)
    if not bundle:
        raise EnvelopeError(f"identity key for @{sender} is locked or unavailable")
    if header.get("sender") != sender or header.get("recipient") != recipient:
        raise EnvelopeError("envelope header audience does not match its keys")
    key = crypto.new_chat_key()
    wrapped = {
        sender: crypto.wrap_key_for(_agree_pub(mesh, sender), key),
        recipient: crypto.wrap_key_for(_agree_pub(mesh, recipient), key),
    }
    aad = canonical_json_bytes({"header": header, "wrapped": wrapped})
    nonce, ct = crypto.seal_bytes(key, aad, canonical_json_bytes(payload))
    signed = {"header": header, "wrapped": wrapped, "nonce": nonce, "ct": ct}
    return {**signed, "sig": crypto.sign(bundle, canonical_json_bytes(signed))}


def open_pairwise(mesh, raw: object, *, header_fields: set[str], expected: dict,
                  sender: str, recipient: str, viewer: str) -> OpenedEnvelope:
    """Verify and decrypt one strict pairwise envelope for an audience member."""
    env = _strict(raw, {"header", "wrapped", "nonce", "ct", "sig"}, "envelope")
    header = _strict(env["header"], header_fields, "header")
    if any(header.get(k) != v for k, v in expected.items()):
        raise EnvelopeError("envelope routing does not match the expected authority")
    if not isinstance(header.get("id"), str) or not header["id"]:
        raise EnvelopeError("invalid header id")
    for name, value in header.items():
        if (name == "ns" or name.endswith("_ns")
                or name.endswith("_epoch") or name == "policy_revision"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise EnvelopeError(f"invalid header {name}")
    wrapped = env["wrapped"]
    if not isinstance(wrapped, dict) or set(wrapped) != {sender, recipient}:
        raise EnvelopeError("envelope audience is not exactly sender and recipient")
    if viewer not in wrapped:
        raise EnvelopeError("viewer is outside the envelope audience")
    signed = {k: env[k] for k in ("header", "wrapped", "nonce", "ct")}
    sign_pub = mesh.directory.sign_pub(sender)
    if not sign_pub or not crypto.verify(
            sign_pub, str(env["sig"]), canonical_json_bytes(signed)):
        raise EnvelopeError("invalid envelope signature")
    bundle = mesh.keystore.load(viewer)
    if not bundle:
        raise EnvelopeError(f"identity key for @{viewer} is locked or unavailable")
    try:
        key = crypto.unwrap_key_with(bundle, wrapped[viewer])
        aad = canonical_json_bytes({"header": header, "wrapped": wrapped})
        plain = crypto.unseal_bytes(key, aad, str(env["nonce"]), str(env["ct"]))
        payload = json.loads(plain)
    except (crypto.CryptoFail, ValueError, TypeError) as exc:
        raise EnvelopeError("cannot open envelope") from exc
    if not isinstance(payload, dict):
        raise EnvelopeError("envelope payload must be an object")
    return OpenedEnvelope(header=header, payload=payload)
