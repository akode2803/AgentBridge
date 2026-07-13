"""The harness↔agent bridge (R18/R19) — the 2-way channel an inner CLI uses
to reach its harness while it runs.

One tiny MCP server (streamable-http on 127.0.0.1, ephemeral port) is
started PER RUN and torn down with it: the run's chat, workspace and policy
are bound into the tool closures, so a runner dispatching parallel runs
across chats never mixes up who is asking. Spike-verified primitives only
(R18 spike, 2026-07-13): claude calls ``--permission-prompt-tool`` with
``{tool_name, input, tool_use_id}`` and expects a TEXT JSON string back —
``structured_output=False`` is mandatory (FastMCP's structuredContent
wrapping reads as an invalid response).

Gate tools:
- ``approve``    — the permission gate; the broker decides (policy or owner).
- ``ask_member`` — the agent asks its responsible member a question; the
  same owner-popup pipe answers it.

Capability tools (R19) — the same operations mesh-cli offers, bound to the
agent's OWN Mesh facade, so every membership/privacy/R6 gate applies exactly
as it would to any member. ``send``/``read`` are DELIBERATELY absent: the
reply pipeline owns posting (threading, rate caps, the answered-guard) and
the context file owns reading — a raw send tool would reopen the
duplicate-reply hole. Creates are capped per run (a runaway loop can spam
chats faster than an owner can react); mesh errors come back as plain text,
never as a crashed run.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from ..core.timekit import new_id
from .broker import PermissionBroker

__all__ = ["BridgeServer"]

_START_TIMEOUT_S = 15.0
MAX_CREATES_PER_RUN = 2
MAX_TIMERS_PER_RUN = 5


class BridgeServer:
    """One run's MCP endpoint. Use as a context manager."""

    def __init__(self, broker: PermissionBroker, *, chat_id: str,
                 workspace: Path, auto_allow: list[str],
                 approvals: list[dict], ask_timeout_s: float,
                 deny_roots: list[Path] | None = None,
                 mesh=None, timers_out: list[dict] | None = None) -> None:
        self.broker = broker
        self.chat_id = chat_id
        self.workspace = workspace
        self.auto_allow = list(auto_allow or [])
        self.approvals = list(approvals or [])
        self.ask_timeout_s = ask_timeout_s
        self.deny_roots = list(deny_roots or [])
        self.mesh = mesh                 # capability tools bind to it (R19)
        self.timers = timers_out if timers_out is not None else []
        self._creates = 0
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

        if self.mesh is not None:
            self._capability_tools(mcp)

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

    # ------------------------------------------------- capability tools (R19)
    def _capability_tools(self, mcp) -> None:
        """The mesh-cli parity subset, as THIS agent, in THIS run's chat.
        Every call rides the agent's own facade — membership, privacy and
        the owner's R6 outbound rules gate exactly as they do for anyone."""
        mesh = self.mesh
        chat = self.chat_id

        def guarded(fn) -> str:
            try:
                out = fn()
                mesh.outbox.flush_once()
                return json.dumps(out) if isinstance(out, (dict, list)) \
                    else str(out)
            except Exception as e:  # noqa: BLE001 — a refusal is an answer
                return f"could not do that: {e}"

        def known(message_id: str) -> bool:
            """Only ids the agent can SEE are actable — an invented id must
            get a plain refusal, never an opaque backend error (and a pin
            doc's filename is never built from arbitrary model output)."""
            return any(x.id == message_id for x in mesh.messages_for(chat))

        @mcp.tool(structured_output=False)
        def list_chats() -> str:
            """Every chat you are a member of (id, kind, name, members)."""
            return guarded(lambda: [
                {"id": s.id, "kind": s.kind.value, "name": s.name,
                 "members": sorted(s.members)}
                for s in mesh.membership.chats_for()])

        @mcp.tool(structured_output=False)
        def pin_message(message_id: str) -> str:
            """Pin a message of this chat for everyone (ids are in the
            transcript, e.g. m-...)."""
            if not known(message_id):
                return "no such message in this chat — ids are in the transcript"
            return guarded(lambda: (mesh.pin(chat, message_id), "pinned")[1])

        @mcp.tool(structured_output=False)
        def unpin_message(message_id: str) -> str:
            """Unpin a message of this chat."""
            if not known(message_id):
                return "no such message in this chat — ids are in the transcript"
            return guarded(lambda: (mesh.unpin(chat, message_id), "unpinned")[1])

        @mcp.tool(structured_output=False)
        def star_messages(message_ids: list[str]) -> str:
            """Star messages of this chat (your own bookmark list)."""
            bad = [i for i in (message_ids or []) if not known(i)]
            if bad or not message_ids:
                return "no such message in this chat — ids are in the transcript"
            return guarded(lambda: (mesh.star(chat, message_ids), "starred")[1])

        @mcp.tool(structured_output=False)
        def react(message_id: str, emoji: str = "") -> str:
            """Set your reaction on a message of this chat; empty removes."""
            if not known(message_id):
                return "no such message in this chat — ids are in the transcript"
            return guarded(
                lambda: (mesh.react(chat, message_id, emoji or None), "ok")[1])

        @mcp.tool(structured_output=False)
        def forward_message(message_id: str, to_chat_id: str) -> str:
            """Forward a message of this chat into another chat you are a
            member of (attachments are re-sealed for the target)."""

            def do():
                original = next((m for m in mesh.messages_for(chat)
                                 if m.id == message_id), None)
                if original is None or original.deleted:
                    return "that message is no longer available"
                files = []
                for f in original.files or []:
                    raw = mesh.tx.get_blob(f"chats/{chat}/files/{f.get('id')}")
                    data = mesh.sealer.open_blob(chat, f.get("id"), raw) \
                        if raw is not None else None
                    if data is None:
                        continue
                    name = f.get("name", "file")
                    dot = name.rfind(".")
                    blob_id = new_id("f") + (name[dot:][:12].lower()
                                             if dot > 0 else "")
                    sealed = mesh.sealer.seal_blob(to_chat_id, blob_id, data)
                    mesh.tx.put_blob(
                        f"chats/{to_chat_id}/files/{blob_id}", sealed)
                    files.append({
                        "id": blob_id, "name": name, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
                mesh.post(to_chat_id, original.body, files=files or None,
                          fwd={"from": original.from_, "ts": original.ts})
                return "forwarded"

            return guarded(do)

        @mcp.tool(structured_output=False)
        def create_dm(user: str, message: str = "") -> str:
            """Open a direct chat with a member (your responsible member
            rides along); optionally post an opening message."""

            def do():
                if self._creates >= MAX_CREATES_PER_RUN:
                    return "chat-creation limit for this run reached"
                snap = mesh.create_dm(user)
                self._creates += 1          # a refusal never burns the slot
                if message:
                    mesh.post(snap.id, message[:2000])
                return {"chat_id": snap.id, "members": sorted(snap.members)}

            return guarded(do)

        @mcp.tool(structured_output=False)
        def create_group(name: str, members: list[str],
                         message: str = "") -> str:
            """Create a group chat (owners of agent members are pulled in);
            optionally post an opening message."""

            def do():
                if self._creates >= MAX_CREATES_PER_RUN:
                    return "chat-creation limit for this run reached"
                snap = mesh.create_chat(name, members=members)
                self._creates += 1          # a refusal never burns the slot
                if message:
                    mesh.post(snap.id, message[:2000])
                return {"chat_id": snap.id, "members": sorted(snap.members)}

            return guarded(do)

        @mcp.tool(structured_output=False)
        def schedule_timer(minutes: float, note: str) -> str:
            """Wake yourself up in this chat after ``minutes`` with ``note``
            — your responsible member sees every scheduled timer."""
            if len(self.timers) >= MAX_TIMERS_PER_RUN:
                return "timer limit for this run reached"
            try:
                in_s = max(30.0, float(minutes) * 60.0)
            except (TypeError, ValueError):
                return "minutes must be a number"
            self.timers.append(
                {"in_s": in_s, "note": " ".join(str(note or "").split())[:300]})
            return f"scheduled: a wake-up in {in_s / 60:.0f} min"

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
