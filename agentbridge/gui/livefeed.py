"""Run-feed wire-format helpers shared by the sidebar and chat endpoints.

R108 stores every concurrent run as an independently keyed entry in one
bounded ``status/<agent>_live.json`` document. During a rolling update we also
accept the old ``status/<agent>_run.json`` singleton.
"""

from __future__ import annotations

__all__ = ["expand_runs"]


def expand_runs(path: str, doc: dict) -> list[dict]:
    """Return normalized run entries carried by one status document."""
    leaf = path.rsplit("/", 1)[-1]
    if leaf.endswith("_live.json") and doc.get("kind") == "run-set":
        agent = str(doc.get("agent") or leaf[: -len("_live.json")])
        return [
            {**run, "agent": str(run.get("agent") or agent),
             "run_id": str(run.get("run_id") or "")}
            for run in (doc.get("runs") or [])
            if isinstance(run, dict)
        ]
    if leaf.endswith("_run.json"):
        agent = str(doc.get("agent") or leaf[: -len("_run.json")])
        return [{**doc, "agent": agent,
                 "run_id": str(doc.get("run_id") or f"legacy-{agent}")}]
    return []
