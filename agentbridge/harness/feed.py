"""Runtime status docs — the harness's owner- and member-visible surfaces.

- ``status/<agent>_run.json``: the per-run live feed (v1 shape, kept — the
  GUI livefeed already reads it). v2 drops the streaming ``draft`` body: in
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

import time

from ..core.timekit import utcnow_iso
from ..transport.base import Transport

__all__ = ["RunFeed", "write_harness_doc", "record_tasks", "write_waiting",
           "reap_orphan_run"]

_THROTTLE_S = 1.5


def write_waiting(tx: Transport, agent: str, chat_id: str, activity: str) -> None:
    """V71: surface that a run is HELD on the attachment sync barrier — the
    message line synced ahead of its blob, so the run is deferred until the
    bytes arrive. Written as a normal ``running`` run-feed doc so the GUI
    livefeed shows the agent's activity line ("Waiting for the attachment…")
    instead of nothing — a large file no longer reads as a frozen agent. The
    real run overwrites this the moment the blob lands (or the grace expires
    and it proceeds); a stale one ages out with every other run feed."""
    try:
        tx.put_doc(f"status/{agent}_run.json", {
            "state": "running", "agent": agent, "chat_id": chat_id,
            "started": utcnow_iso(), "updated": utcnow_iso(),
            "turns": 0, "activity": " ".join((activity or "").split())[:120],
            "recent": [], "draft": "", "steps": [], "waiting": True,
        })
    except Exception:  # noqa: BLE001 — a status write never blocks handling
        pass


class RunFeed:
    """One agent run's live feed. Single writer: this agent's machine."""

    def __init__(self, tx: Transport, agent: str, chat_id: str) -> None:
        self.tx = tx
        self.agent = agent
        self.chat_id = chat_id
        self.turns = 0
        self.activity = "Starting up…"
        self.recent: list[str] = []
        self.tasks: list[dict] = []
        self.started = utcnow_iso()
        self._last_write = 0.0
        self.write("running", force=True)

    def step(self, line: str) -> None:
        try:
            line = " ".join((line or "").split())[:120]
            if not line:
                return
            self.turns += 1
            self.activity = line
            self.recent = (self.recent + [line])[-8:]
            self.tasks.append({"text": line, "ts": utcnow_iso()})
            # the first steps land inside the throttle window right after the
            # forced init write — without forcing them too, the pane opens on
            # "Starting up…" and jumps straight to mid-run (live feedback)
            self.write("running", force=self.turns <= 3)
        except Exception:  # noqa: BLE001 — the feed must never break a run
            pass

    def write(self, state: str, force: bool = False) -> None:
        if not force and time.time() - self._last_write < _THROTTLE_S:
            return
        try:
            self.tx.put_doc(f"status/{self.agent}_run.json", {
                "state": state, "agent": self.agent, "chat_id": self.chat_id,
                "started": self.started, "updated": utcnow_iso(),
                "turns": self.turns, "activity": self.activity,
                "recent": self.recent, "draft": "",
                # timestamped steps for the in-progress right-click menu (R36)
                "steps": self.tasks[-12:],
            })
            self._last_write = time.time()
        except Exception:  # noqa: BLE001
            pass

    def finish(self, state: str, note: str | None = None) -> None:
        if note:
            self.activity = note
        self.write(state, force=True)
        self._append_history(state)

    def _append_history(self, state: str) -> None:
        """The 'tasks completed by this agent' record (R36): finished runs
        append to status/<agent>_runs.json, newest last, capped. Single
        writer (this agent's machine), so read-modify-write is safe."""
        try:
            path = f"status/{self.agent}_runs.json"
            doc = self.tx.get_doc(path, default={}) or {}
            runs = doc.get("runs") if isinstance(doc, dict) else None
            runs = runs if isinstance(runs, list) else []
            runs.append({
                "chat_id": self.chat_id, "state": state,
                "started": self.started, "finished": utcnow_iso(),
                "turns": self.turns, "note": self.activity[:160],
            })
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
        path = f"status/{agent}_run.json"
        doc = tx.get_doc(path, default=None)
        if (not isinstance(doc, dict) or doc.get("state") != "running"
                or doc.get("waiting")):
            return False
        if running_chats and doc.get("chat_id") in running_chats:
            return False
        tx.put_doc(path, {
            **doc, "state": "interrupted", "updated": utcnow_iso(),
            "activity": "Interrupted — the run never finished",
        })
        # the run history is the owner's "what happened?" surface — an
        # interruption is an answer, not noise (mirrors _append_history)
        hist = f"status/{agent}_runs.json"
        hdoc = tx.get_doc(hist, default={}) or {}
        runs = hdoc.get("runs") if isinstance(hdoc, dict) else None
        runs = runs if isinstance(runs, list) else []
        runs.append({
            "chat_id": doc.get("chat_id", ""), "state": "interrupted",
            "started": doc.get("started", ""), "finished": utcnow_iso(),
            "turns": doc.get("turns", 0),
            "note": "Interrupted — the app or agent restarted mid-run",
        })
        tx.put_doc(hist, {"agent": agent, "runs": runs[-20:]})
        return True
    except Exception:  # noqa: BLE001 — hygiene never breaks the harness
        return False


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
                 "created": t.get("created")}
                for t in timers[:50]
            ],
        })
    except Exception:  # noqa: BLE001
        pass
