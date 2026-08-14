"""Current authority bindings for owner-controlled runtime records."""

from __future__ import annotations

import hashlib

from .models import RunRecord, RunState, canonical_json_bytes
from ..settings import runtime_policy_revision


class AuthorityError(PermissionError):
    """The agent/owner/chat relationship is not currently authoritative."""


def capability_call_digest(provider: str, tool: str, tool_input: dict) -> str:
    """Canonical provider/tool/input binding for one native call decision."""
    if not isinstance(tool_input, dict):
        raise AuthorityError("provider-native tool input must be an object")
    return hashlib.sha256(canonical_json_bytes({
        "schema_version": 1,
        "provider": provider,
        "tool": tool,
        "input": tool_input,
    })).hexdigest()


def validate_run_authority(mesh, run: RunRecord, *, agent: str, chat_id: str,
                           run_id: str, provider: str, native_policy=None,
                           provider_version: str = "unattested") \
        -> dict[str, int | str]:
    """Re-resolve one signed run against current authority before a call."""
    if (not isinstance(run, RunRecord)
            or run.state is not RunState.RUNNING
            or run.meta.run_id != run_id
            or run.meta.chat_id != chat_id
            or run.manager_agent != agent
            or run.provider != provider
            or run.execution_level != "brokered_native"):
        raise AuthorityError("provider-native run binding is invalid")
    if (native_policy is None
            or run.native_policy_digest
            != native_policy.authority_digest(provider_version)):
        raise AuthorityError("provider-native policy ceiling is invalid")
    current = authority(mesh, agent, chat_id)
    if current["owner"] != run.responsible_member:
        raise AuthorityError("responsible member changed")
    # Key rotation protects record confidentiality; it is not a capability
    # revocation by itself. Membership, ownership and policy are the current
    # authorization epochs. A freshly ensured room key can also precede the
    # local materialized snapshot during the same run start.
    for name in ("membership_epoch", "ownership_epoch", "policy_revision"):
        if int(current[name]) != int(getattr(run.meta, name)):
            raise AuthorityError(f"stale {name}")
    return current


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
