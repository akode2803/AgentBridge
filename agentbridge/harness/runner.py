"""The agent runner — one process gives ONE agent its presence on the mesh.

Lifecycle: verify this machine really hosts the agent (its account names this
machine and the local keystore holds its identity) → start the Mesh facade
(outbox, presence heartbeat) + a sync thread → then loop: SCAN (the poll is
the source of truth; the transport watcher only shortens the wait) → enqueue
new triggers into the durable queue → DISPATCH claimed (chat, sender) groups
on a pool sized by the owner-set concurrency.

The runner is deliberately model-agnostic: it hands deliveries to an injected
``Responder`` (R16's registry provides real ones) and posts what comes back.
Without a responder it can only ``--dry-run`` — honest about R15's scope.

Stand-down: the global ``control.json`` pause (any member) and the agent's
``active`` flag (its owner's explicit switch) both hold scanning and
dispatch; cursors and the queue keep their place so resume answers the
backlog under the catch-up policy instead of dropping it.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..core.config import DEFAULT_HOME, load_app_config
from ..core.models import UserKind
from ..core.timekit import new_id, utcnow_iso
from ..mesh.sealer import E2EESealer
from ..mesh.service import Mesh
from .conversation import ConversationManager
from .feed import RunFeed, record_tasks, write_harness_doc
from .queue import WorkGroup, WorkItem, WorkQueue
from .responder import Reply, Responder, clean_reply
from .settings import HarnessSettings
from .timers import TimerService
from . import triggers

__all__ = ["AgentRunner", "SingleInstance", "supervise", "main",
           "EXIT_ALREADY_RUNNING"]

EXIT_ALREADY_RUNNING = 3
MAX_WORKERS = 8          # hard ceiling; the owner-set concurrency gates below
RATE_RETRY_S = 600.0     # capped chat: revisit in this many seconds
NOTICE = ("@{agent}'s harness could not produce a reply here "
          "({err}). Its responsible member can check the harness on "
          "{machine}.")


class AgentRunner:
    def __init__(
        self,
        root: Path | str,
        agent: str,
        *,
        home: Path | str | None = None,
        machine: str = "",
        encrypt: bool = True,
        responder: Responder | None = None,
        poll_s: float = 5.0,
    ) -> None:
        self.agent = agent
        self.home = Path(home) if home else DEFAULT_HOME
        self.machine = machine or platform.node() or "harness"
        self.responder = responder
        self.poll_s = poll_s
        self.mesh = Mesh(root, agent, self.machine, encrypt=encrypt,
                         home=self.home)
        self.queue = WorkQueue(self.mesh.store, agent)
        self.timers = TimerService(self.mesh.store)
        self.conversation = ConversationManager(self.mesh)
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                        thread_name_prefix="ab-harness")
        self._inflight: dict[tuple[str, str], Future] = {}
        # RLock: a future that completes instantly runs its done-callback on
        # the submitting thread, which still holds this lock
        self._inflight_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._started_ns = time.time_ns()
        self._last_doc: tuple | None = None

    # ------------------------------------------------------------ identity
    def verify_identity(self) -> list[str]:
        """The reasons this machine may NOT run the agent (empty = go).
        All account/key mutations are owner-side (D19) — the runner only
        checks; ``accounts.adopt_agent`` is the owner's fix for a mismatch."""
        problems = []
        acc = self.mesh.directory.get(self.agent)
        if acc is None or acc.kind is not UserKind.AGENT:
            return [f"@{self.agent} is not an agent on this mesh"]
        hosted_on = acc.agent.machine if acc.agent else ""
        if hosted_on != self.machine:
            problems.append(
                f"@{self.agent} is hosted on {hosted_on!r}, not this machine "
                f"({self.machine!r}) — its responsible member can adopt it "
                f"here (accounts.adopt_agent)")
        if isinstance(self.mesh.sealer, E2EESealer):
            if not acc.keys.sign_pub:
                problems.append(
                    f"@{self.agent} has no published identity keys — "
                    f"adopt_agent provisions them")
            elif self.mesh.keystore.load(self.agent) is None:
                problems.append(
                    f"this machine does not hold @{self.agent}'s keys — "
                    f"adopt_agent re-homes the agent here")
        return problems

    # ----------------------------------------------------------- stand-down
    def standing_down(self) -> bool:
        doc = self.mesh.tx.get_doc("control.json")
        if isinstance(doc, dict) and doc.get("paused"):
            return True
        acc = self.mesh.directory.get(self.agent)
        return acc is None or not acc.active

    def settings(self) -> HarnessSettings:
        return HarnessSettings.from_account(self.mesh.directory.get(self.agent))

    # ----------------------------------------------------------------- scan
    def scan_all(self, *, collect: list | None = None) -> int:
        """One truth pass: new triggers + due timers -> the queue. Returns
        how many items were enqueued. ``collect`` (dry-run) gathers what
        WOULD be enqueued without persisting anything."""
        settings = self.settings()
        owner = self.mesh.directory.owner_of(self.agent)
        added = 0
        for snap in self.mesh.membership.chats_for():
            try:
                added += self._scan_chat(snap, settings, owner, collect)
            except Exception:  # noqa: BLE001 — one chat never blocks the rest
                continue
        for t in self.timers.due():
            item = WorkItem(
                key=f"{t['chat_id']}|timer:{t['id']}",
                chat_id=t["chat_id"], kind="timer", msg_id=f"timer:{t['id']}",
                sender=self.agent, ns=int(t.get("at_ns", 0)),
                reason="timer", note=t.get("note", ""),
            )
            if collect is not None:
                collect.append(item)
            elif self.queue.offer(item):
                added += 1
        return added

    def _scan_chat(self, snap, settings: HarnessSettings, owner: str | None,
                   collect: list | None) -> int:
        chat_id = snap.id
        last_ns, last_edit = self.queue.scan_cursor(chat_id)
        msgs = self.mesh.messages_for(chat_id)
        senders = {m.from_ for m in msgs}
        kinds = {s: self.mesh.directory.kind(s) for s in senders}
        rule = settings.rule_for(chat_id)
        me = snap.members.get(self.agent)
        cands, max_ns, max_edit = triggers.extract(
            msgs, self.agent, rule, kinds,
            joined_ns=me.joined_ns if me else 0,
            after_ns=last_ns, after_edit_ns=last_edit,
        )
        # loop damping, ported from v1 but scoped to rule-all triggers only:
        # when the newest visible message is my own, a rule-all trigger from
        # before it is part of an exchange I already closed. Explicit asks
        # (tags / replies / rule-humans / edits) always stay answerable.
        tail_mine = bool(msgs) and msgs[-1].from_ == self.agent
        added = 0
        for c in cands:
            if self.queue.answered(chat_id, c.message.id, c.edit_ns):
                continue
            if c.reason == "rule-all" and tail_mine:
                if collect is None:
                    self.queue.record_skip(chat_id, c.message.id, c.edit_ns,
                                           "own-tail")
                continue
            # per-purpose routing (R16): an audience the owner turned off is
            # resolved here, before it can claim a slot or burn the rate cap
            kind = kinds.get(c.message.from_)
            category = HarnessSettings.category(
                kind.value if kind else "agent", c.message.from_, owner)
            if not settings.route(category).enabled:
                if collect is None:
                    self.queue.record_skip(chat_id, c.message.id, c.edit_ns,
                                           f"routing-off:{category}")
                continue
            skip = self._catchup_skip(c, settings)
            if skip:
                if collect is None:
                    self.queue.record_skip(chat_id, c.message.id, c.edit_ns, skip)
                continue
            item = WorkItem(
                key=f"{chat_id}|{c.key}", chat_id=chat_id, kind="message",
                msg_id=c.message.id, edit_ns=c.edit_ns, sender=c.message.from_,
                ns=c.trigger_ns, reason=c.reason,
            )
            if collect is not None:
                collect.append(item)
                added += 1
            elif self.queue.offer(item):
                added += 1
        if collect is None:
            self.queue.set_scan_cursor(chat_id, max_ns, max_edit)
        return added

    def _catchup_skip(self, cand, settings: HarnessSettings) -> str | None:
        """The graceful-catch-up policy: how a considerate colleague treats a
        backlog. Fresh triggers always fire; stale ones follow the owner-set
        policy instead of spamming N late replies."""
        age_s = max(0.0, (time.time_ns() - cand.trigger_ns) / 1e9)
        if settings.catchup == "all":
            return None
        if settings.catchup == "none" and cand.trigger_ns < self._started_ns \
                and age_s > 60:
            return "catch-up:none"
        if age_s > settings.catchup_window_h * 3600:
            return "catch-up:window"
        return None

    # ------------------------------------------------------------- dispatch
    def dispatch_fill(self) -> int:
        """Top the pool up to the owner-set concurrency. Returns how many
        groups were started."""
        if self.responder is None:
            return 0
        settings = self.settings()
        started = 0
        with self._inflight_lock:
            room = min(settings.concurrency, MAX_WORKERS) - len(self._inflight)
            if room <= 0:
                return 0
            groups = self.queue.claim_groups(
                limit=room, exclude=set(self._inflight))
            for g in groups:
                gkey = (g.chat_id, g.sender if g.kind == "message" else
                        g.items[0].key)
                fut = self._pool.submit(self._process_group, g, settings)
                self._inflight[gkey] = fut
                fut.add_done_callback(lambda _f, k=gkey: self._done(k))
                started += 1
        return started

    def _done(self, gkey) -> None:
        with self._inflight_lock:
            self._inflight.pop(gkey, None)
        self._wake.set()  # a finished run may unblock the next group

    def _process_group(self, group: WorkGroup, settings: HarnessSettings) -> None:
        chat_id = group.chat_id
        try:
            if self.standing_down():
                self.queue.release(group, retry_in_s=self.poll_s * 2)
                return
            transcript = self.mesh.messages_for(chat_id)
            if group.kind == "message":
                # second guard leg: my own visible reply already answers it
                # (covers a lost local ledger) — resolve those, keep the rest
                done = [it for it in group.items
                        if self.queue.answered_in_transcript(transcript, it.msg_id)]
                if done:
                    self.queue.finish(WorkGroup(chat_id, group.sender, done),
                                      "answered-in-transcript")
                group.items = [it for it in group.items if it not in done]
                if not group.items:
                    return
            # the reply slot is claimed ATOMICALLY before the run (parallel
            # groups can't both pass a cap of one); non-posting outcomes refund
            if not self.queue.rate_acquire(chat_id, settings.max_replies_per_hour):
                self.queue.release(group, retry_in_s=RATE_RETRY_S)
                return
            delivery = self.conversation.build(group, transcript, settings)
            if group.kind == "message" and not delivery.triggers:
                self.queue.rate_refund(chat_id)
                self.queue.finish(group, "gone")  # trigger deleted meanwhile
                return
            self.mesh.messaging.mark_read(chat_id)  # context read = read
            feed = RunFeed(self.mesh.tx, self.agent, chat_id)
            try:
                reply = self.responder.respond(delivery, on_step=feed.step)
            except Exception as e:  # noqa: BLE001 — a run dies, the loop lives
                self._run_failed(group, feed, settings, delivery, e)
                return
            self._deliver_reply(group, delivery, reply, feed)
        except Exception:  # noqa: BLE001 — never kill the pool thread
            self.queue.release(group, retry_in_s=self.poll_s * 4)

    def _deliver_reply(self, group: WorkGroup, delivery, reply: Reply,
                       feed: RunFeed) -> None:
        chat_id = group.chat_id
        body, no_reply = clean_reply(reply.body)
        timer_ids = self.timers.add_from_reply(chat_id, reply.timers)
        if group.kind == "timer":
            self.timers.pop(group.items[0].key.split("timer:", 1)[-1])
        if no_reply or not body:
            self.queue.rate_refund(chat_id)  # a silent run costs no slot
            self.queue.finish(group, "no_reply")
            feed.finish("done", "No reply needed")
            self.publish_status()
            return
        reply_to = None
        if group.kind == "message" and delivery.triggers:
            last = delivery.triggers[-1].message
            reply_to = {"id": last.id, "from": last.from_,
                        "body": (last.body or "")[:200]}
        posted = self.mesh.post(chat_id, body, reply_to=reply_to,
                                files=self._attach(chat_id, reply.files))
        self.queue.finish(group, posted.id)
        record_tasks(self.mesh.tx, chat_id, posted.id, self.agent,
                     reply.steps or feed.tasks)
        note = "Reply posted" + (f" (+{len(timer_ids)} timer(s))"
                                 if timer_ids else "")
        feed.finish("done", note)
        self.publish_status()  # new timers become owner-visible immediately

    def _run_failed(self, group: WorkGroup, feed: RunFeed,
                    settings: HarnessSettings, delivery, err: Exception) -> None:
        feed.finish("error", f"Run failed: {type(err).__name__}")
        self.queue.finish(group, f"error:{type(err).__name__}")
        if group.kind == "timer":
            self.timers.pop(group.items[0].key.split("timer:", 1)[-1])
        if not settings.error_notices:
            self.queue.rate_refund(group.chat_id)  # nothing was posted
            return
        try:
            reply_to = None
            if delivery.triggers:
                last = delivery.triggers[-1].message
                reply_to = {"id": last.id, "from": last.from_,
                            "body": (last.body or "")[:200]}
            self.mesh.post(
                group.chat_id,
                NOTICE.format(agent=self.agent, err=type(err).__name__,
                              machine=self.machine),
                reply_to=reply_to,
            )
            # the acquired slot stays spent: notices count against the cap,
            # so a broken adapter can't flood a chat with error posts
        except Exception:  # noqa: BLE001 — the notice is best-effort
            pass

    def _attach(self, chat_id: str, paths: list[str]) -> list[dict]:
        """Local files a Reply shares -> sealed chat blobs -> files[] records
        (the harness-side mirror of the GUI's seal_attachments)."""
        import hashlib

        out = []
        for p in paths or []:
            try:
                raw = Path(p).read_bytes()
            except OSError:
                continue
            name = Path(p).name[:120]
            dot = name.rfind(".")
            blob_id = new_id("f") + (name[dot:][:12].lower() if dot > 0 else "")
            sealed = self.mesh.sealer.seal_blob(chat_id, blob_id, raw)
            self.mesh.tx.put_blob(f"chats/{chat_id}/files/{blob_id}", sealed)
            out.append({"id": blob_id, "name": name, "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest()})
        return out

    # ------------------------------------------------------------ lifecycle
    def publish_status(self) -> None:
        state = (tuple(map(str, self.queue.snapshot())),
                 tuple(map(str, self.timers.snapshot())),
                 self.standing_down())
        if state == self._last_doc:
            return
        self._last_doc = state
        write_harness_doc(self.mesh.tx, self.agent,
                          queue=self.queue.snapshot(),
                          timers=self.timers.snapshot(),
                          paused=self.standing_down())

    def tick(self) -> int:
        """One scan+dispatch pass (the run loop's body; tests call it too)."""
        if self.standing_down():
            self.publish_status()
            return 0
        added = self.scan_all()
        self.dispatch_fill()
        self.publish_status()
        return added

    def attach_cli_responder(self) -> None:
        """The default production responder: the R16 registry + CLI engine."""
        from .adapters import CliResponder, ModelRegistry

        self.responder = CliResponder(
            ModelRegistry.load(self.home), self.mesh, self.home)

    def run(self, *, once: bool = False) -> None:
        if self.responder is None and not once:
            raise SystemExit(
                "no responder configured — attach_cli_responder() or inject "
                "one; use --dry-run to inspect what would trigger")
        self.mesh.start()  # outbox flusher + presence heartbeat
        sync_thread = threading.Thread(
            target=self.mesh.sync.run,
            kwargs={"poll_s": self.poll_s,
                    "on_new": lambda n: self._wake.set()},
            daemon=True, name="ab-harness-sync",
        )
        sync_thread.start()
        try:
            if once:
                self.mesh.sync.sync_once()
                for _ in range(10):
                    self.tick()
                    if not self.drain(timeout=self.settings().timeout_s):
                        break
                return
            while not self._stop.is_set():
                self.tick()
                self._wake.wait(self.poll_s)
                self._wake.clear()
        finally:
            self.close()

    def drain(self, timeout: float = 30.0) -> int:
        """Wait for every in-flight run to finish; returns how many there were."""
        with self._inflight_lock:
            futures = list(self._inflight.values())
        for f in futures:
            try:
                f.result(timeout=timeout)
            except Exception:  # noqa: BLE001 — worker errors are handled inside
                pass
        return len(futures)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def close(self) -> None:
        self.stop()
        self.drain()
        self._pool.shutdown(wait=True)
        self.mesh.sync.stop()
        self.mesh.close()


# --------------------------------------------------------------- resilience
class SingleInstance:
    """Per-agent run lock (ported from v1): a second harness for the SAME
    agent on this machine exits fast instead of double-replying; the OS frees
    the lock the instant the process dies — no stale PID cleanup."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except OSError:
            return True  # can't lock (odd FS) — don't block the harness
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        self._fh = fh
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


def supervise(agent: str, argv: list[str]) -> None:
    """Keep an agent's harness alive with capped-backoff restarts (v1 model):
    rc 0 = clean stop, rc 3 = another harness owns this agent — stand aside."""
    child = [sys.executable, "-m", "agentbridge.harness",
             *[a for a in argv if a != "--supervise"]]
    print(f"[supervisor] @{agent}: keeping the harness up — Ctrl+C to stop")
    backoff = 2.0
    while True:
        started = time.time()
        try:
            rc = subprocess.call(child)
        except KeyboardInterrupt:
            return
        if rc == 0:
            return
        if rc == EXIT_ALREADY_RUNNING:
            print(f"[supervisor] @{agent}: already running — standing aside")
            return
        ran = time.time() - started
        backoff = 2.0 if ran > 60 else min(backoff * 2, 60.0)
        print(f"[supervisor] @{agent}: harness exited (rc={rc}) after "
              f"{ran:.0f}s — restarting in {backoff:.0f}s")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            return


def hosted_agents(root: Path, machine: str) -> list[str]:
    """Agents whose accounts name THIS machine as home (active or not — the
    active flag is a runtime hold, and a held agent still syncs)."""
    from ..mesh.directory import Directory
    from ..transport.folder import FolderTransport

    directory = Directory(FolderTransport(root))
    out = []
    for name in directory.names():
        acc = directory.get(name)
        if acc and acc.kind is UserKind.AGENT and acc.agent \
                and acc.agent.machine == machine:
            out.append(name)
    return out


def supervise_all(root: Path, machine: str, argv: list[str]) -> int:
    """One supervised runner per hosted agent (AgentHarness.pyw's engine).
    Each child holds its own single-instance lock; a second launcher's
    children simply stand aside (rc 3)."""
    agents = hosted_agents(root, machine)
    if not agents:
        print(f"no agents are hosted on this machine ({machine}) — "
              f"adopt or create one in Settings, then relaunch")
        return 0
    passthru = [a for a in argv if a not in ("--all",)]
    children = [
        subprocess.Popen([sys.executable, "-m", "agentbridge.harness",
                          name, "--supervise", *passthru])
        for name in agents
    ]
    print(f"[harness] supervising {len(children)} agent(s): "
          + ", ".join(f"@{a}" for a in agents))
    try:
        for c in children:
            c.wait()
    except KeyboardInterrupt:
        for c in children:
            c.terminate()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agentbridge-harness",
        description="AgentBridge agent harness (v2) — one process per agent")
    ap.add_argument("agent", nargs="?", default="",
                    help="agent account name (omit with --all)")
    ap.add_argument("--all", action="store_true",
                    help="supervise every agent hosted on this machine")
    ap.add_argument("--root", default="", help="mesh root (default: remembered)")
    ap.add_argument("--home", default="", help="local home (default: ~/.agentbridge)")
    ap.add_argument("--machine", default="", help="machine name override")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would trigger; never post")
    ap.add_argument("--supervise", action="store_true")
    args = ap.parse_args(argv)

    home = Path(args.home) if args.home else None
    cfg = load_app_config(home)
    root = args.root or cfg.get("mesh_root")
    if not root:
        ap.error("no --root given and none remembered in config.json")

    if args.all:
        machine = args.machine or platform.node() or "harness"
        return supervise_all(Path(root), machine,
                             [a for a in (argv or sys.argv[1:])])
    if not args.agent:
        ap.error("an agent name is required (or use --all)")
    if args.supervise:
        supervise(args.agent, [a for a in (argv or sys.argv[1:])])
        return 0

    lock = None
    if not args.dry_run:
        lock = SingleInstance((home or DEFAULT_HOME) / f"harness_{args.agent}.lock")
        if not lock.acquire():
            print(f"@{args.agent} is already running on this machine")
            return EXIT_ALREADY_RUNNING
    try:
        runner = AgentRunner(Path(root), args.agent, home=home,
                             machine=args.machine, poll_s=args.poll)
        problems = runner.verify_identity()
        if problems:
            for p in problems:
                print(f"cannot start: {p}")
            return 2
        if not args.dry_run:
            runner.attach_cli_responder()
        if args.dry_run:
            runner.mesh.sync.sync_once()
            would: list[WorkItem] = []
            runner.scan_all(collect=would)
            print(f"[dry-run] @{args.agent} at {utcnow_iso()}: "
                  f"{len(would)} trigger(s) would dispatch")
            for it in would:
                print(f"  {it.chat_id}: {it.kind} from @{it.sender} "
                      f"({it.reason})")
            runner.mesh.close()
            return 0
        runner.run(once=args.once)
        return 0
    finally:
        if lock:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
