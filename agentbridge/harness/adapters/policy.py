"""Trusted provider bridge declarations and per-run policy compilation.

Bridge authority is package code, not owner-supplied adapter data.  A profile
is accepted only from a shipped preset, validated strictly, and bound to the
resolved executable/version before any bearer credential exists.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ...core.errors import ValidationError
from ..capabilities import BRIDGE_CAPABILITIES, compile_capability_ceiling
from .native import (
    CODEX_PROVIDER_VERSION, EffectiveNativePolicy, codex_native_policy,
)

__all__ = ["BridgeProfile", "CompiledBridgePolicy", "compile_bridge_policy"]

_PROFILE_FIELDS = {
    "schema", "provider", "renderer", "transport", "version_args",
    "version_pattern", "enforcement_locus", "config_isolation",
    "continuation", "capabilities", "safe_overlay_keys",
    "blocked_env",
}
_CAPABILITIES = {
    name for name, spec in BRIDGE_CAPABILITIES.items()
    if spec.surface == "model-capability"
}
_SAFE_OVERLAY_KEYS = {
    "model_context_window", "model_auto_compact_token_limit", "personality",
    "service_tier",
}


@dataclass(frozen=True)
class BridgeProfile:
    schema: int
    provider: str
    renderer: str
    transport: str
    version_args: tuple[str, ...]
    version_pattern: str
    enforcement_locus: str
    config_isolation: str
    continuation: str
    capabilities: tuple[str, ...]
    safe_overlay_keys: tuple[str, ...]
    blocked_env: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "BridgeProfile":
        if not isinstance(raw, dict):
            raise ValidationError("bridge profile must be an object")
        unknown = set(raw) - _PROFILE_FIELDS
        missing = _PROFILE_FIELDS - set(raw)
        if unknown or missing:
            detail = sorted(unknown or missing)
            raise ValidationError(f"invalid bridge profile fields: {detail}")
        if raw["schema"] != 1 or raw["provider"] != "codex":
            raise ValidationError("unsupported bridge profile schema/provider")
        if raw["renderer"] != "codex-0.144":
            raise ValidationError("unsupported bridge profile renderer")
        if raw["transport"] != "streamable-http-bearer":
            raise ValidationError("unsupported bridge transport")
        if raw["enforcement_locus"] != "provider-filter+server-authority":
            raise ValidationError("unsupported bridge enforcement locus")
        if raw["config_isolation"] != "ignore-user-config":
            raise ValidationError("unsupported bridge config isolation")
        if raw["continuation"] != "fresh-only":
            raise ValidationError("unsupported bridge continuation mode")
        version_args = _string_tuple(raw["version_args"], "version_args")
        capabilities = _string_tuple(raw["capabilities"], "capabilities")
        overlay = _string_tuple(raw["safe_overlay_keys"], "safe_overlay_keys")
        blocked_env = _string_tuple(raw["blocked_env"], "blocked_env")
        if not version_args or any("{" in item for item in version_args):
            raise ValidationError("invalid bridge version probe")
        if not capabilities or set(capabilities) - _CAPABILITIES:
            raise ValidationError("unknown or empty bridge capability set")
        if len(set(capabilities)) != len(capabilities):
            raise ValidationError("duplicate bridge capability")
        if set(overlay) - _SAFE_OVERLAY_KEYS:
            raise ValidationError("unsafe bridge overlay key")
        required_blocked = {
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_EVERYWHERE_API_KEY",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
            "https_proxy", "all_proxy",
        }
        if not required_blocked.issubset(blocked_env):
            raise ValidationError("bridge profile must block credential endpoints")
        pattern = raw["version_pattern"]
        if not isinstance(pattern, str) or not pattern.startswith("^") \
                or not pattern.endswith("$"):
            raise ValidationError("bridge version pattern must be anchored")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValidationError("invalid bridge version pattern") from exc
        reviewed_pattern = "^" + CODEX_PROVIDER_VERSION.replace(".", r"\.") + "$"
        if pattern != reviewed_pattern:
            raise ValidationError("bridge version pattern is not the reviewed version")
        return cls(
            schema=1, provider="codex", renderer="codex-0.144",
            transport="streamable-http-bearer", version_args=version_args,
            version_pattern=pattern,
            enforcement_locus="provider-filter+server-authority",
            config_isolation="ignore-user-config", continuation="fresh-only",
            capabilities=capabilities, safe_overlay_keys=overlay,
            blocked_env=blocked_env,
        )

    def public_facts(self) -> dict:
        return {
            "declared": True,
            "provider": self.provider,
            "transport": self.transport,
            "enforcement_locus": self.enforcement_locus,
            "config_isolation": self.config_isolation,
            "continuation": self.continuation,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class CompiledBridgePolicy:
    executable: str
    executable_version: str
    workspace: str
    renderer: str
    capabilities: tuple[str, ...]
    launch_args: tuple[str, ...]
    tool_timeout_s: int
    blocked_env: tuple[str, ...]

    def native_policy(self) -> EffectiveNativePolicy:
        return codex_native_policy(bridge_attached=bool(self.capabilities))

    def authority_digest(self) -> str:
        """Bind the complete non-token launch authority used by this run."""
        native = self.native_policy()
        payload = {
            "schema_version": native.schema_version,
            "native_policy_digest": native.authority_digest(
                self.executable_version),
            "executable": self.executable,
            "executable_version": self.executable_version,
            "workspace": self.workspace,
            "renderer": self.renderer,
            "capabilities": self.capabilities,
            "launch_args": self.launch_args,
            "tool_timeout_s": self.tool_timeout_s,
            "blocked_env": self.blocked_env,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def attachment_args(self, *, url: str) -> tuple[str, ...]:
        if not re.fullmatch(r"http://127\.0\.0\.1:\d+/mcp", url):
            raise ValidationError("bridge URL must be a loopback MCP endpoint")
        args = list(self.launch_args)
        settings = {
            "mcp_servers.ab.url": url,
            "mcp_servers.ab.bearer_token_env_var": "AGENTBRIDGE_MCP_TOKEN",
            "mcp_servers.ab.enabled": True,
            "mcp_servers.ab.required": True,
            "mcp_servers.ab.enabled_tools": list(self.capabilities),
            "mcp_servers.ab.default_tools_approval_mode": "prompt",
            "mcp_servers.ab.tool_timeout_sec": self.tool_timeout_s,
        }
        for capability in self.capabilities:
            settings[f"mcp_servers.ab.tools.{capability}.approval_mode"] = "approve"
        for key, value in settings.items():
            args.extend(("-c", f"{key}={_toml_literal(value)}"))
        return tuple(args)

    def sanitize_environment(self, env: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in env.items()
                if key not in self.blocked_env}


def compile_bridge_policy(profile: BridgeProfile, *, command: str,
                          workspace: Path, timeout_s: float,
                          requested_capabilities: set[str] | frozenset[str],
                          source_env=None) -> CompiledBridgePolicy:
    """Validate the installed provider and freeze one fresh-run policy."""
    executable = shutil.which(command) or (command if Path(command).is_file() else "")
    if not executable:
        raise ValidationError("trusted bridge provider is not installed")
    executable = str(Path(executable).resolve())
    resolved_workspace = str(workspace.resolve())
    try:
        result = subprocess.run(
            [executable, *profile.version_args], capture_output=True, text=True,
            timeout=5, check=False,
            env=_probe_env(source_env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("could not verify the bridge provider version") from exc
    version = " ".join((result.stdout or result.stderr).split())
    if result.returncode or not re.fullmatch(profile.version_pattern, version):
        raise ValidationError(
            f"bridge disabled for unverified provider version {version or 'unknown'!r}")
    capabilities = compile_capability_ceiling(profile, requested_capabilities)
    overlay = _safe_codex_overlay(profile, source_env)
    launch = [
        "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config",
        "-c", 'approval_policy="never"',
        "-c", 'default_permissions="agentbridge-run"',
        "-c", 'permissions.agentbridge-run.description="AgentBridge workspace only"',
        "-c", ('permissions.agentbridge-run.filesystem={":root" = "deny", '
               '":minimal" = "read", ":workspace_roots" = '
               '{"." = "write"}}'),
        "-c", 'permissions.agentbridge-run.network.enabled=false',
        "-c", ('projects={%s = {trust_level = "untrusted"}}' %
               _toml_literal(resolved_workspace)),
        "-c", 'project_doc_max_bytes=0',
        "-c", 'web_search="disabled"',
        "-c", 'features.apps=false',
        "-c", 'features.plugins=false',
        "-c", 'features.hooks=false',
        "-c", 'features.multi_agent=false',
        "-c", 'features.browser_use=false',
        "-c", 'features.browser_use_external=false',
        "-c", 'features.browser_use_full_cdp_access=false',
        "-c", 'features.computer_use=false',
        "-c", 'features.image_generation=false',
        "-c", 'features.in_app_browser=false',
        "-c", 'features.workspace_dependencies=false',
        "-c", 'features.skill_mcp_dependency_install=false',
        "-c", 'features.memories=false',
        "-c", 'memories.use_memories=false',
    ]
    for key, value in overlay.items():
        launch.extend(("-c", f"{key}={_toml_literal(value)}"))
    return CompiledBridgePolicy(
        executable=executable, executable_version=version,
        workspace=resolved_workspace,
        renderer=profile.renderer, capabilities=capabilities,
        launch_args=tuple(launch),
        tool_timeout_s=max(30, min(3600, int(max(timeout_s, 1) + 60))),
        blocked_env=profile.blocked_env,
    )


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in value):
        raise ValidationError(f"bridge {name} must be a non-empty string list")
    return tuple(value)


def _probe_env(source_env) -> dict[str, str]:
    source = os.environ if source_env is None else source_env
    return {name: str(source[name]) for name in ("PATH", "HOME", "CODEX_HOME")
            if name in source}


def _safe_codex_overlay(profile: BridgeProfile, source_env) -> dict:
    source = os.environ if source_env is None else source_env
    home = Path(source.get("CODEX_HOME") or Path(source.get("HOME", "~")) / ".codex")
    path = home.expanduser() / "config.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    out = {}
    for key in profile.safe_overlay_keys:
        value = raw.get(key)
        if key in ("model_context_window", "model_auto_compact_token_limit"):
            if type(value) is int and 1 <= value <= 2_000_000:
                out[key] = value
        elif key == "personality" and value in ("none", "friendly", "pragmatic"):
            out[key] = value
        elif key == "service_tier" and isinstance(value, str) \
                and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value):
            out[key] = value
    return out


def _toml_literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ",".join(_toml_literal(item) for item in value) + "]"
    raise ValidationError("unsafe provider config value")
