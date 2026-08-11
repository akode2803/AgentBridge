"""Privacy-minimized GUI projections of canonical agent runtime records.

The runtime ledgers remain the authority. This module only turns their
membership-gated fold into compact conversation rows; it never exposes task
content, results, policy material, grants, or raw wire records.
"""

from __future__ import annotations

from ..harness.runtime.handoffs import HandoffLedger
from ..harness.runtime.runs import RunLedger
from ..harness.runtime.tasks import TaskLedger
from .routing import authed

__all__ = ["GET", "POST"]


def contributor_rows(mesh, chat_id: str, *,
                     limit: int | None = 50) -> list[dict]:
    """Return the newest canonical handoff rows visible to ``mesh.user``."""
    hidden = set(mesh.my_state(chat_id)["hidden_runtime"])
    try:
        snapshot = mesh.tx.cached_docs_bounded(
            f"chats/{chat_id}/runtime/", 10_000,
        )
    except OverflowError as exc:
        from ..harness.runtime.handoffs import HandoffLedgerError

        raise HandoffLedgerError(
            "runtime task history exceeds the bounded GUI projection",
        ) from exc
    # The sync mirror already receives runtime docs. GUI polling must never
    # invoke the authority ledgers' deliberate live Supabase read-through.
    runs = RunLedger(
        mesh, fresh_reads=False, register_outbox=False,
        read_snapshot=snapshot,
    )
    tasks = TaskLedger(
        mesh, runs, fresh_reads=False, register_outbox=False,
        read_snapshot=snapshot,
    )
    views = HandoffLedger(
        mesh, tasks, fresh_reads=False, register_outbox=False,
        read_snapshot=snapshot,
    ).read(
        chat_id, historical=True,
    )
    rows = []
    for view in views:
        latest = view.events[-1]
        offered = view.events[0]
        if (offered.meta.call_id or "") in hidden:
            continue
        rows.append({
            "id": offered.meta.call_id or "",
            "run_id": offered.meta.run_id or "",
            "manager": offered.source_agent,
            "contributor": offered.destination_agent,
            "kind": offered.handoff_type.value,
            "state": latest.state.value,
            "started_ns": offered.meta.ns,
            "updated_ns": latest.meta.ns,
        })
    rows.sort(key=lambda row: (row["updated_ns"], row["id"]))
    return rows if limit is None else rows[-limit:]


@authed
def runtime_tasks(app, req, mesh) -> dict:
    chat_id = str(req.params.get("id") or "")
    limit = req.int_param("limit", 50, 1, 100)
    return {"tasks": contributor_rows(mesh, chat_id, limit=limit)}


GET = {"/api/mesh/runtime_tasks": runtime_tasks}
POST = {}
