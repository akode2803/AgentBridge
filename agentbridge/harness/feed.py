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

__all__ = ["RunFeed", "write_harness_doc", "record_tasks"]

_THROTTLE_S = 1.5


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
            self.write("running")
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
            })
            self._last_write = time.time()
        except Exception:  # noqa: BLE001
            pass

    def finish(self, state: str, note: str | None = None) -> None:
        if note:
            self.activity = note
        self.write(state, force=True)


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
