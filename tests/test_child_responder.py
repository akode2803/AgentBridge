"""V157 bounded, text-only child responder sidecar."""

from __future__ import annotations

import hashlib
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentbridge.harness.adapters.cli as cli_module
from agentbridge.core.errors import ValidationError
from agentbridge.harness.adapters.cli import (
    ChildRequest,
    CliResponder,
)
from agentbridge.harness.adapters.registry import ModelRegistry, Preset
from agentbridge.harness.responder import MESSAGE_BREAK, SILENCE


def _account(preset_id: str, *, model: str = "model-a") -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(harness={
        "adapter": preset_id,
        "model": model,
        "routing": {"agents": {"enabled": True}},
        "timeout_s": 30,
    }))


def _responder(tmp_path: Path, preset: Preset, *,
               account: SimpleNamespace | None = None) -> CliResponder:
    """Construct only the sidecar dependencies, without normal run stores."""
    registry = ModelRegistry({preset.id: preset})
    registry._which[preset.id] = True
    account = account or _account(preset.id)
    responder = object.__new__(CliResponder)
    responder.registry = registry
    responder.mesh = SimpleNamespace(
        user="specialist",
        directory=SimpleNamespace(get=lambda _name: account),
    )
    responder.agent = "specialist"
    responder.home = tmp_path / "agent-home"
    responder._minimal = set()
    return responder


def _request(*, context: str = "message m1 revision 3", bound: int = 200) \
        -> ChildRequest:
    return ChildRequest(
        objective="Check the calculation",
        success_criteria=("State the result", "Identify uncertainty"),
        rendered_context=context,
        max_output_chars=bound,
    )


def _fake_preset(**overrides) -> Preset:
    data = {
        "id": "fake-text",
        "label": "Fake text provider",
        "command": sys.executable,
        "args": ["--provider-mode", "{prompt}"],
        "args_minimal": ["{prompt}"],
        "safety_args": ["--safe"],
        "model_args": ["--model", "{model}"],
        "env_allow": ["FAKE_PROVIDER_TOKEN"],
        "format": "text",
        "child_text_only": True,
    }
    data.update(overrides)
    return Preset.from_dict(data)


def test_child_preset_declaration_is_explicit_and_fail_closed(tmp_path):
    registry = ModelRegistry.load(tmp_path / "empty-home")
    enabled = {
        preset.id for preset in registry.presets.values()
        if preset.is_child_text_only_safe()
    }
    assert enabled == {"ollama", "deepseek"}
    assert not registry.presets["codex"].child_text_only
    assert not registry.presets["claude"].child_text_only

    assert not Preset.from_dict({
        "id": "plain", "command": "plain", "format": "text",
    }).is_child_text_only_safe()
    assert not Preset(
        id="direct-string", command="plain", format="text",
        child_text_only="true",
    ).is_child_text_only_safe()
    assert not Preset.from_dict({
        "id": "string-flag", "command": "plain", "format": "text",
        "child_text_only": "true",
    }).is_child_text_only_safe()
    with pytest.raises(ValidationError, match="non-text invocation"):
        Preset.from_dict({
            "id": "contradiction", "command": "tool-cli",
            "format": "text", "child_text_only": True,
            "permission_args": ["--mcp-config", "{mcp_config}"],
        })

    overlay = tmp_path / "overlay-home" / "adapters"
    overlay.mkdir(parents=True)
    (overlay / "unsafe.json").write_text(
        '{"id":"unsafe","command":"python","format":"text",'
        '"child_text_only":true}', encoding="utf-8",
    )
    assert not ModelRegistry.load(
        tmp_path / "overlay-home",
    ).presets["unsafe"].is_child_text_only_safe()


def test_shipped_deepseek_formats_required_model_in_base_argv(tmp_path):
    preset = ModelRegistry.load(tmp_path / "empty-home").presets["deepseek"]
    argv = preset.build_argv(
        prompt="Return proof", workdir="/tmp/child", reply_file="",
        model="deepseek-coder:latest",
    )
    assert argv == ["ollama", "run", "deepseek-coder:latest", "Return proof"]


@pytest.mark.parametrize("preset_id", ["codex", "claude"])
def test_tool_capable_presets_are_refused_before_launch(
        tmp_path, monkeypatch, preset_id):
    shipped = ModelRegistry.load(tmp_path / "empty-home").presets[preset_id]
    responder = _responder(tmp_path, shipped, account=_account(preset_id))
    monkeypatch.setattr(
        responder, "_run_child_process",
        lambda *_args, **_kwargs: pytest.fail("unsafe preset was launched"),
    )

    with pytest.raises(ValidationError, match="not approved"):
        responder.prepare_child(_request(), chat_id="room-1")


def test_child_prompt_digest_is_deterministic_and_bound_to_exact_context(
        tmp_path):
    responder = _responder(tmp_path, _fake_preset())
    first = responder.prepare_child(_request(), chat_id="room-1")
    second = responder.prepare_child(_request(), chat_id="room-1")
    changed = responder.prepare_child(
        _request(context="message m1 revision 4"), chat_id="room-1")

    assert first.prompt == second.prompt
    assert first.prompt_digest == second.prompt_digest
    assert first.prompt_digest == hashlib.sha256(
        first.prompt.encode("utf-8")).hexdigest()
    assert changed.prompt_digest != first.prompt_digest
    assert "bounded specialist contribution" in first.prompt.lower()
    assert "do not use tools" in first.prompt.lower()
    assert "post to the room" in first.prompt.lower()
    assert "message m1 revision 3" in first.prompt


def test_child_invocation_is_isolated_bounded_and_keeps_process_rails(
        tmp_path, monkeypatch):
    responder = _responder(tmp_path, _fake_preset())
    responder.home.mkdir()
    before = list(responder.home.iterdir())
    seen: dict = {}

    def forbidden(*_args, **_kwargs):
        pytest.fail("normal responder side effect reached the child path")

    monkeypatch.setattr(cli_module, "BridgeServer", forbidden)
    monkeypatch.setattr(cli_module, "prepare_outbox", forbidden)
    responder._retrieve = forbidden
    responder._stage_inbox = forbidden
    monkeypatch.setenv("FAKE_PROVIDER_TOKEN", "provider-secret")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "mesh-secret")
    monkeypatch.setenv("AGENTBRIDGE_OUTBOX", "/forged/outbox")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "forged-timeout")

    def invoke(argv, workdir, timeout_s, env, **_kwargs):
        seen.update(argv=argv, workdir=workdir, timeout_s=timeout_s, env=env)
        assert workdir.is_dir()
        (workdir / "ephemeral.txt").write_text("temporary", encoding="utf-8")
        return 0, [
            "A useful \x1b[Kspecialist result.",
            MESSAGE_BREAK,
            "One contribution continues.",
            SILENCE.lower(),
            "x" * 300,
        ], ""

    responder._run_child_process = invoke
    prepared = responder.prepare_child(_request(bound=96), chat_id="room-1")
    result = responder.respond_child(prepared)

    assert result.provider == "fake-text"
    assert result.model == "model-a"
    assert result.prompt_digest == prepared.prompt_digest
    assert 0 < len(result.text) <= 96
    assert MESSAGE_BREAK not in result.text.upper()
    assert SILENCE not in result.text.upper()
    assert "\x1b" not in result.text and "[K" not in result.text
    assert "--safe" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == "model-a"
    assert "--mcp-config" not in seen["argv"]
    assert seen["env"]["FAKE_PROVIDER_TOKEN"] == "provider-secret"
    assert "SUPABASE_SECRET_KEY" not in seen["env"]
    assert "AGENTBRIDGE_OUTBOX" not in seen["env"]
    assert "MCP_TOOL_TIMEOUT" not in seen["env"]
    assert not seen["workdir"].exists()
    assert list(responder.home.iterdir()) == before


def test_fake_plain_text_provider_runs_without_network(tmp_path):
    preset = _fake_preset(
        args=["-c", "print('specialist result')", "{prompt}"],
        args_minimal=[],
    )
    responder = _responder(tmp_path, preset)
    prepared = responder.prepare_child(_request(bound=40), chat_id="room-1")

    result = responder.respond_child(prepared)

    assert result.text == "specialist result"
    assert result.provider == "fake-text"
    assert result.model == "model-a"


def test_empty_child_output_and_modified_preparation_fail_closed(
        tmp_path):
    responder = _responder(tmp_path, _fake_preset())
    prepared = responder.prepare_child(_request(), chat_id="room-1")
    responder._run_child_process = lambda *_args, **_kwargs: (0, ["  \x00  "], "")
    with pytest.raises(RuntimeError, match="no contribution text"):
        responder.respond_child(prepared)

    forged = replace(prepared, prompt=prepared.prompt + "\nextra")
    with pytest.raises(ValidationError, match="prompt was modified"):
        responder.respond_child(forged)


def test_owner_adapter_changes_invalidate_a_prepared_child(tmp_path):
    account = _account("fake-text")
    responder = _responder(tmp_path, _fake_preset(), account=account)
    prepared = responder.prepare_child(_request(), chat_id="room-1")
    account.agent.harness["model"] = "model-b"
    responder._run_child_process = lambda *_args: pytest.fail(
        "stale child invocation was launched")

    with pytest.raises(ValidationError, match="settings changed"):
        responder.respond_child(prepared)


def test_child_process_is_killed_when_authority_changes(tmp_path):
    cancelled = threading.Event()
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    try:
        rc, _lines, error = CliResponder._run_child_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path, 5.0, {}, cancelled=cancelled.is_set,
        )
    finally:
        timer.cancel()
    assert rc is None
    assert "authority change" in error
