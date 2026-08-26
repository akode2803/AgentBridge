"""Run-feed wire-format helpers shared by the sidebar and chat endpoints.

R108 stores every concurrent run as an independently keyed entry in one
bounded ``status/<agent>_live.json`` document. During a rolling update we also
accept the old ``status/<agent>_run.json`` singleton.
"""

from __future__ import annotations

__all__ = ["expand_runs", "suppress_superseded_preparing"]


def expand_runs(path: str, doc: dict) -> list[dict]:
    """Return normalized run entries carried by one status document."""
    leaf = path.rsplit("/", 1)[-1]
    suffix = next(
        (value for value in ("_live.json", "_preparing.json")
         if leaf.endswith(value)), None)
    if suffix and doc.get("kind") == "run-set":
        agent = str(doc.get("agent") or leaf[: -len(suffix)])
        return [
            {**run, "agent": str(run.get("agent") or agent),
             "run_id": str(run.get("run_id") or ""),
             "preparing": suffix == "_preparing.json"}
            for run in (doc.get("runs") or [])
            if isinstance(run, dict)
        ]
    if leaf.endswith("_run.json"):
        agent = str(doc.get("agent") or leaf[: -len("_run.json")])
        return [{**doc, "agent": agent,
                 "run_id": str(doc.get("run_id") or f"legacy-{agent}")}]
    return []


def suppress_superseded_preparing(runs: list[dict]) -> list[dict]:
    """Hide a pre-run entry once its matching canonical run is visible."""
    live = {
        str(run.get("transition_id") or "") for run in runs
        if not run.get("preparing") and run.get("transition_id")
    }
    return [
        run for run in runs
        if not (run.get("preparing") and run.get("transition_id") in live)
    ]
