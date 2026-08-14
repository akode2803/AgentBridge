"""C4.2 provider-native inventory, compilation, and call authority."""

from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness.adapters.native import (
    NATIVE_CAPABILITIES, NATIVE_CAPABILITY_SCHEMA, native_capability_report,
)
from agentbridge.harness.adapters.registry import (
    PRESET_DIR, ModelRegistry, Preset, effective_gates,
    effective_native_policy,
)
from agentbridge.harness.broker import PermissionBroker
from agentbridge.harness.runtime.authority import capability_call_digest
from agentbridge.harness.runtime.authority import validate_run_authority
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.settings import HarnessSettings
from agentbridge.mesh.service import Mesh


def _settings(**values) -> HarnessSettings:
    class Agent:
        harness = values

    class Account:
        agent = Agent()

    return HarnessSettings.from_account(Account())


def test_shipped_native_profiles_are_complete_for_every_declared_tool_string():
    registry = ModelRegistry.load()
    assert {name for name, preset in registry.presets.items()
            if preset.native_profile is not None} == {"claude", "cortex"}
    for provider in ("claude", "cortex"):
        preset = registry.presets[provider]
        profile = preset.native_profile
        assert profile is not None and profile.provider == provider
        capability_ids = (*profile.auto_allow, *profile.blocked)
        assert set(capability_ids) == {
            item for item, spec in NATIVE_CAPABILITIES.items()
            if spec.provider == provider
        }
        assert all(NATIVE_CAPABILITIES[item].provider == provider
                   for item in capability_ids)
        declared_tools = {
            tool for item in capability_ids
            for tool in NATIVE_CAPABILITIES[item].tools
        }
        assert declared_tools == set(preset.auto_allow) | set(preset.blocklist)
        assert set(preset.aux_web) <= set(preset.blocklist)
        assert all(spec.failure_mode == "deny"
                   for spec in NATIVE_CAPABILITIES.values())


def test_no_permission_callback_hard_blocks_broker_dependent_native_tools():
    preset = ModelRegistry.load().presets["claude"]
    policy = effective_native_policy(
        preset, _settings(aux={"read": True, "web": True}),
        permission_callback=False,
    )
    assert policy is not None
    assert policy.enabled == () and policy.approval_gated == ()
    assert {"Read", "Write", "Edit", "Glob", "Grep", "TodoWrite",
            "WebFetch", "WebSearch"} \
        <= set(policy.blocked_tools)
    auto, blocked = effective_gates(
        preset, _settings(aux={"read": True, "web": True}),
        permission_callback=False,
    )
    assert auto == [] and set(blocked) == set(policy.blocked_tools)


def test_verified_callback_compiles_read_and_web_states_without_broadening_shell():
    preset = ModelRegistry.load().presets["claude"]
    enabled = effective_native_policy(
        preset, _settings(aux={"read": True, "web": True}),
        permission_callback=True,
    )
    assert enabled is not None
    assert set(enabled.auto_allow_tools) == {
        "Read", "Glob", "LS", "Grep", "TodoWrite",
    }
    assert set(enabled.approval_gated) == {
        "claude.web_fetch", "claude.web_search",
    }
    assert "claude.shell" in enabled.blocked and "Bash" in enabled.blocked_tools


def test_native_profile_rejects_unknown_ids_and_malformed_deny_flag_template():
    raw = json.loads((PRESET_DIR / "claude.json").read_text(encoding="utf-8"))
    raw["native_profile"]["auto_allow"][0] = "claude.future"
    with pytest.raises(ValidationError, match="unknown native capability"):
        Preset.from_dict(raw, trusted=True)

    raw = json.loads((PRESET_DIR / "claude.json").read_text(encoding="utf-8"))
    raw["blocklist_args"] = ["--disallowedTools"]
    with pytest.raises(ValidationError, match="reviewed provider template"):
        Preset.from_dict(raw, trusted=True)

    raw = json.loads((PRESET_DIR / "claude.json").read_text(encoding="utf-8"))
    raw["blocklist_args"] = ["--allowedTools", "{tool}"]
    with pytest.raises(ValidationError, match="reviewed provider template"):
        Preset.from_dict(raw, trusted=True)


def test_incomplete_native_inventory_is_quarantined(tmp_path):
    registry = ModelRegistry.load(tmp_path)
    registry._which["claude"] = True
    assert registry.runnable(registry.presets["claude"]) is False
    assert registry.presets["claude"] not in registry.installed()
    with pytest.raises(ValidationError, match="quarantined"):
        registry.resolve(_settings(adapter="claude"), "owner")


def test_same_id_owner_overlay_cannot_replace_shipped_native_policy(tmp_path):
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "claude.json").write_text(json.dumps({
        "id": "claude", "label": "Unsafe override", "command": "other-cli",
        "format": "text",
    }), encoding="utf-8")
    preset = ModelRegistry.load(tmp_path).presets["claude"]
    assert preset.command == "claude"
    assert preset.native_profile is not None


def test_native_registry_report_is_versioned_and_secret_free():
    report = native_capability_report()
    assert report["schema_version"] == NATIVE_CAPABILITY_SCHEMA
    assert {row["id"] for row in report["capabilities"]} == set(NATIVE_CAPABILITIES)
    encoded = json.dumps(report).lower()
    assert "token" not in encoded and "credential" not in encoded


def test_canonical_call_digest_binds_provider_tool_and_structured_input():
    first = capability_call_digest("claude", "Read", {"file_path": "a"})
    reordered = capability_call_digest("claude", "Read", {"file_path": "a"})
    assert first == reordered and len(first) == 64
    assert first != capability_call_digest(
        "claude", "Read", {"file_path": "b"})
    assert first != capability_call_digest(
        "cortex", "Read", {"file_path": "a"})


def test_native_authority_digest_binds_catalog_and_deny_template(
        tmp_path, monkeypatch):
    import agentbridge.harness.adapters.native as native_module

    policy = effective_native_policy(
        ModelRegistry.load(tmp_path).presets["claude"],
        _settings(aux={"read": True}), permission_callback=True,
    )
    assert policy is not None
    facts = NATIVE_CAPABILITIES["claude.file_read"].public_facts()
    assert facts["tools"] == ["Read"]
    assert facts["path_required"] is True
    baseline = policy.authority_digest()
    changed_catalog = dict(NATIVE_CAPABILITIES)
    changed_catalog["claude.file_read"] = replace(
        changed_catalog["claude.file_read"],
        tools=("Read", "FutureRead"), path_keys=("source",),
    )
    monkeypatch.setattr(
        native_module, "NATIVE_CAPABILITIES",
        MappingProxyType(changed_catalog),
    )
    assert policy.authority_digest() != baseline

    monkeypatch.setattr(native_module, "NATIVE_CAPABILITIES", NATIVE_CAPABILITIES)
    changed_templates = dict(native_module.NATIVE_DENY_ARG_TEMPLATES)
    changed_templates["claude"] = ("--future-deny", "{tool}")
    monkeypatch.setattr(
        native_module, "NATIVE_DENY_ARG_TEMPLATES",
        MappingProxyType(changed_templates),
    )
    assert policy.authority_digest() != baseline


@pytest.fixture()
def native_run(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("helper", harness={"adapter": "claude"})
    agent = Mesh(root, "helper", "box", encrypt=True, home=home,
                 store_path=tmp_path / "agent.sqlite")
    chat = owner.create_chat("Native", members=["helper"])
    owner.outbox.flush_once()
    agent.sync.sync_once([chat.id])
    workspace = home / "harness" / "helper" / "workspaces" / chat.id
    workspace.mkdir(parents=True)
    preset = ModelRegistry.load(home).presets["claude"]
    policy = effective_native_policy(
        preset, _settings(aux={"read": True}), permission_callback=True)
    assert policy is not None
    run = RunLedger(agent).start(
        run_id="run-native", chat_id=chat.id, trigger_id="message-native",
        provider="claude", model="provider-default-unattested",
        native_policy_digest=policy.authority_digest(),
    )
    try:
        yield owner, agent, chat.id, workspace, run, policy
    finally:
        agent.close()
        owner.close()


def test_native_call_classifies_before_workspace_allow_and_rechecks_run_authority(
        native_run):
    owner, agent, chat_id, workspace, run, policy = native_run
    broker = PermissionBroker(agent, "helper")
    validate_run_authority(
        agent, run, agent="helper", chat_id=chat_id,
        run_id="run-native", provider="claude", native_policy=policy)

    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="Read",
        tool_input={"file_path": str(workspace / "notes.md")},
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native", call_id="call-1",
        native_policy=policy, run_record=run,
    )
    assert allowed, reason

    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="FutureWrite",
        tool_input={"file_path": str(workspace / "notes.md")},
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native", call_id="call-2",
        native_policy=policy, run_record=run,
    )
    assert not allowed and "unknown" in reason

    owner.set_agent_harness("helper", {"adapter": "claude", "timeout_s": 90})
    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="Read",
        tool_input={"file_path": str(workspace / "notes.md")},
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native", call_id="call-3",
        native_policy=policy, run_record=run,
    )
    assert not allowed and "authority" in reason


def test_approval_gated_native_read_does_not_bypass_on_workspace_or_file_key(
        native_run):
    _owner, agent, chat_id, workspace, _run, _enabled_policy = native_run
    preset = ModelRegistry.load(agent.home).presets["claude"]
    gated = effective_native_policy(
        preset, _settings(aux={"read": False}), permission_callback=True)
    assert gated is not None and "claude.file_read" in gated.approval_gated
    run = RunLedger(agent).start(
        run_id="run-native-gated", chat_id=chat_id,
        trigger_id="message-native-gated", provider="claude",
        model="provider-default-unattested",
        native_policy_digest=gated.authority_digest(),
    )
    broker = PermissionBroker(agent, "helper")
    broker.ask = lambda **_values: ("deny", "owner denied")

    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="Read",
        tool_input={"file_path": str(workspace / "notes.md")},
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native-gated", call_id="call-gated-1",
        native_policy=gated, run_record=run,
    )
    assert not allowed and reason == "owner denied"

    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="Read",
        tool_input={"source": str(workspace.parent / "outside.md")},
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native-gated", call_id="call-gated-3",
        native_policy=gated, run_record=run,
    )
    assert not allowed and "path input" in reason

    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="Read",
        tool_input={
            "file_path": str(workspace / "inside.md"),
            "path": str(workspace.parent / "outside.md"),
        },
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native-gated", call_id="call-gated-4",
        native_policy=gated, run_record=run,
    )
    assert not allowed and "path input" in reason

    allowed, reason = broker.decide(
        chat_id=chat_id, workspace=workspace, tool="Read",
        tool_input={"file": str(workspace.parent / "outside.md")},
        auto_allow=[], approvals=[], timeout_s=1,
        run_id="run-native-gated", call_id="call-gated-2",
        native_policy=gated, run_record=run,
    )
    assert not allowed and reason == "owner denied"
