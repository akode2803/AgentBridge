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
    "CODEX_PROVIDER_VERSION",
    "NATIVE_CAPABILITIES", "NATIVE_CAPABILITY_SCHEMA",
    "NATIVE_DENY_ARG_TEMPLATES", "NativeCapabilitySpec", "NativeProfile",
    "EffectiveNativePolicy", "codex_native_policy", "native_capability_report",
    "validate_native_authority_facts",
]

NATIVE_CAPABILITY_SCHEMA = 2
CODEX_PROVIDER_VERSION = "codex-cli 0.144.5"
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
    controls: tuple[str, ...]
    surface: str
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
            "controls": list(self.controls),
            "surface": self.surface,
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
        controls=(),
        surface="provider-tool",
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


def _codex(capability_id: str, *controls: str, effect: str,
           risk: str = "high", surface: str = "provider-feature",
           approval: str = "package-compiled-provider-policy",
           enforcement_locus: str = "codex-0.144-config") \
        -> NativeCapabilitySpec:
    return NativeCapabilitySpec(
        id=capability_id, provider="codex", tools=(),
        controls=tuple(controls), surface=surface, effect=effect, risk=risk,
        approval=approval, enforcement_locus=enforcement_locus,
        evidence="https://developers.openai.com/codex/config-reference/",
        backend_minimum="brokered_native", failure_mode="deny",
    )


_SPECS = (
    _codex("codex.workspace_read", "permissions.agentbridge-run.filesystem",
           effect="host-file-read", surface="host-boundary"),
    _codex("codex.workspace_write", "permissions.agentbridge-run.filesystem",
           effect="host-file-write", risk="critical", surface="host-boundary"),
    _codex("codex.process_exec", "default_permissions",
           effect="host-process-exec", risk="critical", surface="host-boundary"),
    _codex("codex.workspace_binding", "-C", "projects",
           effect="workspace-scope-binding", risk="critical",
           surface="host-boundary"),
    _codex("codex.provider_prompts", "approval_policy",
           effect="provider-native-approval", risk="critical"),
    _codex("codex.session_persistence", "--ephemeral",
           effect="provider-persistent-state", risk="high"),
    _codex("codex.strict_config", "--strict-config",
           effect="provider-config-validation", risk="high",
           surface="configuration-source"),
    _codex("codex.project_trust", "projects.*.trust_level",
           effect="trusted-project-authority", risk="critical",
           surface="configuration-source"),
    _codex("codex.safe_overlay", "model_context_window",
           "model_auto_compact_token_limit", "personality", "service_tier",
           effect="reviewed-model-config-overlay", risk="medium",
           surface="configuration-source"),
    _codex("codex.network", "permissions.agentbridge-run.network.enabled",
           effect="network-read-or-write", risk="critical", surface="host-boundary"),
    _codex("codex.web_search", "web_search", effect="network-read"),
    _codex("codex.agentbridge_mcp", "mcp_servers.ab",
           effect="agentbridge-broker-tools", risk="high",
           surface="provider-tool-transport",
           approval="signed-bridge-ceiling+server-authority",
           enforcement_locus="codex-mcp-filter+agentbridge-server"),
    _codex("codex.external_mcp", "--ignore-user-config", "mcp_servers",
           effect="external-tool-access", risk="critical",
           surface="configuration-source"),
    _codex("codex.user_config", "--ignore-user-config",
           effect="provider-config-inheritance", risk="critical",
           surface="configuration-source"),
    _codex("codex.rules", "--ignore-rules", "project_doc_max_bytes",
           effect="instruction-inheritance", surface="configuration-source"),
    _codex("codex.apps", "features.apps", effect="external-tool-access"),
    _codex("codex.plugins", "features.plugins", effect="external-tool-access"),
    _codex("codex.hooks", "features.hooks", effect="host-process-exec",
           risk="critical"),
    _codex("codex.multi_agent", "features.multi_agent",
           effect="provider-delegation"),
    _codex("codex.browser", "features.browser_use",
           "features.browser_use_external", "features.browser_use_full_cdp_access",
           "features.in_app_browser", effect="browser-control", risk="critical"),
    _codex("codex.computer_use", "features.computer_use",
           effect="computer-control", risk="critical"),
    _codex("codex.image_generation", "features.image_generation",
           effect="external-service-use"),
    _codex("codex.workspace_dependencies", "features.workspace_dependencies",
           "features.skill_mcp_dependency_install", effect="dependency-install",
           risk="critical"),
    _codex("codex.memories", "features.memories", "memories.use_memories",
           effect="provider-persistent-state"),
    _codex("codex.endpoint_override", "OPENAI_BASE_URL",
           "CODEX_EVERYWHERE_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           effect="credential-or-endpoint-redirection", risk="critical",
           surface="environment-boundary"),
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
    enforcement_contract: tuple[str, ...]

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
            "enforcement_contract": self.enforcement_contract,
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


_CODEX_ENABLED = (
    "codex.workspace_read", "codex.workspace_write", "codex.process_exec",
    "codex.workspace_binding", "codex.strict_config", "codex.safe_overlay",
)
_CODEX_BLOCKED = tuple(
    spec.id for spec in _SPECS
    if spec.provider == "codex" and spec.id not in _CODEX_ENABLED
    and spec.id != "codex.agentbridge_mcp"
)


def codex_native_policy(*, bridge_attached: bool) -> EffectiveNativePolicy:
    """Return the reviewed effective authority of the exact Codex renderer."""
    enabled = _CODEX_ENABLED
    approval_gated = (("codex.agentbridge_mcp",) if bridge_attached else ())
    blocked = (*_CODEX_BLOCKED,
               *(("codex.agentbridge_mcp",) if not bridge_attached else ()))
    return EffectiveNativePolicy(
        schema_version=NATIVE_CAPABILITY_SCHEMA, provider="codex",
        inventory_complete=True,
        enforcement_locus="codex-0.144-config+agentbridge-server-authority",
        evidence="https://developers.openai.com/codex/config-reference/",
        enabled=tuple(enabled), approval_gated=approval_gated,
        blocked=tuple(blocked),
        auto_allow_tools=(), blocked_tools=(), permission_callback=False,
        enforcement_contract=(
            "codex-0.144-exact-version", "ignore-user-config", "ignore-rules",
            "strict-config", "fresh-ephemeral-run", "agentbridge-run-permissions",
            "credential-endpoint-env-filter", "optional-signed-mcp-ceiling",
        ),
    )


def validate_native_authority_facts(
    *, provider: str, provider_version: str, authority_digest: str,
    enabled: tuple[str, ...], approval_gated: tuple[str, ...],
    blocked: tuple[str, ...],
) -> None:
    """Reject unknown, cross-provider or non-canonical signed state facts."""
    groups = (enabled, approval_gated, blocked)
    ids = tuple(item for group in groups for item in group)
    if len(set(ids)) != len(ids):
        raise ValidationError("native capability states overlap")
    unknown = {
        item for item in ids
        if item not in NATIVE_CAPABILITIES
        or NATIVE_CAPABILITIES[item].provider != provider
    }
    if unknown:
        raise ValidationError(f"unknown native capability: {sorted(unknown)}")
    if provider == "codex":
        expected = codex_native_policy(
            bridge_attached="codex.agentbridge_mcp" in approval_gated,
        )
        if (provider_version != CODEX_PROVIDER_VERSION
                or enabled != expected.enabled
                or approval_gated != expected.approval_gated
                or blocked != expected.blocked
                or authority_digest
                != expected.authority_digest(provider_version)):
            raise ValidationError("non-canonical Codex native authority facts")


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
            enforcement_contract=NATIVE_DENY_ARG_TEMPLATES[self.provider],
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
