"""Canonical redaction/void verification with a request-supplied resolver.

This preserves short-circuit and signature construction order for both the live
messaging path and bounded page attempts. The resolver owns current authority.
"""
from __future__ import annotations

from .. import crypto
from .events import redaction_signing_bytes, unredaction_signing_bytes


def redaction_verifier(chat_id: str, directory):
    def authorized(actor: str, original_from: str) -> bool:
        return actor == original_from or \
            directory.owner_of(original_from) == actor

    def signed(actor: str, sig: str, payload: bytes) -> bool:
        pub = directory.sign_pub(actor)
        return bool(pub and sig and crypto.verify(pub, sig, payload))

    def ok(msg_id: str, red: dict, original_from: str) -> bool:
        void = red.get("void")
        if isinstance(void, dict):
            vby = void.get("by") or ""
            if authorized(vby, original_from) and signed(
                vby, void.get("sig") or "",
                unredaction_signing_bytes(
                    chat_id, msg_id, int(red.get("ns", 0)),
                    vby, int(void.get("ns", 0))),
            ):
                return False  # validly restored — no tombstone
            # forged/misbound void: fall through, verify the redaction
        by = red.get("by") or ""
        if not authorized(by, original_from):
            return False
        return signed(
            by, red.get("sig") or "",
            redaction_signing_bytes(chat_id, msg_id, by, int(red.get("ns", 0))),
        )

    return ok
