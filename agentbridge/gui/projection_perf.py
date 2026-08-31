"""One bounded, content-free observation per sidebar/chat projection request."""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from ..core.timekit import utcnow_iso

__all__ = ["ProjectionObservation"]

_STAGES = frozenset({
    "directory_projection", "visible_chat_enumeration", "status_annotation",
    "overview_folds", "membership_gate", "transcript_fold", "receipts_fold",
    "viewer_state", "pins", "pause_state", "payload_assembly",
    "snapshot", "envelope_load", "edits_load", "redactions_load",
    "reactions_load", "viewer_state_load", "readmodel_fold",
})
_COUNTERS = frozenset({
    "room_count", "fold_calls", "raw_messages", "deduplicated_messages",
    "visible_messages", "unseal_attempted", "unseal_failed",
    "edit_attempted", "edit_honored", "redaction_attempted",
    "redaction_honored", "drop_breadcrumb", "drop_history", "drop_tenure",
    "drop_hidden", "drop_clear", "drop_delete", "serialization_count",
})
_MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()


class ProjectionObservation:
    """In-memory aggregation; one append happens only after the request ends."""

    def __init__(self, scope: str) -> None:
        if scope not in {"sidebar", "chat"}:
            raise ValueError("projection scope must be sidebar or chat")
        self.scope = scope
        self.request_ref = secrets.token_hex(8)
        self.started = time.perf_counter()
        self.stages: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    def stage(self, name: str, seconds: float) -> None:
        if name in _STAGES and seconds >= 0:
            self.stages[name] = self.stages.get(name, 0.0) + float(seconds)

    def count(self, name: str, value: int = 1) -> None:
        if name in _COUNTERS and value >= 0:
            self.counts[name] = self.counts.get(name, 0) + int(value)

    def measure(self, name: str, fn):
        started = time.perf_counter()
        value = fn()
        self.stage(name, time.perf_counter() - started)
        return value

    def record(self, outcome: str) -> dict:
        return {
            "v": 1,
            "ts": utcnow_iso(),
            "request_ref": self.request_ref,
            "scope": self.scope,
            "outcome": str(outcome or "unknown")[:40],
            "total_s": round(max(0.0, time.perf_counter() - self.started), 6),
            "stages_s": {name: round(value, 6)
                         for name, value in sorted(self.stages.items())},
            "counts": dict(sorted(self.counts.items())),
        }

    def log(self, home: Path, outcome: str) -> None:
        """Best effort and one write only; profiling never changes a request."""
        try:
            path = Path(home) / "gui" / "perf" / "projections.jsonl"
            encoded = json.dumps(
                self.record(outcome), separators=(",", ":"), ensure_ascii=True,
            )
            with _LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > _MAX_BYTES:
                    path.unlink()
                with path.open("a", encoding="ascii") as handle:
                    handle.write(encoded + "\n")
        except Exception:
            pass
