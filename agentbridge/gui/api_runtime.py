"""Privacy-minimized GUI projections of canonical agent runtime records.

The runtime ledgers remain the authority. This module only turns their
membership-gated fold into compact conversation rows; it never exposes task
content, results, policy material, grants, or raw wire records.
"""

from __future__ import annotations

import re

from ..harness.runtime.handoffs import HandoffLedger
from ..harness.runtime.runs import RunLedger, RunLedgerError
from ..harness.runtime.tasks import TaskLedger
from ..harness.adapters.native import (
    NATIVE_CAPABILITIES, codex_native_policy,
    validate_native_authority_facts,
)
from ..core.errors import ValidationError
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


def authority_rows(mesh, chat_id: str, *, limit: int = 50,
                   run_ids: tuple[str, ...] | None = None,
                   current_only: bool = False) -> list[dict]:
    """Project non-secret effective authority from canonical signed runs."""
    hidden = set(mesh.my_state(chat_id)["hidden_runtime"])
    if run_ids is None:
        try:
            snapshot = mesh.tx.cached_docs_bounded(
                f"chats/{chat_id}/runtime/", 10_000,
            )
        except OverflowError as exc:
            raise RunLedgerError(
                "runtime run history exceeds the bounded GUI projection",
            ) from exc
        records = RunLedger(
            mesh, fresh_reads=False, register_outbox=False,
            read_snapshot=snapshot,
        ).read(chat_id)
    else:
        ledger = RunLedger(
            mesh, fresh_reads=False, register_outbox=False,
        )
        records = [
            record for run_id in run_ids
            for record in ledger.read(chat_id, run_id)
        ]
    grouped = {}
    for record in records:
        grouped.setdefault(record.meta.run_id or "", []).append(record)
    rows = []
    for run_id, events in grouped.items():
        if not run_id or run_id in hidden:
            continue
        events.sort(key=lambda item: (item.meta.ns, item.meta.id))
        start, latest = events[0], events[-1]
        if current_only and latest.state.value != "running":
            continue
        if not start.native_policy_digest or start.provider != "codex":
            continue
        try:
            validate_native_authority_facts(
                provider=start.provider,
                provider_version=start.native_provider_version,
                authority_digest=start.native_policy_digest,
                enabled=start.native_enabled,
                approval_gated=start.native_approval_gated,
                blocked=start.native_blocked,
            )
        except ValidationError:
            continue
        policy = codex_native_policy(
            bridge_attached="codex.agentbridge_mcp"
            in start.native_approval_gated,
        )
        capabilities = []
        for state, capability_ids in (
                ("enabled", start.native_enabled),
                ("approval_gated", start.native_approval_gated),
                ("blocked", start.native_blocked)):
            for capability_id in capability_ids:
                spec = NATIVE_CAPABILITIES.get(capability_id)
                if spec is None or spec.provider != start.provider:
                    capabilities = []
                    break
                capabilities.append({
                    "id": spec.id,
                    "label": spec.presentation_label,
                    "state": state,
                    "surface": spec.surface,
                    "effect": spec.effect,
                    "risk": spec.risk,
                })
                if not spec.presentation_label:
                    capabilities = []
                    break
            if not capabilities and capability_ids:
                break
        if not capabilities:
            continue
        rows.append({
            "run_id": run_id,
            "manager": start.manager_agent,
            "provider": start.provider,
            "provider_version": start.native_provider_version,
            "state": latest.state.value,
            "started_ns": start.meta.ns,
            "updated_ns": latest.meta.ns,
            "native_policy_digest": start.native_policy_digest,
            "authority_digest": start.provider_policy_digest,
            "schema_version": policy.schema_version,
            "inventory_complete": policy.inventory_complete,
            "enforcement_locus": policy.enforcement_locus,
            "evidence": policy.evidence,
            "enabled": list(start.native_enabled),
            "approval_gated": list(start.native_approval_gated),
            "blocked": list(start.native_blocked),
            "capabilities": capabilities,
        })
    rows.sort(key=lambda row: (row["updated_ns"], row["run_id"]))
    return rows if run_ids is not None else rows[-limit:]


@authed
def runtime_tasks(app, req, mesh) -> dict:
    chat_id = str(req.params.get("id") or "")
    limit = req.int_param("limit", 50, 1, 100)
    return {"tasks": contributor_rows(mesh, chat_id, limit=limit)}


@authed
def runtime_authority(app, req, mesh) -> dict:
    chat_id = str(req.params.get("id") or "")
    limit = req.int_param("limit", 50, 1, 100)
    return {"runs": authority_rows(mesh, chat_id, limit=limit)}


@authed
def runtime_authority_current(app, req, mesh) -> dict:
    chat_id = str(req.data.get("chat_id") or "")
    raw_ids = req.data.get("run_ids")
    if (not isinstance(raw_ids, list) or not raw_ids
            or len(raw_ids) > 20):
        raise ValidationError("run_ids must contain 1 to 20 active run ids")
    run_ids = tuple(str(run_id) for run_id in raw_ids)
    if (len(set(run_ids)) != len(run_ids)
            or any(not re.fullmatch(r"r-[0-9]+-[0-9a-f]{8}", run_id)
                   for run_id in run_ids)):
        raise ValidationError("invalid active run ids")
    return {"runs": authority_rows(
        mesh, chat_id, run_ids=run_ids, current_only=True,
    )}


GET = {
    "/api/mesh/runtime_tasks": runtime_tasks,
    "/api/mesh/runtime_authority": runtime_authority,
}
POST = {
    "/api/mesh/runtime_authority": runtime_authority_current,
}
