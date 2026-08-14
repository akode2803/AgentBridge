from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness.adapters import ModelRegistry, Preset
from agentbridge.harness.adapters.policy import (
    BridgeProfile, compile_bridge_policy,
)
from agentbridge.harness.adapters.native import NATIVE_CAPABILITIES
from agentbridge.harness.adapters.registry import PRESET_DIR
from agentbridge.harness.capabilities import compile_capability_ceiling


def codex_profile() -> BridgeProfile:
    raw = json.loads((PRESET_DIR / "codex.json").read_text(encoding="utf-8"))
    return Preset.from_dict(raw, trusted=True).bridge_profile


def test_bridge_profile_schema_is_strict():
    raw = json.loads((PRESET_DIR / "codex.json").read_text(encoding="utf-8"))[
        "bridge_profile"]
    with pytest.raises(ValidationError, match="fields"):
        BridgeProfile.from_dict({**raw, "surprise": True})
    with pytest.raises(ValidationError, match="capability"):
        BridgeProfile.from_dict({**raw, "capabilities": ["*"]})
    with pytest.raises(ValidationError, match="anchored"):
        BridgeProfile.from_dict({**raw, "version_pattern": "codex"})
    with pytest.raises(ValidationError, match="reviewed version"):
        BridgeProfile.from_dict({
            **raw, "version_pattern": "^codex-cli 0\\.144\\.6$",
        })


def test_capability_ceiling_rejects_unknown_control_and_undeclared_ids():
    profile = codex_profile()
    assert compile_capability_ceiling(
        profile, {"delegate_agent"}) == ("delegate_agent",)
    with pytest.raises(ValidationError, match="unknown bridge capability"):
        compile_capability_ceiling(profile, {"future_tool"})
    with pytest.raises(ValidationError, match="unknown bridge capability"):
        compile_capability_ceiling(profile, {"codex.process_exec"})
    with pytest.raises(ValidationError, match="not a model capability"):
        compile_capability_ceiling(profile, {"approve"})
    with pytest.raises(ValidationError, match="no trusted bridge profile"):
        compile_capability_ceiling(None, {"delegate_agent"})


def test_owner_overlay_cannot_claim_trusted_bridge(tmp_path):
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    shipped = json.loads((PRESET_DIR / "codex.json").read_text(encoding="utf-8"))
    (adapters / "owner.json").write_text(json.dumps({
        "id": "owner", "command": "owner-cli", "format": "text",
        "bridge_profile": shipped["bridge_profile"],
    }), encoding="utf-8")
    preset = ModelRegistry.load(tmp_path).presets["owner"]
    assert preset.bridge_profile is None
    assert "cannot attach" in preset.bridge_unavailable_reason


def test_shipped_codex_does_not_forward_api_or_endpoint_credentials(tmp_path):
    preset = ModelRegistry.load(tmp_path).presets["codex"]
    assert preset.env_allow == ["CODEX_HOME"]


def test_compiler_binds_version_filters_overlay_and_renders_exact_tools(
        tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_context_window = 123456\n'
        'personality = "pragmatic"\n'
        '[mcp_servers.host]\nurl = "https://unrelated.invalid"\n'
        '[permissions.host]\nextends = ":danger-full-access"\n',
        encoding="utf-8",
    )
    import agentbridge.harness.adapters.policy as module
    monkeypatch.setattr(module.shutil, "which", lambda _command: "/tmp/codex")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="codex-cli 0.144.5\n",
                                        stderr=""))
    policy = compile_bridge_policy(
        codex_profile(), command="codex", workspace=tmp_path / "work",
        timeout_s=900, requested_capabilities={"delegate_agent"},
        source_env={"PATH": "/bin", "CODEX_HOME": str(codex_home)},
    )
    assert policy.executable == str(Path("/tmp/codex").resolve())
    assert policy.executable_version == "codex-cli 0.144.5"
    assert "OPENAI_API_KEY" in policy.blocked_env
    assert "HTTPS_PROXY" in policy.blocked_env
    rendered = "\n".join(policy.attachment_args(
        url="http://127.0.0.1:8123/mcp"))
    assert 'enabled_tools=["delegate_agent"]' in rendered
    assert "tools.delegate_agent.approval_mode" in rendered
    assert "AGENTBRIDGE_MCP_TOKEN" in rendered
    assert "unrelated.invalid" not in rendered
    assert "danger-full-access" not in rendered
    assert "model_context_window=123456" in rendered
    assert "personality=\"pragmatic\"" in rendered
    assert 'filesystem={":root" = "deny"' in rendered
    assert 'trust_level = "untrusted"' in rendered
    assert "features.multi_agent=false" in rendered
    assert 'web_search="disabled"' in rendered
    assert "features.apps=false" in rendered
    assert "features.plugins=false" in rendered
    assert "--sandbox" not in rendered
    clean = policy.sanitize_environment({
        "PATH": "/bin", "HTTPS_PROXY": "https://user:secret@example.test",
        "OPENAI_API_KEY": "secret", "AGENTBRIDGE_MCP_TOKEN": "run-token",
    })
    assert clean == {"PATH": "/bin", "AGENTBRIDGE_MCP_TOKEN": "run-token"}
    native = policy.native_policy()
    assert native.inventory_complete is True
    assert native.provider == "codex"
    assert native.approval_gated == ("codex.agentbridge_mcp",)
    assert {"codex.workspace_read", "codex.workspace_write",
            "codex.process_exec", "codex.workspace_binding"} \
        <= set(native.enabled)
    assert {"codex.network", "codex.web_search", "codex.external_mcp",
            "codex.user_config", "codex.rules", "codex.provider_prompts",
            "codex.session_persistence", "codex.project_trust",
            "codex.apps", "codex.plugins", "codex.hooks",
            "codex.multi_agent", "codex.browser", "codex.computer_use",
            "codex.image_generation", "codex.workspace_dependencies",
            "codex.memories", "codex.endpoint_override"} <= set(native.blocked)
    assert native.authority_digest(policy.executable_version) != \
        native.authority_digest("codex-cli 0.144.6")
    assert policy.authority_digest() != replace(
        policy, launch_args=(*policy.launch_args, "--dangerously-broaden"),
    ).authority_digest()
    assert policy.authority_digest() != replace(
        policy, workspace=str(tmp_path / "other-workspace"),
    ).authority_digest()


def test_codex_catalog_controls_are_not_misreported_as_callback_tools():
    codex = [spec for spec in NATIVE_CAPABILITIES.values()
             if spec.provider == "codex"]
    assert codex and all(not spec.tools and spec.controls for spec in codex)
    controls = {control for spec in codex for control in spec.controls}
    assert {"--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--strict-config", "approval_policy", "default_permissions",
            "permissions.agentbridge-run.filesystem",
            "permissions.agentbridge-run.network.enabled", "web_search",
            "features.apps", "features.plugins", "features.hooks",
            "features.multi_agent", "features.computer_use",
            "features.image_generation", "features.workspace_dependencies",
            "features.memories", "mcp_servers.ab"} <= controls


def test_compiler_rejects_unverified_version(monkeypatch, tmp_path):
    import agentbridge.harness.adapters.policy as module
    monkeypatch.setattr(module.shutil, "which", lambda _command: "/tmp/codex")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="codex-cli 9.9.9\n",
                                        stderr=""))
    with pytest.raises(ValidationError, match="unverified provider version"):
        compile_bridge_policy(
            codex_profile(), command="codex", workspace=tmp_path,
            timeout_s=30, requested_capabilities={"delegate_agent"},
            source_env={"PATH": "/bin"},
        )


def test_compiler_keeps_native_isolation_without_bridge_tools(
        monkeypatch, tmp_path):
    import agentbridge.harness.adapters.policy as module
    monkeypatch.setattr(module.shutil, "which", lambda _command: "/tmp/codex")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="codex-cli 0.144.5\n",
                                        stderr=""))
    policy = compile_bridge_policy(
        codex_profile(), command="codex", workspace=tmp_path,
        timeout_s=30, requested_capabilities=set(), source_env={"PATH": "/bin"},
    )
    assert policy.capabilities == ()
    assert "--ignore-user-config" in policy.launch_args
    assert "--sandbox" not in policy.launch_args
    preset = ModelRegistry.load(tmp_path / "home").presets["codex"]
    argv = preset.build_argv(
        prompt="p", workdir=str(tmp_path), reply_file="", command=policy.executable,
        bridge_args=policy.launch_args, include_safety=False,
    )
    assert argv[0] == policy.executable
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--ephemeral" in argv
    assert 'default_permissions="agentbridge-run"' in argv
    assert "--sandbox" not in argv
