"""Runtime status docs — the harness's owner- and member-visible surfaces.

- ``status/<agent>_live.json``: a bounded set of independently keyed live runs.
  The GUI also reads the pre-R108 singleton ``<agent>_run.json`` during rollout.
  The current shape drops the streaming ``draft`` body: in
  an E2EE mesh a reply draft is content, not metadata, and this doc is plain
  at rest. Activity lines stay (tool-step summaries; R17 rewords them).
- ``chats/<id>/tasks/<msg_id>.json``: the task steps behind one reply, for
  the Message-info dialog.
- ``status/<agent>_harness.json``: the owner-visible harness state — pending
  queue (metadata only) + every scheduled timer. Nothing runs invisibly.

All writes are best-effort and throttled: a status doc must never break
message handling (v1 rule, kept).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..core.timekit import new_id, utcnow_iso
from ..transport.base import Transport

__all__ = ["RunFeed", "write_harness_doc", "record_tasks", "write_waiting",
           "reap_orphan_run"]

_THROTTLE_S = 1.5
_HEARTBEAT_S = 60.0


def _live_path(agent: str) -> str:
    return f"status/{agent}_live.json"


def _waiting_id(chat_id: str) -> str:
    import hashlib

    return "waiting-" + hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]


@dataclass
class _FeedCoordinator:
    """One bounded cloud document for every concurrent run of one agent.

    Agent runners are single-process owners of their status lane, but model
    calls run on several threads. Serializing the aggregate here prevents one
    run's finish from erasing another run's activity without creating a new
    Supabase row (and eventual tombstone) for every turn.
    """

    tx: Transport
    agent: str
    runs: dict[str, dict] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def write(self) -> None:
        self.tx.put_doc(_live_path(self.agent), {
            "kind": "run-set", "agent": self.agent, "updated": utcnow_iso(),
            "runs": list(self.runs.values()),
        })


_COORDINATORS: dict[tuple[int, str], _FeedCoordinator] = {}
_COORDINATORS_LOCK = threading.RLock()


def _coordinator(tx: Transport, agent: str) -> _FeedCoordinator:
    key = (id(tx), agent)
    with _COORDINATORS_LOCK:
        coord = _COORDINATORS.get(key)
        if coord is None or coord.tx is not tx:
            coord = _FeedCoordinator(tx, agent)
            _COORDINATORS[key] = coord
        return coord


def write_waiting(tx: Transport, agent: str, chat_id: str, activity: str) -> None:
    """V71: surface that a run is HELD on the attachment sync barrier — the
    message line synced ahead of its blob, so the run is deferred until the
    bytes arrive. Written as a normal ``running`` run-feed doc so the GUI
    livefeed shows the agent's activity line ("Waiting for the attachment…")
    instead of nothing — a large file no longer reads as a frozen agent. The
    real run overwrites this the moment the blob lands (or the grace expires
    and it proceeds); a stale one ages out with every other run feed."""
    try:
        coord = _coordinator(tx, agent)
        run_id = _waiting_id(chat_id)
        now = utcnow_iso()
        with coord.lock:
            previous = coord.runs.get(run_id) or {}
            coord.runs[run_id] = {
                "run_id": run_id, "state": "running", "agent": agent,
                "chat_id": chat_id, "started": previous.get("started", now),
                "updated": now, "turns": 0,
                "activity": " ".join((activity or "").split())[:120],
                "recent": [], "draft": "", "steps": [], "waiting": True,
            }
            coord.write()
    except Exception:  # noqa: BLE001 — a status write never blocks handling
        pass


class RunFeed:
    """One agent run's live feed. Single writer: this agent's machine."""

    def __init__(self, tx: Transport, agent: str, chat_id: str) -> None:
        self.tx = tx
        self.agent = agent
        self.chat_id = chat_id
        self.run_id = new_id("r")
        self.turns = 0
        self.activity = "Starting up…"
        self.recent: list[str] = []
        self.tasks: list[dict] = []
        self.started = utcnow_iso()
        self._last_write = 0.0
        self._finished = False
        self._stop = threading.Event()
        self._coord = _coordinator(tx, agent)
        # A real claim supersedes the attachment-wait placeholder for this
        # chat. Only that stable waiting entry is removed; parallel runs stay.
        with self._coord.lock:
            self._coord.runs.pop(_waiting_id(chat_id), None)
        self.write("running", force=True)
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name=f"ab-feed-{agent}-{self.run_id}",
            daemon=True,
        )
        self._heartbeat.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(_HEARTBEAT_S):
            self.write("running", force=True)

    def step(self, line: str) -> None:
        try:
            line = " ".join((line or "").split())[:120]
            if not line:
                return
            with self._coord.lock:
                self.turns += 1
                self.activity = line
                self.recent = (self.recent + [line])[-8:]
                self.tasks.append({"text": line, "ts": utcnow_iso()})
                # the first steps land inside the throttle window right after
                # init — otherwise the pane jumps from startup to mid-run.
                self.write("running", force=self.turns <= 3)
        except Exception:  # noqa: BLE001 — the feed must never break a run
            pass

    def write(self, state: str, force: bool = False) -> None:
        if not force and time.time() - self._last_write < _THROTTLE_S:
            return
        try:
            with self._coord.lock:
                if self._finished and state == "running":
                    return
                self._coord.runs[self.run_id] = {
                    "run_id": self.run_id, "state": state,
                    "agent": self.agent, "chat_id": self.chat_id,
                    "started": self.started, "updated": utcnow_iso(),
                    "turns": self.turns, "activity": self.activity,
                    "recent": self.recent, "draft": "",
                    # timestamped steps for the in-progress task disclosure
                    "steps": self.tasks[-12:],
                }
                self._coord.write()
                self._last_write = time.time()
        except Exception:  # noqa: BLE001
            pass

    def finish(self, state: str, note: str | None = None) -> None:
        # V107: a stop/error note ("Stopped by your member") REPLACES the last
        # activity line — capture what the run was doing before it's gone, so
        # the history (and the agent's next-run context) can say it. turns==0
        # means nothing ran yet ("Starting up…" is not an activity).
        self._stop.set()
        try:
            with self._coord.lock:
                if self._finished:
                    return
                self._finished = True
                doing = self.activity if self.turns and note \
                    and note != self.activity else ""
                if note:
                    self.activity = note
                # History is the durable completed-run surface. The live
                # aggregate contains active runs only, so finishing one cannot
                # hide another.
                self._append_history(state, doing=doing)
                self._coord.runs.pop(self.run_id, None)
                self._coord.write()
        except Exception:  # noqa: BLE001 - status must never break delivery
            pass

    def _append_history(self, state: str, doing: str = "") -> None:
        """The 'tasks completed by this agent' record (R36): finished runs
        append to status/<agent>_runs.json, newest last, capped. Single
        writer (this agent's machine), so read-modify-write is safe."""
        try:
            path = f"status/{self.agent}_runs.json"
            doc = self.tx.get_doc(path, default={}) or {}
            runs = doc.get("runs") if isinstance(doc, dict) else None
            runs = runs if isinstance(runs, list) else []
            entry = {
                "chat_id": self.chat_id, "state": state,
                "started": self.started, "finished": utcnow_iso(),
                "turns": self.turns, "note": self.activity[:160],
            }
            # only interrupted outcomes need the what-was-it-doing detail;
            # for a posted reply the note already says everything
            if doing and state in ("stopped", "error"):
                entry["doing"] = doing[:120]
            runs.append(entry)
            self.tx.put_doc(path, {"agent": self.agent, "runs": runs[-20:]})
        except Exception:  # noqa: BLE001 — history must never break a run
            pass


def reap_orphan_run(tx: Transport, agent: str,
                    running_chats: set[str] | None = None) -> bool:
    """V129: finish a run doc that claims "running" when this process runs
    no such run. A killed runner never writes its finish, so the doc
    haunted the chat as a working bubble until the 600s ghost cutoff —
    while the RELAUNCHED harness was honestly alive (V109's process truth
    checks the RUNNER, not the RUN; live screenshot report). Called at
    boot (a starting runner runs nothing by definition) and every loop
    tick (self-heals an in-process finish-less death too). A V71
    ``waiting`` doc is deliberately spared — the deferred run it
    advertises lives in the durable queue, not in ``running_chats``, and
    the queue rewrites it on its own cadence. Returns True when reaped."""
    try:
        coord = _coordinator(tx, agent)
        with coord.lock:
            doc = tx.get_doc(_live_path(agent), default=None)
            saved = doc.get("runs") if isinstance(doc, dict) else None
            saved = saved if isinstance(saved, list) else []
            active = set(coord.runs)
            orphans = [r for r in saved if isinstance(r, dict)
                       and r.get("state") == "running" and not r.get("waiting")
                       and r.get("run_id") not in active
                       and (not running_chats
                            or r.get("chat_id") not in running_chats)]
            # Rollout compatibility: the pre-R108 single-run document may be
            # the only stale record left after an update/restart.
            legacy_path = f"status/{agent}_run.json"
            legacy = tx.get_doc(legacy_path, default=None)
            if (isinstance(legacy, dict) and legacy.get("state") == "running"
                    and not legacy.get("waiting")
                    and (not running_chats
                         or legacy.get("chat_id") not in running_chats)):
                orphans.append(legacy)
                tx.put_doc(legacy_path, {
                    **legacy, "state": "interrupted", "updated": utcnow_iso(),
                    "activity": "Interrupted — the run never finished",
                })
            if not orphans:
                return False
            for orphan in orphans:
                _append_interrupted_history(tx, agent, orphan)
            # Rebuild from process truth. Waiting placeholders and all active
            # entries survive; stale saved entries do not.
            coord.runs = {
                str(r.get("run_id")): r for r in saved
                if isinstance(r, dict) and r.get("run_id")
                and (r.get("run_id") in active or r.get("waiting"))
            }
            coord.write()
            return True
    except Exception:  # noqa: BLE001 — hygiene never breaks the harness
        return False


def _append_interrupted_history(tx: Transport, agent: str, doc: dict) -> None:
    """Record one orphan in the same capped history used by RunFeed.finish."""
    try:
        # the run history is the owner's "what happened?" surface — an
        # interruption is an answer, not noise (mirrors _append_history)
        hist = f"status/{agent}_runs.json"
        hdoc = tx.get_doc(hist, default={}) or {}
        runs = hdoc.get("runs") if isinstance(hdoc, dict) else None
        runs = runs if isinstance(runs, list) else []
        entry = {
            "chat_id": doc.get("chat_id", ""), "state": "interrupted",
            "started": doc.get("started", ""), "finished": utcnow_iso(),
            "turns": doc.get("turns", 0),
            "note": "Interrupted — the app or agent restarted mid-run",
        }
        # the orphan doc's activity IS the last thing the run did (V107)
        doing = str(doc.get("activity") or "")
        if doing:
            entry["doing"] = doing[:120]
        runs.append(entry)
        tx.put_doc(hist, {"agent": agent, "runs": runs[-20:]})
    except Exception:  # noqa: BLE001 — hygiene never breaks the harness
        pass


def record_tasks(tx: Transport, chat_id: str, msg_id: str,
                 agent: str, tasks: list[dict]) -> None:
    """Persist the steps behind one reply (Message info). Best-effort."""
    if not tasks or not msg_id:
        return
    try:
        tx.put_doc(f"chats/{chat_id}/tasks/{msg_id}.json", {
            "agent": agent, "msg_id": msg_id, "updated": utcnow_iso(),
            "tasks": [{"text": str(t.get("text", ""))[:200],
                       "ts": t.get("ts", "")} for t in tasks[:100]],
        })
    except Exception:  # noqa: BLE001
        pass


def write_harness_doc(tx: Transport, agent: str, *, queue: list[dict],
                      timers: list[dict], paused: bool) -> None:
    """The owner-visible harness state (queue metadata + timers)."""
    try:
        tx.put_doc(f"status/{agent}_harness.json", {
            "agent": agent, "updated": utcnow_iso(), "paused": paused,
            "queue": queue[:50],
            "timers": [
                {"id": t.get("id"), "chat_id": t.get("chat_id"),
                 "at_ns": t.get("at_ns"), "note": t.get("note"),
                 "created": t.get("created"),
                 # V88: the chip says "repeats daily" etc.
                 **({"repeat": t["repeat"]} if t.get("repeat") else {})}
                for t in timers[:50]
            ],
        })
    except Exception:  # noqa: BLE001
        pass
