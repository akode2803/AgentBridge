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
from agentbridge.harness.adapters import policy as policy_module
from agentbridge.harness.adapters.registry import PRESET_DIR
from agentbridge.harness.capabilities import compile_capability_ceiling
from agentbridge.harness.settings import HarnessSettings


def codex_profile() -> BridgeProfile:
    raw = json.loads((PRESET_DIR / "codex.json").read_text(encoding="utf-8"))
    return Preset.from_dict(raw, trusted=True).bridge_profile


def mock_codex_admission(monkeypatch, *, executable="/tmp/codex"):
    monkeypatch.setattr(
        policy_module, "_codex_binary_identity",
        lambda _path: (
            "a" * 64, "/tmp/codex-code-mode-host", "b" * 64, "2DC432GLL2",
        ),
    )
    monkeypatch.setattr(
        policy_module, "_codex_non_user_config_layers",
        lambda *_args: ("project:sha256:" + "c" * 64,
                        "system:sha256:" + "d" * 64),
    )


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
            **raw, "version_pattern": "^codex-cli 0\\.147\\.1$",
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
    user_skill = tmp_path / "home" / ".agents" / "skills" / "user-skill"
    codex_skill = codex_home / "skills" / "codex-skill"
    workspace_skill = tmp_path / "work" / ".agents" / "skills" / "repo-skill"
    for skill in (user_skill, codex_skill, workspace_skill):
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("untrusted skill", encoding="utf-8")
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
                        SimpleNamespace(returncode=0, stdout="codex-cli 0.147.0\n",
                                        stderr=""))
    mock_codex_admission(monkeypatch)
    policy = compile_bridge_policy(
        codex_profile(), command="codex", workspace=tmp_path / "work",
        timeout_s=900, requested_capabilities={"delegate_agent"},
        source_env={
            "PATH": "/bin", "HOME": str(tmp_path / "home"),
            "CODEX_HOME": str(codex_home),
        },
    )
    assert policy.executable == str(Path("/tmp/codex").resolve())
    assert policy.executable_version == "codex-cli 0.147.0"
    assert policy.executable_sha256 == "a" * 64
    assert policy.code_mode_host_sha256 == "b" * 64
    assert policy.signing_team == "2DC432GLL2"
    assert len(policy.config_layers) == 2
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
    assert str((tmp_path / "work").resolve()) in rendered
    assert '":workspace_roots"' not in rendered
    assert 'trust_level = "untrusted"' in rendered
    assert "features.multi_agent=false" in rendered
    assert 'web_search="disabled"' in rendered
    assert "features.apps=false" in rendered
    assert "features.plugins=false" in rendered
    assert "features.code_mode_host.enabled=true" in rendered
    assert "features.code_mode_host.disable_in_process_fallback=true" in rendered
    assert "features.tool_registry.error_on_tool_collisions=true" in rendered
    assert "features.shell_snapshot=false" in rendered
    assert "features.view_image=false" in rendered
    assert "skills.include_instructions=false" in rendered
    assert "skills.bundled.enabled=false" in rendered
    assert "skills.config=[" in rendered
    assert all(str((skill / "SKILL.md").resolve()) in rendered
               for skill in (user_skill, codex_skill, workspace_skill))
    assert "features.auth_elicitation=false" in rendered
    assert "features.tool_call_mcp_elicitation=false" in rendered
    assert "features.remote_plugin=false" in rendered
    assert "features.plugin_sharing=false" in rendered
    assert "--sandbox" not in rendered
    invocation_overrides = NATIVE_CAPABILITIES[
        "codex.invocation_overrides"
    ].controls
    assert all(control not in policy.launch_args for control in invocation_overrides)
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
            "codex.skills", "codex.memories", "codex.shell_snapshot",
            "codex.view_image", "codex.provider_approvals",
            "codex.elicitation", "codex.goals", "codex.in_app_updates",
            "codex.network_proxy", "codex.invocation_overrides",
            "codex.endpoint_override"} <= set(native.blocked)
    for capability_id in native.blocked:
        for control in NATIVE_CAPABILITIES[capability_id].controls:
            if control.startswith(("features.", "skills.", "memories.")):
                expected = ("skills.config=[" if control == "skills.config"
                            else f"{control}=false")
                assert expected in rendered
    assert native.authority_digest(policy.executable_version) != \
        native.authority_digest("codex-cli 0.147.1")
    assert policy.authority_digest() != replace(
        policy, launch_args=(*policy.launch_args, "--dangerously-broaden"),
    ).authority_digest()
    assert policy.authority_digest() != replace(
        policy, workspace=str(tmp_path / "other-workspace"),
    ).authority_digest()
    assert policy.authority_digest() != replace(
        policy, executable_sha256="f" * 64,
    ).authority_digest()
    assert policy.authority_digest() != replace(
        policy, code_mode_host_sha256="e" * 64,
    ).authority_digest()
    assert policy.authority_digest() != replace(
        policy, config_layers=("system:sha256:" + "0" * 64,),
    ).authority_digest()
    executable = tmp_path / "signed-codex"
    host = tmp_path / "codex-code-mode-host"
    executable.write_bytes(b"codex")
    host.write_bytes(b"host")
    launch_policy = replace(
        policy, executable=str(executable), code_mode_host=str(host),
        executable_sha256=policy_module._sha256_file(executable),
        code_mode_host_sha256=policy_module._sha256_file(host),
    )
    launch_policy.verify_unchanged(source_env={})
    host.write_bytes(b"changed")
    with pytest.raises(ValidationError, match="identity changed"):
        launch_policy.verify_unchanged(source_env={})


def test_codex_0147_catalog_uses_current_models_and_exact_efforts():
    preset = ModelRegistry.load().presets["codex"]
    assert preset.default_model == "gpt-5.6-luna"
    assert preset.models == [
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
    ]
    assert preset.efforts_for("gpt-5.4") == [
        "low", "medium", "high", "xhigh",
    ]
    assert preset.efforts_for("gpt-5.6-luna") == [
        "low", "medium", "high", "xhigh", "max",
    ]
    assert preset.efforts_for("gpt-5.6-sol")[-2:] == ["max", "ultra"]


def test_exact_codex_profile_rejects_stale_model_and_effort_before_launch():
    registry = ModelRegistry.load()
    with pytest.raises(ValidationError, match="does not support configured model"):
        registry.resolve(HarnessSettings(
            adapter="codex", model="gpt-5.1-codex", reasoning="high",
        ), "direct")
    with pytest.raises(ValidationError, match="reasoning level"):
        registry.resolve(HarnessSettings(
            adapter="codex", model="gpt-5.4", reasoning="ultra",
        ), "direct")
    invocation = registry.resolve(HarnessSettings(
        adapter="codex", model="gpt-5.6-sol", reasoning="ultra",
    ), "direct")
    assert invocation.model == "gpt-5.6-sol"
    assert invocation.effort == "ultra"
    default = registry.resolve(HarnessSettings(adapter="codex"), "direct")
    assert default.model == "gpt-5.6-luna"


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
            "features.skill_search", "skills.include_instructions",
            "features.memories", "features.shell_snapshot", "features.view_image",
            "features.guardian_approval", "features.auth_elicitation",
            "features.network_proxy", "mcp_servers.ab"} <= controls


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
                        SimpleNamespace(returncode=0, stdout="codex-cli 0.147.0\n",
                                        stderr=""))
    mock_codex_admission(monkeypatch)
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


def test_non_user_config_layers_reject_unreviewed_authority():
    base = [
        {"name": {"type": "project"}, "version": "sha256:" + "1" * 64,
         "config": {"default_permissions": "host", "approval_policy": "on-request"}},
        {"name": {"type": "user"}, "version": "sha256:" + "2" * 64,
         "config": {"mcp_servers": {"ignored-by-exec": {"url": "https://x"}}}},
        {"name": {"type": "system"}, "version": "sha256:" + "3" * 64,
         "config": {}},
    ]
    assert policy_module._validate_codex_config_layers(base) == (
        "project:sha256:" + "1" * 64,
        "system:sha256:" + "3" * 64,
    )
    for layer_type, config in (
        ("project", {"mcp_servers": {"outside": {"url": "https://x"}}}),
        ("system", {"openai_base_url": "https://proxy.invalid"}),
        ("enterpriseManaged", {"model_providers": {"openai": {}}}),
        ("legacyManagedConfigTomlFromMdm", {"features": {"apps": True}}),
    ):
        layers = [
            {"name": {"type": layer_type}, "version": "sha256:" + "4" * 64,
             "config": config},
        ]
        with pytest.raises(ValidationError, match="unreviewed authority"):
            policy_module._validate_codex_config_layers(layers)
