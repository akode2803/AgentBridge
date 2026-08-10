"""Canonical same-room handoff authority and depth-one child lifecycles.

R127 established paired offers and destination decisions. R129 adds the
manager-retained agent-tool chain without broadening capabilities: a source
authorizes one exact acceptance, and destination-authored active/terminal
events are paired with child-task events. Provider execution remains owned by
the orchestration coordinator, not this immutable ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, replace

from ...core.errors import ValidationError
from ...core.models import UserKind
from ...core.timekit import new_id, next_ns
from .authority import AuthorityError, authority
from .eventio import deliver_immutable, open_record, seal_record
from .models import (
    HandoffRecord, HandoffState, HandoffType, RecordKind, RecordMeta,
    RunRecord, RunState, RuntimeContractError, TaskRecord, TaskState,
    canonical_json_bytes,
)
from .runs import run_event_path, run_prefix
from .tasks import TaskLedger, task_event_path, task_prefix

__all__ = [
    "HandoffLedger", "HandoffLedgerError", "HandoffView",
    "handoff_event_path", "handoff_prefix",
]


class HandoffLedgerError(RuntimeContractError):
    """A canonical handoff is unauthorized, malformed, or ambiguous."""


@dataclass(frozen=True, slots=True)
class HandoffView:
    task: TaskRecord
    events: tuple[HandoffRecord, ...]


_SOURCE_STATES = {
    HandoffState.OFFERED, HandoffState.AUTHORIZED,
    HandoffState.CONSUMED, HandoffState.TIMED_OUT,
}
_DESTINATION_STATES = {
    HandoffState.ACCEPTED, HandoffState.ACTIVE, HandoffState.RETURNED,
    HandoffState.DECLINED, HandoffState.STOPPED, HandoffState.INTERRUPTED,
}
_EXECUTION_TERMINALS = {
    HandoffState.RETURNED, HandoffState.STOPPED, HandoffState.INTERRUPTED,
}
_MAX_RESULT = 8_000


def _result_payload(record: HandoffRecord) -> dict:
    try:
        value = json.loads(record.result or "")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _causal_result(after: str, **values) -> str:
    return json.dumps({"after": after, **values}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def handoff_prefix(chat_id: str, run_id: str = "", handoff_id: str = "") -> str:
    base = f"chats/{chat_id}/runtime/handoffs"
    if run_id:
        base += f"/{run_id}"
    return f"{base}/{handoff_id}" if handoff_id else base


def handoff_event_path(chat_id: str, run_id: str, handoff_id: str,
                       record_id: str) -> str:
    return f"{handoff_prefix(chat_id, run_id, handoff_id)}/{record_id}.json"


def _digest(*, chat_id: str, run_id: str, parent_task_id: str,
            child_task_id: str, handoff_id: str, handoff_type: HandoffType,
            source: str, destination: str, source_auth: dict,
            destination_auth: dict, capabilities: tuple[str, ...]) -> str:
    manifest = {
        "version": 1, "history_policy": "summary_with_anchors",
        "chat_id": chat_id, "run_id": run_id,
        "parent_task_id": parent_task_id, "child_task_id": child_task_id,
        "handoff_id": handoff_id, "handoff_type": handoff_type.value,
        "source_agent": source, "destination_agent": destination,
        "source_policy_revision": int(source_auth["policy_revision"]),
        "source_ownership_epoch": int(source_auth["ownership_epoch"]),
        "destination_policy_revision": int(destination_auth["policy_revision"]),
        "destination_ownership_epoch": int(destination_auth["ownership_epoch"]),
        "requested_capabilities": list(capabilities),
        "body_transfer": "destination_authorized_room_view_only",
    }
    return hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()


class HandoffLedger:
    """Write and validate one chat's immutable same-room handoff stream."""

    OUTBOX_KIND = "runtime-handoff-event"
    OPEN_PATH = "runtime/handoff-open"

    def __init__(self, mesh, task_ledger: TaskLedger) -> None:
        self.mesh = mesh
        self.task_ledger = task_ledger
        self.run_ledger = task_ledger.run_ledger
        self._lock = threading.RLock()
        mesh.outbox.handlers[self.OUTBOX_KIND] = self._deliver
        mesh.outbox.dead_hooks[self.OUTBOX_KIND] = self._dead

    def _dead(self, _target: str, payload: dict) -> None:
        self._discard_open(payload)

    def _deliver(self, _target: str, payload: dict) -> None:
        docs = payload.get("docs")
        if (not isinstance(docs, list) or not docs
                or any(not isinstance(item, dict)
                       or not isinstance(item.get("path"), str)
                       or not isinstance(item.get("doc"), dict)
                       for item in docs)):
            raise ValidationError("runtime handoff outbox payload is malformed")
        expected_target = handoff_prefix(
            str(payload.get("chat_id") or ""),
            str(payload.get("run_id") or ""),
            str(payload.get("handoff_id") or ""),
        )
        if _target != expected_target:
            raise ValidationError("runtime handoff outbox target is malformed")
        for item in docs:
            try:
                meta = RecordMeta.from_dict(item["doc"].get("meta"))
            except (RuntimeContractError, TypeError, ValueError) as exc:
                raise ValidationError(
                    "runtime handoff outbox metadata is malformed",
                ) from exc
            expected_path = (
                task_event_path(meta.chat_id, meta.run_id or "",
                                meta.task_id or "", meta.id)
                if meta.kind is RecordKind.TASK else
                handoff_event_path(meta.chat_id, meta.run_id or "",
                                   meta.call_id or "", meta.id)
                if meta.kind is RecordKind.HANDOFF else ""
            )
            if (item["path"] != expected_path
                    or meta.chat_id != payload.get("chat_id")
                    or meta.run_id != payload.get("run_id")
                    or meta.call_id != payload.get("handoff_id")):
                raise ValidationError("runtime handoff outbox routing is malformed")
        phase = payload.get("phase")
        metas = [RecordMeta.from_dict(item["doc"]["meta"]) for item in docs]
        if phase == "offer":
            if (len(metas) != 2
                    or {meta.kind for meta in metas}
                       != {RecordKind.TASK, RecordKind.HANDOFF}
                    or len({(meta.chat_id, meta.run_id, meta.task_id,
                             meta.call_id, meta.ns, meta.expires_ns)
                            for meta in metas}) != 1):
                raise ValidationError("runtime handoff offer pair is malformed")
        elif phase in {"decision", "transition"}:
            if len(metas) != 1 or metas[0].kind is not RecordKind.HANDOFF:
                raise ValidationError("runtime handoff decision is malformed")
        elif phase == "lifecycle":
            if (len(metas) != 2
                    or {meta.kind for meta in metas}
                       != {RecordKind.TASK, RecordKind.HANDOFF}
                    or len({(meta.chat_id, meta.run_id, meta.task_id,
                             meta.call_id, meta.ns, meta.expires_ns)
                            for meta in metas}) != 1):
                raise ValidationError("runtime handoff lifecycle pair is malformed")
        else:
            raise ValidationError("runtime handoff outbox phase is malformed")
        for item in docs:
            deliver_immutable(self.mesh.tx, item["path"], item["doc"])
        self._delivered(payload)

    def _attempt(self, seq: int, target: str, payload: dict) -> None:
        try:
            self._deliver(target, payload)
        except ValidationError as exc:
            self.mesh.store.outbox_dead(seq, f"{type(exc).__name__}: {exc}")
            self._discard_open(payload)
            raise HandoffLedgerError(
                "canonical handoff event conflicts with its ledger",
            ) from exc
        except Exception as exc:
            self.mesh.store.outbox_retry(
                seq, f"{type(exc).__name__}: {exc}", 1.0,
            )
            self.mesh.outbox.notify()
        else:
            try:
                self.mesh.store.outbox_done(seq)
            except Exception:
                self.mesh.outbox.notify()

    def _delivered(self, payload: dict) -> None:
        handoff_id = str(payload.get("handoff_id") or "")
        record_id = str(payload.get("record_id") or "")
        with self._lock:
            opened = self._opened()
            entry = opened.get(handoff_id)
            if isinstance(entry, dict) and entry.get("record_id") == record_id:
                opened.pop(handoff_id, None)
                self.mesh.store.cache_doc(self.OPEN_PATH, opened)

    def _discard_open(self, payload: dict) -> None:
        handoff_id = str(payload.get("handoff_id") or "")
        record_id = str(payload.get("record_id") or "")
        with self._lock:
            opened = self._opened()
            entry = opened.get(handoff_id)
            if isinstance(entry, dict) and entry.get("record_id") == record_id:
                opened.pop(handoff_id, None)
                self.mesh.store.cache_doc(self.OPEN_PATH, opened)

    def _sealed(self, record) -> dict:
        try:
            return seal_record(self.mesh, record)
        except RuntimeContractError as exc:
            raise HandoffLedgerError(str(exc)) from exc

    def offer(self, *, chat_id: str, run_id: str, parent_task_id: str,
              destination_agent: str, objective: str, reason: str,
              success_criteria: tuple[str, ...],
              requested_capabilities: tuple[str, ...] = (),
              handoff_type: HandoffType = HandoffType.AGENT_TOOL,
              timeout_s: float = 180.0) -> HandoffView:
        with self._lock:
            for name, value in {
                "chat_id": chat_id, "run_id": run_id,
                "parent_task_id": parent_task_id,
                "destination_agent": destination_agent,
                "objective": objective, "reason": reason,
            }.items():
                if not isinstance(value, str) or not value.strip():
                    raise HandoffLedgerError(f"{name} must be a non-empty string")
            if not isinstance(handoff_type, HandoffType):
                raise HandoffLedgerError("handoff_type must be a HandoffType")
            for name, values in {
                "success_criteria": success_criteria,
                "requested_capabilities": requested_capabilities,
            }.items():
                if (not isinstance(values, tuple)
                        or any(not isinstance(item, str) or not item.strip()
                               for item in values)
                        or len(set(values)) != len(values)):
                    raise HandoffLedgerError(
                        f"{name} must be an immutable unique string tuple",
                    )
            if not success_criteria:
                raise HandoffLedgerError("success_criteria must not be empty")
            if destination_agent == self.mesh.user:
                raise HandoffLedgerError("a handoff destination must be distinct")
            if (isinstance(timeout_s, bool)
                    or not isinstance(timeout_s, (int, float))
                    or not math.isfinite(timeout_s) or timeout_s <= 0):
                raise HandoffLedgerError("handoff timeout must be positive")
            account = self.mesh.directory.get(destination_agent)
            if not account or account.kind is not UserKind.AGENT or not account.active:
                raise HandoffLedgerError("handoff destination must be an active agent")
            source_auth = authority(self.mesh, self.mesh.user, chat_id)
            destination_auth = authority(self.mesh, destination_agent, chat_id)
            parents = self.task_ledger.read(chat_id, run_id, parent_task_id)
            runs = self.run_ledger.read(chat_id, run_id)
            if (len(parents) != 1 or parents[0].state is not TaskState.ACTIVE
                    or len(runs) != 1 or runs[0].state is not RunState.RUNNING
                    or parents[0].assigned_agent != self.mesh.user
                    or parents[0].parent_task_id is not None):
                raise HandoffLedgerError("handoff parent is not an active root task")
            run = runs[0]
            if any(view.task.parent_task_id == parent_task_id
                   for view in self.read(chat_id, run_id)):
                raise HandoffLedgerError(
                    "this root task already has its one allowed child",
                )
            if any(
                isinstance(entry, dict)
                and entry.get("parent_task_id") == parent_task_id
                for entry in self._opened().values()
            ):
                raise HandoffLedgerError(
                    "this root task already has a pending child offer",
                )
            capabilities = tuple(requested_capabilities)
            if not set(capabilities).issubset(run.capability_ceiling):
                raise HandoffLedgerError("requested capabilities exceed the root ceiling")

            ns = max(next_ns(), parents[0].meta.ns + 1)
            expires_ns = ns + max(1, int(timeout_s * 1_000_000_000))
            handoff_id = new_id("handoff", ns)
            child_task_id = new_id("task", ns)
            epoch, _key = self.mesh.keys.ensure(chat_id, self.mesh.snapshot(chat_id))
            digest = _digest(
                chat_id=chat_id, run_id=run_id,
                parent_task_id=parent_task_id, child_task_id=child_task_id,
                handoff_id=handoff_id, handoff_type=handoff_type,
                source=self.mesh.user, destination=destination_agent,
                source_auth=source_auth, destination_auth=destination_auth,
                capabilities=capabilities,
            )
            common = dict(
                schema_version=1, ns=ns, actor=self.mesh.user,
                chat_id=chat_id, signer=self.mesh.user,
                root_run_id=run.meta.root_run_id, run_id=run_id,
                task_id=child_task_id, call_id=handoff_id, key_epoch=epoch,
                policy_revision=int(source_auth["policy_revision"]),
                membership_epoch=int(source_auth["membership_epoch"]),
                ownership_epoch=int(source_auth["ownership_epoch"]),
                expires_ns=expires_ns,
            )
            task = TaskRecord(
                meta=RecordMeta(kind=RecordKind.TASK,
                                id=new_id("task-event", ns), **common),
                state=TaskState.OFFERED, objective=" ".join(objective.split())[:500],
                assigned_agent=destination_agent, assigning_agent=self.mesh.user,
                responsible_member=str(destination_auth["owner"]),
                parent_task_id=parent_task_id,
                success_criteria=tuple(success_criteria), context_digest=digest,
                grant_ids=(), dependency_ids=(),
                progress=f"Awaiting @{destination_agent}", result=None,
                return_to_agent=self.mesh.user,
            )
            handoff = HandoffRecord(
                meta=RecordMeta(kind=RecordKind.HANDOFF,
                                id=new_id("handoff-event", ns), **common),
                state=HandoffState.OFFERED, handoff_type=handoff_type,
                source_agent=self.mesh.user,
                destination_agent=destination_agent,
                source_owner=str(source_auth["owner"]),
                destination_owner=str(destination_auth["owner"]),
                initiating_member=str(source_auth["owner"]),
                reason=" ".join(reason.split())[:500], context_digest=digest,
                requested_capabilities=capabilities, transferred_grant_ids=(),
                return_to_agent=self.mesh.user, result=None,
            )
            docs = [
                {"path": task_event_path(chat_id, run_id, child_task_id,
                                         task.meta.id),
                 "doc": self._sealed(task)},
                {"path": handoff_event_path(chat_id, run_id, handoff_id,
                                            handoff.meta.id),
                 "doc": self._sealed(handoff)},
            ]
            payload = {
                "docs": docs, "chat_id": chat_id, "run_id": run_id,
                "handoff_id": handoff_id, "record_id": handoff.meta.id,
                "phase": "offer",
            }
            opened = self._opened()
            opened[handoff_id] = {
                "record_id": handoff.meta.id, "record": handoff.to_dict(),
                "parent_task_id": parent_task_id,
            }
            seq = self.mesh.store.cache_doc_and_outbox_add(
                self.OPEN_PATH, opened, self.OUTBOX_KIND,
                handoff_prefix(chat_id, run_id, handoff_id), payload,
            )
        self._attempt(seq, handoff_prefix(chat_id, run_id, handoff_id), payload)
        return HandoffView(task, (handoff,))

    def decide(self, *, chat_id: str, run_id: str, handoff_id: str,
               accept: bool, result: str = "") -> HandoffRecord:
        with self._lock:
            if not isinstance(accept, bool) or not isinstance(result, str):
                raise HandoffLedgerError("handoff decision input is malformed")
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.destination_agent:
                raise HandoffLedgerError("only the destination may decide a handoff")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                wanted = HandoffState.ACCEPTED if accept else HandoffState.DECLINED
                if pending.state is wanted:
                    return pending
                raise HandoffLedgerError("handoff already has a pending decision")
            if time.time_ns() >= int(offer.meta.expires_ns or 0):
                raise HandoffLedgerError("handoff offer has expired")
            if not self._parent_open(view):
                raise HandoffLedgerError("handoff parent is no longer active")
            if len(view.events) > 1:
                decision = view.events[1]
                wanted = HandoffState.ACCEPTED if accept else HandoffState.DECLINED
                if decision.state is wanted:
                    return decision
                raise HandoffLedgerError("handoff already has a different decision")
            return self._decision(offer, HandoffState.ACCEPTED if accept
                                  else HandoffState.DECLINED, result)

    def timeout(self, *, chat_id: str, run_id: str,
                handoff_id: str) -> HandoffRecord:
        with self._lock:
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.source_agent:
                raise HandoffLedgerError("only the source may time out a handoff")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                if pending.state is HandoffState.TIMED_OUT:
                    return pending
                raise HandoffLedgerError("handoff already has a pending transition")
            if time.time_ns() < int(offer.meta.expires_ns or 0):
                raise HandoffLedgerError("handoff offer has not expired")
            if not self._parent_open(view):
                raise HandoffLedgerError("handoff parent is no longer active")
            if len(view.events) > 1:
                decision = view.events[1]
                if decision.state is HandoffState.TIMED_OUT:
                    return decision
                raise HandoffLedgerError("handoff already has a decision")
            return self._decision(offer, HandoffState.TIMED_OUT,
                                  "Destination did not answer before expiry")

    def authorize(self, *, chat_id: str, run_id: str, handoff_id: str,
                  execution_timeout_s: float = 1800.0) -> HandoffRecord:
        """Source-authorize one exact in-window destination acceptance."""
        with self._lock:
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.source_agent:
                raise HandoffLedgerError("only the source may authorize a handoff")
            if offer.handoff_type is not HandoffType.AGENT_TOOL:
                raise HandoffLedgerError("active execution handoff is not enabled")
            if offer.requested_capabilities:
                raise HandoffLedgerError("agent-tool execution requires zero capabilities")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                if pending.state is HandoffState.AUTHORIZED:
                    return pending
                raise HandoffLedgerError("handoff already has a pending transition")
            if (isinstance(execution_timeout_s, bool)
                    or not isinstance(execution_timeout_s, (int, float))
                    or not math.isfinite(execution_timeout_s)
                    or execution_timeout_s <= 0):
                raise HandoffLedgerError("execution timeout must be positive")
            if len(view.events) >= 3:
                existing = view.events[2]
                if existing.state is HandoffState.AUTHORIZED:
                    return existing
                raise HandoffLedgerError("handoff already has a terminal decision")
            if len(view.events) != 2 or view.events[1].state is not HandoffState.ACCEPTED:
                raise HandoffLedgerError("handoff has not been accepted")
            accepted = view.events[1]
            if time.time_ns() >= int(offer.meta.expires_ns or 0):
                raise HandoffLedgerError("handoff acceptance window has expired")
            if not self._parent_open(view):
                raise HandoffLedgerError("handoff parent is no longer active")
            expires_ns = max(time.time_ns(), accepted.meta.ns) + max(
                1, int(execution_timeout_s * 1_000_000_000),
            )
            return self._transition(
                accepted, HandoffState.AUTHORIZED,
                _causal_result(accepted.meta.id), expires_ns=expires_ns,
            )

    def activate(self, *, chat_id: str, run_id: str, handoff_id: str,
                 manifest: dict) -> HandoffRecord:
        """Destination-publish the frozen disclosure manifest before work."""
        with self._lock:
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.destination_agent:
                raise HandoffLedgerError("only the destination may activate a handoff")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                if pending.state is HandoffState.ACTIVE:
                    return pending
                raise HandoffLedgerError("handoff already has a pending transition")
            if len(view.events) >= 4:
                existing = view.events[3]
                if existing.state is HandoffState.ACTIVE:
                    return existing
                raise HandoffLedgerError("handoff cannot become active")
            if (len(view.events) != 3
                    or view.events[1].state is not HandoffState.ACCEPTED
                    or view.events[2].state is not HandoffState.AUTHORIZED):
                raise HandoffLedgerError("handoff is not authorized")
            current = authority(self.mesh, self.mesh.user, chat_id)
            if (view.events[1].meta.policy_revision
                    != int(current["policy_revision"])):
                raise HandoffLedgerError(
                    "destination policy changed before activation",
                )
            authorized = view.events[2]
            if time.time_ns() >= int(authorized.meta.expires_ns or 0):
                raise HandoffLedgerError("handoff execution window has expired")
            if not self._parent_open(view):
                raise HandoffLedgerError("handoff parent is no longer active")
            if not isinstance(manifest, dict) or not manifest:
                raise HandoffLedgerError("handoff disclosure manifest is required")
            result = _causal_result(authorized.meta.id, manifest=manifest)
            if len(result.encode("utf-8")) > _MAX_RESULT:
                raise HandoffLedgerError("handoff disclosure manifest is too large")
            return self._lifecycle(
                view, HandoffState.ACTIVE, TaskState.ACTIVE, result,
                progress=f"@{offer.destination_agent} is working", task_result=None,
            )

    def return_result(self, *, chat_id: str, run_id: str, handoff_id: str,
                      contribution: str, prompt_digest: str) -> HandoffRecord:
        """Destination-publish one bounded, non-posting specialist result."""
        with self._lock:
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.destination_agent:
                raise HandoffLedgerError("only the destination may return a result")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                if pending.state is HandoffState.RETURNED:
                    return pending
                raise HandoffLedgerError("handoff already has a pending transition")
            if len(view.events) >= 5:
                existing = view.events[4]
                if existing.state is HandoffState.RETURNED:
                    return existing
                raise HandoffLedgerError("handoff already has a terminal outcome")
            if len(view.events) != 4 or view.events[3].state is not HandoffState.ACTIVE:
                raise HandoffLedgerError("handoff is not active")
            active = view.events[3]
            current = authority(self.mesh, self.mesh.user, chat_id)
            if active.meta.policy_revision != int(current["policy_revision"]):
                raise HandoffLedgerError(
                    "destination policy changed during execution",
                )
            authorized = view.events[2]
            if time.time_ns() >= int(authorized.meta.expires_ns or 0):
                raise HandoffLedgerError("handoff execution window has expired")
            body = str(contribution or "").strip()
            digest = str(prompt_digest or "").strip()
            if not body or not digest:
                raise HandoffLedgerError("handoff result is incomplete")
            result = _causal_result(active.meta.id, contribution=body,
                                    prompt_digest=digest)
            if len(result.encode("utf-8")) > _MAX_RESULT:
                raise HandoffLedgerError("handoff result is too large")
            if not self._parent_open(view):
                raise HandoffLedgerError("handoff parent is no longer active")
            return self._lifecycle(
                view, HandoffState.RETURNED, TaskState.RETURNED, result,
                progress=f"Returned to @{offer.source_agent}", task_result=body,
            )

    def interrupt(self, *, chat_id: str, run_id: str, handoff_id: str,
                  reason: str) -> HandoffRecord:
        """Destination-settle an invocation whose completion is ambiguous."""
        with self._lock:
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.destination_agent:
                raise HandoffLedgerError("only the destination may interrupt a handoff")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                if pending.state is HandoffState.INTERRUPTED:
                    return pending
                raise HandoffLedgerError("handoff already has a pending transition")
            if (view.events[-1].state in _EXECUTION_TERMINALS
                    and view.events[-1].state is HandoffState.INTERRUPTED):
                return view.events[-1]
            if len(view.events) >= 5:
                existing = view.events[4]
                if existing.state is HandoffState.INTERRUPTED:
                    return existing
                raise HandoffLedgerError("handoff already has a terminal outcome")
            if (len(view.events) == 3
                    and view.events[2].state is HandoffState.AUTHORIZED):
                predecessor = view.events[2]
            elif (len(view.events) == 4
                  and view.events[3].state is HandoffState.ACTIVE):
                predecessor = view.events[3]
            else:
                raise HandoffLedgerError("handoff is not executable")
            note = " ".join(str(reason or "Interrupted").split())[:500]
            return self._lifecycle(
                view, HandoffState.INTERRUPTED, TaskState.INTERRUPTED,
                _causal_result(predecessor.meta.id, reason=note),
                progress="Interrupted", task_result="Interrupted",
            )

    def consume(self, *, chat_id: str, run_id: str,
                handoff_id: str) -> HandoffRecord:
        """Source acknowledge that the manager received one returned result."""
        with self._lock:
            view = self._exact_offer(chat_id, run_id, handoff_id)
            offer = view.events[0]
            if self.mesh.user != offer.source_agent:
                raise HandoffLedgerError("only the source may consume a result")
            pending = self._pending_record(handoff_id)
            if pending is not None:
                if pending.state is HandoffState.CONSUMED:
                    return pending
                raise HandoffLedgerError("handoff already has a pending transition")
            if len(view.events) >= 6:
                existing = view.events[5]
                if existing.state is HandoffState.CONSUMED:
                    return existing
                raise HandoffLedgerError("handoff result cannot be consumed")
            if len(view.events) != 5 or view.events[4].state is not HandoffState.RETURNED:
                raise HandoffLedgerError("handoff has no returned result")
            return self._transition(
                view.events[4], HandoffState.CONSUMED,
                _causal_result(view.events[4].meta.id), expires_ns=None,
            )

    def _decision(self, offer: HandoffRecord, state: HandoffState,
                  result: str) -> HandoffRecord:
        auth = authority(self.mesh, self.mesh.user, offer.meta.chat_id)
        ns = max(next_ns(), offer.meta.ns + 1)
        epoch, _key = self.mesh.keys.ensure(
            offer.meta.chat_id, self.mesh.snapshot(offer.meta.chat_id),
        )
        record = replace(
            offer,
            meta=replace(
                offer.meta, id=new_id("handoff-event", ns), ns=ns,
                actor=self.mesh.user, signer=self.mesh.user, key_epoch=epoch,
                policy_revision=int(auth["policy_revision"]),
                membership_epoch=int(auth["membership_epoch"]),
                ownership_epoch=int(auth["ownership_epoch"]),
                expires_ns=(offer.meta.expires_ns
                            if state is not HandoffState.TIMED_OUT else None),
            ),
            state=state, result=(" ".join(result.split())[:500] or state.value),
        )
        path = handoff_event_path(
            offer.meta.chat_id, offer.meta.run_id or "", offer.meta.call_id or "",
            record.meta.id,
        )
        payload = {
            "docs": [{"path": path, "doc": self._sealed(record)}],
            "chat_id": offer.meta.chat_id, "run_id": offer.meta.run_id,
            "handoff_id": offer.meta.call_id, "record_id": record.meta.id,
            "phase": "decision",
        }
        opened = self._opened()
        opened[offer.meta.call_id or ""] = {
            "record_id": record.meta.id, "record": record.to_dict(),
        }
        seq = self.mesh.store.cache_doc_and_outbox_add(
            self.OPEN_PATH, opened, self.OUTBOX_KIND,
            handoff_prefix(offer.meta.chat_id, offer.meta.run_id or "",
                           offer.meta.call_id or ""), payload,
        )
        self._attempt(seq, handoff_prefix(
            offer.meta.chat_id, offer.meta.run_id or "", offer.meta.call_id or "",
        ), payload)
        return record

    def _transition(self, predecessor: HandoffRecord, state: HandoffState,
                    result: str, *, expires_ns: int | None) -> HandoffRecord:
        auth = authority(self.mesh, self.mesh.user, predecessor.meta.chat_id)
        ns = max(next_ns(), predecessor.meta.ns + 1)
        epoch, _key = self.mesh.keys.ensure(
            predecessor.meta.chat_id,
            self.mesh.snapshot(predecessor.meta.chat_id),
        )
        record = replace(
            predecessor,
            meta=replace(
                predecessor.meta, id=new_id("handoff-event", ns), ns=ns,
                actor=self.mesh.user, signer=self.mesh.user, key_epoch=epoch,
                policy_revision=int(auth["policy_revision"]),
                membership_epoch=int(auth["membership_epoch"]),
                ownership_epoch=int(auth["ownership_epoch"]),
                expires_ns=expires_ns,
            ),
            state=state, result=result,
        )
        self._publish_records(predecessor, [record], phase="transition")
        return record

    def _lifecycle(self, view: HandoffView, handoff_state: HandoffState,
                   task_state: TaskState, result: str, *, progress: str,
                   task_result: str | None) -> HandoffRecord:
        offer = view.events[0]
        auth = authority(self.mesh, self.mesh.user, offer.meta.chat_id)
        ns = max(next_ns(), view.events[-1].meta.ns + 1)
        epoch, _key = self.mesh.keys.ensure(
            offer.meta.chat_id, self.mesh.snapshot(offer.meta.chat_id),
        )
        expires_ns = view.events[2].meta.expires_ns
        if (handoff_state is HandoffState.INTERRUPTED
                and ns >= int(expires_ns or 0)):
            expires_ns = None
        common_meta = dict(
            id=new_id("handoff-event", ns), ns=ns,
            actor=self.mesh.user, signer=self.mesh.user, key_epoch=epoch,
            policy_revision=int(auth["policy_revision"]),
            membership_epoch=int(auth["membership_epoch"]),
            ownership_epoch=int(auth["ownership_epoch"]),
            expires_ns=expires_ns,
        )
        handoff = replace(
            offer, meta=replace(offer.meta, **common_meta),
            state=handoff_state, result=result,
        )
        task_meta = replace(
            view.task.meta, **{**common_meta,
                              "id": new_id("task-event", ns)},
        )
        task = replace(
            view.task, meta=task_meta, state=task_state,
            progress=progress, result=task_result,
        )
        self._publish_records(offer, [task, handoff], phase="lifecycle")
        return handoff

    def _publish_records(self, offer: HandoffRecord, records: list,
                         *, phase: str) -> None:
        docs = []
        handoff_record = next(
            record for record in records if isinstance(record, HandoffRecord)
        )
        for record in records:
            if isinstance(record, TaskRecord):
                path = task_event_path(
                    record.meta.chat_id, record.meta.run_id or "",
                    record.meta.task_id or "", record.meta.id,
                )
            else:
                path = handoff_event_path(
                    record.meta.chat_id, record.meta.run_id or "",
                    record.meta.call_id or "", record.meta.id,
                )
            docs.append({"path": path, "doc": self._sealed(record)})
        payload = {
            "docs": docs, "chat_id": offer.meta.chat_id,
            "run_id": offer.meta.run_id, "handoff_id": offer.meta.call_id,
            "record_id": handoff_record.meta.id, "phase": phase,
        }
        opened = self._opened()
        opened[offer.meta.call_id or ""] = {
            "record_id": handoff_record.meta.id,
            "record": handoff_record.to_dict(),
        }
        target = handoff_prefix(
            offer.meta.chat_id, offer.meta.run_id or "",
            offer.meta.call_id or "",
        )
        seq = self.mesh.store.cache_doc_and_outbox_add(
            self.OPEN_PATH, opened, self.OUTBOX_KIND, target, payload,
        )
        self._attempt(seq, target, payload)

    def _opened(self) -> dict[str, dict]:
        value = self.mesh.store.cached_doc(self.OPEN_PATH, default={})
        return dict(value) if isinstance(value, dict) else {}

    def _pending_record(self, handoff_id: str) -> HandoffRecord | None:
        entry = self._opened().get(handoff_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("record"), dict):
            return None
        try:
            record = HandoffRecord.from_dict(entry["record"])
        except (RuntimeContractError, TypeError, ValueError):
            return None
        if (record.meta.call_id != handoff_id
                or record.meta.actor != self.mesh.user
                or record.meta.signer != self.mesh.user):
            return None
        return record

    def retry_open(self) -> int:
        self.mesh.outbox.notify()
        return len(self._opened())

    def _exact_offer(self, chat_id: str, run_id: str,
                     handoff_id: str) -> HandoffView:
        views = self.read(chat_id, run_id, handoff_id)
        if len(views) != 1:
            raise HandoffLedgerError("canonical handoff offer is unavailable")
        return views[0]

    def _parent_open(self, view: HandoffView) -> bool:
        offer = view.events[0]
        if self._parent_has_terminal(offer, view.task.parent_task_id or ""):
            return False
        parents = self.task_ledger.read(
            offer.meta.chat_id, offer.meta.run_id or "",
            view.task.parent_task_id or "",
        )
        runs = self.run_ledger.read(
            offer.meta.chat_id, offer.meta.run_id or "",
        )
        starts = [record for record in parents
                  if record.state is TaskState.ACTIVE
                  and record.progress == "Working"]
        terminals = [record for record in parents
                     if record.state is not TaskState.ACTIVE]
        run_starts = [record for record in runs
                      if record.state is RunState.RUNNING]
        run_terminals = [record for record in runs
                         if record.state is not RunState.RUNNING]
        return (len(starts) == 1 and not terminals
                and len(run_starts) == 1 and not run_terminals)

    def read(self, chat_id: str, run_id: str = "",
             handoff_id: str = "") -> list[HandoffView]:
        if handoff_id and not run_id:
            raise HandoffLedgerError("exact handoff lookup requires its run id")
        snap = self.mesh.snapshot(chat_id)
        if not snap.is_member(self.mesh.user):
            raise AuthorityError("viewer is not a current room member")
        records: list[HandoffRecord] = []
        prefix = handoff_prefix(chat_id, run_id, handoff_id) + "/"
        for path in self.mesh.tx.list_docs(prefix):
            try:
                record = open_record(
                    self.mesh, snap, self.mesh.tx.get_doc(path),
                    RecordKind.HANDOFF, HandoffRecord.from_dict,
                )
                meta = record.meta
                if (meta.chat_id != chat_id or meta.root_run_id != meta.run_id
                        or not meta.call_id or not meta.task_id):
                    raise HandoffLedgerError("handoff envelope routing mismatch")
                if run_id and meta.run_id != run_id:
                    raise HandoffLedgerError("handoff run id mismatch")
                if handoff_id and meta.call_id != handoff_id:
                    raise HandoffLedgerError("handoff id mismatch")
                if path != handoff_event_path(
                        chat_id, meta.run_id or "", meta.call_id, meta.id):
                    raise HandoffLedgerError("handoff envelope path mismatch")
                source_auth = authority(self.mesh, record.source_agent, chat_id)
                destination_auth = authority(
                    self.mesh, record.destination_agent, chat_id,
                )
                if (record.source_agent == record.destination_agent
                        or record.source_owner != source_auth["owner"]
                        or record.destination_owner != destination_auth["owner"]
                        or record.initiating_member != source_auth["owner"]
                        or record.return_to_agent != record.source_agent
                        or record.transferred_grant_ids):
                    raise HandoffLedgerError("handoff authority chain is invalid")
                actor_auth = (source_auth if meta.actor == record.source_agent
                              else destination_auth)
                expected_actor = (record.source_agent
                                  if record.state in _SOURCE_STATES
                                  else record.destination_agent)
                if meta.actor != expected_actor:
                    raise HandoffLedgerError("handoff state has the wrong signer")
                if record.state not in _SOURCE_STATES | _DESTINATION_STATES:
                    raise HandoffLedgerError("handoff state is not executable")
                if (record.state is HandoffState.OFFERED) != (record.result is None):
                    raise HandoffLedgerError("handoff result does not match its state")
                latest_key = self.mesh.keys.latest(chat_id)
                if latest_key is None or meta.key_epoch != latest_key[0]:
                    raise HandoffLedgerError("handoff key_epoch is stale")
                for name in ("membership_epoch", "ownership_epoch"):
                    if getattr(meta, name) != int(actor_auth[name]):
                        raise HandoffLedgerError(f"handoff {name} is stale")
                # A destination policy change after acceptance must remain
                # visible long enough to settle the authorized branch with a
                # signed INTERRUPTED event. The offer digest below binds the
                # destination's frozen decision policy; source policy remains
                # current because it owns the still-open parent run.
                if (meta.actor == record.source_agent
                        and meta.policy_revision
                            != int(source_auth["policy_revision"])):
                    raise HandoffLedgerError("handoff policy_revision is stale")
                records.append(record)
            except (AuthorityError, HandoffLedgerError, RuntimeContractError,
                    TypeError, ValueError):
                continue
        return self._fold(chat_id, records)

    def _fold(self, chat_id: str,
              records: list[HandoffRecord]) -> list[HandoffView]:
        views: list[HandoffView] = []
        grouped: dict[str, list[HandoffRecord]] = {}
        for record in records:
            grouped.setdefault(record.meta.call_id or "", []).append(record)
        for group in grouped.values():
            offers = [record for record in group
                      if record.state is HandoffState.OFFERED
                      and record.meta.actor == record.source_agent
                      and record.result is None and record.meta.expires_ns]
            if len(offers) != 1:
                continue
            offer = offers[0]
            try:
                source_auth = authority(self.mesh, offer.source_agent, chat_id)
                destination_auth = authority(
                    self.mesh, offer.destination_agent, chat_id,
                )
                # Recover the parent from the paired task before accepting the
                # digest, so a retargeted or missing child fails independently.
                task = self._paired_task(
                    offer, snap=self.mesh.snapshot(chat_id),
                )
                parents = self.task_ledger.read(
                    chat_id, offer.meta.run_id or "", task.parent_task_id or "",
                )
                runs = self.run_ledger.read(chat_id, offer.meta.run_id or "")
            except (AuthorityError, HandoffLedgerError, RuntimeContractError,
                    TypeError, ValueError):
                continue
            parent_starts = [record for record in parents
                             if record.state is TaskState.ACTIVE
                             and record.progress == "Working"]
            run_starts = [record for record in runs
                          if record.state is RunState.RUNNING]
            if (len(parent_starts) != 1 or len(run_starts) != 1
                    or parent_starts[0].parent_task_id is not None
                    or parent_starts[0].assigned_agent != offer.source_agent
                    or run_starts[0].manager_agent != offer.source_agent
                    or offer.meta.root_run_id != run_starts[0].meta.root_run_id
                    or offer.meta.ns <= parent_starts[0].meta.ns
                    or not set(offer.requested_capabilities).issubset(
                        run_starts[0].capability_ceiling)):
                continue
            decisions = [
                record for record in group
                if record.state in {HandoffState.ACCEPTED,
                                    HandoffState.DECLINED}
                and record.meta.actor == offer.destination_agent
                and record.meta.ns > offer.meta.ns
                and self._same_offer(offer, record)
            ]
            frozen_destination_auth = dict(destination_auth)
            if len(decisions) == 1:
                frozen_destination_auth["policy_revision"] = (
                    decisions[0].meta.policy_revision
                )
            expected_digest = _digest(
                chat_id=chat_id, run_id=offer.meta.run_id or "",
                parent_task_id=task.parent_task_id or "",
                child_task_id=offer.meta.task_id or "",
                handoff_id=offer.meta.call_id or "",
                handoff_type=offer.handoff_type, source=offer.source_agent,
                destination=offer.destination_agent, source_auth=source_auth,
                destination_auth=frozen_destination_auth,
                capabilities=offer.requested_capabilities,
            )
            if offer.context_digest != expected_digest:
                continue
            events = self._fold_events(offer, task, group)
            if (self._parent_has_terminal(offer, task.parent_task_id or "")
                    and not any(record.state is HandoffState.AUTHORIZED
                                for record in events)):
                events = (offer,)
            views.append(HandoffView(task, events))
        views.sort(key=lambda view: (view.events[0].meta.ns,
                                    view.events[0].meta.id))
        return views

    def _fold_events(self, offer: HandoffRecord, task: TaskRecord,
                     group: list[HandoffRecord]) -> tuple[HandoffRecord, ...]:
        decisions = [
            record for record in group
            if record.state in {HandoffState.ACCEPTED, HandoffState.DECLINED,
                                HandoffState.TIMED_OUT}
            and record.meta.ns > offer.meta.ns
            and self._same_offer(offer, record)
        ]
        if len(decisions) != 1:
            return (offer,)
        decision = decisions[0]
        acceptance_deadline = int(offer.meta.expires_ns or 0)
        if decision.state is HandoffState.TIMED_OUT:
            if (decision.meta.ns < acceptance_deadline
                    or time.time_ns() < acceptance_deadline):
                return (offer,)
            return (offer, decision)
        if decision.meta.ns >= acceptance_deadline:
            return (offer,)
        if decision.state is HandoffState.DECLINED:
            return ((offer, decision) if time.time_ns() < acceptance_deadline
                    else (offer,))

        authorizations = [
            record for record in group
            if record.state is HandoffState.AUTHORIZED
            and record.meta.ns > decision.meta.ns
            and self._same_offer(offer, record)
        ]
        if (len(authorizations) != 1
                or _result_payload(authorizations[0]).get("after")
                   != decision.meta.id
                or int(authorizations[0].meta.expires_ns or 0)
                   <= authorizations[0].meta.ns):
            if authorizations or time.time_ns() < acceptance_deadline:
                return (offer, decision)
            return (offer,)
        authorized = authorizations[0]
        chain: list[HandoffRecord] = [offer, decision, authorized]

        preflight_terminals = [
            record for record in group
            if record.state in {HandoffState.STOPPED, HandoffState.INTERRUPTED}
            and record.meta.ns > authorized.meta.ns
            and self._same_offer(offer, record)
            and _result_payload(record).get("after") == authorized.meta.id
        ]
        if preflight_terminals:
            if (len(preflight_terminals) == 1
                    and (preflight_terminals[0].meta.expires_ns
                         == authorized.meta.expires_ns
                         or (preflight_terminals[0].state
                             is HandoffState.INTERRUPTED
                             and preflight_terminals[0].meta.expires_ns is None))
                    and _result_payload(preflight_terminals[0]).get("after")
                        == authorized.meta.id
                    and self._paired_lifecycle(
                        task, preflight_terminals[0], {
                            HandoffState.STOPPED: TaskState.STOPPED,
                            HandoffState.INTERRUPTED: TaskState.INTERRUPTED,
                        }[preflight_terminals[0].state])):
                chain.append(preflight_terminals[0])
            return tuple(chain)

        active = [
            record for record in group
            if record.state is HandoffState.ACTIVE
            and record.meta.ns > authorized.meta.ns
            and self._same_offer(offer, record)
        ]
        if (len(active) != 1
                or active[0].meta.expires_ns != authorized.meta.expires_ns
                or active[0].meta.policy_revision
                    != decision.meta.policy_revision
                or _result_payload(active[0]).get("after") != authorized.meta.id
                or not self._paired_lifecycle(
                    task, active[0], TaskState.ACTIVE)):
            return tuple(chain)
        chain.append(active[0])

        terminals = [
            record for record in group
            if record.state in _EXECUTION_TERMINALS
            and record.meta.ns > active[0].meta.ns
            and self._same_offer(offer, record)
        ]
        if (len(terminals) != 1
                or not (terminals[0].meta.expires_ns
                        == authorized.meta.expires_ns
                        or (terminals[0].state is HandoffState.INTERRUPTED
                            and terminals[0].meta.expires_ns is None))
                or _result_payload(terminals[0]).get("after")
                   != active[0].meta.id
                or (terminals[0].state is HandoffState.RETURNED
                    and terminals[0].meta.policy_revision
                        != active[0].meta.policy_revision)
                or not self._paired_lifecycle(
                    task, terminals[0], {
                        HandoffState.RETURNED: TaskState.RETURNED,
                        HandoffState.STOPPED: TaskState.STOPPED,
                        HandoffState.INTERRUPTED: TaskState.INTERRUPTED,
                    }[terminals[0].state])):
            return tuple(chain)
        terminal = terminals[0]
        chain.append(terminal)
        if terminal.state is not HandoffState.RETURNED:
            return tuple(chain)

        consumed = [
            record for record in group
            if record.state is HandoffState.CONSUMED
            and record.meta.ns > terminal.meta.ns
            and self._same_offer(offer, record)
        ]
        if (len(consumed) == 1 and consumed[0].meta.expires_ns is None
                and _result_payload(consumed[0]).get("after")
                    == terminal.meta.id):
            chain.append(consumed[0])
        return tuple(chain)

    def _paired_lifecycle(self, offered: TaskRecord,
                          handoff: HandoffRecord,
                          state: TaskState) -> bool:
        snap = self.mesh.snapshot(handoff.meta.chat_id)
        matches = []
        prefix = task_prefix(
            handoff.meta.chat_id, handoff.meta.run_id or "",
            handoff.meta.task_id or "",
        ) + "/"
        for path in self.mesh.tx.list_docs(prefix):
            try:
                task = open_record(
                    self.mesh, snap, self.mesh.tx.get_doc(path),
                    RecordKind.TASK, TaskRecord.from_dict,
                )
                expected_result = {
                    TaskState.ACTIVE: None,
                    TaskState.RETURNED:
                        _result_payload(handoff).get("contribution"),
                    TaskState.INTERRUPTED: "Interrupted",
                    TaskState.STOPPED: "Stopped",
                }.get(state)
                if (path == task_event_path(
                        task.meta.chat_id, task.meta.run_id or "",
                        task.meta.task_id or "", task.meta.id)
                        and task.state is state
                        and task.meta.ns == handoff.meta.ns
                        and task.meta.actor == handoff.destination_agent
                        and task.meta.signer == handoff.destination_agent
                        and task.meta.chat_id == offered.meta.chat_id
                        and task.meta.run_id == offered.meta.run_id
                        and task.meta.root_run_id == offered.meta.root_run_id
                        and task.meta.task_id == offered.meta.task_id
                        and task.meta.call_id == offered.meta.call_id
                        and task.meta.expires_ns == handoff.meta.expires_ns
                        and task.meta.key_epoch == handoff.meta.key_epoch
                        and task.meta.policy_revision
                            == handoff.meta.policy_revision
                        and task.meta.membership_epoch
                            == handoff.meta.membership_epoch
                        and task.meta.ownership_epoch
                            == handoff.meta.ownership_epoch
                        and task.objective == offered.objective
                        and task.assigned_agent == offered.assigned_agent
                        and task.assigning_agent == offered.assigning_agent
                        and task.responsible_member == offered.responsible_member
                        and task.parent_task_id == offered.parent_task_id
                        and task.success_criteria == offered.success_criteria
                        and task.context_digest == offered.context_digest
                        and task.grant_ids == offered.grant_ids
                        and task.dependency_ids == offered.dependency_ids
                        and task.return_to_agent == offered.return_to_agent
                        and task.result == expected_result
                        and (state is not TaskState.ACTIVE
                             or task.progress.startswith("@"))
                        and (state is not TaskState.INTERRUPTED
                             or task.progress == "Interrupted")):
                    matches.append(task)
            except (RuntimeContractError, TypeError, ValueError):
                continue
        return len(matches) == 1

    def _parent_has_terminal(self, offer: HandoffRecord,
                             parent_task_id: str) -> bool:
        """Treat any authentic exact-parent terminal as closure.

        The folded run/task projections intentionally suppress ambiguous
        competing terminals. Handoffs must not interpret that suppression as
        proof that the parent reopened, and signer-chosen ``ns`` cannot prove
        whether a remote decision was committed before termination.
        """
        snap = self.mesh.snapshot(offer.meta.chat_id)
        run_id = offer.meta.run_id or ""
        for path in self.mesh.tx.list_docs(
                task_prefix(offer.meta.chat_id, run_id, parent_task_id) + "/"):
            try:
                record = open_record(
                    self.mesh, snap, self.mesh.tx.get_doc(path),
                    RecordKind.TASK, TaskRecord.from_dict,
                )
                if (path == task_event_path(
                        offer.meta.chat_id, run_id, parent_task_id, record.meta.id)
                        and record.meta.actor == offer.source_agent
                        and record.meta.chat_id == offer.meta.chat_id
                        and record.meta.run_id == run_id
                        and record.meta.task_id == parent_task_id
                        and record.state not in {TaskState.ACTIVE,
                                                 TaskState.OFFERED}):
                    return True
            except (RuntimeContractError, TypeError, ValueError):
                continue
        for path in self.mesh.tx.list_docs(
                run_prefix(offer.meta.chat_id, run_id) + "/"):
            try:
                record = open_record(
                    self.mesh, snap, self.mesh.tx.get_doc(path),
                    RecordKind.RUN, RunRecord.from_dict,
                )
                if (path == run_event_path(
                        offer.meta.chat_id, run_id, record.meta.id)
                        and record.meta.actor == offer.source_agent
                        and record.meta.chat_id == offer.meta.chat_id
                        and record.meta.run_id == run_id
                        and record.state is not RunState.RUNNING):
                    return True
            except (RuntimeContractError, TypeError, ValueError):
                continue
        return False

    def _paired_task(self, offer: HandoffRecord, *, snap) -> TaskRecord:
        prefix = (f"chats/{offer.meta.chat_id}/runtime/tasks/"
                  f"{offer.meta.run_id}/{offer.meta.task_id}/")
        matches: list[TaskRecord] = []
        for path in self.mesh.tx.list_docs(prefix):
            try:
                task = open_record(
                    self.mesh, snap, self.mesh.tx.get_doc(path),
                    RecordKind.TASK, TaskRecord.from_dict,
                )
                source_auth = authority(
                    self.mesh, offer.source_agent, offer.meta.chat_id,
                )
                latest_key = self.mesh.keys.latest(offer.meta.chat_id)
                if (path == task_event_path(
                        offer.meta.chat_id, offer.meta.run_id or "",
                        offer.meta.task_id or "", task.meta.id)
                        and task.state is TaskState.OFFERED
                        and task.meta.call_id == offer.meta.call_id
                        and task.meta.chat_id == offer.meta.chat_id
                        and task.meta.run_id == offer.meta.run_id
                        and task.meta.task_id == offer.meta.task_id
                        and task.meta.expires_ns == offer.meta.expires_ns
                        and task.meta.ns == offer.meta.ns
                        and task.assigned_agent == offer.destination_agent
                        and task.assigning_agent == offer.source_agent
                        and task.return_to_agent == offer.source_agent
                        and task.responsible_member == offer.destination_owner
                        and task.context_digest == offer.context_digest
                        and not task.grant_ids and not task.dependency_ids
                        and task.result is None
                        and task.meta.actor == offer.source_agent
                        and task.meta.signer == offer.source_agent
                        and task.meta.root_run_id == offer.meta.root_run_id
                        and task.meta.key_epoch == (latest_key[0]
                                                    if latest_key else -1)
                        and task.meta.policy_revision
                            == int(source_auth["policy_revision"])
                        and task.meta.membership_epoch
                            == int(source_auth["membership_epoch"])
                        and task.meta.ownership_epoch
                            == int(source_auth["ownership_epoch"])):
                    matches.append(task)
            except (AuthorityError, RuntimeContractError, TypeError, ValueError):
                continue
        if len(matches) != 1:
            raise HandoffLedgerError("handoff child-task pair is unavailable")
        return matches[0]

    @staticmethod
    def _same_offer(offer: HandoffRecord, decision: HandoffRecord) -> bool:
        static = (
            "handoff_type", "source_agent", "destination_agent",
            "source_owner", "destination_owner", "initiating_member", "reason",
            "context_digest", "requested_capabilities", "transferred_grant_ids",
            "return_to_agent",
        )
        if decision.state in {HandoffState.TIMED_OUT, HandoffState.CONSUMED}:
            expiry_matches = decision.meta.expires_ns is None
        elif decision.state is HandoffState.AUTHORIZED:
            expiry_matches = int(decision.meta.expires_ns or 0) > decision.meta.ns
        elif decision.state in {
                HandoffState.ACTIVE, HandoffState.RETURNED,
                HandoffState.STOPPED, HandoffState.INTERRUPTED}:
            expiry_matches = bool(decision.meta.expires_ns)
        else:
            expiry_matches = decision.meta.expires_ns == offer.meta.expires_ns
        return (expiry_matches
                and decision.meta.chat_id == offer.meta.chat_id
                and decision.meta.run_id == offer.meta.run_id
                and decision.meta.root_run_id == offer.meta.root_run_id
                and decision.meta.task_id == offer.meta.task_id
                and decision.meta.call_id == offer.meta.call_id
                and all(getattr(decision, name) == getattr(offer, name)
                        for name in static))
