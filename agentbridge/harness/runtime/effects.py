"""Canonical one-use effect attempts for AgentBridge-owned execution.

The signed owner decision is the grant.  This ledger atomically consumes that
grant by creating one globally exclusive PREPARED effect record, then records
EXECUTING before calling AgentBridge-owned code.  A missing terminal after
EXECUTING is UNKNOWN and is never an automatic retry.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
import hashlib
import time
from typing import Callable, TypeVar

from ...core.jsonkit import canonical_json_bytes
from ...core.timekit import new_id, next_ns
from ..capabilities import BRIDGE_CAPABILITIES
from .authority import AuthorityError, authority
from .eventio import open_record, seal_record
from .models import (
    EffectRecord, EffectState, RecordKind, RecordMeta, RunRecord, RunState,
    RuntimeContractError, TaskRecord, TaskState,
)

__all__ = [
    "EffectClaim", "EffectLedger", "EffectLedgerError", "effect_claim_path",
    "effect_prefix", "effect_state_path",
]


class EffectLedgerError(RuntimeContractError):
    """An effect grant, transition, or authoritative claim is invalid."""


@dataclass(frozen=True, slots=True)
class EffectClaim:
    prepared: EffectRecord

    @property
    def grant_id(self) -> str:
        return self.prepared.grant_id


def effect_prefix(chat_id: str, run_id: str = "", call_id: str = "") -> str:
    base = f"chats/{chat_id}/runtime/effects"
    if run_id:
        base += f"/{run_id}"
    return f"{base}/{call_id}" if call_id else base


def effect_claim_path(chat_id: str, run_id: str, call_id: str) -> str:
    return f"{effect_prefix(chat_id, run_id, call_id)}/claim.json"


def effect_state_path(chat_id: str, run_id: str, call_id: str,
                      state_version: int) -> str:
    return f"{effect_prefix(chat_id, run_id, call_id)}/state-{state_version}.json"


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


_TERMINAL = {EffectState.COMMITTED, EffectState.REJECTED, EffectState.UNKNOWN}
T = TypeVar("T")


class EffectLedger:
    """Signed effect history with a transport-authoritative one-use claim."""

    RECOVERY_GRACE_S = 120.0

    def __init__(self, mesh) -> None:
        self.mesh = mesh

    @property
    def available(self) -> bool:
        probe = getattr(self.mesh.tx, "effect_claims_ready", None)
        return bool(callable(probe) and probe())

    def _create(self, path: str, record: EffectRecord, *,
                ask_envelope: dict | None = None,
                decision_envelope: dict | None = None) -> None:
        create = getattr(self.mesh.tx, "create_effect_doc", None)
        if not callable(create):
            raise EffectLedgerError("authoritative effect transition is unavailable")
        create(
            path, seal_record(self.mesh, record),
            ask_envelope=ask_envelope,
            decision_envelope=decision_envelope,
        )

    def claim(
        self,
        *,
        ask: dict,
        ask_envelope: dict,
        decision: dict,
        decision_envelope: dict,
        run: RunRecord,
        task: TaskRecord,
        capability_id: str,
        argument_digest: str,
    ) -> EffectClaim:
        """Consume one signed decision by exclusive PREPARED record creation."""
        if not self.available:
            raise EffectLedgerError(
                "one-use effect grants are unavailable on this transport")
        self._validate_parent(run, task, capability_id)
        self._validate_decision(ask, decision, run, task, capability_id,
                                argument_digest)
        now = time.time_ns()
        if now >= int(decision["expires_ns"]):
            raise EffectLedgerError("effect grant expired before claim")
        ns = max(next_ns(), int(decision["ns"]) + 1, task.meta.ns + 1)
        call_id = str(decision["call_id"])
        grant_id = str(decision["id"])
        epoch, _key = self.mesh.keys.ensure(
            run.meta.chat_id, self.mesh.snapshot(run.meta.chat_id),
        )
        meta = RecordMeta(
            schema_version=1, kind=RecordKind.EFFECT,
            id=new_id("effect-event", ns), ns=ns, actor=self.mesh.user,
            chat_id=run.meta.chat_id, signer=self.mesh.user,
            root_run_id=run.meta.root_run_id, run_id=run.meta.run_id,
            task_id=task.meta.task_id, call_id=call_id, key_epoch=epoch,
            policy_revision=run.meta.policy_revision,
            membership_epoch=run.meta.membership_epoch,
            ownership_epoch=run.meta.ownership_epoch,
            expires_ns=None,
        )
        idempotency_key = _digest({
            "v": 1, "grant_id": grant_id, "run_id": run.meta.run_id,
            "task_id": task.meta.task_id, "call_id": call_id,
            "capability_id": capability_id, "argument_digest": argument_digest,
        })
        prepared = EffectRecord(
            meta=meta, state=EffectState.PREPARED,
            capability_id=capability_id, argument_digest=argument_digest,
            idempotency_key=idempotency_key, grant_id=grant_id,
            lease_owner=f"{self.mesh.machine}:{self.mesh.user}",
            state_version=1, receipt_digest=None,
            cancellation_state="not_started",
        )
        path = effect_claim_path(meta.chat_id, meta.run_id or "", call_id)
        try:
            # For Supabase this is one global INSERT against (root,path). A
            # conflicting claimant has different signed bytes and is rejected.
            self._create(
                path, prepared, ask_envelope=ask_envelope,
                decision_envelope=decision_envelope,
            )
        except Exception as exc:
            raise EffectLedgerError(
                "effect grant was already claimed or claim is unavailable") from exc
        return EffectClaim(prepared)

    def execute(self, claim: EffectClaim, run: RunRecord, task: TaskRecord,
                action: Callable[[], T]) -> T:
        if not isinstance(claim, EffectClaim) or not callable(action):
            raise EffectLedgerError("effect execution needs a valid claim and action")
        prepared = claim.prepared
        if (prepared.meta.run_id != run.meta.run_id
                or prepared.meta.task_id != task.meta.task_id
                or prepared.meta.chat_id != run.meta.chat_id):
            raise EffectLedgerError("effect claim parent changed before execution")
        executing = self._transition(
            prepared, EffectState.EXECUTING,
            cancellation_state="dispatch_committed", receipt_digest=None,
        )
        try:
            result = action()
        except BaseException:
            # Once callback dispatch begins, an exception cannot prove the
            # mutation did not commit before its response was lost.
            with contextlib.suppress(Exception):
                self.mark_unknown(executing)
            raise
        self._transition(
            executing, EffectState.COMMITTED,
            cancellation_state="completed",
            receipt_digest=_digest({"outcome": "succeeded", "result": result}),
        )
        return result

    def mark_unknown(self, record: EffectRecord) -> EffectRecord:
        if record.state is not EffectState.EXECUTING:
            raise EffectLedgerError("only an executing effect can become unknown")
        return self._transition(
            record, EffectState.UNKNOWN, cancellation_state="outcome_unknown",
            receipt_digest=_digest({"outcome": "unknown"}),
        )

    def recover_incomplete(self) -> int:
        """Settle this host's abandoned effects without invoking them again."""
        recovered = 0
        groups: set[tuple[str, str, str]] = set()
        for snap in self.mesh.membership.chats_for():
            prefix = effect_prefix(snap.id)
            for path in self.mesh.tx.list_cached_docs(prefix):
                parts = path.split("/")
                if (len(parts) == 7 and parts[:1] == ["chats"]
                        and parts[2:4] == ["runtime", "effects"]):
                    groups.add((parts[1], parts[4], parts[5]))
        for chat_id, run_id, call_id in sorted(groups):
            try:
                history = self.read(chat_id, run_id, call_id)
                if not history:
                    continue
                last = history[-1]
                account = self.mesh.directory.get(self.mesh.user)
                if (last.meta.actor != self.mesh.user or account is None
                        or account.agent is None
                        or account.agent.machine != self.mesh.machine
                        or time.time_ns() - last.meta.ns
                        < int(self.RECOVERY_GRACE_S * 1e9)):
                    continue
                if last.state is EffectState.PREPARED:
                    self._transition(
                        last, EffectState.REJECTED,
                        cancellation_state="abandoned_before_dispatch",
                        receipt_digest=_digest({"outcome": "not_started"}),
                    )
                    recovered += 1
                elif last.state is EffectState.EXECUTING:
                    self.mark_unknown(last)
                    recovered += 1
            except Exception:
                continue
        return recovered

    def read(self, chat_id: str, run_id: str, call_id: str) -> list[EffectRecord]:
        try:
            snap = self.mesh.snapshot(chat_id)
            if not snap.is_member(self.mesh.user):
                raise AuthorityError("viewer is not a current room member")
            records = []
            prefix = effect_prefix(chat_id, run_id, call_id)
            paths = self.mesh.tx.list_docs(prefix)
            for path in paths:
                if not (path.endswith("/claim.json")
                        or path.endswith("/state-2.json")
                        or path.endswith("/state-3.json")):
                    continue
                raw = self.mesh.tx.get_doc(path, default=None)
                if raw is None:
                    continue
                record = open_record(
                    self.mesh, snap, raw, RecordKind.EFFECT, EffectRecord.from_dict,
                )
                if (record.meta.chat_id != chat_id
                        or record.meta.run_id != run_id
                        or record.meta.call_id != call_id):
                    continue
                expected_path = (effect_claim_path(chat_id, run_id, call_id)
                                 if record.state_version == 1 else
                                 effect_state_path(
                                     chat_id, run_id, call_id,
                                     record.state_version))
                if path != expected_path:
                    continue
                records.append(record)
            history = self._fold(records)
            if history:
                from .permissions import open_effect_grant

                ask, decision = open_effect_grant(
                    self.mesh, chat_id=chat_id, agent=history[0].meta.actor,
                    grant_id=history[0].grant_id,
                    ask_raw=self.mesh.tx.get_doc(
                        f"{prefix}/grant-ask.json", default=None),
                    decision_raw=self.mesh.tx.get_doc(
                        f"{prefix}/grant-decision.json", default=None),
                )
                if (ask["tool"] != history[0].capability_id
                        or ask["input_digest"] != history[0].argument_digest
                        or ask["run_id"] != run_id
                        or ask["call_id"] != call_id
                        or decision["id"] != history[0].grant_id
                        or decision["verdict"] not in ("allow", "always")):
                    raise EffectLedgerError(
                        "effect history has no matching owner-signed grant")
            return history
        except EffectLedgerError:
            raise
        except Exception as exc:
            raise EffectLedgerError("effect history is unavailable") from exc

    def _transition(self, previous: EffectRecord, state: EffectState, *,
                    cancellation_state: str,
                    receipt_digest: str | None) -> EffectRecord:
        allowed = {
            EffectState.PREPARED: {EffectState.EXECUTING, EffectState.REJECTED},
            EffectState.EXECUTING: _TERMINAL,
        }
        if state not in allowed.get(previous.state, set()):
            raise EffectLedgerError("invalid effect state transition")
        history = self.read(
            previous.meta.chat_id, previous.meta.run_id or "",
            previous.meta.call_id or "",
        )
        if not history or history[-1] != previous:
            raise EffectLedgerError("effect state changed before transition")
        ns = max(next_ns(), previous.meta.ns + 1)
        record = replace(
            previous,
            meta=replace(previous.meta, id=new_id("effect-event", ns), ns=ns),
            state=state, state_version=previous.state_version + 1,
            receipt_digest=receipt_digest,
            cancellation_state=cancellation_state,
        )
        path = effect_state_path(
            record.meta.chat_id, record.meta.run_id or "",
            record.meta.call_id or "", record.state_version,
        )
        try:
            self._create(path, record)
        except Exception as exc:
            raise EffectLedgerError("effect transition could not be committed") from exc
        return record

    def _validate_parent(self, run: RunRecord, task: TaskRecord,
                         capability_id: str) -> None:
        if capability_id not in BRIDGE_CAPABILITIES:
            raise EffectLedgerError("effect capability is not in the bridge catalog")
        if (not isinstance(run, RunRecord) or run.state is not RunState.RUNNING
                or run.manager_agent != self.mesh.user
                or not isinstance(task, TaskRecord)
                or task.state is not TaskState.ACTIVE
                or task.assigned_agent != self.mesh.user
                or task.meta.run_id != run.meta.run_id
                or run.active_task_ids != (task.meta.task_id,)
                or task.meta.chat_id != run.meta.chat_id
                or task.parent_task_id is not None):
            raise EffectLedgerError("effect parent run or root task is not active")
        account = self.mesh.directory.get(self.mesh.user)
        if (account is None or account.agent is None
                or account.agent.machine != self.mesh.machine):
            raise EffectLedgerError("effect agent is not hosted on this machine")
        if capability_id not in run.capability_ceiling:
            raise EffectLedgerError("effect capability is outside the signed run ceiling")
        try:
            current = authority(self.mesh, self.mesh.user, run.meta.chat_id)
        except AuthorityError as exc:
            raise EffectLedgerError("current effect authority is unavailable") from exc
        if str(current["owner"]) != run.responsible_member:
            raise EffectLedgerError("effect responsible member changed")
        for name in ("membership_epoch", "ownership_epoch", "policy_revision"):
            if int(current[name]) != int(getattr(run.meta, name)):
                raise EffectLedgerError(f"effect authority has stale {name}")
        try:
            from .runs import RunLedger
            from .tasks import TaskLedger

            run_ledger = RunLedger(
                self.mesh, register_outbox=False, fresh_reads=True)
            task_ledger = TaskLedger(
                self.mesh, run_ledger, register_outbox=False, fresh_reads=True)
            run_history = run_ledger.read(
                run.meta.chat_id, run.meta.run_id or "")
            task_history = task_ledger.read(
                task.meta.chat_id, task.meta.run_id or "",
                task.meta.task_id or "")
        except Exception as exc:
            raise EffectLedgerError(
                "signed effect parent records are unavailable") from exc
        if run_history != [run] or not task_history or task_history[0] != task \
                or task_history[-1].state is not TaskState.ACTIVE:
            raise EffectLedgerError(
                "signed effect parent records changed before execution")

    @staticmethod
    def _validate_decision(ask: dict, decision: dict, run: RunRecord,
                           task: TaskRecord, capability_id: str,
                           argument_digest: str) -> None:
        if (not isinstance(ask, dict) or not isinstance(decision, dict)
                or decision.get("verdict") not in ("allow", "always")
                or decision.get("one_use") is not True
                or decision.get("ask_id") != ask.get("id")
                or decision.get("ask_digest") != _digest(ask)
                or decision.get("run_id") != run.meta.run_id
                or decision.get("run_id") != ask.get("run_id")
                or decision.get("call_id") != ask.get("call_id")
                or decision.get("chat_id") != run.meta.chat_id
                or decision.get("chat_id") != ask.get("chat_id")
                or decision.get("agent") != run.manager_agent
                or decision.get("agent") != ask.get("agent")
                or decision.get("owner") != run.responsible_member
                or decision.get("owner") != ask.get("owner")
                or any(decision.get(name) != ask.get(name)
                       for name in ("key_epoch", "membership_epoch",
                                    "ownership_epoch", "policy_revision"))
                or any(int(decision.get(name, -1))
                       != int(getattr(run.meta, name))
                       for name in ("membership_epoch", "ownership_epoch",
                                    "policy_revision"))
                or ask.get("kind") != "permission"
                or ask.get("tool") != capability_id
                or ask.get("input_digest") != argument_digest):
            raise EffectLedgerError("permission decision is not an effect grant")
        if not isinstance(argument_digest, str) or len(argument_digest) != 64:
            raise EffectLedgerError("effect argument digest must be SHA-256")
        if task.meta.task_id is None:
            raise EffectLedgerError("effect grant needs a root task")

    @staticmethod
    def _fold(records: list[EffectRecord]) -> list[EffectRecord]:
        ordered = sorted(records, key=lambda record: (record.meta.ns, record.meta.id))
        if not ordered:
            return []
        first = ordered[0]
        if (first.state is not EffectState.PREPARED or first.state_version != 1
                or first.capability_id not in BRIDGE_CAPABILITIES
                or first.receipt_digest is not None):
            return []
        out = [first]
        fixed = (
            "capability_id", "argument_digest", "idempotency_key", "grant_id",
            "lease_owner",
        )
        transitions = {
            EffectState.PREPARED: {EffectState.EXECUTING, EffectState.REJECTED},
            EffectState.EXECUTING: _TERMINAL,
        }
        for record in ordered[1:]:
            previous = out[-1]
            if (previous.state in _TERMINAL
                    or record.state not in transitions.get(previous.state, set())
                    or record.state_version != previous.state_version + 1
                    or any(getattr(record, name) != getattr(first, name)
                           for name in fixed)
                    or record.meta.root_run_id != first.meta.root_run_id
                    or record.meta.run_id != first.meta.run_id
                    or record.meta.task_id != first.meta.task_id
                    or record.meta.call_id != first.meta.call_id
                    or record.meta.actor != first.meta.actor):
                raise EffectLedgerError("effect history contains competing transitions")
            if ((record.state is EffectState.EXECUTING
                 and record.receipt_digest is not None)
                    or (record.state in _TERMINAL
                        and (not isinstance(record.receipt_digest, str)
                             or len(record.receipt_digest) != 64))):
                raise EffectLedgerError("effect receipt does not match its state")
            for name in ("key_epoch", "policy_revision", "membership_epoch",
                         "ownership_epoch", "expires_ns"):
                if getattr(record.meta, name) != getattr(first.meta, name):
                    raise EffectLedgerError(
                        "effect history changed its authority binding")
            out.append(record)
        return out
