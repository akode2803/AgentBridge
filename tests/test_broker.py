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


def test_auto_allow_never_greenlights_a_read_outside_the_workspace(tmp_path):
    """V79 (R67): reading a file OUTSIDE the workspace is a privacy decision
    the owner must make — auto_allow no longer short-circuits it (the live
    agent read a personal PDF in Downloads with no prompt). It falls to the
    ask, which times out closed here."""
    _, b, ws = make(tmp_path)
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Read",
                     tool_input={"file_path": str(tmp_path / "downloads" / "tax.pdf")},
                     auto_allow=["Read", "Glob", "Grep"], approvals=[],
                     timeout_s=0.2)
    assert not ok            # gated: no auto-allow reach onto the host
    # a Glob pointed at an outside directory is gated the same way
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Glob",
                     tool_input={"pattern": "**/*.pdf",
                                 "path": str(tmp_path / "downloads")},
                     auto_allow=["Read", "Glob", "Grep"], approvals=[],
                     timeout_s=0.2)
    assert not ok


def test_auto_allow_still_runs_workspace_cwd_and_stateless_tools(tmp_path):
    """The confinement doesn't over-restrict: a no-path Glob (cwd is the
    workspace) and a stateless tool like TodoWrite still run instantly."""
    _, b, ws = make(tmp_path)
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Glob",
                     tool_input={"pattern": "**/*.py"},   # no path -> cwd = ws
                     auto_allow=["Read", "Glob", "Grep", "TodoWrite"],
                     approvals=[], timeout_s=1)
    assert ok
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="TodoWrite",
                     tool_input={"todos": [{"content": "x"}]},
                     auto_allow=["Read", "Glob", "Grep", "TodoWrite"],
                     approvals=[], timeout_s=1)
    assert ok
    # a read INSIDE the workspace is instant regardless of auto_allow
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Read",
                     tool_input={"file_path": str(ws / "inbox" / "doc.txt")},
                     auto_allow=[], approvals=[], timeout_s=1)
    assert ok


def test_owner_can_still_grant_an_outside_read_via_standing_approval(tmp_path):
    """V79 leaves the escape hatch: an owner who wants the agent to read a
    host path without a prompt each time grants a standing approval (or
    clicks always-allow) — the fix removes the SILENT default, not the
    ability."""
    _, b, ws = make(tmp_path)
    ok, _ = b.decide(chat_id="c1", workspace=ws, tool="Read",
                     tool_input={"file_path": str(tmp_path / "downloads" / "ok.txt")},
                     auto_allow=["Read"],
                     approvals=[{"tool": "Read", "chat": "*"}], timeout_s=0.2)
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


def test_ask_doc_carries_label_and_options(tmp_path):
    """R43/Q28: a permission ask publishes the friendly verb phrase (never
    the raw tool id alone) and a question publishes its offered choices."""
    from agentbridge.harness.docs import ToolDocs

    tx = FakeTx()
    b = PermissionBroker(tx, "helper", docs=ToolDocs.load(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()

    seen: dict = {}

    def capture(delay=0.1):
        def run():
            deadline = time.time() + 5
            while time.time() < deadline:
                asks = (tx.docs.get("status/asks/helper.json") or {}).get("asks") or []
                if asks:
                    seen.update(asks[0])
                    tx.docs["status/asks/helper_answers.json"] = {
                        "answers": {asks[0]["id"]: {"verdict": "deny",
                                                    "text": "try the outbox"}}}
                    return
                time.sleep(0.02)
        t = threading.Timer(delay, run)
        t.start()
        return t

    t = capture()
    try:
        ok, msg = b.decide(chat_id="c1", workspace=ws, tool="Write",
                           tool_input={"file_path": str(tmp_path / "x.txt")},
                           auto_allow=[], approvals=[], timeout_s=5)
        assert not ok and msg == "try the outbox"   # the deny note reaches it
        assert seen["label"] == "write a file"
    finally:
        t.join()

    seen.clear()
    t = capture()
    try:
        verdict, text = b.ask(chat_id="c1", kind="question", tool="question",
                              detail="which one?", timeout_s=5,
                              options=["red", "blue"])
        assert verdict == "answer"
        assert seen["options"] == ["red", "blue"]
        assert "label" not in seen                  # questions carry no phrase
    finally:
        t.join()

    # an unmapped tool still gets a humanized phrase, never a raw id
    assert b.docs.ask_phrase("mcp__github__create_issue") \
        == "use create issue (github)"


def test_tooldocs_catalog_topic_and_override(tmp_path):
    """R43/Q7/Q11: the shipped manual serves a catalog + full entries; a
    home overlay rewords an entry without touching code."""
    from agentbridge.harness.docs import ToolDocs

    docs = ToolDocs.load(tmp_path)          # no overlay: shipped data
    cat = docs.catalog()
    assert "memory" in cat and "pin_message" in cat
    # inner-CLI tools carry only an ask phrase — they are not the agent's
    # manual, so the catalog must not list them
    listed = [ln[2:].split(":")[0] for ln in cat.splitlines()
              if ln.startswith("- ")]
    assert "write" not in listed and "bash" not in listed
    assert "'global' spans your chats" in docs.topic("remember")
    assert docs.topic("mcp__ab__remember") == docs.topic("remember")
    missing = docs.topic("no_such_thing")
    assert "No entry" in missing

    over = tmp_path / "prompts"
    over.mkdir(parents=True)
    (over / "tooldocs.json").write_text(json.dumps({
        "tools": {"remember": {"ask": "save a note",
                               "short": "S", "long": "OVERRIDDEN"}}}),
        encoding="utf-8")
    docs2 = ToolDocs.load(tmp_path)
    assert docs2.topic("remember") == "OVERRIDDEN"
    assert "pin_message" in docs2.catalog()   # the rest of the pack survives


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

        # V79 over the wire: an auto_allow READ of a path outside the
        # workspace no longer passes silently — it becomes an ask (deny here)
        out = json.loads(call_tool(bridge.url, "approve", {
            "tool_name": "Read",
            "input": {"file_path": str(tmp_path / "downloads" / "personal.pdf")}}))
        assert out["behavior"] == "deny"
        # but a no-path read stays instant (workspace cwd)
        out = json.loads(call_tool(bridge.url, "approve", {
            "tool_name": "Read", "input": {}}))
        assert out["behavior"] == "allow"

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


def test_bridge_question_options_and_read_docs_over_http(tmp_path):
    """R43: ask_member forwards sanitized options into the ask doc, and
    read_docs serves the manual — both over the real MCP channel."""
    from agentbridge.harness.docs import ToolDocs

    tx, b, ws = make(tmp_path)
    with BridgeServer(b, chat_id="c1", workspace=ws, auto_allow=[],
                      approvals=[], ask_timeout_s=1.0,
                      docs=ToolDocs.load(tmp_path / "home")) as bridge:
        seen: dict = {}

        def run():
            deadline = time.time() + 5
            while time.time() < deadline:
                asks = (tx.docs.get("status/asks/helper.json") or {}).get("asks") or []
                if asks:
                    seen.update(asks[0])
                    tx.docs["status/asks/helper_answers.json"] = {
                        "answers": {asks[0]["id"]: {"verdict": "answer",
                                                    "text": "blue"}}}
                    return
                time.sleep(0.02)

        t = threading.Timer(0.05, run)
        t.start()
        try:
            reply = call_tool(bridge.url, "ask_member", {
                "question": "which color?",
                # strings and {label, description} mix; junk drops; caps at 4
                "options": ["red", {"label": "blue", "description": "calm and cool"},
                            "  green ", "", "gold", "extra"]})
            assert reply == "blue"
            assert seen["options"] == [
                {"label": "red"},
                {"label": "blue", "description": "calm and cool"},
                {"label": "green"}, {"label": "gold"}]
        finally:
            t.join()

        cat = call_tool(bridge.url, "read_docs", {})
        assert "pin_message" in cat and "Guides:" in cat
        entry = call_tool(bridge.url, "read_docs", {"topic": "workspace"})
        assert "your own desk" in entry
        assert "No entry" in call_tool(bridge.url, "read_docs",
                                       {"topic": "flying"})


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
            # V74: the confirmation states the resolved local fire time (+UTC
            # offset) so an agent/member tz mismatch is unambiguous
            assert "at 20" in note and "UTC" in note   # "...(at 20YY-...UTC±..)"
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


def test_read_status_tool_is_privacy_gated(tmp_path):
    """R35: the agent can query a member's availability on demand, but only
    the fields that member shares with it."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        owner.set_status("dnd", "heads-down on the migration")
        chat = owner.create_chat("Main", members=["helper"])
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])

        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3, mesh=agent) as bridge:
            out = call_tool(bridge.url, "read_status", {"username": "aryan"})
            assert "dnd" in out and "migration" in out
            assert "no such member" in call_tool(
                bridge.url, "read_status", {"username": "nobody"})

        owner.set_privacy({"status": "nobody"})   # aryan hides status
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3, mesh=agent) as bridge:
            out = call_tool(bridge.url, "read_status", {"username": "aryan"})
            assert "dnd" not in out and "migration" not in out
    finally:
        agent.close()
        owner.close()


def test_agent_profile_and_permission_tools(tmp_path):
    """R38: set_status/set_about keep the agent's OWN profile current (owner
    and agent both write, most recent wins), and read_permissions returns its
    own owner-set rules — but only the PUBLIC gates for anyone else."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        owner.set_privacy({"messaging": "members"})       # aryan's public gate
        owner.set_agent_rules("helper", {"messaging": "members"})
        chat = owner.create_chat("Main", members=["helper"])
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])

        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3, mesh=agent) as bridge:
            assert call_tool(bridge.url, "set_status", {
                "state": "busy", "working_on": "indexing the repo",
            }) == "status updated"
            assert call_tool(bridge.url, "set_about", {
                "about": "I run the nightly reports",
            }) == "about updated"

            own = json.loads(call_tool(bridge.url, "read_permissions", {}))
            assert own["outbound"]["may_message"] == "members"
            assert "privacy" in own and own["set_by"]

            other = json.loads(call_tool(
                bridge.url, "read_permissions", {"username": "aryan"}))
            assert other["messaging"] == "members"          # public by design
            assert "privacy" not in other                   # the rest hidden

        acc = agent.directory.get("helper")
        assert acc.status.state == "busy"
        assert acc.status.text == "indexing the repo"
        assert acc.about == "I run the nightly reports"
        # most recent wins: the owner overwrites the agent's own status
        owner.set_status("available", agent="helper")
        assert owner.directory.get("helper").status.state == "available"
    finally:
        agent.close()
        owner.close()


def test_agent_edits_and_deletes_only_its_own_messages(tmp_path):
    """R33: an agent gets edit_message/delete_message over its OWN messages
    (author-only, like a human) — never over another member's."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Main", members=["helper"])
        theirs = owner.post(chat.id, "the owner's message")
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])
        mine = agent.post(chat.id, "the agent's first take")
        agent.outbox.flush_once()
        agent.sync.sync_once([chat.id])

        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3, mesh=agent) as bridge:
            url = bridge.url
            # its own message: edit + delete both work
            assert call_tool(url, "edit_message", {
                "message_id": mine.id, "new_body": "the agent's revised take"}) \
                == "edited"
            # the owner's message: refused on both, no backend error leaks
            assert "your own" in call_tool(url, "edit_message", {
                "message_id": theirs.id, "new_body": "hijack"})
            assert "your own" in call_tool(url, "delete_message",
                                           {"message_id": theirs.id})
            assert call_tool(url, "delete_message",
                             {"message_id": mine.id}) == "deleted"

        owner.sync.sync_once([chat.id])
        seen = {x.id: x for x in owner.messages_for(chat.id)}
        assert seen[theirs.id].body == "the owner's message"   # untouched
        assert seen[mine.id].deleted                           # deleted wins
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

# ------------------------------------------- chat-level member tools (V53)

def _real_answer(tx, verdict, text="", delay=0.1):
    """A stand-in owner over a REAL transport: answers the first pending
    ask (the fake-transport helper above writes tx.docs directly)."""
    def run():
        deadline = time.time() + 5
        while time.time() < deadline:
            doc = tx.get_doc("status/asks/helper.json") or {}
            asks = doc.get("asks") or []
            if asks:
                tx.put_doc("status/asks/helper_answers.json", {
                    "answers": {asks[0]["id"]: {"verdict": verdict,
                                                "text": text}}})
                return
            time.sleep(0.02)
    t = threading.Timer(delay, run)
    t.start()
    return t


def test_chat_member_tools_flags_and_group_edits(tmp_path):
    """V53: mute/archive write the agent's OWN overlay; group edits ride
    the real authz gates (default all-members, refused once admins-only);
    message_info returns receipts for its own message only."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Main", members=["helper"])
        m = owner.post(chat.id, "note for receipts")
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])

        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.4,
                          mesh=agent) as bridge:
            url = bridge.url
            # own per-chat flags — the agent's view only
            assert "muted for 8h" in call_tool(url, "mute_chat",
                                               {"duration": "8h"})
            assert "unmuted" in call_tool(url, "mute_chat",
                                          {"duration": "off"})
            assert "archived" in call_tool(url, "archive_chat",
                                           {"archived": True})
            assert agent.chat_overview(chat.id)["archived"] is True
            assert owner.chat_overview(chat.id)["archived"] is False

            # group edits under the default (all-members) permission
            assert call_tool(url, "rename_chat",
                             {"name": "Renamed by helper"}) == "renamed"
            agent.outbox.flush_once()
            owner.sync.sync_once([chat.id])
            assert owner.snapshot(chat.id).name == "Renamed by helper"
            assert call_tool(url, "set_description",
                             {"text": "the desc"}) == "description updated"

            # admins-only flips the same tools to an honest refusal
            owner.set_permissions(chat.id, {"edit_settings": "admins"})
            owner.outbox.flush_once()
            agent.sync.sync_once([chat.id])
            agent.membership.refold(chat.id)
            out = call_tool(url, "rename_chat", {"name": "nope"})
            assert "could not do that" in out

            # receipts: own message only
            own = agent.post(chat.id, "mine")
            agent.outbox.flush_once()
            info = call_tool(url, "message_info", {"message_id": own.id})
            assert "receipts are for your own" not in info
            other = call_tool(url, "message_info", {"message_id": m.id})
            assert "receipts are for your own" in other
    finally:
        agent.close()
        owner.close()


def test_leave_and_clear_are_owner_gated(tmp_path):
    """V53: leave_chat/clear_chat ask the owner. No answer = refusal;
    allow = clear executes now, leave is DEFERRED (flag for the runner)."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Gated", members=["helper"])
        owner.post(chat.id, "history line")
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])

        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.5,
                          mesh=agent) as bridge:
            url = bridge.url
            # silence = fail closed, nothing changed
            out = call_tool(url, "leave_chat", {"reason": "done here"})
            assert "did not approve" in out
            assert bridge.leave_requested is False

            # owner denies with a note — the note reaches the agent
            t = _real_answer(agent.tx, "deny", text="stay put", delay=0.05)
            out = call_tool(url, "clear_chat", {})
            t.join()
            assert "declined" in out and "stay put" in out
            assert len(agent.messages_for(chat.id)) >= 1

            # owner allows — clear executes (agent's view only)
            t = _real_answer(agent.tx, "allow", delay=0.05)
            out = call_tool(url, "clear_chat", {})
            t.join()
            assert "cleared" in out
            assert [m for m in agent.messages_for(chat.id)
                    if m.kind.value == "message"] == []
            assert len([m for m in owner.messages_for(chat.id)
                        if m.kind.value == "message"]) == 1

            # owner allows the leave — deferred, membership intact for now
            t = _real_answer(agent.tx, "allow", delay=0.05)
            out = call_tool(url, "leave_chat", {"reason": "wrapping up"})
            t.join()
            assert "after this reply posts" in out
            assert bridge.leave_requested is True
            assert "helper" in agent.snapshot(chat.id).members
    finally:
        agent.close()
        owner.close()


def test_context_and_files_parity_c(tmp_path):
    """V54 (parity c): list_chats carries unread + own flags; list_files
    inventories the chat; fetch_file decrypts an older blob into the
    workspace inbox; reactions/genesis/roles ride the rendered context."""
    from agentbridge.harness.conversation import ConversationManager
    from agentbridge.harness.prompt import PromptManager
    from agentbridge.harness.queue import WorkGroup, WorkItem
    from agentbridge.harness.settings import HarnessSettings

    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "devbox", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Facts", members=["helper"])
        m = owner.post(chat.id, "hey @helper look at this")
        owner.react(chat.id, m.id, "🎯")
        # a real sealed attachment, posted the GUI way
        blob_id = "f-parityc.txt"
        data = b"the file body"
        sealed = owner.sealer.seal_blob(chat.id, blob_id, data)
        owner.tx.put_blob(f"chats/{chat.id}/files/{blob_id}", sealed)
        owner.post(chat.id, "with a file", files=[{
            "id": blob_id, "name": "notes.txt", "bytes": len(data)}])
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])

        # --- context: reactions, genesis, roles, permissions
        cm = ConversationManager(agent)
        group = WorkGroup(chat.id, "aryan", [WorkItem(
            key=f"{chat.id}|{m.id}", chat_id=chat.id, kind="message",
            msg_id=m.id, sender="aryan", ns=m.ns, reason="mention")])
        delivery = cm.build(group, agent.messages_for(chat.id),
                            HarnessSettings())
        assert delivery.created_by == "aryan" and delivery.created_at
        assert delivery.permissions.get("edit_settings") == "all"
        aryan_row = next(r for r in delivery.roster if r["name"] == "aryan")
        assert aryan_row["desc"] == "admin"
        ctx = PromptManager(home).for_agent(
            agent.directory.get("helper")).context_text(delivery)
        assert "[reactions: 🎯 by @aryan]" in ctx
        assert "Created by @aryan" in ctx
        assert "Group permissions:" in ctx and "send_messages=all" in ctx

        # --- tools over real HTTP
        b = PermissionBroker(agent.tx, "helper")
        ws = tmp_path / "ws"
        ws.mkdir()
        with BridgeServer(b, chat_id=chat.id, workspace=ws, auto_allow=[],
                          approvals=[], ask_timeout_s=0.3,
                          mesh=agent) as bridge:
            url = bridge.url
            chats = json.loads(call_tool(url, "list_chats", {}))
            row = next(c for c in chats if c["id"] == chat.id)
            assert row["unread"] >= 1          # the promised counts (c2)
            call_tool(url, "archive_chat", {"archived": True})
            call_tool(url, "mute_chat", {"duration": "forever"})
            row = next(c for c in json.loads(call_tool(url, "list_chats", {}))
                       if c["id"] == chat.id)
            assert row.get("archived") is True and row.get("muted") is True

            files = json.loads(call_tool(url, "list_files", {}))
            assert files[0]["name"] == "notes.txt"
            assert files[0]["file_id"] == blob_id
            out = call_tool(url, "fetch_file", {"file_id": blob_id})
            assert out == "saved to inbox/notes.txt"
            assert (ws / "inbox" / "notes.txt").read_bytes() == data
            miss = call_tool(url, "fetch_file", {"file_id": "f-nope"})
            assert "no such file" in miss
    finally:
        agent.close()
        owner.close()
