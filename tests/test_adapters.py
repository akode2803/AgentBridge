"""R16 adapters: preset loading + argv building, routing resolution, the
subprocess engine against a stub CLI, and the runner end-to-end through a
home-overlay preset — the same path a real family takes, minus the model.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness import AgentRunner
from agentbridge.harness.adapters import (
    CliResponder, ModelRegistry, Preset, reply_from_output,
)
from agentbridge.harness.settings import HarnessSettings
from agentbridge.mesh.service import Mesh

# ------------------------------------------------------------------ the stub

STUB = textwrap.dedent("""
    import json, os, sys, time
    args = sys.argv[1:]
    if "--bogus-flag" in args:
        sys.stderr.write("Usage: stub [options]\\n")
        sys.exit(2)
    if "--sleep" in args:
        time.sleep(30)
    prompt = args[-1]
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
    if out:
        with open(os.path.join(out, "made.txt"), "w") as fh:
            fh.write("made by the stub")
        open(os.path.join(out, "scrap.txt"), "w").close()  # empty scratch
    print(json.dumps({"type": "result",
                      "result": f"stub reply model={model} blocked={blocked}"}))
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

    gated = Preset(id="p", command="x",
                   permission_args=["--mcp-config", "{mcp_config}"],
                   auto_allow=["Read", "Grep"],
                   blocklist=["Bash", "WebFetch", "WebSearch"],
                   aux_web=["WebFetch", "WebSearch"])
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
    # no ask gate (no permission_args): the toggle is inert
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
        assert replies[0].body.startswith("stub reply")
        assert "blocked=shell" in replies[0].body     # the blocklist rode argv
        assert (replies[0].reply_to or {}).get("id") == trig.id
        # the streamed tool line became a recorded task step
        doc = runner.mesh.tx.get_doc(
            f"chats/{snap.id}/tasks/{replies[0].id}.json")
        assert any("search" in t["text"] for t in doc["tasks"])
    finally:
        runner.close()


def test_owner_stop_kills_the_run_cleanly(tmp_path):
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
    owner.accounts.create_agent("helper", harness={"adapter": "stub"})
    snap = owner.create_chat("Slow", members=["helper"])
    owner.post(snap.id, "@helper take your time")
    owner.outbox.flush_once()

    runner = AgentRunner(root, "helper", home=home,
                         machine="devbox", poll_s=0.2)
    runner.attach_cli_responder()
    try:
        runner.mesh.sync.sync_once([snap.id])
        # the stop request lands just before dispatch — the poller's first
        # check catches it and kills the subprocess long before 30s
        owner.tx.put_doc("status/helper_stop.json",
                         {"ns": time.time_ns(), "by": "aryan", "chat_id": ""})
        runner.tick()
        runner.drain(timeout=60)
        runner.mesh.outbox.flush_once()
        owner.sync.sync_once([snap.id])

        assert [m for m in owner.messages_for(snap.id)
                if m.from_ == "helper"] == []           # no reply, no notice
        feed = runner.mesh.tx.get_doc("status/helper_run.json")
        assert feed and feed["state"] == "stopped"
        runs = runner.mesh.tx.get_doc("status/helper_runs.json")
        assert runs and runs["runs"][-1]["state"] == "stopped"
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


def test_usage_error_falls_back_to_minimal_args(arig, tmp_path):
    # a preset whose full argv the stub rejects; the minimal one works
    bad = stub_preset(tmp_path, id="stub")
    bad["args"] = [str(tmp_path / "stub_cli.py"), "--bogus-flag", "{prompt}"]
    (arig.home / "adapters" / "stub.json").write_text(
        json.dumps(bad), encoding="utf-8")

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
    finally:
        runner.close()


def test_outbox_files_ride_back_except_empty_ones(arig, monkeypatch):
    """Files a run leaves in its outbox attach to the reply; 0-byte scratch
    does not (a live model once shipped an empty placeholder.txt)."""
    snap = arig.owner.create_chat("Files", members=["helper"])
    monkeypatch.setenv("STUB_OUTBOX",           # the per-chat workspace (R18)
                       str(arig.home / "harness" / "helper" / "workspaces"
                           / snap.id / "outbox"))
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
    responder = CliResponder(reg, SimpleNamespace(user="helper", tx=None),
                             arig.home)
    inv = reg.resolve(settings(adapter="stub"), "humans")
    pack = responder.prompts.for_agent(None)
    stub = tmp_path / "stub_cli.py"
    rc, lines, err = responder._run(
        [sys.executable, str(stub), "--sleep", "p"],
        arig.home, 1.0, inv, pack, lambda s: None)
    assert rc is None and err == "timed out"
