"""Package-authoritative provider-native tool inventory and policy compiler.

Provider CLIs spell equivalent powers differently. Shipped presets select
reviewed capability ids from this catalog; they never classify arbitrary tool
strings themselves. Owner adapter overlays remain ordinary CLI configuration
and cannot self-certify a native authority contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from ...core.errors import ValidationError

__all__ = [
    "NATIVE_CAPABILITIES", "NATIVE_CAPABILITY_SCHEMA",
    "NATIVE_DENY_ARG_TEMPLATES", "NativeCapabilitySpec", "NativeProfile",
    "EffectiveNativePolicy", "native_capability_report",
]

NATIVE_CAPABILITY_SCHEMA = 1
NATIVE_DENY_ARG_TEMPLATES = MappingProxyType({
    "claude": ("--disallowedTools", "{tool}"),
    "cortex": ("--disallowed-tools", "{tool}"),
})
_PROFILE_FIELDS = {
    "schema", "provider", "inventory_complete", "enforcement_locus",
    "evidence", "auto_allow", "blocked", "aux_web",
}


@dataclass(frozen=True, slots=True)
class NativeCapabilitySpec:
    id: str
    provider: str
    tools: tuple[str, ...]
    effect: str
    risk: str
    approval: str
    enforcement_locus: str
    evidence: str
    backend_minimum: str = "brokered_native"
    failure_mode: str = "deny"
    path_keys: tuple[str, ...] = ()
    path_required: bool = False
    schema_version: int = NATIVE_CAPABILITY_SCHEMA

    def public_facts(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "tools": list(self.tools),
            "effect": self.effect,
            "risk": self.risk,
            "approval": self.approval,
            "enforcement_locus": self.enforcement_locus,
            "evidence": self.evidence,
            "backend_minimum": self.backend_minimum,
            "failure_mode": self.failure_mode,
            "path_keys": list(self.path_keys),
            "path_required": self.path_required,
            "schema_version": self.schema_version,
        }


def _native(capability_id: str, provider: str, *tools: str,
            effect: str, risk: str = "medium",
            approval: str = "provider-deny-or-agentbridge-broker",
            enforcement_locus: str = "provider-native-tool-filter") \
        -> NativeCapabilitySpec:
    path_keys = (
        "file_path", "path", "notebook_path", "file", "filename", "filepath",
    ) if effect.startswith("host-file-") else ()
    return NativeCapabilitySpec(
        id=capability_id,
        provider=provider,
        tools=tuple(tools),
        effect=effect,
        risk=risk,
        approval=approval,
        enforcement_locus=enforcement_locus,
        evidence=(
            "https://docs.anthropic.com/en/docs/claude-code/cli-usage"
            if provider == "claude" else
            "https://docs.snowflake.com/en/user-guide/cortex-code/tools"
        ),
        path_keys=path_keys,
        path_required=bool(path_keys),
    )


_SPECS = (
    _native("claude.file_read", "claude", "Read", effect="host-file-read",
            risk="high"),
    _native("claude.file_glob", "claude", "Glob", "LS",
            effect="host-file-list",
            risk="high"),
    _native("claude.file_grep", "claude", "Grep", effect="host-file-search",
            risk="high"),
    _native("claude.todo_write", "claude", "TodoWrite",
            effect="provider-local-state-write", risk="low"),
    _native("claude.file_write", "claude", "Write", effect="host-file-write",
            risk="high"),
    _native("claude.file_edit", "claude", "Edit", effect="host-file-write",
            risk="high"),
    _native("claude.shell", "claude", "Bash", effect="host-process-exec",
            risk="critical"),
    _native("claude.web_fetch", "claude", "WebFetch", effect="network-read",
            risk="high"),
    _native("claude.web_search", "claude", "WebSearch", effect="network-read",
            risk="high"),
    _native("claude.task", "claude", "Task", effect="provider-delegation",
            risk="high"),
    _native("claude.agent", "claude", "Agent", effect="provider-delegation",
            risk="high"),
    _native("claude.notebook_edit", "claude", "NotebookEdit",
            effect="host-file-write", risk="high"),
    _native("claude.kill_shell", "claude", "KillShell",
            effect="host-process-control", risk="high"),
    _native("claude.ask_user", "claude", "AskUserQuestion",
            effect="provider-user-contact", risk="medium"),
    _native("cortex.file_read", "cortex", "Read", effect="host-file-read",
            risk="high"),
    _native("cortex.file_write", "cortex", "Write", effect="host-file-write",
            risk="high"),
    _native("cortex.file_edit", "cortex", "Edit", effect="host-file-write",
            risk="high"),
    _native("cortex.file_glob", "cortex", "Glob", effect="host-file-list",
            risk="high"),
    _native("cortex.file_grep", "cortex", "Grep", effect="host-file-search",
            risk="high"),
    _native("cortex.shell", "cortex", "Bash", "bash",
            effect="host-process-exec", risk="critical"),
    _native("cortex.shell_output", "cortex", "BashOutput", "bash_output",
            effect="host-process-read", risk="high"),
    _native("cortex.kill_shell", "cortex", "KillShell", "kill_shell",
            effect="host-process-control", risk="high"),
    _native("cortex.python_repl", "cortex", "python_repl",
            effect="host-process-exec", risk="critical"),
    _native("cortex.web_fetch", "cortex", "WebFetch", "web_fetch",
            effect="network-read", risk="high"),
    _native("cortex.web_search", "cortex", "WebSearch", "web_search",
            effect="network-read", risk="high"),
    _native("cortex.cron_create", "cortex", "cron_create",
            effect="scheduled-work-create", risk="high"),
    _native("cortex.cron_delete", "cortex", "cron_delete",
            effect="scheduled-work-delete", risk="high"),
    _native("cortex.cron_list", "cortex", "cron_list",
            effect="scheduled-work-read"),
    _native("cortex.notebook_execute", "cortex", "NotebookExecute",
            effect="host-process-exec", risk="critical"),
    _native("cortex.notebook_actions", "cortex", "NotebookEdit",
            "notebook_actions",
            effect="host-file-write", risk="high"),
    _native("cortex.run_subagent", "cortex", "RunSubagent",
            effect="provider-delegation", risk="high"),
    _native("cortex.review", "cortex", "Review",
            effect="provider-delegation", risk="high"),
    _native("cortex.team_create", "cortex", "team_create",
            effect="provider-delegation", risk="high"),
    _native("cortex.team_delete", "cortex", "team_delete",
            effect="provider-delegation-delete", risk="high"),
    _native("cortex.send_message", "cortex", "send_message",
            effect="external-send", risk="critical"),
    _native("cortex.ask_user", "cortex", "AskUserQuestion",
            "ask_user_question",
            effect="provider-user-contact", risk="medium"),
    _native("cortex.sql_execute", "cortex", "SnowflakeSqlExecute",
            effect="remote-database-read-or-write", risk="critical"),
    _native("cortex.object_search", "cortex", "SnowflakeObjectSearch",
            effect="remote-database-metadata-read", risk="high"),
    _native("cortex.product_docs", "cortex", "SnowflakeProductDocs",
            effect="network-read", risk="medium"),
    _native("cortex.semantic_validate", "cortex", "ReflectSemanticModel",
            effect="remote-database-read", risk="high"),
    _native("cortex.analyst", "cortex", "SnowflakeMultiCortexAnalyst",
            effect="remote-database-read", risk="high"),
    _native("cortex.data_diff", "cortex", "DataDiff",
            effect="remote-database-read", risk="high"),
    _native("cortex.plan_enter", "cortex", "EnterPlanMode",
            effect="provider-control-state", risk="low"),
    _native("cortex.plan_exit", "cortex", "ExitPlanMode",
            effect="provider-control-state", risk="medium"),
    _native("cortex.memory", "cortex", "Memory",
            effect="provider-persistent-state", risk="high"),
)

NATIVE_CAPABILITIES = MappingProxyType({spec.id: spec for spec in _SPECS})
if len(NATIVE_CAPABILITIES) != len(_SPECS):
    raise RuntimeError("duplicate provider-native capability id")
_TOOL_KEYS = [
    (spec.provider, tool)
    for spec in NATIVE_CAPABILITIES.values()
    for tool in spec.tools
]
if len(set(_TOOL_KEYS)) != len(_TOOL_KEYS):
    raise RuntimeError("ambiguous provider-native tool alias")


@dataclass(frozen=True, slots=True)
class EffectiveNativePolicy:
    schema_version: int
    provider: str
    inventory_complete: bool
    enforcement_locus: str
    evidence: str
    enabled: tuple[str, ...]
    approval_gated: tuple[str, ...]
    blocked: tuple[str, ...]
    auto_allow_tools: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    permission_callback: bool

    def authority_digest(self, provider_version: str = "unattested") -> str:
        """Digest every fact that can broaden one native provider run."""
        capability_ids = tuple(dict.fromkeys(
            (*self.enabled, *self.approval_gated, *self.blocked)))
        payload = {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "provider_version": provider_version,
            "inventory_complete": self.inventory_complete,
            "enforcement_locus": self.enforcement_locus,
            "evidence": self.evidence,
            "permission_callback": self.permission_callback,
            "enabled": self.enabled,
            "approval_gated": self.approval_gated,
            "blocked": self.blocked,
            "deny_arg_template": NATIVE_DENY_ARG_TEMPLATES[self.provider],
            "catalog": {
                capability_id: NATIVE_CAPABILITIES[capability_id].public_facts()
                for capability_id in sorted(capability_ids)
            },
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def capability_for_tool(self, tool: str) -> str:
        matches = [
            capability_id for capability_id in (*self.enabled,
                                                *self.approval_gated,
                                                *self.blocked)
            if tool in NATIVE_CAPABILITIES[capability_id].tools
        ]
        if len(matches) != 1:
            raise ValidationError("unknown or ambiguous provider-native tool")
        return matches[0]

    def path_keys_for_tool(self, tool: str) -> tuple[str, ...]:
        return NATIVE_CAPABILITIES[self.capability_for_tool(tool)].path_keys

    def path_required_for_tool(self, tool: str) -> bool:
        return NATIVE_CAPABILITIES[
            self.capability_for_tool(tool)
        ].path_required

    def public_facts(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "inventory_complete": self.inventory_complete,
            "enforcement_locus": self.enforcement_locus,
            "evidence": self.evidence,
            "permission_callback": self.permission_callback,
            "authority_digest": self.authority_digest(),
            "enabled": list(self.enabled),
            "approval_gated": list(self.approval_gated),
            "blocked": list(self.blocked),
        }


@dataclass(frozen=True, slots=True)
class NativeProfile:
    schema: int
    provider: str
    inventory_complete: bool
    enforcement_locus: str
    evidence: str
    auto_allow: tuple[str, ...]
    blocked: tuple[str, ...]
    aux_web: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object, *, expected_provider: str) -> "NativeProfile":
        if not isinstance(raw, dict):
            raise ValidationError("native profile must be an object")
        unknown = set(raw) - _PROFILE_FIELDS
        missing = _PROFILE_FIELDS - set(raw)
        if unknown or missing:
            raise ValidationError(
                f"invalid native profile fields: {sorted(unknown or missing)}")
        if raw["schema"] != NATIVE_CAPABILITY_SCHEMA:
            raise ValidationError("unsupported native profile schema")
        provider = raw["provider"]
        if provider != expected_provider or provider not in {"claude", "cortex"}:
            raise ValidationError("native profile provider mismatch")
        if raw["inventory_complete"] is not False:
            # No shipped CLI inventory has a version-bound exhaustive tool list.
            raise ValidationError("native inventory completeness is unverified")
        locus = raw["enforcement_locus"]
        evidence = raw["evidence"]
        if locus != "provider-deny-flags+optional-agentbridge-broker":
            raise ValidationError("unsupported native enforcement locus")
        if not isinstance(evidence, str) or not evidence:
            raise ValidationError("native profile needs evidence")
        groups = {
            name: _capability_tuple(raw[name], provider, name)
            for name in ("auto_allow", "blocked", "aux_web")
        }
        if set(groups["aux_web"]) - set(groups["blocked"]):
            raise ValidationError("native web capabilities must be blocked by default")
        if set(groups["auto_allow"]) & set(groups["blocked"]):
            raise ValidationError("native capability has conflicting defaults")
        declared = (*groups["auto_allow"], *groups["blocked"])
        if len(set(declared)) != len(declared):
            raise ValidationError("duplicate native capability declaration")
        return cls(
            schema=NATIVE_CAPABILITY_SCHEMA,
            provider=provider,
            inventory_complete=False,
            enforcement_locus=locus,
            evidence=evidence,
            auto_allow=groups["auto_allow"],
            blocked=groups["blocked"],
            aux_web=groups["aux_web"],
        )

    def compile(self, *, allow_read: bool, allow_web: bool,
                permission_callback: bool) -> EffectiveNativePolicy:
        enabled: list[str] = []
        approval: list[str] = []
        blocked = list(self.blocked)
        if permission_callback:
            (enabled if allow_read else approval).extend(self.auto_allow)
            if allow_web:
                for capability_id in self.aux_web:
                    blocked.remove(capability_id)
                    approval.append(capability_id)
        else:
            # A provider deny flag is the only enforceable fallback when no
            # verified callback can ask AgentBridge. Broker-dependent tools do
            # not silently become native ambient authority.
            blocked.extend(self.auto_allow)
        blocked = list(dict.fromkeys(blocked))
        return EffectiveNativePolicy(
            schema_version=NATIVE_CAPABILITY_SCHEMA,
            provider=self.provider,
            inventory_complete=self.inventory_complete,
            enforcement_locus=self.enforcement_locus,
            evidence=self.evidence,
            enabled=tuple(enabled),
            approval_gated=tuple(approval),
            blocked=tuple(blocked),
            auto_allow_tools=_tools(enabled),
            blocked_tools=_tools(blocked),
            permission_callback=permission_callback,
        )

    def public_facts(self) -> dict:
        policy = self.compile(
            allow_read=True, allow_web=False, permission_callback=False)
        return {
            "declared": True,
            **policy.public_facts(),
            "limitation": (
                "known configured tools only; provider inventory is not "
                "version-bound or exhaustive"
            ),
            "execution_ready": False,
            "quarantine_reason": (
                "provider-native inventory is not version-bound and exhaustive"
            ),
        }


def _capability_tuple(value: object, provider: str, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value):
        raise ValidationError(f"native {name} must be a string list")
    result = tuple(value)
    unknown = set(result) - NATIVE_CAPABILITIES.keys()
    wrong = {item for item in result
             if item in NATIVE_CAPABILITIES
             and NATIVE_CAPABILITIES[item].provider != provider}
    if unknown or wrong:
        raise ValidationError(
            f"unknown native capability: {sorted(unknown or wrong)}")
    if len(set(result)) != len(result):
        raise ValidationError(f"duplicate native {name} capability")
    return result


def _tools(capability_ids) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        tool for capability_id in capability_ids
        for tool in NATIVE_CAPABILITIES[capability_id].tools
    ))


def native_capability_report() -> dict:
    return {
        "schema_version": NATIVE_CAPABILITY_SCHEMA,
        "capabilities": [NATIVE_CAPABILITIES[name].public_facts()
                         for name in sorted(NATIVE_CAPABILITIES)],
    }
