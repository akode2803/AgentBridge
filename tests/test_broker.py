"""The permission broker + the harness↔agent bridge (R18): policy order,
deny roots, the ask/answer pipe, timeouts failing closed, deny caching, and
the MCP channel end-to-end over real streamable-http."""

from __future__ import annotations

import json
import threading
import time

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from agentbridge.harness import BridgeServer, PermissionBroker  # noqa: E402
from agentbridge.harness import broker as broker_mod  # noqa: E402
from agentbridge.harness.adapters.registry import Preset  # noqa: E402
from agentbridge.mesh.service import Mesh  # noqa: E402


class FakeTx:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def put_doc(self, path, doc):
        self.docs[path] = doc

    def get_doc(self, path):
        return self.docs.get(path)


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(broker_mod, "POLL_S", 0.05)


def make(tmp_path):
    tx = FakeTx()
    b = PermissionBroker(tx, "helper")
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return tx, b, ws


def answer(tx, verdict, text="", delay=0.15):
    """A stand-in owner: answers the first pending ask after ``delay``."""

    def run():
        deadline = time.time() + 5
        while time.time() < deadline:
            doc = tx.docs.get("status/asks/helper.json") or {}
            asks = doc.get("asks") or []
            if asks:
                tx.docs["status/asks/helper_answers.json"] = {
                    "answers": {asks[0]["id"]: {"verdict": verdict,
                                                "text": text}}}
                return
            time.sleep(0.02)

    t = threading.Timer(delay, run)
    t.start()
    return t


# ---------------------------------------------------------------- the policy

def test_workspace_targets_allow_without_asking(tmp_path):
    _, b, ws = make(tmp_path)
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Write",
                     tool_input={"file_path": str(ws / "notes.md")},
                     auto_allow=[], approvals=[], timeout_s=1)
    assert ok
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Edit",
                     tool_input={"file_path": "sub/rel.txt"},  # cwd = workspace
                     auto_allow=[], approvals=[], timeout_s=1)
    assert ok


def test_traversal_out_of_the_workspace_is_not_inside(tmp_path):
    _, b, ws = make(tmp_path)
    outside = str(ws / ".." / "escape.txt")
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Write",
                     tool_input={"file_path": outside},
                     auto_allow=[], approvals=[], timeout_s=0.2)
    assert not ok                       # fell through to ask -> timeout deny


def test_deny_roots_refuse_even_reads_with_no_ask(tmp_path):
    tx, b, ws = make(tmp_path)
    home = tmp_path / "home"
    (home / "keys").mkdir(parents=True)
    ok, msg = b.decide(chat_id="c1", workspace=ws, tool="Read",
                       tool_input={"file_path": str(home / "keys" / "a.key")},
                       auto_allow=["Read"], approvals=[], timeout_s=5,
                       deny_roots=[home])
    assert not ok and "off limits" in msg
    assert tx.docs.get("status/asks/helper.json") is None  # never asked


def test_auto_allow_reads_anywhere_else(tmp_path):
    _, b, ws = make(tmp_path)
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Read",
                     tool_input={"file_path": str(tmp_path / "other.txt")},
                     auto_allow=["Read", "Glob", "Grep"], approvals=[],
                     timeout_s=1)
    assert ok
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Glob",
                     tool_input={"pattern": "**/*.py"},   # no path at all
                     auto_allow=["Read", "Glob", "Grep"], approvals=[],
                     timeout_s=1)
    assert ok


def test_owner_approvals_allow_by_tool_and_chat(tmp_path):
    _, b, ws = make(tmp_path)
    rules = [{"tool": "Write", "chat": "c1"}]
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Write",
                     tool_input={"file_path": str(tmp_path / "x.txt")},
                     auto_allow=[], approvals=rules, timeout_s=0.2)
    assert ok
    ok, _ = b.decide(chat_id="OTHER", workspace=ws, tool="Write",
                     tool_input={"file_path": str(tmp_path / "x.txt")},
                     auto_allow=[], approvals=rules, timeout_s=0.2)
    assert not ok                        # scoped rule doesn't leak across chats
    ok, _ = b.decide(chat_id="OTHER", workspace=ws, tool="Read",
                     tool_input={"file_path": str(tmp_path / "y.txt")},
                     auto_allow=[], approvals=[{"tool": "Read", "chat": "*"}],
                     timeout_s=0.2)
    assert ok                            # "*" = everywhere


# ------------------------------------------------------------- the ask pipe

def test_ask_answered_allow_and_doc_lifecycle(tmp_path):
    tx, b, ws = make(tmp_path)
    t = answer(tx, "allow")
    try:
        ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Write",
                         tool_input={"file_path": str(tmp_path / "out.txt")},
                         auto_allow=[], approvals=[], timeout_s=5)
        assert ok
    finally:
        t.join()
    doc = tx.docs["status/asks/helper.json"]
    assert doc["asks"] == []             # cleared once resolved


def test_timeout_fails_closed_and_denials_cache(tmp_path):
    tx, b, ws = make(tmp_path)
    args = dict(chat_id="c1", workspace=ws, tool="Write",
                tool_input={"file_path": str(tmp_path / "no.txt")},
                auto_allow=[], approvals=[], timeout_s=0.2)
    t0 = time.time()
    ok, msg = b.decide(**args)
    assert not ok and "no answer" in msg
    # the retry of the SAME intent answers instantly from the deny cache
    ok, _ = b.decide(**args)
    assert not ok and (time.time() - t0) < 2.0
    assert len([d for d in (tx.docs["status/asks/helper.json"]["asks"])]) == 0


def test_question_pipe_returns_the_text(tmp_path):
    tx, b, _ = make(tmp_path)
    t = answer(tx, "answer", text="ship it")
    try:
        verdict, text = b.ask(chat_id="c1", kind="question", tool="question",
                              detail="may I?", timeout_s=5)
        assert (verdict, text) == ("answer", "ship it")
    finally:
        t.join()


# ---------------------------------------------------------- the MCP channel

def call_tool(url: str, tool: str, args: dict) -> str:
    async def _run():
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool(tool, args)
                return res.content[0].text

    return anyio.run(_run)


def test_bridge_approve_and_ask_member_over_http(tmp_path):
    tx, b, ws = make(tmp_path)
    with BridgeServer(b, chat_id="c1", workspace=ws, auto_allow=["Read"],
                      approvals=[], ask_timeout_s=0.3,
                      deny_roots=[tmp_path / "home"]) as bridge:
        out = json.loads(call_tool(bridge.url, "approve", {
            "tool_name": "Write",
            "input": {"file_path": str(ws / "ok.txt")}}))
        assert out["behavior"] == "allow"
        assert out["updatedInput"]["file_path"].endswith("ok.txt")

        out = json.loads(call_tool(bridge.url, "approve", {
            "tool_name": "Write",
            "input": {"file_path": str(tmp_path / "elsewhere.txt")}}))
        assert out["behavior"] == "deny"          # timeout -> fail closed

        t = answer(tx, "answer", text="go ahead", delay=0.05)
        try:
            reply = call_tool(bridge.url, "ask_member",
                              {"question": "may I proceed?"})
            assert reply == "go ahead"
        finally:
            t.join()
    # the context manager tore the server down
    with pytest.raises(Exception):
        call_tool(bridge.url, "approve", {"tool_name": "Read", "input": {}})


# -------------------------------------------------- capability tools (R19)

def test_capability_tools_ride_the_agents_own_gates(tmp_path):
    """pin/star/react/forward/create/timer over real http, as the agent's
    own identity — membership and the owner's R6 rules gate every call."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Main", members=["helper"])
        other = owner.create_chat("Side", members=["helper"])
        m = owner.post(chat.id, "important note")
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id, other.id])

        b = PermissionBroker(agent.tx, "helper")
        timers: list[dict] = []
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3, mesh=agent,
                          timers_out=timers) as bridge:
            url = bridge.url
            chats = json.loads(call_tool(url, "list_chats", {}))
            assert {c["id"] for c in chats} == {chat.id, other.id}

            assert call_tool(url, "pin_message", {"message_id": m.id}) == "pinned"
            assert call_tool(url, "star_messages",
                             {"message_ids": [m.id]}) == "starred"
            assert call_tool(url, "react",
                             {"message_id": m.id, "emoji": "👍"}) == "ok"
            assert call_tool(url, "forward_message", {
                "message_id": m.id, "to_chat_id": other.id}) == "forwarded"

            out = json.loads(call_tool(url, "create_dm", {
                "user": "aryan", "message": "opening line"}))
            assert set(out["members"]) == {"helper", "aryan"}
            dm_id = out["chat_id"]

            note = call_tool(url, "schedule_timer",
                             {"minutes": 5, "note": "check back"})
            assert "5 min" in note
            assert timers == [{"in_s": 300.0, "note": "check back"}]

        owner.sync.sync_once()
        assert m.id in owner.pins(chat.id)                 # pin landed
        fwd = [x for x in owner.messages_for(other.id) if x.fwd]
        assert fwd and fwd[0].body == "important note"
        assert fwd[0].fwd["from"] == "aryan"               # provenance kept
        opening = [x for x in owner.messages_for(dm_id)
                   if x.from_ == "helper" and x.kind.value == "message"]
        assert [x.body for x in opening] == ["opening line"]
    finally:
        agent.close()
        owner.close()


def test_capability_creates_are_gated_and_capped(tmp_path):
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    owner.accounts.create_human("sudhir", "sudhir-pw-1")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Main", members=["helper"])
        agent.sync.sync_once()
        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3,
                          mesh=agent) as bridge:
            url = bridge.url
            # the owner's R6 rule refuses politely — and burns no slot
            owner.set_agent_rules("helper", {"messaging": "nobody"})
            out = call_tool(url, "create_dm", {"user": "sudhir"})
            assert out.startswith("could not do that:")
            owner.set_agent_rules("helper", {"messaging": "everyone"})
            for i in range(2):                             # the cap is 2/run
                out = json.loads(call_tool(url, "create_group", {
                    "name": f"Made {i}", "members": ["aryan"]}))
                assert out["chat_id"]
            out = call_tool(url, "create_dm", {"user": "sudhir"})
            assert "limit" in out
    finally:
        agent.close()
        owner.close()


# ------------------------------------------------------------- argv plumbing

def test_permission_args_ride_both_argv_modes():
    p = Preset.from_dict({
        "id": "x", "command": "x",
        "args": ["--nice", "{prompt}"], "args_minimal": ["{prompt}"],
        "permission_args": ["--mcp-config", "{mcp_config}",
                            "--permission-prompt-tool", "mcp__ab__approve"],
    })
    full = p.build_argv(prompt="p", workdir="w", reply_file="r",
                        mcp_config='{"u":1}')
    slim = p.build_argv(prompt="p", workdir="w", reply_file="r",
                        mcp_config='{"u":1}', minimal=True)
    for argv in (full, slim):                     # plumbing is never dropped
        assert argv[argv.index("--mcp-config") + 1] == '{"u":1}'
        assert "--permission-prompt-tool" in argv
    bare = p.build_argv(prompt="p", workdir="w", reply_file="r")
    assert "--mcp-config" not in bare             # no bridge, no flags