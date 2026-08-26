"""Per-run response timing (R30) — where does an agent reply's time go?

Stages of one run, as the sender experiences them:
- ``pickup``  — trigger ordinal -> this runner began processing (an
  approximate cross-machine wall-clock interval, retained for compatibility)
- ``trigger_to_scan`` / ``enqueue`` / ``queue`` — the explicit pre-run
  handoffs as observed by this runner; these do not claim transport commit time
- ``context`` — building the delivery (transcript, retrieval, memory, prompt)
- ``model``   — the responder run (the CLI/model actually generating)
- ``post``    — sealing + committing the reply to the outbox

Where it lands:
- one JSON line per run in ``<home>/harness/perf/<agent>.jsonl`` — the
  machine-readable profile (rotated by size, newest-last);
- a compact human summary rides the existing run feed + Message-info task
  docs (owner-visible with ZERO new UI).

Everything here is best-effort: profiling must never break a run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..core.timekit import utcnow_iso

__all__ = ["RunTimings"]

_MAX_LOG_BYTES = 2 * 1024 * 1024   # ~2 MB, then the log restarts


class RunTimings:
    """Stopwatch for one run. ``stage(...)`` wraps each phase; ``pickup_s``
    is derived from the trigger's mint time (ns since epoch)."""

    def __init__(self, trigger_ns: int, *, observed_ns: int = 0,
                 enqueued_ns: int = 0, claimed_ns: int = 0) -> None:
        self.trigger_ns = int(trigger_ns or 0)
        self.observed_ns = int(observed_ns or 0)
        self.enqueued_ns = int(enqueued_ns or 0)
        self.claimed_ns = int(claimed_ns or 0)
        self.pickup_s = (
            max(0.0, (time.time_ns() - self.trigger_ns) / 1e9)
            if self.trigger_ns else 0.0
        )
        self.stages: dict[str, float] = {}
        self._t0: float | None = None
        self._name = ""

    @staticmethod
    def _between(start_ns: int, end_ns: int) -> float:
        return max(0.0, (end_ns - start_ns) / 1e9) if start_ns and end_ns else 0.0

    def propagation(self) -> dict[str, float]:
        """Content-free wall-clock handoff breakdown before runner startup."""
        return {
            "trigger_to_scan_s": self._between(self.trigger_ns, self.observed_ns),
            "enqueue_s": self._between(self.observed_ns, self.enqueued_ns),
            "queue_s": self._between(self.enqueued_ns, self.claimed_ns),
        }

    # ------------------------------------------------------------- stopwatch
    def start(self, name: str) -> None:
        self._name = name
        self._t0 = time.perf_counter()

    def stop(self) -> None:
        if self._t0 is not None and self._name:
            self.stages[self._name] = time.perf_counter() - self._t0
        self._t0 = None
        self._name = ""

    # -------------------------------------------------------------- reporting
    def total_s(self) -> float:
        return self.pickup_s + sum(self.stages.values())

    def summary(self) -> str:
        """`44.6s total · pickup 2.1s · context 0.3s · model 41.8s · post 0.4s`"""
        parts = [f"{self.total_s():.1f}s total"]
        if self.trigger_ns:
            parts.append(f"pickup {self.pickup_s:.1f}s")
        parts += [f"{k} {v:.1f}s" for k, v in self.stages.items()]
        return " · ".join(parts)

    def record(self, *, agent: str, chat_id: str, kind: str,
               outcome: str) -> dict:
        return {
            "ts": utcnow_iso(), "agent": agent, "chat_id": chat_id,
            "kind": kind, "outcome": outcome,
            "total_s": round(self.total_s(), 3),
            "pickup_s": round(self.pickup_s, 3),
            **{k: round(v, 3) for k, v in self.propagation().items()},
            **{f"{k}_s": round(v, 3) for k, v in self.stages.items()},
        }

    def log(self, home: Path, *, agent: str, chat_id: str, kind: str,
            outcome: str) -> None:
        """Append one profile line, best-effort, size-capped."""
        try:
            path = Path(home) / "harness" / "perf" / f"{agent}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
                path.unlink()   # a profile log is disposable — restart it
            line = json.dumps(
                self.record(agent=agent, chat_id=chat_id, kind=kind,
                            outcome=outcome),
                ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — profiling never breaks a run
            pass
