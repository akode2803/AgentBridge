"""Canonical same-room child-task offers and handoff decisions.

R127 establishes authority and durable visibility only. It does not invoke the
destination agent or grant capabilities. Offers pair one child-task record with
one handoff record; destination-authored decisions remain handoff events until
the execution-routing slice adds active child-task transitions.
"""

from __future__ import annotations

import hashlib
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
        elif phase == "decision":
            if len(metas) != 1 or metas[0].kind is not RecordKind.HANDOFF:
                raise ValidationError("runtime handoff decision is malformed")
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

    def _opened(self) -> dict[str, dict]:
        value = self.mesh.store.cached_doc(self.OPEN_PATH, default={})
        return dict(value) if isinstance(value, dict) else {}

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
                                  if record.state in {HandoffState.OFFERED,
                                                      HandoffState.TIMED_OUT}
                                  else record.destination_agent)
                if meta.actor != expected_actor:
                    raise HandoffLedgerError("handoff state has the wrong signer")
                if (record.state is HandoffState.OFFERED) != (record.result is None):
                    raise HandoffLedgerError("handoff result does not match its state")
                latest_key = self.mesh.keys.latest(chat_id)
                if latest_key is None or meta.key_epoch != latest_key[0]:
                    raise HandoffLedgerError("handoff key_epoch is stale")
                for name in ("policy_revision", "membership_epoch",
                             "ownership_epoch"):
                    if getattr(meta, name) != int(actor_auth[name]):
                        raise HandoffLedgerError(f"handoff {name} is stale")
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
            expected_digest = _digest(
                chat_id=chat_id, run_id=offer.meta.run_id or "",
                parent_task_id=task.parent_task_id or "",
                child_task_id=offer.meta.task_id or "",
                handoff_id=offer.meta.call_id or "",
                handoff_type=offer.handoff_type, source=offer.source_agent,
                destination=offer.destination_agent, source_auth=source_auth,
                destination_auth=destination_auth,
                capabilities=offer.requested_capabilities,
            )
            if offer.context_digest != expected_digest:
                continue
            decisions = [record for record in group
                         if record.state in {HandoffState.ACCEPTED,
                                             HandoffState.DECLINED,
                                             HandoffState.TIMED_OUT}
                         and record.meta.ns > offer.meta.ns
                         and self._same_offer(offer, record)]
            if self._parent_has_terminal(offer, task.parent_task_id or ""):
                decisions = []
            if len(decisions) > 1:
                views.append(HandoffView(task, (offer,)))
            elif len(decisions) == 1:
                decision = decisions[0]
                if (decision.state is HandoffState.TIMED_OUT
                        and (decision.meta.ns < int(offer.meta.expires_ns or 0)
                             or time.time_ns()
                                < int(offer.meta.expires_ns or 0))):
                    views.append(HandoffView(task, (offer,)))
                elif (decision.state is not HandoffState.TIMED_OUT
                      and (decision.meta.ns >= int(offer.meta.expires_ns or 0)
                           or time.time_ns()
                              >= int(offer.meta.expires_ns or 0))):
                    views.append(HandoffView(task, (offer,)))
                else:
                    views.append(HandoffView(task, (offer, decision)))
            else:
                views.append(HandoffView(task, (offer,)))
        views.sort(key=lambda view: (view.events[0].meta.ns,
                                    view.events[0].meta.id))
        return views

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
        expiry_matches = (
            decision.meta.expires_ns is None
            if decision.state is HandoffState.TIMED_OUT
            else decision.meta.expires_ns == offer.meta.expires_ns
        )
        return (expiry_matches
                and decision.meta.chat_id == offer.meta.chat_id
                and decision.meta.run_id == offer.meta.run_id
                and decision.meta.root_run_id == offer.meta.root_run_id
                and decision.meta.task_id == offer.meta.task_id
                and decision.meta.call_id == offer.meta.call_id
                and all(getattr(decision, name) == getattr(offer, name)
                        for name in static))
