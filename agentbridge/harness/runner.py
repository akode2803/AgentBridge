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
import contextlib
import os
import platform
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from .. import __version__
from ..core.config import DEFAULT_HOME, load_app_config
from ..core.models import ChatKind, UserKind
from ..mesh import authz
from ..core.timekit import new_id, utcnow_iso
from ..mesh.sealer import E2EESealer
from ..mesh.service import Mesh
from .conversation import ConversationManager
from .feed import RunFeed, record_tasks, write_harness_doc
from .peer import PeerService
from .perf import RunTimings
from .queue import WorkGroup, WorkItem, WorkQueue
from .responder import Reply, Responder, RunStopped, clean_reply
from .settings import HarnessSettings
from .timers import TimerService
from . import triggers

__all__ = ["AgentRunner", "SingleInstance", "supervise", "main",
           "EXIT_ALREADY_RUNNING"]

EXIT_ALREADY_RUNNING = 3
MAX_WORKERS = 8          # hard ceiling; the owner-set concurrency gates below
RATE_RETRY_S = 600.0     # capped chat: revisit in this many seconds
BLOB_GRACE_S = 600.0     # v1 value: a lost attachment must not wedge a chat
STOP_FRESH_S = 600.0     # a claim-time stop doc older than this is stale
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
                         home=self.home, app_version=__version__)
        self.queue = WorkQueue(self.mesh.store, agent)
        self.timers = TimerService(self.mesh.store)
        self.conversation = ConversationManager(self.mesh)
        # peer harness access (R22) + repair mutations (R22.5): the runner
        # injects the repair actions so the peer service can only touch this
        # harness's OWN runtime state (its hold, queue, timers) — nothing else
        self.peer = PeerService(self.mesh, repair_ops={
            "pause": lambda: self._peer_set_hold(True),
            "resume": lambda: self._peer_set_hold(False),
            "clear_queue": lambda: f"cleared {self.queue.clear_pending()} pending",
            "clear_timers": lambda: f"cancelled {self.timers.clear()} timer(s)",
        })
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
        self._blobs_ok: set[str] = set()  # sync-barrier verified blob ids

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
    HOLD_DOC = "harness/peer_hold"

    def _peer_set_hold(self, held: bool) -> str:
        """A peer-repair pause: a harness-LOCAL hold, distinct from the owner's
        active flag and the global control.json. Persisted so it survives a
        restart — a peer pauses a runaway agent and it STAYS paused until
        resumed (by the peer or the owner)."""
        self.mesh.store.cache_doc(self.HOLD_DOC, {"held": bool(held)})
        self._wake.set()
        return "harness held" if held else "harness resumed"

    def _peer_held(self) -> bool:
        doc = self.mesh.store.cached_doc(self.HOLD_DOC, default={})
        return bool(doc.get("held")) if isinstance(doc, dict) else False

    def standing_down(self) -> bool:
        doc = self.mesh.tx.get_doc("control.json")
        if isinstance(doc, dict) and doc.get("paused"):
            return True
        if self._peer_held():                # a peer-repair hold (R22.5)
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
        rule = settings.rule_for(chat_id, dm=snap.kind is ChatKind.DM)
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
        # response-time profile (R30): pickup = trigger posted -> claimed here
        timings = RunTimings(max((it.ns for it in group.items), default=0))
        slot = False  # a rate slot is held (the failure paths must refund it)
        try:
            if self.standing_down():
                self.queue.release(group, retry_in_s=self.poll_s * 2)
                return
            if self._owner_stop_requested(chat_id):
                # R55 (V35): a Stop pressed while nothing was running used to
                # evaporate — the in-run poller was the only consumer. Honor
                # it at claim time: the owner already refused this run.
                self.queue.finish(group, "stopped-by-owner")
                RunFeed(self.mesh.tx, self.agent, chat_id).finish(
                    "stopped", "Stopped by your member")
                self.publish_status()
                return
            transcript = self.mesh.messages_for(chat_id)
            if group.kind == "message":
                if not self._can_post(chat_id):
                    # R55 (V35): an agent that cannot post here must not burn
                    # a model run — resolve through the ledger (never re-fires)
                    # and say why in the runs list. The live loop: a group's
                    # send_messages flipped to admins-only while the agents
                    # stayed plain members; every run then died at post.
                    self.queue.finish(group, "skipped:cannot-post")
                    self._log_perf(timings, group, "cannot-post")
                    RunFeed(self.mesh.tx, self.agent, chat_id).finish(
                        "done", "Can't reply — sending is restricted in this chat")
                    self.publish_status()
                    return
                # second guard leg: my own visible reply already answers it
                # (covers a lost local ledger) — resolve those, keep the rest.
                # R54 (V30): an EDIT revision (edit_ns > 0) skips this leg —
                # a reply to the PRE-edit text must not swallow the fresh
                # attention; the ledger leg still keys msg_id@edit_ns, so
                # each revision fires at most once.
                done = [it for it in group.items
                        if not it.edit_ns
                        and self.queue.answered_in_transcript(transcript, it.msg_id)]
                if done:
                    self.queue.finish(WorkGroup(chat_id, group.sender, done),
                                      "answered-in-transcript")
                group.items = [it for it in group.items if it not in done]
                if not group.items:
                    return
            # R55 (V36): the v1 sync barrier, restored — a message line can
            # sync ahead of its attachment blob, and running then hands the
            # CLI a transcript advertising a file that is not on disk yet.
            # Defer (slot-free) while a recent blob is still syncing.
            waiting = self._blob_syncing(chat_id, transcript)
            if waiting:
                self.queue.release(group, retry_in_s=self.poll_s * 3)
                return
            # the reply slot is claimed ATOMICALLY before the run (parallel
            # groups can't both pass a cap of one); non-posting outcomes refund
            if not self.queue.rate_acquire(chat_id, settings.max_replies_per_hour):
                self.queue.release(group, retry_in_s=RATE_RETRY_S)
                return
            slot = True
            timings.start("context")
            delivery = self.conversation.build(group, transcript, settings)
            timings.stop()
            if group.kind == "message" and not delivery.triggers:
                self.queue.rate_refund(chat_id)
                self.queue.finish(group, "gone")  # trigger deleted meanwhile
                return
            self.mesh.messaging.mark_read(chat_id)  # context read = read
            feed = RunFeed(self.mesh.tx, self.agent, chat_id)
            timings.start("model")
            try:
                reply = self.responder.respond(delivery, on_step=feed.step)
            except RunStopped:
                # the owner pressed Stop (R36): a deliberate outcome, not a
                # failure — no error notice, the slot is refunded, and the
                # triggers are recorded handled so they never re-fire
                timings.stop()
                self._log_perf(timings, group, "stopped")
                self.queue.rate_refund(chat_id)
                self.queue.finish(group, "stopped-by-owner")
                if group.kind == "timer":
                    self.timers.pop(group.items[0].key.split("timer:", 1)[-1])
                feed.finish("stopped", "Stopped by your member")
                self.publish_status()
                return
            except Exception as e:  # noqa: BLE001 — a run dies, the loop lives
                timings.stop()
                self._log_perf(timings, group, f"error:{type(e).__name__}")
                self._run_failed(group, feed, settings, delivery, e)
                return
            timings.stop()
            try:
                self._deliver_reply(group, delivery, reply, feed, timings)
            except Exception as e:  # noqa: BLE001 — a failed POST is terminal
                # R55 (V35): before this, a post-phase exception fell through
                # to the blanket release below — a silent 20s retry loop that
                # re-ran the model forever and leaked a rate slot per lap.
                # Resolving through _run_failed writes the ledger and stops it.
                self._log_perf(timings, group, f"error:post:{type(e).__name__}")
                self._run_failed(group, feed, settings, delivery, e)
        except Exception:  # noqa: BLE001 — never kill the pool thread
            if slot:
                self.queue.rate_refund(chat_id)
            # R55 (V35): bounded — a group that keeps failing before the model
            # resolves as an error instead of retrying forever
            if not self.queue.retry_or_fail(group, retry_in_s=self.poll_s * 4):
                with contextlib.suppress(Exception):
                    RunFeed(self.mesh.tx, self.agent, chat_id).finish(
                        "error", "Run failed repeatedly — giving up on this trigger")
                    self.publish_status()

    # ------------------------------------------------- claim-time guards (R55)
    def _can_post(self, chat_id: str) -> bool:
        """May this agent send in the chat right now? Unsure = run — the post
        path still enforces, and its failure is terminal (not a retry loop)."""
        try:
            snap = self.mesh.messaging.snapshot(chat_id)
            return authz.can_send(snap, self.agent)
        except Exception:  # noqa: BLE001
            return True

    def _owner_stop_requested(self, chat_id: str) -> bool:
        """A fresh stop doc for this agent (global or naming this chat) that
        no live run consumed. Consumed here exactly once; stale docs are
        ignored so a leftover can't eat runs hours later."""
        path = f"status/{self.agent}_stop.json"
        try:
            doc = self.mesh.tx.get_doc(path)
            if not isinstance(doc, dict):
                return False
            if doc.get("chat_id") and doc.get("chat_id") != chat_id:
                return False
            if int(doc.get("ns", 0)) < time.time_ns() - int(STOP_FRESH_S * 1e9):
                return False
            with contextlib.suppress(Exception):
                self.mesh.tx.delete_doc(path)
            return True
        except Exception:  # noqa: BLE001 — a transport blip never stops a run
            return False

    def _blob_syncing(self, chat_id: str, transcript) -> str | None:
        """The name of a RECENT attachment whose blob is not fetchable yet
        (line synced ahead of its bytes), or None. Messages older than the
        grace window never defer — a lost blob must not wedge the chat; the
        run then proceeds with the bare filename (v1 semantics)."""
        horizon = time.time_ns() - int(BLOB_GRACE_S * 1e9)
        for m in reversed(transcript):
            if getattr(m, "ns", 0) < horizon:
                break
            for f in m.files or []:
                blob_id, name = f.get("id"), f.get("name", "")
                if not blob_id or blob_id in self._blobs_ok:
                    continue
                try:
                    raw = self.mesh.tx.get_blob(f"chats/{chat_id}/files/{blob_id}")
                    if raw is None:
                        return name or blob_id
                    data = self.mesh.sealer.open_blob(chat_id, blob_id, raw)
                    if data is None or (f.get("bytes") is not None
                                        and len(data) != f["bytes"]):
                        return name or blob_id
                except Exception:  # noqa: BLE001 — a blip = not fetchable yet
                    return name or blob_id
                self._blobs_ok.add(blob_id)
        return None

    def _log_perf(self, timings: RunTimings, group: WorkGroup,
                  outcome: str) -> None:
        timings.log(self.home, agent=self.agent, chat_id=group.chat_id,
                    kind=group.kind, outcome=outcome)

    def _deliver_reply(self, group: WorkGroup, delivery, reply: Reply,
                       feed: RunFeed, timings: RunTimings) -> None:
        chat_id = group.chat_id
        body, no_reply = clean_reply(reply.body)
        timer_ids = self.timers.add_from_reply(chat_id, reply.timers)
        if group.kind == "timer":
            self.timers.pop(group.items[0].key.split("timer:", 1)[-1])
        if no_reply or not body:
            self.queue.rate_refund(chat_id)  # a silent run costs no slot
            self.queue.finish(group, "no_reply")
            feed.finish("done", "No reply needed")
            self._log_perf(timings, group, "no_reply")
            self.publish_status()
            return
        reply_to = None
        if group.kind == "message" and delivery.triggers:
            last = delivery.triggers[-1].message
            reply_to = {"id": last.id, "from": last.from_,
                        "body": (last.body or "")[:200]}
            # R31: answering the NEWEST message displays as a plain standalone
            # message (WhatsApp) — quote=False keeps the attribution in the
            # record (the answered-guard's transcript leg depends on it) while
            # clients skip the quote bubble. A chat that moved on keeps the
            # visible quote so readers see what the reply belongs to.
            if not self._chat_moved_on(chat_id, last.ns):
                reply_to["quote"] = False
        timings.start("post")
        posted = self.mesh.post(chat_id, body, reply_to=reply_to,
                                files=self._attach(chat_id, reply.files))
        timings.stop()
        self.queue.finish(group, posted.id)
        # the timing line rides the Message-info task doc — owner-visible
        # profiling with no new UI (R30)
        record_tasks(self.mesh.tx, chat_id, posted.id, self.agent,
                     (reply.steps or feed.tasks)
                     + [{"text": f"⏱ {timings.summary()}", "ts": utcnow_iso()}])
        note = "Reply posted" + (f" (+{len(timer_ids)} timer(s))"
                                 if timer_ids else "")
        feed.finish("done", f"{note} · {timings.summary()}")
        self._log_perf(timings, group, "posted")
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

    def _chat_moved_on(self, chat_id: str, since_ns: int) -> bool:
        """Did any chat MESSAGE land after ``since_ns``? Info events (joins,
        renames) don't count — they never need a quote to stay readable."""
        try:
            return any(
                int(r.get("ns", 0)) > since_ns and r.get("kind") == "message"
                for r in self.mesh.store.messages(chat_id))
        except Exception:  # noqa: BLE001 — unsure = quote, the safe default
            return True

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
        acc = self.mesh.directory.get(self.agent)
        if acc is not None and acc.deactivated:
            # R56 (V49): the agent was DELETED (soft) — exit cleanly (rc 0,
            # so the supervisor stops too) instead of idling forever. Distinct
            # from active=False alone, which is the owner's pause switch.
            print(f"[harness] @{self.agent} was deleted — standing down")
            raise SystemExit(0)
        settings = self.settings()
        # MCP-only (Q21): adapter "none" means this agent runs no local CLI —
        # it connects through mesh-cli itself. Stand the runner down cleanly
        # (rc 0, so the supervisor stops too) instead of erroring per trigger.
        if settings.adapter == "none":
            print(f"[harness] @{self.agent} is MCP-only (adapter 'none') — "
                  f"no local runs; standing down")
            raise SystemExit(0)
        # peer access runs even while standing down — diagnosing a paused or
        # stuck agent is exactly when a peer needs in (read-only, R22)
        self.peer.serve_once(settings)
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
        try:  # R25: warm the cache, then populate tenure + re-sign redactions
            self.mesh.sync.sync_once()
            self.mesh.harden_startup()
        except Exception:  # noqa: BLE001 — hardening never blocks the harness
            pass
        # V51: advertise this machine's app version on the R11 registry so
        # peers' update checks can hint at it (records age out at STALE_S,
        # so a long-lived harness re-announces below)
        with contextlib.suppress(Exception):
            self.mesh.applink.announce(["harness"])
        announced = time.monotonic()
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
                if time.monotonic() - announced > 1800:
                    announced = time.monotonic()
                    with contextlib.suppress(Exception):
                        self.mesh.applink.announce(["harness"])
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
        resp_close = getattr(self.responder, "close", None)
        if callable(resp_close):
            resp_close()   # e.g. the CLI responder's qdrant path lock
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


def hosted_agents(root, machine: str) -> list[str]:
    """Agents whose accounts name THIS machine as home (active or not — the
    active flag is a runtime hold, and a held agent still syncs; a DELETED
    agent — ``deactivated`` set — gets no runner, R56/V49). An agent set to
    adapter "none" (MCP-only, Q21) runs no local CLI — it connects through
    mesh-cli on its own, so no runner is spawned for it."""
    from ..mesh.directory import Directory
    from ..transport import make_transport

    directory = Directory(make_transport(root))
    out = []
    for name in directory.names():
        acc = directory.get(name)
        if acc and acc.kind is UserKind.AGENT and acc.agent \
                and not acc.deactivated \
                and acc.agent.machine == machine \
                and str(acc.agent.harness.get("adapter") or "") != "none":
            out.append(name)
    return out


def supervise_all(root, machine: str, argv: list[str],
                  *, rescan_s: float = 30.0) -> int:
    """One supervised runner per hosted agent (AgentHarness.pyw's engine).
    Each child holds its own single-instance lock; a second launcher's
    children simply stand aside (rc 3).

    R54 (V26): the roster is RE-SCANNED every ``rescan_s`` — an agent
    created or adopted while the fleet is up gets its supervisor within a
    scan (it used to take a relaunch), and a supervisor that exited is
    respawned as long as its agent is still hosted here. A stand-aside
    exit (another instance owns the agent, e.g. a GUI-started runner)
    retries on a slow leash instead of hot-looping."""
    passthru = [a for a in argv if a != "--all"]
    children: dict[str, subprocess.Popen] = {}
    cooldown: dict[str, float] = {}      # name -> not-before (monotonic)
    first = True
    try:
        while True:
            agents = hosted_agents(root, machine)
            if first and not agents:
                print(f"no agents are hosted on this machine ({machine}) — "
                      f"create or adopt one in Settings; this launcher "
                      f"picks it up within {rescan_s:.0f}s", flush=True)
            now = time.monotonic()
            for name in list(children):
                c = children[name]
                if c.poll() is None:
                    continue
                if c.returncode == EXIT_ALREADY_RUNNING:
                    cooldown[name] = now + 300.0
                children.pop(name)
            spawned = []
            for name in agents:
                if name in children or now < cooldown.get(name, 0.0):
                    continue
                children[name] = subprocess.Popen(
                    [sys.executable, "-m", "agentbridge.harness",
                     name, "--supervise", *passthru])
                spawned.append(name)
            if spawned:
                print(f"[harness] supervising {len(children)} agent(s): "
                      + ", ".join(f"@{a}" for a in sorted(children)), flush=True)
            first = False
            time.sleep(rescan_s)
    except KeyboardInterrupt:
        for c in children.values():
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
    # a scheme spec (supabase://…) stays a string; Path() would mangle it
    root = root if "://" in str(root) else Path(root)

    if args.all:
        machine = args.machine or platform.node() or "harness"
        return supervise_all(root, machine,
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
        runner = AgentRunner(root, args.agent, home=home,
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
