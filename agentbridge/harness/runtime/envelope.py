"""Runtime-specific wrappers around the mesh pairwise envelope primitive."""

from __future__ import annotations

from ...mesh.pairwise import (
    EnvelopeError,
    OpenedEnvelope,
    open_pairwise,
    seal_pairwise,
)

__all__ = [
    "EnvelopeError", "OpenedEnvelope", "open_envelope", "open_pairwise",
    "seal", "seal_pairwise",
]


def seal(mesh, *, kind: str, record_id: str, ns: int, sender: str,
         recipient: str, agent: str, chat_id: str, expires_ns: int,
         key_epoch: int, membership_epoch: int, ownership_epoch: int,
         policy_revision: int, payload: dict) -> dict:
    header = {
        "v": 1, "kind": kind, "id": record_id, "ns": int(ns),
        "sender": sender, "recipient": recipient, "agent": agent,
        "chat_id": chat_id, "expires_ns": int(expires_ns),
        "key_epoch": int(key_epoch),
        "membership_epoch": int(membership_epoch),
        "ownership_epoch": int(ownership_epoch),
        "policy_revision": int(policy_revision),
    }
    return seal_pairwise(
        mesh, header=header, sender=sender, recipient=recipient,
        payload=payload,
    )


def open_envelope(mesh, raw: object, *, expected_kind: str,
                  expected_sender: str, expected_recipient: str,
                  expected_agent: str, expected_chat: str) -> OpenedEnvelope:
    fields = {
        "v", "kind", "id", "ns", "sender", "recipient", "agent",
        "chat_id", "expires_ns", "membership_epoch", "ownership_epoch",
        "policy_revision", "key_epoch",
    }
    expected = {
        "v": 1, "kind": expected_kind, "sender": expected_sender,
        "recipient": expected_recipient, "agent": expected_agent,
        "chat_id": expected_chat,
    }
    return open_pairwise(
        mesh, raw, header_fields=fields, expected=expected,
        sender=expected_sender, recipient=expected_recipient,
        viewer=expected_recipient,
    )
