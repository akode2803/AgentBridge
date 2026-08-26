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
import select
import shutil
import subprocess
import sys
import time
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
        if raw["renderer"] != "codex-0.147":
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
            schema=1, provider="codex", renderer="codex-0.147",
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
    executable_sha256: str
    code_mode_host: str
    code_mode_host_sha256: str
    signing_team: str
    config_layers: tuple[str, ...]
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
            "executable_sha256": self.executable_sha256,
            "code_mode_host": self.code_mode_host,
            "code_mode_host_sha256": self.code_mode_host_sha256,
            "signing_team": self.signing_team,
            "config_layers": self.config_layers,
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

    def verify_unchanged(self, source_env=None) -> None:
        """Recheck mutable executable and lower-layer facts before launch."""
        if (_sha256_file(Path(self.executable)) != self.executable_sha256
                or _sha256_file(Path(self.code_mode_host))
                != self.code_mode_host_sha256):
            raise ValidationError("Codex executable identity changed after signing")
        current_layers = _codex_non_user_config_layers(
            self.executable, Path(self.workspace), source_env,
        )
        if current_layers != self.config_layers:
            raise ValidationError("Codex config authority changed after signing")


def compile_bridge_policy(profile: BridgeProfile, *, command: str,
                          workspace: Path, timeout_s: float,
                          requested_capabilities: set[str] | frozenset[str],
                          source_env=None, observe=None) -> CompiledBridgePolicy:
    """Validate the installed provider and freeze one fresh-run policy."""
    def measured(name: str, fn):
        started = time.perf_counter()
        value = fn()
        if callable(observe):
            observe(name, time.perf_counter() - started)
        return value

    executable = shutil.which(command) or (command if Path(command).is_file() else "")
    if not executable:
        raise ValidationError("trusted bridge provider is not installed")
    executable = str(Path(executable).resolve())
    resolved_workspace = str(workspace.resolve())
    try:
        result = measured("provider_version", lambda: subprocess.run(
            [executable, *profile.version_args], capture_output=True, text=True,
            timeout=5, check=False, env=_probe_env(source_env)))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("could not verify the bridge provider version") from exc
    version = " ".join((result.stdout or result.stderr).split())
    if result.returncode or not re.fullmatch(profile.version_pattern, version):
        raise ValidationError(
            f"bridge disabled for unverified provider version {version or 'unknown'!r}")
    executable_sha256, code_mode_host, code_mode_host_sha256, signing_team = \
        measured("provider_identity", lambda: _codex_binary_identity(executable))
    config_layers = measured(
        "config_layers", lambda: _codex_non_user_config_layers(
            executable, Path(resolved_workspace), source_env))
    capabilities = measured(
        "capability_inventory", lambda: compile_capability_ceiling(
            profile, requested_capabilities))
    overlay = measured("safe_overlay", lambda: _safe_codex_overlay(
        profile, source_env))
    disabled_skills = measured(
        "skill_inventory", lambda: _disabled_codex_skill_config(
            source_env, Path(resolved_workspace)))
    launch = [
        "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config",
        "-c", 'approval_policy="never"',
        "-c", 'default_permissions="agentbridge-run"',
        "-c", 'permissions.agentbridge-run.description="AgentBridge workspace only"',
        "-c", ('permissions.agentbridge-run.filesystem={":root" = "deny", '
               '":minimal" = "read", %s = "write"}' %
               _toml_literal(resolved_workspace)),
        "-c", 'permissions.agentbridge-run.network.enabled=false',
        "-c", ('projects={%s = {trust_level = "untrusted"}}' %
               _toml_literal(resolved_workspace)),
        "-c", 'project_doc_max_bytes=0',
        "-c", 'web_search="disabled"',
        "-c", 'features.tool_registry.error_on_tool_collisions=true',
        "-c", 'features.shell_tool=true',
        "-c", 'features.unified_exec=true',
        "-c", 'features.code_mode_host.enabled=true',
        "-c", 'features.code_mode_host.disable_in_process_fallback=true',
        "-c", 'features.shell_snapshot=false',
        "-c", 'features.view_image=false',
        "-c", 'features.apps=false',
        "-c", 'features.tool_suggest=false',
        "-c", 'features.plugins=false',
        "-c", 'features.remote_plugin=false',
        "-c", 'features.plugin_sharing=false',
        "-c", 'features.recommended_plugins=false',
        "-c", 'features.hooks=false',
        "-c", 'features.multi_agent=false',
        "-c", 'features.multi_agent_v2=false',
        "-c", 'features.browser_use=false',
        "-c", 'features.browser_use_external=false',
        "-c", 'features.browser_use_full_cdp_access=false',
        "-c", 'features.computer_use=false',
        "-c", 'features.image_generation=false',
        "-c", 'features.in_app_browser=false',
        "-c", 'features.in_app_updates=false',
        "-c", 'features.workspace_dependencies=false',
        "-c", 'features.executor_capability_discovery=false',
        "-c", 'features.skill_mcp_dependency_install=false',
        "-c", 'features.skill_search=false',
        "-c", 'skills.include_instructions=false',
        "-c", 'skills.bundled.enabled=false',
        "-c", f'skills.config={disabled_skills}',
        "-c", 'features.memories=false',
        "-c", 'memories.use_memories=false',
        "-c", 'features.guardian_approval=false',
        "-c", 'features.guardianv2=false',
        "-c", 'features.request_permissions_tool=false',
        "-c", 'features.exec_permission_approvals=false',
        "-c", 'features.tool_call_mcp_elicitation=false',
        "-c", 'features.auth_elicitation=false',
        "-c", 'features.enable_mcp_apps=false',
        "-c", 'features.mcp_2026_07_28=false',
        "-c", 'features.non_prefixed_mcp_tool_names=false',
        "-c", 'features.goals=false',
        "-c", 'features.network_proxy=false',
        "-c", 'features.respect_system_proxy=false',
    ]
    for key, value in overlay.items():
        launch.extend(("-c", f"{key}={_toml_literal(value)}"))
    return CompiledBridgePolicy(
        executable=executable, executable_version=version,
        executable_sha256=executable_sha256,
        code_mode_host=code_mode_host,
        code_mode_host_sha256=code_mode_host_sha256,
        signing_team=signing_team, config_layers=config_layers,
        workspace=resolved_workspace,
        renderer=profile.renderer, capabilities=capabilities,
        launch_args=tuple(launch),
        tool_timeout_s=max(30, min(3600, int(max(timeout_s, 1) + 60))),
        blocked_env=profile.blocked_env,
    )


def _codex_binary_identity(executable: str) -> tuple[str, str, str, str]:
    """Bind the two OpenAI-signed macOS executables used by code-mode runs."""
    if sys.platform != "darwin":
        raise ValidationError(
            "the exact Codex bridge is currently admitted only on macOS")
    executable_path = Path(executable)
    package_root = executable_path.parent.parent
    host_candidates: list[Path] = []
    metadata_path = package_root / "codex-package.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        metadata = {}
    resources_dir = metadata.get("resourcesDir")
    if isinstance(resources_dir, str) and resources_dir:
        host_candidates.append(package_root / resources_dir / "codex-code-mode-host")
    host_candidates.append(executable_path.with_name("codex-code-mode-host"))
    host_path = next((path.resolve() for path in host_candidates if path.is_file()), None)
    if host_path is None:
        raise ValidationError("could not resolve the Codex code-mode host")

    team = "2DC432GLL2"
    for path, identifier in (
        (executable_path, "codex"), (host_path, "codex-code-mode-host"),
    ):
        try:
            verify = subprocess.run(
                ["/usr/bin/codesign", "--verify", "--strict", str(path)],
                capture_output=True, text=True, timeout=10, check=False,
            )
            details = subprocess.run(
                ["/usr/bin/codesign", "-dv", "--verbose=4", str(path)],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationError("could not verify the Codex code signature") from exc
        signed = "\n".join((details.stdout, details.stderr))
        if (verify.returncode or details.returncode
                or f"Identifier={identifier}" not in signed
                or f"TeamIdentifier={team}" not in signed
                or "Authority=Developer ID Application: OpenAI OpCo, LLC" not in signed):
            raise ValidationError("Codex executable is not signed by the reviewed publisher")
    return (
        _sha256_file(executable_path), str(host_path), _sha256_file(host_path), team,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError("could not fingerprint a Codex executable") from exc
    return digest.hexdigest()


def _codex_non_user_config_layers(
        executable: str, workspace: Path, source_env) -> tuple[str, ...]:
    """Inspect resolved project, cloud, system, and managed config layers.

    ``--ignore-user-config`` excludes only the user layer.  The remaining
    layers are admitted only when empty, except for the two project settings
    that this launch overrides with higher-precedence session flags.
    """
    process = None
    try:
        process = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            cwd=str(workspace), env=_probe_env(source_env),
        )
        assert process.stdin is not None and process.stdout is not None
        _write_rpc(process, {
            "method": "initialize", "id": 1,
            "params": {"clientInfo": {
                "name": "agentbridge", "title": "AgentBridge", "version": "0.24",
            }},
        })
        _read_rpc_response(process, 1, timeout_s=15)
        _write_rpc(process, {"method": "initialized", "params": {}})
        _write_rpc(process, {
            "method": "config/read", "id": 2,
            "params": {"includeLayers": True, "cwd": str(workspace)},
        })
        response = _read_rpc_response(process, 2, timeout_s=20)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValidationError("could not inspect the effective Codex config layers") from exc
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    result = response.get("result")
    layers = result.get("layers") if isinstance(result, dict) else None
    if not isinstance(layers, list):
        raise ValidationError("Codex did not return inspectable config layers")
    return _validate_codex_config_layers(layers)


def _validate_codex_config_layers(layers: list) -> tuple[str, ...]:
    admitted: list[str] = []
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
            raise ValidationError("Codex returned an invalid config layer")
        layer_type = layer["name"].get("type")
        if layer_type == "user":
            continue
        config = layer.get("config") or {}
        if not isinstance(config, dict):
            raise ValidationError("Codex returned an invalid config layer")
        allowed = {"default_permissions", "approval_policy"} \
            if layer_type == "project" else set()
        if set(config) - allowed:
            raise ValidationError(
                f"Codex {layer_type or 'unknown'} config adds unreviewed authority")
        version = layer.get("version")
        if not isinstance(version, str) or not version.startswith("sha256:"):
            raise ValidationError("Codex config layer has no stable fingerprint")
        admitted.append(f"{layer_type}:{version}")
    return tuple(sorted(admitted))


def _write_rpc(process: subprocess.Popen, payload: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_rpc_response(
        process: subprocess.Popen, request_id: int, *, timeout_s: float) -> dict:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        ready, _, _ = select.select(
            [process.stdout], [], [], min(0.25, deadline - time.monotonic()),
        )
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            break
        payload = json.loads(line)
        if payload.get("id") == request_id:
            if "error" in payload:
                raise ValueError("Codex config RPC failed")
            return payload
    raise ValueError("Codex config RPC timed out")


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


def _disabled_codex_skill_config(source_env, workspace: Path) -> str:
    """Disable every currently discoverable host skill by canonical path.

    Codex 0.147 has no global skill-disable switch: hiding the catalog does not
    stop an explicit ``$skill`` mention.  Compile all fixed discovery roots
    into the signed launch policy and fail closed if their inventory cannot be
    bounded.  A fresh policy is compiled for every run.
    """
    source = os.environ if source_env is None else source_env
    user_home = Path(source.get("HOME") or "~").expanduser()
    codex_home = Path(
        source.get("CODEX_HOME") or user_home / ".codex"
    ).expanduser()
    roots = (
        codex_home / "skills",
        user_home / ".agents" / "skills",
        workspace / ".agents" / "skills",
        workspace / ".codex" / "skills",
    )
    paths: set[str] = set()
    seen_dirs: set[tuple[int, int]] = set()
    stack = [root for root in roots if root.exists()]
    try:
        while stack:
            current = stack.pop()
            stat = current.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen_dirs:
                continue
            seen_dirs.add(identity)
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=True):
                        stack.append(Path(entry.path))
                    elif entry.name == "SKILL.md" and entry.is_file(
                            follow_symlinks=True):
                        paths.add(str(Path(entry.path).resolve()))
                        if len(paths) > 4096:
                            raise ValidationError(
                                "Codex skill inventory exceeds the safe bound")
    except (OSError, RuntimeError) as exc:
        raise ValidationError("could not inventory Codex skill roots") from exc
    return "[" + ",".join(
        "{path=%s,enabled=false}" % _toml_literal(path)
        for path in sorted(paths)
    ) + "]"


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
