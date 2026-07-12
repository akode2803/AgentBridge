"""Sealer — the crypto seam (D4). The messaging service never touches crypto
directly: it hands a BodyRecord to a Sealer and gets envelope fields back.

``PlainSealer`` is the pre-R9 implementation: the "ciphertext" is just the
JSON of the body-record (epoch 0, no signature). R9 swaps in the real E2EE
sealer (per-chat keys, ChaCha20Poly1305, Ed25519 signatures — prototyped in
spikes/r1/smoke_crypto.py) WITHOUT changing the envelope shape or any caller.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from ..core.models import BodyRecord, Envelope

__all__ = ["Sealer", "PlainSealer"]


class Sealer(ABC):
    @abstractmethod
    def seal(self, chat_id: str, body: BodyRecord) -> dict:
        """Return the envelope fields ``{epoch, nonce, ct, sig}``."""

    @abstractmethod
    def unseal(self, chat_id: str, env: Envelope) -> BodyRecord | None:
        """Decrypt an envelope's body-record; None if it cannot be opened."""


class PlainSealer(Sealer):
    """Format-v2 envelopes without encryption (epoch 0) — pre-R9 and also the
    honest representation for migrated-but-unencrypted test roots."""

    def seal(self, chat_id: str, body: BodyRecord) -> dict:
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
