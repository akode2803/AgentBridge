"""The subprocess adapter — ONE engine drives every CLI family; the family's
particulars are preset data (registry.py). Successor to v1's run_agent /
CMD_TEMPLATES, upgraded:

- argv LISTS, never a shell string (v1 quoted prompts into `shell=True`);
- streamed stdout with a watchdog kill at the owner-set timeout;
- live activity lines flow to the run feed via ``on_step`` as they happen;
- a usage error (a CLI update rejecting flags) retries ONCE with the
  preset's minimal argv — safety args and the tool blocklist are never
  part of what gets dropped (v1 rule, kept);
- inbound attachments are unsealed into the run's workdir (headless CLIs
  can only read inside it), size-verified; files the agent leaves in the
  outbox ride back on the Reply.

Every word — the prompt, the context headers, the feed lines — comes from
the R17 prompt manager (``..prompt``); this module only extracts FACTS from
the stream (``extract_step``) and runs the process.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from ...core.config import DEFAULT_HOME
from ...core.timekit import utcnow_iso
from ..bridge import BridgeServer
from ..broker import PermissionBroker
from ..conversation import Delivery
from ..docs import ToolDocs
from ..memory import MemoryStore
from ..prompt import PromptManager, PromptPack, TRANSCRIPT_TAIL
from ..responder import OnStep, Reply, RunStopped
from ..retrieval import HistoryIndex, plan_query
from ..settings import HarnessSettings
from .registry import Invocation, ModelRegistry, effective_gates

__all__ = ["CliResponder", "extract_step", "reply_from_output",
           "stream_errors"]

STAGE_TAIL = 30          # messages whose attachments get staged (v1 value)
STDERR_SNIP = 1200


def extract_step(obj: dict, fmt: str) -> tuple[str, str, str] | None:
    """The FACT in one streamed event: ``(kind, name, detail)`` with kind in
    init | result | tool | text — or None. Wording is the prompt pack's job
    (``PromptPack.step_line``)."""
    if fmt == "claude-stream":
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            return ("init", "", "")
        if t == "assistant":
            for c in (obj.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    detail = (inp.get("query") or inp.get("command")
                              or inp.get("file_path") or inp.get("description")
                              or "")
                    # generous cap: step_line basenames paths AFTER this, so
                    # a long path must not be cut mid-directory here
                    return ("tool", str(c.get("name", "tool")),
                            " ".join(str(detail).split())[:400])
                if c.get("type") == "text":
                    txt = " ".join((c.get("text") or "").split())[:90]
                    if txt:
                        return ("text", "", txt)
        if t == "result":
            return ("result", "", "")
        return None
    if fmt == "codex-jsonl":
        item = obj.get("item") or {}
        itype = item.get("type") or item.get("item_type") or ""
        if obj.get("type") == "item.completed" and itype:
            if itype in ("agent_message", "assistant_message"):
                return ("result", "", "")
            detail = " ".join(str(item.get("text") or item.get("command")
                                  or "").split())[:90]
            return ("tool", str(itype), detail)
        return None
    return None


def stream_errors(lines: list[str], fmt: str) -> str:
    """CC's own explicit failure reason out of a finished stream — an
    is_error result event carries ``errors`` ("Reached maximum number of
    turns (60)") while stderr is often EMPTY, so the failure path used to
    raise an opaque blank (V86 probe evidence, claude 2.1.202)."""
    if fmt != "claude-stream":
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            errs = [str(e) for e in obj.get("errors") or [] if e]
            if errs:
                return "; ".join(errs)[:300]
            sub = str(obj.get("subtype") or "")
            if obj.get("is_error") and sub:
                return sub.removeprefix("error_").replace("_", " ")
    return ""


def reply_from_output(lines: list[str], fmt: str) -> str:
    """The final reply text out of a finished run's stdout."""
    if fmt == "text":
        return "\n".join(lines).strip()
    result = ""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if fmt == "claude-stream":
            if obj.get("type") == "result" and obj.get("result"):
                result = str(obj["result"])
        elif fmt == "codex-jsonl":
            item = obj.get("item") or {}
            itype = item.get("type") or item.get("item_type") or ""
            if itype in ("agent_message", "assistant_message") and item.get("text"):
                result = str(item["text"])
    return result.strip()


TMP_MAX_AGE_S = 7 * 86400.0   # V97: scratch older than a week is gone


def _prune_tmp(workdir: Path, max_age_s: float = TMP_MAX_AGE_S) -> int:
    """Best-effort janitor for the workspace's tmp/ scratch area (V97):
    files untouched for a week vanish, then emptied stale dirs. Only tmp/
    — everything else in the workspace is the agent's to keep."""
    tmp = workdir / "tmp"
    if not tmp.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    pruned = 0
    # deepest first, so a dir emptied by file pruning goes in the same pass;
    # empty dirs go regardless of age (deleting a child bumps the parent's
    # mtime on Windows, and an empty scratch dir holds nothing worth keeping)
    for p in sorted(tmp.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_file():
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    pruned += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:  # a locked file just waits for the next run
            continue
    return pruned


class CliResponder:
    """Resolve (owner config, audience) -> one CLI run -> a Reply."""

    def __init__(self, registry: ModelRegistry, mesh, home: Path | None = None,
                 timers=None) -> None:
        self.registry = registry
        self.mesh = mesh
        self.agent = mesh.user
        self.home = Path(home) if home else DEFAULT_HOME
        self.timer_svc = timers  # V87: the runner's TimerService (or None)
        self.prompts = PromptManager(self.home)
        self.docs = ToolDocs.load(self.home)   # R43: manual + popup phrases
        self.broker = PermissionBroker(mesh.tx, self.agent, docs=self.docs)
        # one store per agent process (qdrant local mode is single-process
        # per path); backends load lazily on the first remember/recall
        self.memory = MemoryStore(self.home / "harness" / self.agent / "memory")
        # the history index (R21) shares the store's client + embedder; the
        # per-chat high-water mark lives in the agent's SQLite store
        self.history = HistoryIndex(self.memory, getattr(mesh, "store", None))
        self._minimal: set[str] = set()  # preset ids that needed the fallback

    # ------------------------------------------------------------- the run
    def respond(self, delivery: Delivery, on_step: OnStep | None = None) -> Reply:
        acc = self.mesh.directory.get(self.agent)
        settings = HarnessSettings.from_account(acc)
        category = self._category(delivery, acc)
        inv = self.registry.resolve(settings, category,
                                    delivery.chat_id)  # raises with a reason
        pack = self.prompts.for_agent(acc)

        # per-chat context ceiling (Q30): the owner caps how many DAYS of
        # history a run may see — the ceiling applies to the verbatim tail
        # here and to retrieval below; 0 = auto (no ceiling)
        days = settings.context_days_for(delivery.chat_id)
        cutoff_ns = time.time_ns() - days * 86_400 * 10**9 if days else 0
        if cutoff_ns:
            delivery.transcript = [
                m for m in delivery.transcript if m.ns >= cutoff_ns]

        # per-chat WORKSPACE (R18): the agent's own desk for this chat —
        # context, inbox, outbox (R20 adds memory) live here, runs cwd here
        workdir = (self.home / "harness" / self.agent / "workspaces"
                   / delivery.chat_id)
        outbox = workdir / "outbox"
        # V97: tmp/ is the declared SCRATCH area — the prompt sends
        # intermediates here, tidy_workspace empties it on demand, and
        # week-old leftovers are pruned so workspaces never grow forever
        for d in (workdir, outbox, workdir / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        _prune_tmp(workdir)
        for stale in outbox.iterdir():  # a fresh run owns an empty outbox
            if stale.is_file():
                stale.unlink(missing_ok=True)

        self._retrieve(delivery, cutoff_ns)  # long chats stop forgetting (R21)
        staged = self._stage_inbox(delivery, workdir)
        context_file = workdir / "context.md"
        context_file.write_text(pack.context_text(delivery, staged),
                                encoding="utf-8", newline="\n")
        notes = workdir / "MEMORY.md"        # the workspace note tier (R20)
        if not notes.exists():
            notes.write_text("# Notes for this chat\n\nYours to keep — "
                             "edit freely; it stays in this workspace.\n",
                             encoding="utf-8", newline="\n")
        reply_file = workdir / "reply.md"
        reply_file.unlink(missing_ok=True)

        steps: list[dict] = []

        def step(line: str) -> None:
            steps.append({"text": line[:200], "ts": utcnow_iso()})
            if on_step:
                on_step(line)

        timers: list[dict] = []          # the bridge's schedule_timer fills it
        # H2/R43: the owner's aux flags shape the run's gates — auto_allow
        # may empty (reads ask too) and web tools may move from the hard
        # blocklist into the ask gate (never without the gate; see
        # effective_gates). Both argv builds below use THIS blocklist.
        auto_allow, blocklist = effective_gates(inv.preset, settings)
        with contextlib.ExitStack() as stack:
            mcp_config = ""
            env = None
            bridge = None
            if inv.preset.permission_args:
                bridge = stack.enter_context(BridgeServer(
                    self.broker, chat_id=delivery.chat_id,
                    workspace=workdir, auto_allow=auto_allow,
                    approvals=settings.approvals,
                    ask_timeout_s=settings.ask_timeout_s,
                    deny_roots=self._deny_roots(),
                    mesh=self.mesh, timers_out=timers,
                    memory=self.memory, chat_kind=delivery.chat_kind,
                    # H6/R41: the per-chat override resolves here, so the
                    # bridge's memory gate sees the effective policy
                    global_memory=settings.global_memory_for(delivery.chat_id),
                    docs=self.docs, timer_svc=self.timer_svc,
                ))
                mcp_config = bridge.mcp_config()
                # the inner CLI must out-wait the owner-answer window
                env = dict(os.environ)
                env["MCP_TOOL_TIMEOUT"] = str(
                    int((settings.ask_timeout_s + 60) * 1000))
            if not mcp_config:
                # no live ask gate on this run — the web relax never applies
                blocklist = list(inv.preset.blocklist)
            cap = int(getattr(self.mesh.tx, "max_upload_bytes", 0) or 0)
            file_limit = (f"{max(1, cap // (1024 * 1024))} MB per file"
                          if cap else "the configured per-file limit")
            prompt = pack.prompt(delivery, acc, context_file=context_file,
                                 outbox=outbox, bridge=bool(mcp_config),
                                 file_limit=file_limit)
            argv = inv.preset.build_argv(
                prompt=prompt, workdir=str(workdir),
                reply_file=str(reply_file), model=inv.model,
                effort=inv.effort, minimal=inv.preset.id in self._minimal,
                mcp_config=mcp_config, blocklist=blocklist,
            )
            rc, lines, err = self._run(argv, workdir, settings.timeout_s,
                                       inv, pack, step, env=env,
                                       chat_id=delivery.chat_id)
            if self._usage_error(rc, err) and inv.preset.id not in self._minimal:
                # a CLI update rejected our flags — drop conveniences, keep
                # safety args AND the permission plumbing
                step("Flags rejected — retrying with the minimal set")
                self._minimal.add(inv.preset.id)
                argv = inv.preset.build_argv(
                    prompt=prompt, workdir=str(workdir),
                    reply_file=str(reply_file), model=inv.model,
                    effort=inv.effort, minimal=True, mcp_config=mcp_config,
                    blocklist=blocklist,
                )
                rc, lines, err = self._run(argv, workdir, settings.timeout_s,
                                           inv, pack, step, env=env,
                                           chat_id=delivery.chat_id)

        text = reply_from_output(lines, inv.preset.format)
        if not text and reply_file.is_file():
            # some CLIs (-o) accumulate ALL assistant text there — fallback
            # only, never the primary (v1: thinking leaked verbatim once)
            text = reply_file.read_text(encoding="utf-8-sig").strip()
        if rc != 0 or not text:
            # prefer stderr; else CC's own explicit reason from the stream
            # ("Reached maximum number of turns") — it was an opaque blank
            why = err or stream_errors(lines, inv.preset.format) \
                or "no reply text"
            raise RuntimeError(
                f"{inv.preset.id} run failed (rc={rc}): {why[:STDERR_SNIP]}")

        # everything the run left in the outbox rides the reply — except
        # empty files: a model poking at its workdir once shipped a 0-byte
        # placeholder.txt as an attachment (live @claude, 2026-07-13).
        # R18's workspace scoping owns the real fix.
        files = sorted(str(p) for p in outbox.iterdir()
                       if p.is_file() and p.stat().st_size)
        return Reply(body=text, steps=steps, files=files, timers=timers,
                     leave_chat=bool(bridge and bridge.leave_requested))

    def close(self) -> None:
        """Release process-held resources (the qdrant path lock above all)."""
        self.memory.close()

    def _retrieve(self, delivery: Delivery, cutoff_ns: int = 0) -> None:
        """Index anything new, then pull the older messages this trigger
        makes relevant into the delivery. Retrieval is garnish: any failure
        (no backend, no qdrant, a mid-index crash) leaves the run intact.
        ``cutoff_ns`` is the owner's per-chat context ceiling (Q30) — the
        index holds older history from previous runs, so recall must honor
        the window too."""
        try:
            if delivery.kind != "message" or not self.history.available():
                return
            self.history.ensure_indexed(delivery.chat_id, delivery.transcript)
            query = plan_query(delivery)
            if not query:
                return
            visible = {m.id for m in delivery.transcript[-TRANSCRIPT_TAIL:]}
            recalled = self.history.relevant(
                delivery.chat_id, query, exclude_ids=visible)
            if cutoff_ns:
                recalled = [m for m in recalled
                            if getattr(m, "ns", 0) >= cutoff_ns]
            delivery.recalled = recalled
        except Exception:  # noqa: BLE001 — never block a reply on retrieval
            delivery.recalled = []

    # ------------------------------------------------------------ plumbing
    def _deny_roots(self) -> list[Path]:
        """Paths no run may touch even with an owner's click: the harness
        home (keystore, caches, config) and the shared mesh folder — the
        workspace subtree is exempted by the broker's first rule. A cloud
        transport's root is a name, not a directory — nothing local to deny."""
        roots = [self.home]
        mesh_root = getattr(self.mesh.tx, "root", None)
        if mesh_root and Path(str(mesh_root)).is_dir():
            roots.append(Path(mesh_root))
        return roots

    def _category(self, delivery: Delivery, acc) -> str:
        owner = acc.agent.owner if (acc and acc.agent) else None
        if delivery.kind == "timer" or not delivery.triggers:
            return "owner"
        t = delivery.triggers[-1]
        return HarnessSettings.category(t.sender_kind, t.sender, owner)

    def _run(self, argv: list[str], workdir: Path, timeout_s: float,
             inv: Invocation, pack: PromptPack, step,
             env: dict | None = None,
             chat_id: str = "") -> tuple[int | None, list[str], str]:
        kwargs: dict = {}
        if os.name == "nt":  # no console flash under pythonw (v1 lesson)
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        if env is not None:
            kwargs["env"] = env
        proc = subprocess.Popen(
            argv, cwd=str(workdir), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", **kwargs,
        )
        timed_out = threading.Event()
        watchdog = threading.Timer(
            timeout_s, lambda: (timed_out.set(), proc.kill()))
        watchdog.daemon = True
        watchdog.start()
        # owner stop button (R36): a stop doc on the transport kills this run.
        # Polled here (not the runner) because only the adapter owns the Popen;
        # best-effort — a transport blip just means the next poll catches it.
        # stop_req = the owner really asked; run_over = just releases the poller
        stop_req = threading.Event()
        run_over = threading.Event()
        run_start_ns = time.time_ns()
        stop_path = f"status/{self.mesh.user}_stop.json"

        def _poll_stop() -> None:
            while proc.poll() is None and not run_over.is_set() \
                    and not timed_out.is_set():
                try:
                    doc = self.mesh.tx.get_doc(stop_path)
                    if (isinstance(doc, dict)
                            and int(doc.get("ns", 0)) >= run_start_ns - int(30e9)
                            and (not doc.get("chat_id")
                                 or doc.get("chat_id") == chat_id)):
                        stop_req.set()
                        proc.kill()
                        with contextlib.suppress(Exception):
                            self.mesh.tx.delete_doc(stop_path)  # consumed
                        return
                except Exception:  # noqa: BLE001 — polling must never crash
                    pass
                run_over.wait(2.5)

        stopper = threading.Thread(target=_poll_stop, daemon=True)
        stopper.start()
        err_chunks: list[str] = []
        t = threading.Thread(
            target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
        t.start()
        lines: list[str] = []
        try:
            for line in proc.stdout:
                lines.append(line.rstrip("\n"))
                s = line.strip()
                if s.startswith("{"):
                    try:
                        fact = extract_step(json.loads(s), inv.preset.format)
                    except json.JSONDecodeError:
                        fact = None
                    note = pack.step_line(*fact) if fact else None
                    if note:
                        step(note)
            rc = proc.wait(timeout=60)
        finally:
            watchdog.cancel()
            run_over.set()  # release the poller's wait
        if stop_req.is_set():
            raise RunStopped("stopped by the responsible member")
        if timed_out.is_set():
            return None, lines, "timed out"
        t.join(timeout=10)
        return rc, lines, (err_chunks[0] if err_chunks else "")

    @staticmethod
    def _usage_error(rc, err: str) -> bool:
        low = (err or "").lower()
        return rc not in (0, None) and (
            "usage:" in low or "unknown option" in low
            or "unrecognized" in low or "unexpected argument" in low)

    def _stage_inbox(self, delivery: Delivery, workdir: Path) -> dict[str, str]:
        """Unseal recent attachments into the workdir (size-verified) so the
        CLI can actually read them; failures degrade to the bare name."""
        staged: dict[str, str] = {}
        inbox = workdir / "inbox"
        for m in delivery.transcript[-STAGE_TAIL:]:
            for f in m.files or []:
                name, blob_id = f.get("name"), f.get("id")
                if not name or not blob_id or name in staged:
                    continue
                try:
                    raw = self.mesh.tx.get_blob(
                        f"chats/{delivery.chat_id}/files/{blob_id}")
                    if raw is None:
                        continue
                    data = self.mesh.sealer.open_blob(
                        delivery.chat_id, blob_id, raw)
                    if data is None or (
                            f.get("bytes") is not None
                            and len(data) != f["bytes"]):
                        continue  # unopenable or still syncing
                    inbox.mkdir(exist_ok=True)
                    (inbox / name).write_bytes(data)
                    staged[name] = f"inbox/{name}"
                except Exception:  # noqa: BLE001 — a bad blob never kills a run
                    continue
        return staged
