"""Canonical facts for tools exposed by the per-run AgentBridge MCP server.

The catalog is package authority. Provider declarations may select reviewed
model capabilities from it, but neither owner config nor a provider can add a
tool or broaden its classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from ..core.errors import ValidationError

__all__ = [
    "BRIDGE_CAPABILITIES", "BRIDGE_CAPABILITY_SCHEMA", "CapabilitySpec",
    "bridge_capability_report", "compile_capability_ceiling",
    "validate_bridge_tools", "validate_model_capability_ceiling",
]

BRIDGE_CAPABILITY_SCHEMA = 1


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    surface: str
    effect: str
    risk: str
    approval: str
    enforcement_locus: str
    availability: str
    evidence: str
    backend_minimum: str = "authenticated-per-run-mcp"
    failure_mode: str = "deny"
    schema_source: str = "fastmcp-python-signature"
    schema_version: int = BRIDGE_CAPABILITY_SCHEMA

    def public_facts(self) -> dict:
        return {
            "id": self.id,
            "surface": self.surface,
            "effect": self.effect,
            "risk": self.risk,
            "approval": self.approval,
            "enforcement_locus": self.enforcement_locus,
            "availability": self.availability,
            "backend_minimum": self.backend_minimum,
            "failure_mode": self.failure_mode,
            "schema_source": self.schema_source,
            "schema_version": self.schema_version,
            "evidence": self.evidence,
        }


def _spec(capability_id: str, *, surface: str = "legacy-model-tool",
          effect: str = "read", risk: str = "low",
          approval: str = "mesh-authority",
          availability: str = "mesh-required",
          enforcement_locus: str = "bridge+mesh-authority") -> CapabilitySpec:
    return CapabilitySpec(
        id=capability_id, surface=surface, effect=effect, risk=risk,
        approval=approval, enforcement_locus=enforcement_locus,
        availability=availability,
        evidence=f"agentbridge/harness/bridge.py#mcp-tool:{capability_id}",
    )


_SPECS = (
    _spec("approve", surface="control-plane", effect="authorization",
          risk="high", approval="owner-policy-or-owner-decision",
          availability="always",
          enforcement_locus="permission-broker"),
    _spec("ask_member", surface="control-plane", effect="owner-contact",
          risk="medium", approval="owner-answer",
          availability="always", enforcement_locus="permission-broker"),
    _spec("read_docs", surface="control-plane", availability="docs-required",
          enforcement_locus="bridge"),
    _spec("delegate_agent", surface="model-capability", effect="delegation",
          risk="high", approval="pre-authorized-capability+runtime-authority",
          availability="delegation-coordinator-required",
          enforcement_locus="provider-filter+bridge+canonical-authority"),
    _spec("tidy_workspace", effect="local-delete", risk="medium",
          approval="workspace-boundary", availability="always",
          enforcement_locus="bridge-workspace-boundary"),
    _spec("list_chats"),
    _spec("list_files"),
    _spec("fetch_file", effect="local-write", risk="medium",
          enforcement_locus="bridge+visibility+workspace-boundary"),
    _spec("read_status", enforcement_locus="bridge+privacy-policy"),
    _spec("set_status", effect="profile-write", risk="medium"),
    _spec("set_about", effect="profile-write", risk="medium"),
    _spec("read_permissions", enforcement_locus="bridge+privacy-policy"),
    _spec("pin_message", effect="shared-state-write", risk="medium"),
    _spec("unpin_message", effect="shared-state-write", risk="medium"),
    _spec("star_messages", effect="private-state-write"),
    _spec("react", effect="shared-state-write", risk="medium"),
    _spec("edit_message", effect="shared-content-write", risk="high"),
    _spec("delete_message", effect="shared-content-delete", risk="high"),
    _spec("forward_message", effect="cross-chat-send", risk="high"),
    _spec("create_dm", effect="chat-create+optional-send", risk="high"),
    _spec("create_group", effect="chat-create+optional-send", risk="high"),
    _spec("message_info", enforcement_locus="bridge+receipt-privacy"),
    _spec("mute_chat", effect="private-state-write"),
    _spec("archive_chat", effect="private-state-write"),
    _spec("add_member", effect="membership-write", risk="high"),
    _spec("rename_chat", effect="shared-state-write", risk="medium"),
    _spec("set_description", effect="shared-state-write", risk="medium"),
    _spec("leave_chat", effect="membership-write", risk="high",
          approval="owner-decision",
          enforcement_locus="permission-broker+mesh-authority"),
    _spec("clear_chat", effect="private-content-delete", risk="high",
          approval="owner-decision",
          enforcement_locus="permission-broker+mesh-authority"),
    _spec("schedule_timer", effect="scheduled-work-create", risk="medium"),
    _spec("cancel_timer", effect="scheduled-work-delete", risk="medium",
          availability="mesh+timer-service-required"),
    _spec("peer_diagnose", effect="peer-read-or-repair", risk="high",
          approval="peer-owner-policy-or-owner-decision",
          enforcement_locus="bridge+peer-service+permission-lane"),
    _spec("remember", effect="memory-write", risk="medium",
          availability="memory-store-required"),
    _spec("recall", availability="memory-store-required"),
    _spec("forget", effect="memory-delete", risk="high",
          availability="memory-store-required"),
)

BRIDGE_CAPABILITIES = MappingProxyType({spec.id: spec for spec in _SPECS})
if len(BRIDGE_CAPABILITIES) != len(_SPECS):  # package-time invariant
    raise RuntimeError("duplicate bridge capability id")


def validate_bridge_tools(tool_ids) -> frozenset[str]:
    """Reject any registered MCP tool without a reviewed catalog entry."""
    tools = frozenset(str(item) for item in tool_ids)
    unknown = tools - BRIDGE_CAPABILITIES.keys()
    if unknown:
        raise RuntimeError(f"unregistered bridge tools: {sorted(unknown)}")
    return tools


def validate_model_capability_ceiling(capability_ids) -> tuple[str, ...]:
    """Validate the real-run ceiling at its central Runner boundary."""
    if not isinstance(capability_ids, tuple):
        raise ValidationError("capability ceiling must be an immutable tuple")
    if any(not isinstance(item, str) or not item for item in capability_ids):
        raise ValidationError("capability ceiling contains an invalid id")
    if len(set(capability_ids)) != len(capability_ids):
        raise ValidationError("capability ceiling contains duplicates")
    requested = frozenset(capability_ids)
    unknown = requested - BRIDGE_CAPABILITIES.keys()
    if unknown:
        raise ValidationError(f"unknown bridge capability: {sorted(unknown)}")
    wrong_surface = {
        item for item in requested
        if BRIDGE_CAPABILITIES[item].surface != "model-capability"
    }
    if wrong_surface:
        raise ValidationError(
            f"not a model capability: {sorted(wrong_surface)}")
    return capability_ids


def compile_capability_ceiling(profile, requested_capabilities) -> tuple[str, ...]:
    """Freeze the reviewed model-capability upper bound for one run."""
    requested = tuple(dict.fromkeys(str(item) for item in requested_capabilities))
    validate_model_capability_ceiling(requested)
    requested_set = set(requested)
    if profile is None:
        if requested_set:
            raise ValidationError("provider has no trusted bridge profile")
        return ()
    declared = tuple(profile.capabilities)
    validate_bridge_tools(declared)
    undeclared = requested_set - set(declared)
    if undeclared:
        raise ValidationError(
            f"provider does not declare capability: {sorted(undeclared)}")
    return tuple(item for item in declared if item in requested_set)


def bridge_capability_report() -> dict:
    """Non-secret catalog facts for authenticated member-facing APIs."""
    return {
        "schema_version": BRIDGE_CAPABILITY_SCHEMA,
        "tools": [BRIDGE_CAPABILITIES[name].public_facts()
                  for name in sorted(BRIDGE_CAPABILITIES)],
    }
