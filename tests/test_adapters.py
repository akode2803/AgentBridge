"""R16 adapters: preset loading + argv building, routing resolution, the
subprocess engine against a stub CLI, and the runner end-to-end through a
home-overlay preset — the same path a real family takes, minus the model.
"""

from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness import AgentRunner
from agentbridge.harness.adapters import (
    CliResponder, ModelRegistry, Preset, provider_env, reply_from_output,
)
from agentbridge.harness.settings import HarnessSettings
from agentbridge.mesh.service import Mesh

# ------------------------------------------------------------------ the stub

STUB = textwrap.dedent("""
    import json, os, re, sys, time
    args = sys.argv[1:]
    if "--bogus-flag" in args:
        sys.stderr.write("Usage: stub [options]\\n")
        sys.exit(2)
    if "--sleep" in args:
        time.sleep(30)
    prompt = next((a for a in reversed(args) if "save it into" in a), args[-1])
    print(json.dumps({"type": "system", "subtype": "init"}))
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "search", "input": {"query": "the request"}}
    ]}}))
    model = ""
    if "--model" in args:
        model = args[args.index("--model") + 1]
    blocked = ",".join(a for i, a in enumerate(args)
                       if i and args[i-1] == "--block")
    out = os.environ.get("STUB_OUTBOX")
    if out == "FROM_PROMPT":
        out = os.environ.get("AGENTBRIDGE_OUTBOX", "")
    if out:
        with open(os.path.join(out, "made.txt"), "w") as fh:
            fh.write("made by the stub")
        open(os.path.join(out, "scrap.txt"), "w").close()  # empty scratch
    if os.environ.get("STUB_FAIL_AFTER_FILE"):
        sys.stderr.write("stub failed after producing files\\n")
        sys.exit(1)
    recovery = "yes" if "NOT resent automatically" in prompt else "no"
    print(json.dumps({"type": "result",
                      "result": f"stub reply model={model} blocked={blocked} recovery={recovery}"}))
""")


def stub_preset(tmp_path, **overrides) -> dict:
    stub = tmp_path / "stub_cli.py"
    stub.write_text(STUB, encoding="utf-8")
    d = {
        "id": "stub",
        "label": "Stub CLI",
        "command": sys.executable,
        "args": [str(stub), "--flag", "{prompt}"],
        "args_minimal": [str(stub), "{prompt}"],
        "safety_args": ["--safe"],
        "model_args": ["--model", "{model}"],
        "effort_args": ["--effort", "{effort}"],
        "efforts": ["low", "high"],
        "blocklist_args": ["--block", "{tool}"],
        "blocklist": ["shell"],
        "env_allow": ["STUB_OUTBOX"],
        "format": "claude-stream",
    }
    d.update(overrides)
    return d


def registry_with(tmp_path, preset_dict) -> ModelRegistry:
    """A registry holding ONLY this preset — the dev machine's real CLI
    installs (claude is present here) must not leak into resolution tests."""
    p = Preset.from_dict(preset_dict)
    return ModelRegistry({p.id: p})


def settings(**harness) -> HarnessSettings:
    return HarnessSettings.from_account(
        SimpleNamespace(agent=SimpleNamespace(harness=harness)))


# ------------------------------------------------------------- preset/argv

def test_shipped_presets_load_and_build():
    reg = ModelRegistry.load()
    for fam in ("claude", "cortex", "codex", "grok", "ollama", "deepseek"):
        assert fam in reg.presets
    argv = reg.presets["claude"].build_argv(
        prompt="hello", workdir="w", reply_file="r",
        model="claude-sonnet-5")
    assert argv[0] == "claude" and "hello" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert "--disallowedTools" in argv and "Bash" in argv


def test_minimal_argv_keeps_safety_and_blocklist():
    p = Preset.from_dict({
        "id": "x", "command": "x",
        "args": ["--nice", "{prompt}"], "args_minimal": ["{prompt}"],
        "safety_args": ["--read-only"],
        "blocklist_args": ["--deny", "{tool}"], "blocklist": ["shell"],
        "reply_file_arg": ["-o", "{reply_file}"],
    })
    full = p.build_argv(prompt="p", workdir="w", reply_file="r")
    slim = p.build_argv(prompt="p", workdir="w", reply_file="r", minimal=True)
    assert "--nice" in full and "-o" in full
    assert "--nice" not in slim and "-o" not in slim   # conveniences dropped
    for argv in (full, slim):                          # the rails never are
        assert "--read-only" in argv
        assert argv[argv.index("--deny") + 1] == "shell"


def test_provider_environment_is_default_deny_and_preset_declared():
    preset = Preset.from_dict({
        "id": "x", "command": "x", "env_allow": ["PROVIDER_TOKEN"],
    })
    source = {
        "PATH": "/bin", "HOME": "/Users/test",
        "PROVIDER_TOKEN": "needed", "SUPABASE_SECRET_KEY": "mesh-secret",
        "GITHUB_TOKEN": "unrelated", "MCP_TOOL_TIMEOUT": "forged-host-value",
    }
    env = provider_env(
        preset, source=source, injected={"MCP_TOOL_TIMEOUT": "180000"})

    assert env["PATH"] == "/bin"
    assert env["HOME"] == "/Users/test"
    assert env["PROVIDER_TOKEN"] == "needed"
    assert env["MCP_TOOL_TIMEOUT"] == "180000"
    assert "SUPABASE_SECRET_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_provider_environment_does_not_inherit_undeclared_credentials():
    preset = Preset.from_dict({"id": "x", "command": "x"})
    env = provider_env(preset, source={
        "PATH": "/bin", "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret", "SUPABASE_MEMBER_PASSWORD": "secret",
    })

    assert env == {"PATH": "/bin"}


def test_resolution_order_and_degrades(tmp_path):
    reg = registry_with(tmp_path, stub_preset(tmp_path, models=["m1", "m2"]))
    # single install: no adapter named -> the sole family resolves
    inv = reg.resolve(settings(), "humans")
    assert inv.preset.id == "stub" and inv.model == ""
    # category model
    inv = reg.resolve(settings(routing={"humans": {"model": "m1"}}), "humans")
    assert inv.model == "m1"
    # the override-all wins over the category model
    inv = reg.resolve(settings(model="m2",
                               routing={"humans": {"model": "m1"}}), "humans")
    assert inv.model == "m2"
    # ...and the chat's own pick wins over the override-all
    inv = reg.resolve(settings(model="m2", models={"c9": "m1"}), "humans", "c9")
    assert inv.model == "m1"
    inv = reg.resolve(settings(model="m2", models={"c9": "m1"}), "humans", "cX")
    assert inv.model == "m2"
    # effort only when the family supports the value
    assert reg.resolve(settings(reasoning="high"), "humans").effort == "high"
    assert reg.resolve(settings(reasoning="max"), "humans").effort == ""
    # a disabled audience refuses with a showable reason
    with pytest.raises(ValidationError):
        reg.resolve(settings(routing={"agents": {"enabled": False}}), "agents")
    # unknown / uninstalled families refuse
    with pytest.raises(ValidationError):
        reg.resolve(settings(adapter="nope"), "humans")
    # requires_model without one refuses
    reg2 = registry_with(tmp_path, stub_preset(tmp_path, id="stub2",
                                               requires_model=True))
    with pytest.raises(ValidationError):
        reg2.resolve(settings(adapter="stub2"), "humans")


def test_effort_reaches_argv_with_per_model_sets(tmp_path):
    """Q13: the effort knob rides argv, and a model's own entry in
    model_efforts narrows what it accepts (others use the family list)."""
    reg = registry_with(tmp_path, stub_preset(
        tmp_path, models=["m1", "m2"], model_efforts={"m1": ["high"]}))
    inv = reg.resolve(settings(model="m2", reasoning="low"), "humans")
    assert inv.effort == "low"
    argv = inv.preset.build_argv(prompt="p", workdir="w", reply_file="r",
                                 model=inv.model, effort=inv.effort)
    assert argv[argv.index("--effort") + 1] == "low"
    # m1 accepts only "high": "low" is dropped, "high" passes
    assert reg.resolve(settings(model="m1", reasoning="low"), "humans").effort == ""
    assert reg.resolve(settings(model="m1", reasoning="high"), "humans").effort == "high"


def test_claude_preset_declares_the_real_effort_levels():
    """The live family's picker was dead because the preset declared no
    efforts — these five come straight from `claude --help`."""
    reg = ModelRegistry.load()
    p = reg.presets["claude"]
    assert p.efforts == ["low", "medium", "high", "xhigh", "max"]
    argv = p.build_argv(prompt="x", workdir="w", reply_file="r",
                        model="claude-fable-5", effort="max")
    assert argv[argv.index("--effort") + 1] == "max"


def test_mcp_only_adapter_runs_no_cli(tmp_path):
    """Q21: adapter "none" = the agent connects via mesh-cli (MCP) itself —
    resolution refuses and --all spawns no runner for it."""
    reg = registry_with(tmp_path, stub_preset(tmp_path))
    with pytest.raises(ValidationError):
        reg.resolve(settings(adapter="none"), "humans")
    root = tmp_path / "mesh2"
    root.mkdir()
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=tmp_path / "home2")
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    owner.accounts.create_agent("mcponly")
    owner.set_agent_harness("mcponly", {"adapter": "none"})
    try:
        from agentbridge.harness.runner import hosted_agents
        assert hosted_agents(root, "devbox") == ["helper"]
    finally:
        owner.close()


def test_per_chat_context_and_memory_overrides():
    """Q30/H6: the per-chat context ceiling and global-memory override parse
    defensively and resolve chat-by-chat."""
    s = settings(global_memory="dm",
                 memory_overrides={"c1": "on", "c2": "off", "c3": "bogus"},
                 context_days={"c1": 7, "c2": "junk", "c4": 9999})
    assert s.global_memory_for("c1") == "everywhere"
    assert s.global_memory_for("c2") == "off"
    assert s.global_memory_for("c3") == "dm"     # bogus override dropped
    assert s.global_memory_for("cX") == "dm"     # no override = the policy
    assert s.context_days_for("c1") == 7
    assert s.context_days_for("c2") == 0         # junk dropped = auto
    assert s.context_days_for("c4") == 365       # clamped to the ceiling
    assert s.context_days_for("cX") == 0


def test_aux_flags_shape_the_gates():
    """H2/R43: the owner's aux flags resolve into the run's auto_allow +
    blocklist — and the web relax NEVER applies without the ask gate."""
    from agentbridge.harness.adapters.registry import effective_gates

    gated = ModelRegistry.load().presets["codex"]
    gated.auto_allow = ["Read", "Grep"]
    gated.blocklist = ["Bash", "WebFetch", "WebSearch"]
    gated.aux_web = ["WebFetch", "WebSearch"]
    # defaults: reads free, web hard-blocked
    auto, block = effective_gates(gated, settings())
    assert auto == ["Read", "Grep"]
    assert block == ["Bash", "WebFetch", "WebSearch"]
    # read off: even reads outside the workspace ask
    auto, block = effective_gates(gated, settings(aux={"read": False}))
    assert auto == [] and "Bash" in block
    # web on: the web tools leave the blocklist (into the ask gate);
    # Bash stays hard-blocked regardless
    auto, block = effective_gates(gated, settings(aux={"web": True}))
    assert block == ["Bash"] and auto == ["Read", "Grep"]
    # no trusted bridge profile: the toggle is inert
    bare = Preset(id="b", command="x",
                  blocklist=["web_fetch"], aux_web=["web_fetch"])
    _, block = effective_gates(bare, settings(aux={"web": True}))
    assert block == ["web_fetch"]
    # junk aux parses to the defaults
    s = settings(aux="nonsense")
    assert s.aux == {"read": True, "web": False}


def test_reply_from_output_formats():
    stream = [json.dumps({"type": "result", "result": "final"})]
    assert reply_from_output(stream, "claude-stream") == "final"
    codex = [json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "done"}})]
    assert reply_from_output(codex, "codex-jsonl") == "done"
    assert reply_from_output(["plain", "text"], "text") == "plain\ntext"


def test_stream_errors_surfaces_ccs_reason():
    """V86 rider: a run that ends on a tool call emits an is_error result
    with `errors` and NO `result` (probed live, claude 2.1.202) — the
    failure path surfaces that reason instead of an opaque blank."""
    from agentbridge.harness.adapters.cli import stream_errors

    maxed = [json.dumps({"type": "result", "subtype": "error_max_turns",
                         "is_error": True, "stop_reason": "tool_use",
                         "errors": ["Reached maximum number of turns (60)"]})]
    assert stream_errors(maxed, "claude-stream") \
        == "Reached maximum number of turns (60)"
    assert reply_from_output(maxed, "claude-stream") == ""   # no result text
    # errors absent: the subtype still names the reason
    bare = [json.dumps({"type": "result", "subtype": "error_max_turns",
                        "is_error": True})]
    assert stream_errors(bare, "claude-stream") == "max turns"
    # a healthy stream (or another format) says nothing
    ok = [json.dumps({"type": "result", "result": "fine"})]
    assert stream_errors(ok, "claude-stream") == ""
    assert stream_errors(maxed, "codex-jsonl") == ""


def test_prune_tmp_clears_only_stale_scratch(tmp_path):
    """V97: the per-run janitor removes week-old tmp/ files (then emptied
    stale dirs) and never touches fresh scratch or the workspace root."""
    import os

    from agentbridge.harness.adapters.cli import _prune_tmp

    ws = tmp_path / "ws"
    (ws / "tmp" / "old-dir").mkdir(parents=True)
    old = ws / "tmp" / "old-dir" / "stale.csv"
    old.write_text("x", encoding="utf-8")
    fresh = ws / "tmp" / "fresh.txt"
    fresh.write_text("y", encoding="utf-8")
    keeper = ws / "notes.md"
    keeper.write_text("z", encoding="utf-8")
    then = time.time() - 8 * 86400
    os.utime(old, (then, then))
    os.utime(old.parent, (then, then))
    os.utime(keeper, (then, then))

    assert _prune_tmp(ws) == 1
    assert not old.exists() and not old.parent.exists()  # stale file + dir
    assert fresh.exists() and keeper.exists()
    assert _prune_tmp(ws) == 0                           # idempotent
    assert _prune_tmp(tmp_path / "nowhere") == 0         # no tmp/ = no-op


# ------------------------------------------------------- engine + end-to-end

@pytest.fixture
def arig(tmp_path):
    """Owner + agent + a stub-CLI preset installed via the home overlay."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    (home / "adapters").mkdir(parents=True)
    (home / "adapters" / "stub.json").write_text(
        json.dumps(stub_preset(tmp_path)), encoding="utf-8")
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper", harness={"adapter": "stub"})
    yield SimpleNamespace(root=root, home=home, owner=owner)
    owner.close()


def test_cli_responder_end_to_end_through_the_runner(arig):
    snap = arig.owner.create_chat("Real", members=["helper"])
    trig = arig.owner.post(snap.id, "@helper please run")
    arig.owner.outbox.flush_once()

    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        replies = [m for m in arig.owner.messages_for(snap.id)
                   if m.from_ == "helper"]
        assert len(replies) == 1
        assert replies[0].body == \
            "stub reply model= blocked=shell recovery=no"
        assert "blocked=shell" in replies[0].body     # the blocklist rode argv
        assert (replies[0].reply_to or {}).get("id") == trig.id
        # the streamed tool line became a recorded task step
        doc = runner.mesh.tx.get_doc(
            f"chats/{snap.id}/tasks/{replies[0].id}.json")
        assert any("search" in t["text"] for t in doc["tasks"])
        events = runner.run_ledger.read(snap.id)
        assert [(event.provider, event.model) for event in events] == [
            ("stub", "provider-default-unattested"),
            ("stub", "provider-default-unattested"),
        ]
        tasks = runner.task_ledger.read(snap.id)
        assert [event.state.value for event in tasks] == [
            "active", "active", "completed",
        ]
        assert events[0].active_task_ids == (tasks[0].meta.task_id,)
    finally:
        runner.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_contract_rollout_preserves_runner_reply(arig, monkeypatch, enabled):
    import agentbridge.harness.adapters.cli as cli_module

    arig.owner.accounts.set_agent_harness("helper", {
        "adapter": "stub", "contract_cli_enabled": enabled,
    })
    captured = []
    original = cli_module.prepare_cli_contract

    def observe(**values):
        captured.append(values["delivery"])
        return original(**values)

    monkeypatch.setattr(cli_module, "prepare_cli_contract", observe)
    snap = arig.owner.create_chat(f"Contract {enabled}", members=["helper"])
    arig.owner.post(snap.id, "@helper contract check")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        replies = [m for m in arig.owner.messages_for(snap.id)
                   if m.from_ == "helper"]
        assert len(replies) == 1
        assert replies[0].body == \
            "stub reply model= blocked=shell recovery=no"
        assert bool(captured) is enabled
        if enabled:
            trace = captured[0].cli_contract_trace
            assert trace.result.status.value == "completed"
            assert trace.result.final_text == replies[0].body
            assert trace.events[-1].kind.value == "completed"
    finally:
        runner.close()


def test_cli_invocation_is_resolved_once_with_timer_owner_routing(arig, tmp_path):
    mesh = Mesh(arig.root, "helper", "devbox", encrypt=True, home=arig.home,
                store_path=tmp_path / "prepare.sqlite")
    try:
        responder = CliResponder(ModelRegistry.load(arig.home), mesh, arig.home)
        delivery = SimpleNamespace(
            kind="timer", triggers=[], chat_id="timer-chat",
            invocation=None, harness_settings=None,
        )
        snap = settings(
            adapter="stub",
            routing={"owner": {"model": "owner-model"},
                     "agents": {"model": "wrong-model"}},
        )
        metadata = responder.prepare(delivery, snap)
        assert metadata == {
            "provider": "stub", "model": "owner-model",
            "capability_ceiling": (),
        }
        assert delivery.capability_ceiling == ()
        assert delivery.invocation.model == "owner-model"
        assert delivery.harness_settings is snap
        assert delivery.contract_cli_enabled is False
    finally:
        mesh.close()


def test_cli_contract_flag_is_sampled_when_invocation_is_prepared(arig, tmp_path):
    mesh = Mesh(arig.root, "helper", "devbox", encrypt=True, home=arig.home,
                store_path=tmp_path / "contract-snapshot.sqlite")
    try:
        responder = CliResponder(ModelRegistry.load(arig.home), mesh, arig.home)
        delivery = SimpleNamespace(
            kind="message", triggers=[], chat_id="snapshot-chat",
            invocation=None, harness_settings=None,
        )
        snapshot = settings(adapter="stub", contract_cli_enabled=True)
        responder.prepare(delivery, snapshot)
        snapshot.contract_cli_enabled = False
        assert delivery.contract_cli_enabled is True
    finally:
        mesh.close()


def test_codex_exact_policy_is_compiled_before_signed_run_metadata(
        arig, tmp_path, monkeypatch):
    import agentbridge.harness.adapters.policy as policy_module

    calls = []
    monkeypatch.setattr(policy_module.shutil, "which", lambda _command: "/tmp/codex")
    monkeypatch.setattr(
        policy_module.subprocess, "run",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)) or
            SimpleNamespace(returncode=0, stdout="codex-cli 0.147.0\n", stderr="")
        ),
    )
    monkeypatch.setattr(
        policy_module, "_codex_binary_identity",
        lambda _path: (
            "a" * 64, "/tmp/codex-code-mode-host", "b" * 64, "2DC432GLL2",
        ),
    )
    monkeypatch.setattr(
        policy_module, "_assert_binary_identity", lambda *_args: None)
    def inspect_layers(_executable, workspace, _source_env):
        assert workspace.is_dir()
        return ("system:sha256:" + "c" * 64,)

    monkeypatch.setattr(
        policy_module, "_codex_non_user_config_layers", inspect_layers,
    )
    mesh = Mesh(arig.root, "helper", "devbox", encrypt=True, home=arig.home,
                store_path=tmp_path / "codex-prepare.sqlite")
    try:
        registry = ModelRegistry.load(arig.home)
        registry._which["codex"] = True
        responder = CliResponder(registry, mesh, arig.home)
        assert not (arig.home / "harness" / "helper" / "workspaces"
                    / "codex-chat").exists()
        delivery = SimpleNamespace(
            kind="message", triggers=[], chat_id="codex-chat",
            invocation=None, harness_settings=None,
        )
        metadata = responder.prepare(delivery, settings(adapter="codex"))
        assert len(calls) == 1
        assert metadata["provider"] == "codex"
        assert metadata["native_provider_version"] == "codex-cli 0.147.0"
        assert metadata["native_policy_digest"] == \
            delivery.native_policy.authority_digest("codex-cli 0.147.0")
        assert metadata["provider_policy_digest"] == \
            delivery.compiled_bridge_policy.authority_digest()
        assert metadata["native_enabled"] == delivery.native_policy.enabled
        assert metadata["native_blocked"] == delivery.native_policy.blocked
        assert delivery.compiled_bridge_policy.executable_version == \
            "codex-cli 0.147.0"
    finally:
        mesh.close()


def test_verified_popen_checks_policy_immediately_before_spawn(monkeypatch):
    from agentbridge.harness.adapters import cli as module

    events = []
    policy = SimpleNamespace(
        verify_unchanged=lambda: events.append("verify"))
    sentinel = object()
    monkeypatch.setattr(
        module.subprocess, "Popen",
        lambda argv, **kwargs: (events.append("popen") or sentinel))
    assert module._verified_popen(["codex"], launch_policy=policy) is sentinel
    assert events == ["verify", "popen"]


def test_verified_popen_never_spawns_after_failed_check(monkeypatch):
    from agentbridge.harness.adapters import cli as module

    spawned = []
    policy = SimpleNamespace(
        verify_unchanged=lambda: (_ for _ in ()).throw(
            ValidationError("drifted")))
    monkeypatch.setattr(
        module.subprocess, "Popen",
        lambda *_args, **_kwargs: spawned.append(True))
    with pytest.raises(ValidationError, match="drifted"):
        module._verified_popen(["codex"], launch_policy=policy)
    assert spawned == []


def test_owner_stop_kills_the_run_cleanly(tmp_path, monkeypatch):
    """R36: the owner's stop doc kills the in-flight subprocess; the outcome
    is a deliberate stop — no reply, no error notice, feed state 'stopped',
    the trigger recorded handled so it never re-fires."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    (home / "adapters").mkdir(parents=True)
    # the stub sleeps 30s — plenty of window for the ~2.5s stop poll
    slow = stub_preset(tmp_path)
    slow["args"] = [slow["args"][0], "--sleep", "{prompt}"]
    (home / "adapters" / "stub.json").write_text(json.dumps(slow),
                                                 encoding="utf-8")
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper", harness={
        "adapter": "stub", "contract_cli_enabled": True,
    })
    snap = owner.create_chat("Slow", members=["helper"])
    owner.post(snap.id, "@helper take your time")
    owner.outbox.flush_once()

    runner = AgentRunner(root, "helper", home=home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    import agentbridge.harness.adapters.cli as cli_module

    captured = []
    started = threading.Event()
    original = cli_module.prepare_cli_contract

    def observe(**values):
        captured.append(values["delivery"])
        started.set()
        return original(**values)

    monkeypatch.setattr(cli_module, "prepare_cli_contract", observe)
    try:
        runner.mesh.sync.sync_once([snap.id])
        from agentbridge.harness.runtime.controls import publish_owner_command

        runner.tick()
        assert started.wait(10), "CLI contract was not prepared"
        # The subprocess now owns the run; its signed stop poll kills it.
        publish_owner_command(
            owner, target="helper", action="stop",
            run_id=captured[0].run_id, timeout_s=60,
        )
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        owner.sync.sync_once([snap.id])

        assert [m for m in owner.messages_for(snap.id)
                if m.from_ == "helper"] == []           # no reply, no notice
        runs = (runner.mesh.tx.get_doc("status/helper_runs.json") or {}).get("runs")
        assert runs and runs[-1]["state"] == "stopped"
        runs = runner.mesh.tx.get_doc("status/helper_runs.json")
        assert runs and runs["runs"][-1]["state"] == "stopped"
        assert [event.state.value for event in runner.task_ledger.read(snap.id)] == [
            "active", "stopped",
        ]
        assert len(captured) == 1
        trace = captured[0].cli_contract_trace
        assert trace.result.status.value == "stopped"
        assert trace.events[-1].kind.value == "stopped"
        # handled: a second pass never re-runs the same trigger
        runner.tick()
        runner.drain(timeout=30)
        assert runner.queue.snapshot() == []
    finally:
        runner.close()
        owner.close()


def test_routing_gates_at_scan(arig):
    arig.owner.accounts.set_agent_harness(
        "helper", {"routing": {"owner": {"enabled": False}}})
    snap = arig.owner.create_chat("Off", members=["helper"])
    trig = arig.owner.post(snap.id, "@helper are you there?")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        assert runner.queue.snapshot() == []          # never even queued
        assert runner.queue.answered(snap.id, trig.id, 0)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        assert [m for m in arig.owner.messages_for(snap.id)
                if m.from_ == "helper"] == []
    finally:
        runner.close()


def test_usage_error_falls_back_to_minimal_args(arig, tmp_path, monkeypatch):
    # a preset whose full argv the stub rejects; the minimal one works
    bad = stub_preset(tmp_path, id="stub")
    bad["args"] = [str(tmp_path / "stub_cli.py"), "--bogus-flag", "{prompt}"]
    (arig.home / "adapters" / "stub.json").write_text(
        json.dumps(bad), encoding="utf-8")
    arig.owner.accounts.set_agent_harness("helper", {
        "adapter": "stub", "contract_cli_enabled": True,
    })

    import agentbridge.harness.adapters.cli as cli_module

    captured = []
    original = cli_module.prepare_cli_contract

    def observe(**values):
        captured.append(values["delivery"])
        return original(**values)

    monkeypatch.setattr(cli_module, "prepare_cli_contract", observe)

    snap = arig.owner.create_chat("Fallback", members=["helper"])
    arig.owner.post(snap.id, "@helper still works?")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        replies = [m for m in arig.owner.messages_for(snap.id)
                   if m.from_ == "helper"]
        assert len(replies) == 1 and replies[0].body.startswith("stub reply")
        trace = captured[0].cli_contract_trace
        launches = [event.payload.to_value()["launch_digest"]
                    for event in trace.events
                    if "launch_digest" in event.payload.to_value()]
        assert len(launches) == 2 and launches[0] != launches[1]
        assert trace.invocation.model_settings.to_value()["initial_minimal"] is False
    finally:
        runner.close()


def test_flag_off_does_not_build_unused_minimal_fallback(arig):
    preset = stub_preset(arig.home)
    preset["args_minimal"] = [str(arig.home / "stub_cli.py"), "{unknown}"]
    (arig.home / "adapters" / "stub.json").write_text(
        json.dumps(preset), encoding="utf-8")
    arig.owner.accounts.set_agent_harness("helper", {
        "adapter": "stub", "contract_cli_enabled": False,
    })
    snap = arig.owner.create_chat("No contract fallback", members=["helper"])
    arig.owner.post(snap.id, "@helper normal path only")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        replies = [m for m in arig.owner.messages_for(snap.id)
                   if m.from_ == "helper"]
        assert len(replies) == 1
    finally:
        runner.close()


@pytest.mark.parametrize(
    ("process_result", "category", "code"),
    [
        ((None, [], "timed out"), "timeout", "timeout"),
        ((0, [], ""), "output", "invalid_output"),
        ((2, [], "raw provider credential failure"), "internal", "internal"),
    ],
)
def test_cli_contract_normalizes_failure_without_raw_provider_detail(
        arig, monkeypatch, process_result, category, code):
    import agentbridge.harness.adapters.cli as cli_module

    arig.owner.accounts.set_agent_harness("helper", {
        "adapter": "stub", "contract_cli_enabled": True,
    })
    captured = []
    original = cli_module.prepare_cli_contract

    def observe(**values):
        captured.append(values["delivery"])
        return original(**values)

    monkeypatch.setattr(cli_module, "prepare_cli_contract", observe)
    monkeypatch.setattr(
        CliResponder, "_run", lambda self, *args, **kwargs: process_result)
    snap = arig.owner.create_chat(f"Contract failure {code}", members=["helper"])
    arig.owner.post(snap.id, "@helper fail deterministically")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        trace = captured[0].cli_contract_trace
        assert trace.result.status.value == "failed"
        assert trace.result.error.category.value == category
        assert trace.result.error.code == code
        assert "raw provider credential failure" not in str(
            [event.payload.to_value() for event in trace.events])
    finally:
        runner.close()


def test_outbox_files_ride_back_except_empty_ones(arig, monkeypatch):
    """Files a run leaves in its outbox attach to the reply; 0-byte scratch
    does not (a live model once shipped an empty placeholder.txt)."""
    snap = arig.owner.create_chat("Files", members=["helper"])
    monkeypatch.setenv("STUB_OUTBOX", "FROM_PROMPT")
    arig.owner.post(snap.id, "@helper make me a file")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        reply = [m for m in arig.owner.messages_for(snap.id)
                 if m.from_ == "helper"][0]
        names = [f["name"] for f in reply.files]
        assert names == ["made.txt"]          # scrap.txt (empty) stayed home
    finally:
        runner.close()


def test_failed_cli_files_are_retained_and_disclosed_next_run(arig, monkeypatch):
    preset = stub_preset(
        arig.home, env_allow=["STUB_OUTBOX", "STUB_FAIL_AFTER_FILE"])
    (arig.home / "adapters").mkdir(parents=True, exist_ok=True)
    (arig.home / "adapters" / "stub.json").write_text(
        json.dumps(preset), encoding="utf-8")
    monkeypatch.setenv("STUB_OUTBOX", "FROM_PROMPT")
    monkeypatch.setenv("STUB_FAIL_AFTER_FILE", "1")
    snap = arig.owner.create_chat("Recovery", members=["helper"])
    arig.owner.post(snap.id, "@helper build the file")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        recovery = (arig.home / "harness" / "helper" / "workspaces" /
                    snap.id / "recovery")
        assert len(list(recovery.rglob("made.txt"))) == 1

        monkeypatch.delenv("STUB_FAIL_AFTER_FILE")
        arig.owner.post(snap.id, "@helper try again using what remains")
        arig.owner.outbox.flush_once()
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        arig.owner.sync.sync_once([snap.id])
        replies = [m for m in arig.owner.messages_for(snap.id)
                   if m.from_ == "helper" and "stub reply" in m.body]
        assert replies and "recovery=yes" in replies[-1].body
    finally:
        runner.close()


def test_preprocess_failure_also_retains_run_artifacts(arig, monkeypatch):
    snap = arig.owner.create_chat("Early failure", members=["helper"])
    arig.owner.post(snap.id, "@helper prepare it")
    arig.owner.outbox.flush_once()
    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()

    def fail_stage(_delivery, _workdir):
        outbox = runner.responder._run_local.outbox
        (outbox / "early.txt").write_text("keep me", encoding="utf-8")
        raise OSError("staging failed")

    monkeypatch.setattr(runner.responder, "_stage_inbox", fail_stage)
    try:
        runner.mesh.sync.sync_once([snap.id])
        runner.tick()
        runner.drain(timeout=60)
        recovery = (arig.home / "harness" / "helper" / "workspaces" /
                    snap.id / "recovery")
        kept = list(recovery.rglob("early.txt"))
        assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "keep me"
    finally:
        runner.close()


def test_workspace_leaks_nothing_from_other_chats(arig):
    """R19 leak audit: a run's workspace holds ONLY this chat's material —
    another room's bodies must never appear in any file the run can read."""
    secret = "TOPSECRET-marker-9c41"
    private = arig.owner.create_chat("Private")           # helper NOT a member
    arig.owner.post(private.id, f"the launch code is {secret}")
    snap = arig.owner.create_chat("Open", members=["helper"])
    arig.owner.post(snap.id, "@helper hello there")
    arig.owner.outbox.flush_once()

    runner = AgentRunner(arig.root, "helper", home=arig.home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once()
        runner.tick()
        runner.drain(timeout=60)
        ws_root = arig.home / "harness" / "helper"
        found = []
        for p in ws_root.rglob("*"):
            if not p.is_file():
                continue
            try:  # qdrant (R20) holds its lock file open — not readable text
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if secret in body:
                found.append(str(p))
        assert found == []                                # nothing leaked
        ctx = (ws_root / "workspaces" / snap.id / "context.md").read_text(
            encoding="utf-8")
        assert "hello there" in ctx                       # its own chat is in
    finally:
        runner.close()


def test_engine_timeout_kills_the_run(arig, tmp_path):
    reg = ModelRegistry.load(arig.home)
    mesh = Mesh(arig.root, "helper", "devbox", encrypt=True, home=arig.home,
                store_path=tmp_path / "timeout.sqlite")
    try:
        responder = CliResponder(reg, mesh, arig.home)
        inv = reg.resolve(settings(adapter="stub"), "humans")
        pack = responder.prompts.for_agent(None)
        stub = tmp_path / "stub_cli.py"
        rc, lines, err = responder._run(
            [sys.executable, str(stub), "--sleep", "p"],
            arig.home, 1.0, inv, pack, lambda s: None)
        assert rc is None and err == "timed out"
    finally:
        mesh.close()
