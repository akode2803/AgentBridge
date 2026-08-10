"""Current authority bindings for owner-controlled runtime records."""

from __future__ import annotations

import hashlib

from .models import canonical_json_bytes
from ..settings import runtime_policy_revision


class AuthorityError(PermissionError):
    """The agent/owner/chat relationship is not currently authoritative."""


def _revision(value) -> int:
    raw = hashlib.sha256(canonical_json_bytes(value)).digest()[:8]
    return int.from_bytes(raw, "big") & ((1 << 63) - 1)


def authority(mesh, agent: str, chat_id: str) -> dict[str, int | str]:
    """Return deterministic epochs for the current agent-owner-room binding."""
    base = responsible_authority(mesh, agent)
    owner = str(base["owner"])
    snap = mesh.snapshot(chat_id)
    if agent not in snap.members or owner not in snap.members:
        raise AuthorityError("agent and responsible member must both be chat members")
    members = [
        {
            "name": name,
            "role": getattr(member.role, "value", member.role),
            "joined_ns": int(member.joined_ns),
        }
        for name, member in sorted(snap.members.items())
    ]
    return {
        **base,
        "key_epoch": int(snap.key_epoch),
        "membership_epoch": _revision(members),
    }


def responsible_authority(mesh, agent: str) -> dict[str, int | str]:
    """Return the current chatless target-agent/responsible-member binding."""
    owner = mesh.directory.owner_of(agent)
    if not owner:
        raise AuthorityError("agent has no responsible member")
    agent_acc = mesh.directory.get(agent)
    owner_acc = mesh.directory.get(owner)
    if not agent_acc or not owner_acc or not agent_acc.active or not owner_acc.active:
        raise AuthorityError("agent and responsible member must be active")
    if not agent_acc.keys.sign_pub or not agent_acc.keys.agree_pub:
        raise AuthorityError("agent identity keys are unavailable")
    if not owner_acc.keys.sign_pub or not owner_acc.keys.agree_pub:
        raise AuthorityError("responsible member identity keys are unavailable")
    ownership = {
        "agent": agent,
        "owner": owner,
        "agent_active": bool(agent_acc.active),
        "owner_active": bool(owner_acc.active),
        "agent_sign": agent_acc.keys.sign_pub,
        "agent_agree": agent_acc.keys.agree_pub,
        "owner_sign": owner_acc.keys.sign_pub,
        "owner_agree": owner_acc.keys.agree_pub,
    }
    harness = dict(agent_acc.agent.harness or {}) if agent_acc.agent else {}
    return {
        "owner": owner,
        "ownership_epoch": _revision(ownership),
        "policy_revision": runtime_policy_revision(harness),
    }
