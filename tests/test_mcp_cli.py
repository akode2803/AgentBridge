"""mesh-cli v2 (R12): the MCP tool surface, exercised through a real
in-memory client session against a real folder-backed mesh."""

import json

import anyio
import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import (  # noqa: E402 — after the importorskip
    create_connected_server_and_client_session,
)

from agentbridge.cli.server import build_mcp  # noqa: E402
from agentbridge.mesh.service import Mesh  # noqa: E402
from agentbridge.transport.folder import FolderTransport  # noqa: E402


from conftest import install_key, seed_account  # noqa: E402


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    bundles = {
        "aryan": seed_account(tx, "aryan"),
        "fable": seed_account(tx, "fable"),
        "claude": seed_account(tx, "claude", "agent", owner="aryan"),
    }

    def mk(user):
        home = tmp_path / f"home-{user}"
        install_key(home, user, bundles[user])
        return Mesh(FolderTransport(root), user, "mach1", home=home)

    meshes = {u: mk(u) for u in ("aryan", "fable", "claude")}
    yield meshes
    for m in meshes.values():
        m.close()


def call(server, tool, args=None):
    """One tool call through a real in-memory MCP client session."""

    async def _run():
        async with create_connected_server_and_client_session(
            server._mcp_server
        ) as session:
            result = await session.call_tool(tool, args or {})
            return result

    return anyio.run(_run)


def payload(result) -> dict | list:
    return json.loads(result.content[0].text)


def tool_names(server) -> list[str]:
    async def _run():
        async with create_connected_server_and_client_session(
            server._mcp_server
        ) as session:
            listed = await session.list_tools()
            return [t.name for t in listed.tools]

    return anyio.run(_run)


# ------------------------------------------------------------------ surface

def test_account_tools_absent_by_design(world):
    """D19: the MCP surface offers NO account management — to anyone."""
    server = build_mcp(world["claude"])
    names = tool_names(server)
    assert "send_message" in names and "create_dm" in names
    for forbidden in (
        "set_status", "set_handle", "set_display", "set_privacy",
        "set_about", "block", "unblock", "delete_account", "delete_agent",
        "remove_member",  # agents never remove; the tool doesn't exist at all
    ):
        assert forbidden not in names


# ---------------------------------------------------------------- messaging

def test_send_read_roundtrip_between_identities(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("MCP Room", members=["fable"])
    s_aryan, s_fable = build_mcp(aryan), build_mcp(fable)

    sent = payload(call(s_aryan, "send_message",
                        {"chat_id": chat.id, "body": "hello over MCP"}))
    msgs = payload(call(s_fable, "read_messages", {"chat_id": chat.id}))
    bodies = [m["body"] for m in msgs if m["kind"] == "message"]
    assert bodies == ["hello over MCP"]
    assert msgs[-1]["id"] == sent["id"]


def test_reply_threading_carries_quote(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Threads", members=["fable"])
    s_a, s_f = build_mcp(aryan), build_mcp(fable)
    parent = payload(call(s_a, "send_message", {"chat_id": chat.id, "body": "question?"}))
    call(s_f, "read_messages", {"chat_id": chat.id})  # fable syncs
    payload(call(s_f, "send_message",
                 {"chat_id": chat.id, "body": "answer!", "reply_to_id": parent["id"]}))
    msgs = payload(call(s_a, "read_messages", {"chat_id": chat.id}))
    reply = [m for m in msgs if m.get("reply_to")][0]
    assert reply["reply_to"]["id"] == parent["id"]
    assert reply["reply_to"]["from"] == "aryan"


def test_membership_errors_surface_as_tool_errors(world):
    fable = world["fable"]
    secret = world["aryan"].create_chat("No fable here")
    server = build_mcp(fable)
    result = call(server, "read_messages", {"chat_id": secret.id})
    assert result.isError  # NotAMember becomes a clean tool error


def test_react_star_pin_mark_read(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Ops", members=["fable"])
    s_a, s_f = build_mcp(aryan), build_mcp(fable)
    sent = payload(call(s_a, "send_message", {"chat_id": chat.id, "body": "op me"}))
    call(s_f, "read_messages", {"chat_id": chat.id})

    call(s_f, "react", {"chat_id": chat.id, "message_id": sent["id"], "emoji": "👍"})
    call(s_f, "star_messages", {"chat_id": chat.id, "message_ids": [sent["id"]]})
    call(s_f, "pin_message", {"chat_id": chat.id, "message_id": sent["id"]})
    call(s_f, "mark_read", {"chat_id": chat.id})

    msgs = payload(call(s_a, "read_messages", {"chat_id": chat.id}))
    assert msgs[-1]["reactions"] == {"👍": ["fable"]}
    unread = payload(call(s_f, "my_unread"))
    assert chat.id not in unread


# ------------------------------------------------------------------- chats

def test_agent_creates_dm_owner_rides_along(world):
    claude = world["claude"]
    server = build_mcp(claude)
    out = payload(call(server, "create_dm", {"user": "fable"}))
    assert set(out["members"]) == {"claude", "fable", "aryan"}  # D18


def test_create_group_and_chat_info(world):
    server = build_mcp(world["aryan"])
    made = payload(call(server, "create_group",
                        {"name": "Made by MCP", "members": ["fable"]}))
    info = payload(call(server, "chat_info", {"chat_id": made["chat_id"]}))
    assert info["members"]["aryan"] == "admin"
    assert info["permissions"]["send_history"] is True
    chats = payload(call(server, "list_chats"))
    assert any(c["id"] == made["chat_id"] for c in chats)


def test_who_is_respects_privacy(world):
    aryan, fable = world["aryan"], world["fable"]
    aryan.set_privacy({"about": "nobody", "messaging": "members"})
    prof = payload(call(build_mcp(fable), "who_is", {"user": "aryan"}))
    assert prof["messaging"] == "members"     # the public gate is visible
    assert "about" not in prof                # the private surface is not


# ------------------------------------------------------------------- events

def test_next_events_long_poll_delivers(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Live", members=["fable"])
    s_f = build_mcp(fable)

    aryan.post(chat.id, "event me")
    aryan.outbox.flush_once()
    events = payload(call(s_f, "next_events", {"timeout_s": 2.0}))
    kinds = [e["type"] for e in events]
    assert "message" in kinds
    msg_ev = [e for e in events if e["type"] == "message"][0]
    assert msg_ev["chat"] == chat.id
