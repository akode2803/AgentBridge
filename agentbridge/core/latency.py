"""Bounded, content-free local latency observations.

Each row is one boundary observation on one machine. Correlation uses existing
opaque message/run ids; records never ride the mesh and durations are computed
only from monotonic timestamps sharing a ``clock_id``.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["LatencySink", "clock_id", "sink_for_store",
           "sink_path_for_store_path"]

STAGES = frozenset({
    "origin_minted", "local_commit", "outbox_attempt",
    "append_ack_observed", "sync_observed", "queue_enqueued",
    "queue_claimed", "preparation_started", "feed_started",
    "provider_started", "provider_first_activity", "provider_finished",
    "reply_local_commit", "sse_frame", "browser_received",
    "refetch_completed", "render_completed",
})
LANES = frozenset({"", "startup", "hint", "poll", "fallback", "cache", "local"})
_MAX_BYTES = 2 * 1024 * 1024
_KEEP_ROWS = 1000
_CLOCK_ID = f"p-{os.getpid()}-{secrets.token_hex(6)}"
_SINKS: dict[Path, "LatencySink"] = {}
_SINKS_LOCK = threading.Lock()


def clock_id() -> str:
    return _CLOCK_ID


class LatencySink:
    """Append-only local JSONL with bounded compaction and drop accounting."""

    def __init__(self, path: Path | str, *, max_bytes: int = _MAX_BYTES,
                 wall_ns=time.time_ns, mono_ns=time.perf_counter_ns) -> None:
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self._wall_ns = wall_ns
        self._mono_ns = mono_ns
        self._lock = threading.RLock()
        self._dropped = 0

    def observe(self, stage: str, trace_ref: str, *, run_ref: str = "",
                reply_ref: str = "", lane: str = "", outcome: str = "",
                at_ns: int | None = None, mono_ns: int | None = None,
                observed_clock: str | None = None) -> None:
        """Record one whitelisted metadata-only boundary, best effort."""
        if stage not in STAGES or lane not in LANES:
            return
        refs = (str(trace_ref or "")[:160], str(run_ref or "")[:160],
                str(reply_ref or "")[:160])
        if not refs[0]:
            return
        row: dict[str, Any] = {
            "v": 1, "stage": stage, "trace_ref": refs[0],
            "clock_id": str(observed_clock or _CLOCK_ID)[:80],
            "at_ns": int(self._wall_ns() if at_ns is None else at_ns),
        }
        if observed_clock is None:
            row["mono_ns"] = int(self._mono_ns() if mono_ns is None else mono_ns)
        if refs[1]:
            row["run_ref"] = refs[1]
        if refs[2]:
            row["reply_ref"] = refs[2]
        if lane:
            row["lane"] = lane
        if outcome:
            row["outcome"] = str(outcome)[:40]
        try:
            encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    self._compact_locked()
                with self.path.open("a", encoding="ascii") as handle:
                    handle.write(encoded + "\n")
        except Exception:  # noqa: BLE001 - diagnostics never affect the app
            with self._lock:
                self._dropped += 1

    def read(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        try:
            with self._lock:
                lines = self.path.read_text(encoding="ascii").splitlines()[-limit:]
            return [row for line in lines
                    if isinstance((row := json.loads(line)), dict)]
        except Exception:
            return []

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"dropped": self._dropped,
                    "bytes": self.path.stat().st_size if self.path.exists() else 0}

    def _compact_locked(self) -> None:
        try:
            lines = self.path.read_text(encoding="ascii").splitlines()
            removed = max(0, len(lines) - _KEEP_ROWS)
            kept = lines[-_KEEP_ROWS:]
            self.path.write_text("\n".join(kept) + ("\n" if kept else ""),
                                 encoding="ascii")
            self._dropped += removed
        except Exception:
            self._dropped += 1


def sink_path_for_store_path(store_path: Path | str) -> Path:
    store_path = Path(store_path)
    return store_path.parent / "latency" / f"{store_path.stem}.jsonl"


def sink_for_store(store) -> LatencySink:
    path = sink_path_for_store_path(store.path)
    with _SINKS_LOCK:
        sink = _SINKS.get(path)
        if sink is None:
            sink = LatencySink(path)
            _SINKS[path] = sink
        return sink
