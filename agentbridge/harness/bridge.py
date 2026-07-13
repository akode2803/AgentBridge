"""The harness↔agent bridge (R18) — the 2-way channel an inner CLI uses to
reach its harness while it runs.

One tiny MCP server (streamable-http on 127.0.0.1, ephemeral port) is
started PER RUN and torn down with it: the run's chat, workspace and policy
are bound into the tool closures, so a runner dispatching parallel runs
across chats never mixes up who is asking. Spike-verified primitives only
(R18 spike, 2026-07-13): claude calls ``--permission-prompt-tool`` with
``{tool_name, input, tool_use_id}`` and expects a TEXT JSON string back —
``structured_output=False`` is mandatory (FastMCP's structuredContent
wrapping reads as an invalid response).

Tools:
- ``approve``    — the permission gate; the broker decides (policy or owner).
- ``ask_member`` — the agent asks its responsible member a question; the
  same owner-popup pipe answers it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .broker import PermissionBroker

__all__ = ["BridgeServer"]

_START_TIMEOUT_S = 15.0


class BridgeServer:
    """One run's MCP endpoint. Use as a context manager."""

    def __init__(self, broker: PermissionBroker, *, chat_id: str,
                 workspace: Path, auto_allow: list[str],
                 approvals: list[dict], ask_timeout_s: float,
                 deny_roots: list[Path] | None = None) -> None:
        self.broker = broker
        self.chat_id = chat_id
        self.workspace = workspace
        self.auto_allow = list(auto_allow or [])
        self.approvals = list(approvals or [])
        self.ask_timeout_s = ask_timeout_s
        self.deny_roots = list(deny_roots or [])
        self.port = 0
        self._server = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> "BridgeServer":
        from mcp.server.fastmcp import FastMCP
        import uvicorn

        mcp = FastMCP("ab")

        @mcp.tool(structured_output=False)
        def approve(tool_name: str = "", input: dict | None = None,
                    tool_use_id: str = "",
                    permission_suggestions: list | None = None) -> str:
            """Permission gate for tool use (the harness decides)."""
            allowed, message = self.broker.decide(
                chat_id=self.chat_id, workspace=self.workspace,
                tool=tool_name, tool_input=input or {},
                auto_allow=self.auto_allow, approvals=self.approvals,
                timeout_s=self.ask_timeout_s, deny_roots=self.deny_roots)
            if allowed:
                return json.dumps({"behavior": "allow",
                                   "updatedInput": input or {}})
            return json.dumps({"behavior": "deny", "message": message})

        @mcp.tool(structured_output=False)
        def ask_member(question: str = "") -> str:
            """Ask your responsible member one question; returns their
            answer (or the fact that they did not answer in time)."""
            q = " ".join((question or "").split())[:500]
            if not q:
                return "no question given"
            verdict, text = self.broker.ask(
                chat_id=self.chat_id, kind="question", tool="question",
                detail=q, timeout_s=self.ask_timeout_s)
            if verdict == "answer" and text:
                return text
            return ("no answer within the waiting window — decide "
                    "reasonably yourself or say you'll follow up")

        config = uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1",
                                port=0, log_level="error", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="ab-bridge")
        self._thread.start()
        deadline = time.time() + _START_TIMEOUT_S
        while time.time() < deadline:
            if self._server.started and self._server.servers:
                socks = self._server.servers[0].sockets
                if socks:
                    self.port = socks[0].getsockname()[1]
                    return self
            time.sleep(0.05)
        self.__exit__(None, None, None)
        raise RuntimeError("the harness bridge never came up")

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._server = None
        self._thread = None

    # ------------------------------------------------------------- config
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def mcp_config(self) -> str:
        """The inline --mcp-config JSON an inner CLI consumes."""
        return json.dumps(
            {"mcpServers": {"ab": {"type": "http", "url": self.url}}})
