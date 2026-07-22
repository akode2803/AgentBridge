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
duplicate-reply hole. Multi-message turns (V78) don't change this: the
burst is split from the ONE reply at the delivery seam
(``responder.split_reply``), never posted mid-run. Creates are capped per run (a runaway loop can spam
chats faster than an owner can react); mesh errors come back as plain text,
never as a crashed run.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from ..core.models import MsgKind
from ..core.timekit import new_id
from .broker import PermissionBroker

__all__ = ["BridgeServer"]

_START_TIMEOUT_S = 15.0
MAX_CREATES_PER_RUN = 2
MAX_TIMERS_PER_RUN = 5


class _BearerAuthApp:
    """Small ASGI gate for the per-run MCP endpoint.

    FastMCP owns the protocol; this wrapper owns the local channel boundary.
    Every HTTP request must carry the run's short-lived bearer token. Lifespan
    events pass through because they are server control, not client traffic.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token.encode("ascii")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers") or ():
            if name.lower() == b"authorization":
                supplied = value
                break
        expected = b"Bearer " + self.token
        if not secrets.compare_digest(supplied, expected):
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


class BridgeServer:
    """One run's MCP endpoint. Use as a context manager."""

    def __init__(self, broker: PermissionBroker, *, chat_id: str,
                 workspace: Path, auto_allow: list[str],
                 approvals: list[dict], ask_timeout_s: float,
                 deny_roots: list[Path] | None = None,
                 mesh=None, timers_out: list[dict] | None = None,
                 memory=None, chat_kind: str = "",
                 global_memory: str = "dm", docs=None,
                 timer_svc=None) -> None:
        self.broker = broker
        self.chat_id = chat_id
        self.workspace = workspace
        self.auto_allow = list(auto_allow or [])
        self.approvals = list(approvals or [])
        self.ask_timeout_s = ask_timeout_s
        self.deny_roots = list(deny_roots or [])
        self.mesh = mesh                 # capability tools bind to it (R19)
        self.timers = timers_out if timers_out is not None else []
        # V87: the runner's own TimerService — cancel_timer acts on the
        # DURABLE list (this agent's only; the service is per-agent by
        # construction). None = the tool isn't offered (bare tests).
        self.timer_svc = timer_svc
        self.memory = memory             # MemoryStore (R20); None = no tools
        self.chat_kind = chat_kind
        self.global_memory = global_memory
        self.docs = docs                 # ToolDocs (R43); None = no read_docs
        self._creates = 0
        # V53 (leave_chat): the leave is DEFERRED — the tool only requests
        # it (owner-approved); the runner executes it after the reply posts,
        # so the agent's goodbye still lands while it is a member.
        self.leave_requested = False
        self.port = 0
        self._token = ""
        self._server = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> "BridgeServer":
        from mcp.server.fastmcp import FastMCP
        import uvicorn

        # One unguessable credential per server entry. It exists only in
        # memory and in the child CLI's per-run MCP config, then is cleared at
        # teardown. A sibling local process that finds the port cannot use it.
        self._token = secrets.token_urlsafe(32)
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
        def ask_member(question: str = "", options: list | None = None) -> str:
            """Ask your responsible member one question; returns their
            answer (or the fact that they did not answer in time). When the
            question has natural choices, pass 2-4 options — each a short
            string, or {"label": ..., "description": ...} where a line of
            detail helps them choose. They tap one instead of typing (and
            can still type something else)."""
            q = " ".join((question or "").split())[:500]
            if not q:
                return "no question given"
            # R43/R44: sanitize agent-offered choices into {label,
            # description?} — at most four; junk degrades to free text
            opts = []
            for o in options or []:
                if isinstance(o, dict):
                    label = " ".join(str(o.get("label") or "").split())[:80]
                    desc = " ".join(str(o.get("description") or "").split())[:160]
                else:
                    label, desc = " ".join(str(o).split())[:80], ""
                if label:
                    opts.append({"label": label, **({"description": desc}
                                                    if desc else {})})
                if len(opts) == 4:
                    break
            verdict, text = self.broker.ask(
                chat_id=self.chat_id, kind="question", tool="question",
                detail=q, timeout_s=self.ask_timeout_s, options=opts)
            if verdict == "answer" and text:
                return text
            return ("no answer within the waiting window — decide "
                    "reasonably yourself or say you'll follow up")

        @mcp.tool(structured_output=False)
        def tidy_workspace(paths: list | None = None) -> str:
            """Delete scratch files from YOUR workspace when you're done
            with them — your runtime has no delete (V97). No arguments =
            empty your tmp/ scratch folder; or pass workspace-relative
            paths (files or folders). context.md, reply.md, MEMORY.md and
            the inbox are managed by the harness and stay."""
            import shutil

            ws = self.workspace.resolve()
            removed: list[str] = []
            refused: list[str] = []

            def rm(target: Path, rel: str) -> None:
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    removed.append(rel)
                except OSError as e:
                    refused.append(f"{rel} ({type(e).__name__})")

            if not paths:
                tmp = ws / "tmp"
                for child in (tmp.iterdir() if tmp.is_dir() else ()):
                    rm(child, f"tmp/{child.name}")
                if not removed and not refused:
                    return "tmp/ is already empty"
            else:
                for raw in list(paths)[:50]:
                    rel = str(raw or "").strip().replace("\\", "/")
                    if not rel:
                        continue
                    target = (ws / rel).resolve()
                    if target == ws or not target.is_relative_to(ws):
                        refused.append(f"{rel} (outside your workspace)")
                        continue
                    inner = target.relative_to(ws)
                    if inner.parts[0].lower() == "inbox" or (
                            len(inner.parts) == 1 and inner.name.lower() in
                            ("context.md", "reply.md", "memory.md")):
                        refused.append(f"{rel} (managed by the harness)")
                        continue
                    if not target.exists():
                        refused.append(f"{rel} (not found)")
                        continue
                    rm(target, rel)
            bits = []
            if removed:
                more = f" (+{len(removed) - 10} more)" if len(removed) > 10 else ""
                bits.append("removed " + ", ".join(removed[:10]) + more)
            if refused:
                bits.append("refused " + ", ".join(refused[:5]))
            return "; ".join(bits) or "nothing to remove"

        if self.docs is not None:
            docs = self.docs

            @mcp.tool(structured_output=False)
            def read_docs(topic: str = "") -> str:
                """Your AgentBridge manual — no argument lists every tool
                and guide with a one-liner; pass a name for the full entry.
                Quote it (in your own words) when a member asks what you
                can do."""
                return docs.topic(topic) if topic.strip() else docs.catalog()

        if self.mesh is not None:
            self._capability_tools(mcp)
        if self.memory is not None:
            self._memory_tools(mcp)

        app = _BearerAuthApp(mcp.streamable_http_app(), self._token)
        config = uvicorn.Config(app, host="127.0.0.1",
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
            """Every chat you are a member of — id, kind, name, members,
            YOUR unread count, and your own flags (archived / muted)."""
            def do():
                out = []
                for s in mesh.membership.chats_for():
                    info = {"id": s.id, "kind": s.kind.value, "name": s.name,
                            "members": sorted(s.members)}
                    try:  # V54 c2/c3: the counts this manual always promised
                        info["unread"] = mesh.unread(s.id)["unread"]
                        st = mesh.messaging.state_of(s.id, mesh.user).get()
                        if st.get("archived"):
                            info["archived"] = True
                        mute = st.get("mute")
                        if mute is True or (isinstance(mute, (int, float))
                                            and mute > time.time_ns()):
                            info["muted"] = True
                    except Exception:  # noqa: BLE001 — listing survives a bad chat
                        pass
                    out.append(info)
                return out
            return guarded(do)

        @mcp.tool(structured_output=False)
        def list_files(limit: int = 30) -> str:
            """The files shared in this chat, newest first — name, size,
            sender, date, and the file_id that fetch_file takes. Files of
            deleted messages are gone for good."""
            def do():
                out = []
                cap = max(1, min(int(limit or 30), 100))
                for m in reversed(mesh.messages_for(chat)):
                    for f in m.files or []:
                        out.append({"file_id": f.get("id"),
                                    "name": f.get("name"),
                                    "bytes": f.get("bytes"),
                                    "from": m.from_, "ts": m.ts,
                                    "message_id": m.id})
                        if len(out) >= cap:
                            return out
                return out or "no files shared in this chat yet"
            return guarded(do)

        @mcp.tool(structured_output=False)
        def fetch_file(file_id: str) -> str:
            """Fetch one of this chat's files into your inbox folder and
            return its path — for files older than the auto-staged recent
            ones, or one that was still syncing when your run began."""
            def do():
                rec = None
                for m in mesh.messages_for(chat):
                    for f in m.files or []:
                        if f.get("id") == file_id:
                            rec = f
                if rec is None:
                    return "no such file in this chat — list_files shows ids"
                raw = mesh.tx.get_blob(f"chats/{chat}/files/{file_id}")
                data = mesh.sealer.open_blob(chat, file_id, raw) \
                    if raw is not None else None
                if data is None or (rec.get("bytes") is not None
                                    and len(data) != rec["bytes"]):
                    return ("that file hasn't finished syncing to this "
                            "machine yet — try again in a bit")
                inbox = self.workspace / "inbox"
                inbox.mkdir(parents=True, exist_ok=True)
                name = Path(str(rec.get("name") or file_id)).name  # no paths
                (inbox / name).write_bytes(data)
                return f"saved to inbox/{name}"
            return guarded(do)

        @mcp.tool(structured_output=False)
        def read_status(username: str) -> str:
            """Check a member's availability + presence before messaging them —
            e.g. whether they're DND/busy or offline. Returns only what that
            member shares with you (their privacy rules gate every field); an
            empty result means they share nothing with you."""
            def do():
                name = (username or "").strip().lstrip("@").lower()
                if mesh.directory.get(name) is None:
                    return f"no such member @{name}"
                prof = mesh.privacy.visible_profile(name, viewer=mesh.user)
                pres = mesh.presence.visible_presence(name, viewer=mesh.user)
                out = {}
                st = prof.get("status")
                if isinstance(st, dict) and (st.get("state") or st.get("text")):
                    out["status"] = st.get("state") or "available"
                    if st.get("text"):
                        out["status_text"] = st["text"]
                if pres.get("online") is not None:
                    out["online"] = pres["online"]
                if pres.get("last_seen"):
                    out["last_seen"] = pres["last_seen"]
                return out or f"@{name} shares no status with you"
            return guarded(do)

        @mcp.tool(structured_output=False)
        def set_status(state: str, working_on: str = "") -> str:
            """Set YOUR OWN availability and what you're working on — members
            see it (per your privacy rules) and other agents check it before
            disturbing you. States: available / busy / dnd / away. Set it when
            you start something long ("busy", "indexing the repo") and back to
            "available" when you finish. Your responsible member can also set
            it; the most recent update wins."""
            return guarded(lambda: (
                mesh.set_status(state, working_on), "status updated")[1])

        @mcp.tool(structured_output=False)
        def set_about(about: str) -> str:
            """Set YOUR OWN About line — what you do or know, shown on your
            profile (per your privacy rules). Keep it accurate when your role
            changes. Your responsible member can also set it; the most recent
            update wins."""
            return guarded(lambda: (mesh.set_about(about), "about updated")[1])

        @mcp.tool(structured_output=False)
        def read_permissions(username: str = "") -> str:
            """Read permissions. With no argument: YOUR OWN rules as set by
            your responsible member — your privacy matrix (who sees your
            profile/receipts) and your outbound rules (who you may message /
            add to groups). With a username: that member's PUBLIC gates (who
            may message them / add them to groups — public by design so you
            can check before reaching out); their other privacy settings stay
            hidden."""
            def do():
                name = (username or "").strip().lstrip("@").lower() or mesh.user
                acc = mesh.directory.get(name)
                if acc is None:
                    return f"no such member @{name}"
                if name == mesh.user:
                    rules = acc.rules()
                    return {
                        "user": name,
                        "privacy": {k: getattr(v, "value", v)
                                    for k, v in acc.privacy.__dict__.items()},
                        "outbound": {"may_message": rules.messaging.value,
                                     "may_add_to_group": rules.add_to_group.value},
                        "set_by": "your responsible member",
                    }
                return {
                    "user": name,
                    **mesh.privacy.public_gates(name),
                    "note": "messaging/add_to_group are public by design; "
                            "other privacy settings are hidden",
                }
            return guarded(do)

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

        def mine(message_id: str) -> bool:
            """Your OWN, still-live message in this chat — the only kind you
            may edit or delete for everyone (author-only, like a human)."""
            return any(
                m.id == message_id and m.from_ == mesh.user
                and not m.deleted and m.kind is MsgKind.MESSAGE
                for m in mesh.messages_for(chat))

        @mcp.tool(structured_output=False)
        def edit_message(message_id: str, new_body: str) -> str:
            """Edit one of YOUR OWN messages in this chat (the new text
            replaces the old for everyone; edited messages are marked edited)."""
            if not mine(message_id):
                return "you can only edit your own messages in this chat"
            return guarded(
                lambda: (mesh.edit(chat, message_id, new_body), "edited")[1])

        @mcp.tool(structured_output=False)
        def delete_message(message_id: str) -> str:
            """Delete one of YOUR OWN messages for everyone in this chat
            (delete-for-everyone; it shows as removed, like a human's)."""
            if not mine(message_id):
                return "you can only delete your own messages in this chat"
            return guarded(
                lambda: (mesh.redact(chat, [message_id]), "deleted")[1])

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

        # ------------------------- chat-level member powers (V53, parity b)
        @mcp.tool(structured_output=False)
        def message_info(message_id: str) -> str:
            """Delivery + read receipts for one of YOUR OWN messages in this
            chat — who received it and who has read it. Members' receipt
            privacy applies (someone who hides read receipts shows only
            Delivered), exactly as a human sees ticks."""
            if not mine(message_id):
                return "receipts are for your own messages — ids are in the transcript"
            return guarded(lambda: mesh.message_info(chat, message_id))

        @mcp.tool(structured_output=False)
        def mute_chat(duration: str = "8h") -> str:
            """Quiet YOUR OWN notification lane for this chat: '8h', '1w',
            'forever', or 'off' to unmute. Only your own pings are affected
            (e.g. the CLI watcher) — whether you RUN and reply here is your
            responsible member's reply-rule setting, never this."""
            def do():
                d = str(duration or "").lower().strip()
                if d in ("off", "unmute"):
                    mesh.set_chat_flag(chat, "mute", False)
                    return "unmuted"
                if d == "forever":
                    mesh.set_chat_flag(chat, "mute", True)
                    return "muted until you unmute"
                hours = {"8h": 8.0, "1w": 168.0}.get(d)
                if hours is None:
                    return "duration must be one of 8h / 1w / forever / off"
                mesh.set_chat_flag(chat, "mute",
                                   time.time_ns() + int(hours * 3600 * 1e9))
                return f"muted for {d}"
            return guarded(do)

        @mcp.tool(structured_output=False)
        def archive_chat(archived: bool = True) -> str:
            """Archive (or unarchive) this chat in YOUR OWN chat list —
            nobody else sees your archive, and messages keep arriving."""
            return guarded(lambda: (
                mesh.set_chat_flag(chat, "archived", bool(archived)),
                "archived (your list only)" if archived else "unarchived")[1])

        @mcp.tool(structured_output=False)
        def add_member(username: str) -> str:
            """Add a member to THIS group, under the same rules as any
            member: the group's permissions decide who may add, the person's
            own privacy gate applies, and your responsible member's outbound
            rules bind you. An added agent brings its responsible member."""
            return guarded(lambda: (
                mesh.add_members(
                    chat, [str(username or "").strip().lstrip("@").lower()]),
                "added")[1])

        @mcp.tool(structured_output=False)
        def rename_chat(name: str) -> str:
            """Rename THIS group — allowed only when the group's settings
            permission lets regular members edit (agents are never admins)."""
            return guarded(lambda: (mesh.rename(chat, name), "renamed")[1])

        @mcp.tool(structured_output=False)
        def set_description(text: str) -> str:
            """Set THIS group's description (same permission rule as
            renaming)."""
            return guarded(lambda: (
                mesh.set_description(chat, text), "description updated")[1])

        def _owner_approved(tool: str, detail: str) -> str | None:
            """The owner-confirm gate the irreversible chat tools share —
            None = approved, otherwise the refusal text to return."""
            verdict, text = self.broker.ask(
                chat_id=self.chat_id, kind="permission", tool=tool,
                detail=detail, timeout_s=self.ask_timeout_s)
            if verdict in ("allow", "always"):
                return None
            if verdict == "deny":
                return ("your responsible member declined"
                        + (f": {text}" if text else ""))
            return ("your responsible member did not approve in time — "
                    "leave things as they are")

        @mcp.tool(structured_output=False)
        def leave_chat(reason: str = "") -> str:
            """Leave THIS group. Your responsible member is asked to approve
            first (they put you in your rooms). The leave happens AFTER your
            final reply posts, so say your goodbye in it."""
            def do():
                refusal = _owner_approved(
                    "leave_chat",
                    " ".join((reason or "leave this group").split())[:300])
                if refusal:
                    return refusal
                self.leave_requested = True
                return ("approved — you will leave right after this reply "
                        "posts")
            return guarded(do)

        @mcp.tool(structured_output=False)
        def clear_chat(keep_starred: bool = True) -> str:
            """Clear YOUR OWN view of this chat's history (members keep
            theirs; starred messages survive by default). Irreversible for
            you, so your responsible member is asked to approve first."""
            def do():
                refusal = _owner_approved(
                    "clear_chat", "clear its own view of this chat's history")
                if refusal:
                    return refusal
                mesh.clear_chat(chat, keep_starred=bool(keep_starred))
                return "cleared — your view of this chat starts fresh"
            return guarded(do)

        @mcp.tool(structured_output=False)
        def schedule_timer(minutes: float = 0, note: str = "",
                           at: str = "", repeat: str = "") -> str:
            """Wake yourself up in this chat later — a human 'remembers a
            task'; this is yours (V55). Give ``minutes`` (relative) OR
            ``at`` (absolute time: 'HH:MM' = its next occurrence,
            'YYYY-MM-DD HH:MM', or ISO). An 'HH:MM'/naive time is read in
            YOUR MACHINE's timezone — which may differ from the person
            you're helping (V74), so the confirmation states the exact
            local time it will fire and you should relay that if a member
            asked for a wall-clock time. ``repeat`` (V88) makes it
            RECURRING: 'daily', 'weekly:mon,wed', or 'monthly:15' — each
            firing re-arms the next occurrence at the same wall-clock
            time; your member can dismiss the series from the chat's chip
            (you'll be told). Write ``note`` as a FULL brief for
            your future self — it starts fresh and sees only this note plus
            the chat, so include what to do, for whom, and what done looks
            like. Every timer is visible to your responsible member."""
            from .timers import parse_at, parse_repeat, repeat_label
            from .timers import when_local as _when

            if len(self.timers) >= MAX_TIMERS_PER_RUN:
                return "timer limit for this run reached"
            # keep the brief's line structure; drop blank runs; cap
            text = "\n".join(
                " ".join(ln.split())
                for ln in str(note or "").splitlines() if ln.strip())[:2000]
            if not text:
                return ("write the note — your future run starts from it "
                        "(what to do, for whom, what done looks like)")
            rep = parse_repeat(repeat)
            if str(repeat or "").strip() and rep is None:
                return ("couldn't read that recurrence — use 'daily', "
                        "'weekly:mon,wed', or 'monthly:15'")
            suffix = f" ({repeat_label(rep)})" if rep else ""
            if str(at or "").strip():
                at_ns = parse_at(at)
                if at_ns is None:
                    return ("couldn't read that time — use 'HH:MM', "
                            "'YYYY-MM-DD HH:MM', or ISO")
                if at_ns <= time.time_ns() + int(30 * 1e9):
                    return "that time is already past — pick a future one"
                self.timers.append({"at_ns": at_ns, "note": text,
                                    **({"repeat": rep} if rep else {})})
                return f"scheduled: a wake-up at {_when(at_ns)}{suffix}"
            try:
                mins = float(minutes)
            except (TypeError, ValueError):
                return "give minutes (a number) or at (a time)"
            if mins <= 0:
                return "give minutes (a number) or at (a time)"
            in_s = max(30.0, mins * 60.0)
            self.timers.append({"in_s": in_s, "note": text,
                                **({"repeat": rep} if rep else {})})
            fires = _when(time.time_ns() + int(in_s * 1e9))
            return (f"scheduled: a wake-up in {in_s / 60:.0f} min "
                    f"(at {fires}){suffix}")

        if self.timer_svc is not None:
            timer_svc = self.timer_svc

            @mcp.tool(structured_output=False)
            def cancel_timer(timer_id: str = "") -> str:
                """Cancel one of your pending wake-ups for THIS chat — the
                ids are in your context's wake-up list. A wake-up scheduled
                earlier in this same run has no id yet and can't be
                cancelled here."""
                from .timers import when_local

                tid = str(timer_id or "").strip()
                if not tid:
                    return ("give the timer id — your context lists this "
                            "chat's wake-ups as (id t-...)")
                pending = {t.get("id"): t for t in timer_svc.snapshot()}
                t = pending.get(tid)
                if t is None:
                    return f"no pending wake-up with id {tid}"
                if t.get("chat_id") != self.chat_id:
                    # the context only ever shows THIS chat's ids; an id from
                    # elsewhere stays that chat's business
                    return ("that wake-up belongs to another chat — cancel "
                            "it from there")
                timer_svc.pop(tid)
                note = " ".join(str(t.get("note") or "").split())[:80]
                try:
                    fires = when_local(int(t.get("at_ns", 0)))
                except (ValueError, OSError, OverflowError):
                    fires = "unknown"
                return f"cancelled: the wake-up at {fires} ({note})"

        @mcp.tool(structured_output=False)
        def peer_diagnose(agent: str, command: str = "status") -> str:
            """Reach ANOTHER agent's harness. Diagnose (read-only): ping,
            status, run_feed. Repair (mutations, only if that agent's member
            allows it): pause, resume, clear_queue, clear_timers. The target's
            responsible member must permit the session, so this can take a
            moment or come back pending."""
            from .peer import PEER_COMMANDS, PeerService

            command = str(command or "status").lower()
            if command not in PEER_COMMANDS:
                return f"unknown command — choose one of {', '.join(PEER_COMMANDS)}"
            svc = PeerService(mesh)
            try:
                rid = svc.request(agent, command)
            except Exception as e:  # noqa: BLE001
                return f"could not reach @{agent}: {e}"
            deadline = time.time() + min(self.ask_timeout_s + 30, 200)
            while time.time() < deadline:
                resp = svc.read_response(agent, rid)
                if resp:
                    p = resp.get("payload") or {}
                    return (json.dumps(p.get("result") or {}) if p.get("ok")
                            else f"@{agent} declined or could not answer: "
                                 f"{p.get('error')}")
                time.sleep(2.0)
            return (f"still waiting on @{agent}'s responsible member — "
                    f"try again later")

    # ---------------------------------------------------- memory tools (R20)
    def _memory_tools(self, mcp) -> None:
        """remember/recall over the agent's local vector store. Chat scope
        stays inside this chat's collection; the GLOBAL scope follows the
        owner's policy (default: only in a DM — a group can't quietly write
        into the agent's cross-chat brain)."""

        def global_ok() -> str | None:
            if self.global_memory == "off":
                return "global memory is turned off by your responsible member"
            if self.global_memory == "dm" and self.chat_kind != "dm":
                return ("global memory is only available in a direct chat — "
                        "use scope 'chat' here")
            return None

        @mcp.tool(structured_output=False)
        def remember(text: str, scope: str = "chat") -> str:
            """Save one durable memory. scope 'chat' = this chat only;
            'global' = across your chats (policy-gated)."""
            scope = "global" if str(scope).lower() == "global" else "chat"
            if scope == "global":
                refusal = global_ok()
                if refusal:
                    return refusal
            try:
                if not self.memory.available():
                    return "memory is not available on this machine"
                self.memory.remember(scope=scope, chat_id=self.chat_id,
                                     text=text, by=self.broker.agent)
                return f"remembered ({scope})"
            except Exception as e:  # noqa: BLE001 — a refusal, not a crash
                return f"could not remember: {e}"

        @mcp.tool(structured_output=False)
        def recall(query: str, scope: str = "chat", limit: int = 5) -> str:
            """Search your memories. scope 'chat' = this chat's memories;
            'global' = your cross-chat memories (policy-gated)."""
            scope = "global" if str(scope).lower() == "global" else "chat"
            if scope == "global":
                refusal = global_ok()
                if refusal:
                    return refusal
            try:
                if not self.memory.available():
                    return "memory is not available on this machine"
                hits = self.memory.recall(scope=scope, chat_id=self.chat_id,
                                          query=query, limit=limit)
            except Exception as e:  # noqa: BLE001
                return f"could not recall: {e}"
            if not hits:
                return "nothing relevant remembered yet"
            return "\n".join(
                f"- {h['text']} (relevance {h['score']}, id {h['id']})"
                for h in hits)

        @mcp.tool(structured_output=False)
        def forget(query: str = "", memory_id: str = "",
                   scope: str = "chat") -> str:
            """Delete one saved memory: pass the id from recall (exact), or a
            query (deletes the single closest match — only when it's a
            confident match). scope as in remember/recall."""
            scope = "global" if str(scope).lower() == "global" else "chat"
            if scope == "global":
                refusal = global_ok()
                if refusal:
                    return refusal
            try:
                if not self.memory.available():
                    return "memory is not available on this machine"
                removed = self.memory.forget(
                    scope=scope, chat_id=self.chat_id,
                    query=query, memory_id=memory_id)
            except Exception as e:  # noqa: BLE001 — a refusal, not a crash
                return f"could not forget: {e}"
            if not removed:
                return ("no confidently matching memory — use recall to find "
                        "the exact id, then forget with memory_id")
            return "forgot: " + "; ".join(r["text"] for r in removed)

    def __exit__(self, *exc) -> None:
        # V85/V109: the run is over (posted, stopped, or crashed) — take its
        # pending asks with it, so the owner's popup dies with the run and
        # any thread still blocked in broker.ask() returns promptly.
        try:
            self.broker.withdraw(self.chat_id)
        except Exception:  # noqa: BLE001 — teardown never raises
            pass
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._server = None
        self._thread = None
        self._token = ""

    # ------------------------------------------------------------- config
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def auth_headers(self) -> dict[str, str]:
        """Headers for protocol tests and provider MCP configuration."""
        return {"Authorization": f"Bearer {self._token}"}

    def mcp_config(self) -> str:
        """The authenticated inline --mcp-config an inner CLI consumes."""
        return json.dumps(
            {"mcpServers": {"ab": {
                "type": "http", "url": self.url,
                "headers": self.auth_headers,
            }}})
