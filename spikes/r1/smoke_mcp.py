"""R1 spike: official MCP python SDK — server with a tool, exercised through an
in-memory client session (the transport-independent core of R12's mesh-cli).
"""

import sys

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

server = FastMCP("agentbridge-smoke")


@server.tool()
def send_message(chat: str, body: str) -> str:
    """Pretend to post a message to a mesh chat."""
    return f"posted to {chat}: {body}"


async def run() -> None:
    async with create_connected_server_and_client_session(
        server._mcp_server  # the underlying low-level Server
    ) as session:
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        assert "send_message" in names, names

        result = await session.call_tool("send_message", {"chat": "qa", "body": "hello"})
        text = result.content[0].text
        assert "posted to qa: hello" in text, text

    print(f"OK smoke_mcp: FastMCP server + in-memory client session, tools={names}")


if __name__ == "__main__":
    try:
        anyio.run(run)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_mcp: {type(e).__name__}: {e}")
        sys.exit(1)
