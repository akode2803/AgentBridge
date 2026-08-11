"""Canonical signed and room-encrypted root-task lifecycle.

R126 owns one manager-retained root task per current harness run. R127 pairs
source-authored offered child tasks through ``HandoffLedger``; active child
execution, capabilities, effects and GUI graph projection remain later slices.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace

from ...core.errors import ValidationError
from ...core.timekit import new_id, next_ns
from .authority import AuthorityError, authority
from .eventio import deliver_immutable, open_record, seal_record
from .models import (
    RecordKind, RecordMeta, RunRecord, RunState, RuntimeContractError,
    TaskRecord, TaskState, canonical_json_bytes,
)
from .runs import RunLedger

__all__ = ["TaskLedger", "TaskLedgerError", "task_event_path", "task_prefix"]


class TaskLedgerError(RuntimeContractError):
    """A canonical task event is malformed, forged, stale, or unavailable."""


_TERMINAL = {
    TaskState.COMPLETED, TaskState.FAILED, TaskState.STOPPED,
    TaskState.INTERRUPTED,
}


def task_prefix(chat_id: str, run_id: str = "", task_id: str = "") -> str:
    base = f"chats/{chat_id}/runtime/tasks"
    if run_id:
        base += f"/{run_id}"
    return f"{base}/{task_id}" if task_id else base


def task_event_path(chat_id: str, run_id: str, task_id: str,
                    record_id: str) -> str:
    return f"{task_prefix(chat_id, run_id, task_id)}/{record_id}.json"


def _terminal_state(value: str) -> TaskState:
    mapping = {
        "done": TaskState.COMPLETED,
        "error": TaskState.FAILED,
        "stopped": TaskState.STOPPED,
        "interrupted": TaskState.INTERRUPTED,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise TaskLedgerError(f"unsupported task terminal state: {value}") from exc


def _terminal_summary(state: str) -> str:
    return {
        "done": "Completed",
        "error": "Failed",
        "stopped": "Stopped by the responsible member",
        "interrupted": "Interrupted by an app or agent restart",
    }.get(state, "Task finished")


def _context_digest(run: RunRecord) -> str:
    return hashlib.sha256(canonical_json_bytes({
        "chat_id": run.meta.chat_id, "run_id": run.meta.run_id,
        "trigger_id": run.trigger_id,
    })).hexdigest()


class TaskLedger:
    """Write and validate one root task per canonical harness run."""

    OUTBOX_KIND = "runtime-task-event"
    OPEN_PATH = "runtime/task-open"

    def __init__(self, mesh, run_ledger: RunLedger, *,
                 fresh_reads: bool = True, register_outbox: bool = True,
                 read_snapshot: dict | None = None) -> None:
        self.mesh = mesh
        self.run_ledger = run_ledger
        self.fresh_reads = fresh_reads
        self.read_snapshot = read_snapshot
        self._lock = threading.RLock()
        if register_outbox:
            mesh.outbox.handlers[self.OUTBOX_KIND] = self._deliver

    def _deliver(self, _target: str, payload: dict) -> None:
        path = payload.get("path")
        doc = payload.get("doc")
        if not isinstance(path, str) or not path or not isinstance(doc, dict):
            raise ValidationError("runtime task outbox payload is malformed")
        deliver_immutable(self.mesh.tx, path, doc)
        self._delivered(_target, payload)

    def _payload(self, record: TaskRecord, *, terminal: bool) -> tuple[str, dict]:
        try:
            doc = seal_record(self.mesh, record)
        except RuntimeContractError as exc:
            raise TaskLedgerError(str(exc)) from exc
        meta = record.meta
        path = task_event_path(
            meta.chat_id, meta.run_id or "", meta.task_id or "", meta.id,
        )
        target = task_prefix(meta.chat_id, meta.run_id or "", meta.task_id or "")
        return target, {
            "path": path, "doc": doc, "chat_id": meta.chat_id,
            "run_id": meta.run_id, "task_id": meta.task_id,
            "record_id": meta.id, "terminal": terminal,
        }

    def _attempt(self, seq: int, target: str, payload: dict) -> None:
        try:
            self._deliver(target, payload)
        except ValidationError as exc:
            self.mesh.store.outbox_dead(seq, f"{type(exc).__name__}: {exc}")
            raise TaskLedgerError("canonical task event conflicts with its ledger") from exc
        except Exception as exc:
            self.mesh.store.outbox_retry(seq, f"{type(exc).__name__}: {exc}", 1.0)
            self.mesh.outbox.notify()
        else:
            try:
                self.mesh.store.outbox_done(seq)
            except Exception:  # remote event and local cleanup already landed
                self.mesh.outbox.notify()

    def _delivered(self, _target: str, payload: dict) -> None:
        if not payload.get("terminal"):
            return
        task_id = str(payload.get("task_id") or "")
        record_id = str(payload.get("record_id") or "")
        with self._lock:
            opened = self._opened()
            entry = opened.get(task_id)
            terminal = entry.get("terminal") if isinstance(entry, dict) else None
            if (isinstance(terminal, dict)
                    and terminal.get("record_id") == record_id):
                opened.pop(task_id, None)
                self.mesh.store.cache_doc(self.OPEN_PATH, opened)

    def start(self, run: RunRecord, task_id: str) -> TaskRecord:
        with self._lock:
            record, target, payload, opened = self._prepare_start(run, task_id)
            seq = self.mesh.store.cache_doc_and_outbox_add(
                self.OPEN_PATH, opened, self.OUTBOX_KIND, target, payload,
            )
        self._attempt(seq, target, payload)
        return record

    def start_with_run(self, *, run_id: str, task_id: str, chat_id: str,
                       trigger_id: str, provider: str, model: str,
                       capability_ceiling: tuple[str, ...] = (),
                       policy_revision: int | None = None,
                       ) -> tuple[RunRecord, TaskRecord]:
        """Atomically commit one canonical run and its promised root task."""
        with self.run_ledger._lock, self._lock:
            run, run_target, run_payload, run_opened = (
                self.run_ledger._prepare_start(
                    run_id=run_id, chat_id=chat_id, trigger_id=trigger_id,
                    provider=provider, model=model,
                    capability_ceiling=capability_ceiling,
                    active_task_ids=(task_id,),
                    policy_revision=policy_revision,
                )
            )
            task, task_target, task_payload, task_opened = self._prepare_start(
                run, task_id,
            )
            run_seq, task_seq = self.mesh.store.cache_docs_and_outbox_add_many(
                {
                    "runtime/run-open": run_opened,
                    self.OPEN_PATH: task_opened,
                },
                [
                    (self.run_ledger.OUTBOX_KIND, run_target, run_payload),
                    (self.OUTBOX_KIND, task_target, task_payload),
                ],
            )
        self.run_ledger._attempt(run_seq, run_target, run_payload)
        self._attempt(task_seq, task_target, task_payload)
        return run, task

    def _prepare_start(
        self, run: RunRecord, task_id: str,
    ) -> tuple[TaskRecord, str, dict, dict]:
        if (run.state is not RunState.RUNNING
                or run.manager_agent != self.mesh.user
                or task_id not in run.active_task_ids):
            raise TaskLedgerError("root task is not bound to this active run")
        auth = authority(self.mesh, self.mesh.user, run.meta.chat_id)
        for name in ("policy_revision", "membership_epoch", "ownership_epoch"):
            if getattr(run.meta, name) != int(auth[name]):
                raise TaskLedgerError(f"run {name} changed before task start")
        ns = max(next_ns(), run.meta.ns + 1)
        epoch, _key = self.mesh.keys.ensure(
            run.meta.chat_id, self.mesh.snapshot(run.meta.chat_id),
        )
        meta = RecordMeta(
            schema_version=1, kind=RecordKind.TASK,
            id=new_id("task-event", ns), ns=ns, actor=self.mesh.user,
            chat_id=run.meta.chat_id, signer=self.mesh.user,
            root_run_id=run.meta.root_run_id, run_id=run.meta.run_id,
            task_id=task_id, call_id=None, key_epoch=epoch,
            policy_revision=run.meta.policy_revision,
            membership_epoch=run.meta.membership_epoch,
            ownership_epoch=run.meta.ownership_epoch, expires_ns=None,
        )
        record = TaskRecord(
            meta=meta, state=TaskState.ACTIVE,
            objective="Respond to the triggering message",
            assigned_agent=self.mesh.user, assigning_agent=self.mesh.user,
            responsible_member=run.responsible_member, parent_task_id=None,
            success_criteria=(
                "Produce one policy-compliant response or explicit terminal outcome",
            ),
            context_digest=_context_digest(run), grant_ids=(), dependency_ids=(),
            progress="Working", result=None, return_to_agent=self.mesh.user,
        )
        target, payload = self._payload(record, terminal=False)
        opened = self._opened()
        opened[task_id] = {
            "start": record.to_dict(), "progress_recorded": False,
            "last_ns": record.meta.ns, "terminal": None,
        }
        return record, target, payload, opened

    def progress(self, task_id: str) -> TaskRecord | None:
        """Publish one content-free progress event per root task."""
        with self._lock:
            opened = self._opened()
            entry = opened.get(task_id)
            raw = entry.get("start") if isinstance(entry, dict) else None
            if not isinstance(raw, dict):
                raise TaskLedgerError("canonical task start is unavailable")
            if entry.get("progress_recorded") or entry.get("terminal"):
                return None
            started = TaskRecord.from_dict(raw)
            authority(self.mesh, self.mesh.user, started.meta.chat_id)
            ns = max(next_ns(), int(entry.get("last_ns") or 0) + 1)
            epoch, _key = self.mesh.keys.ensure(
                started.meta.chat_id, self.mesh.snapshot(started.meta.chat_id),
            )
            record = replace(
                started,
                meta=replace(started.meta, id=new_id("task-event", ns), ns=ns,
                             key_epoch=epoch),
                progress="Model work completed",
            )
            target, payload = self._payload(record, terminal=False)
            entry = dict(entry)
            entry.update({"progress_recorded": True, "last_ns": ns})
            opened[task_id] = entry
            self.mesh.store.cache_doc_and_outbox_add(
                self.OPEN_PATH, opened, self.OUTBOX_KIND, target, payload,
            )
            # Model-stream callbacks must never wait on cloud transport.
            # Per-task outbox ordering keeps this behind the start event.
            self.mesh.outbox.notify()
            return record

    def finish(self, task_id: str, state: str) -> TaskRecord:
        with self._lock:
            opened, raw, entry, intent, queued = self._terminal_intent(
                task_id, state,
            )
            if queued is not None:
                return queued
            self.mesh.store.cache_doc(self.OPEN_PATH, opened)
            record, target, payload = self._build_terminal(raw, entry, intent)
            entry.update({
                "last_ns": record.meta.ns,
                "terminal": {
                    **intent, "queued": True, "record_id": record.meta.id,
                    "record": record.to_dict(),
                },
            })
            opened = self._opened()
            opened[task_id] = entry
            seq = self.mesh.store.cache_doc_and_outbox_add(
                self.OPEN_PATH, opened, self.OUTBOX_KIND, target, payload,
            )
        self._attempt(seq, target, payload)
        return record

    def finish_with_run(
        self, task_id: str, run_id: str, state: str, run_status: str,
    ) -> tuple[TaskRecord, RunRecord]:
        """Atomically commit the root-task and parent-run terminal intents."""
        with self.run_ledger._lock, self._lock:
            task_opened, task_raw, task_entry, task_intent, task_queued = (
                self._terminal_intent(task_id, state)
            )
            run_opened, run_raw, run_intent, run_queued = (
                self.run_ledger._terminal_intent(run_id, state, run_status)
            )
            if (task_queued is None) != (run_queued is None):
                raise TaskLedgerError("task and run terminal queues diverged")
            if task_queued is not None and run_queued is not None:
                return task_queued, run_queued

            # The paired desired outcome survives a crash during sealing.
            self.mesh.store.cache_docs_and_outbox_add_many(
                {self.OPEN_PATH: task_opened, "runtime/run-open": run_opened},
                [],
            )
            task_record, task_target, task_payload = self._build_terminal(
                task_raw, task_entry, task_intent,
            )
            run_record, run_target, run_payload = self.run_ledger._build_terminal(
                run_raw, run_intent,
            )
            task_entry.update({
                "last_ns": task_record.meta.ns,
                "terminal": {
                    **task_intent, "queued": True,
                    "record_id": task_record.meta.id,
                    "record": task_record.to_dict(),
                },
            })
            task_opened = self._opened()
            task_opened[task_id] = task_entry
            run_opened = self.run_ledger._opened()
            run_opened[run_id] = {
                "start": run_raw,
                "terminal": {
                    **run_intent, "queued": True,
                    "record_id": run_record.meta.id,
                    "record": run_record.to_dict(),
                },
            }
            run_seq, task_seq = self.mesh.store.cache_docs_and_outbox_add_many(
                {self.OPEN_PATH: task_opened, "runtime/run-open": run_opened},
                [
                    (self.run_ledger.OUTBOX_KIND, run_target, run_payload),
                    (self.OUTBOX_KIND, task_target, task_payload),
                ],
            )
        self.run_ledger._attempt(run_seq, run_target, run_payload)
        self._attempt(task_seq, task_target, task_payload)
        return task_record, run_record

    def _terminal_intent(
        self, task_id: str, state: str,
    ) -> tuple[dict, dict, dict, dict, TaskRecord | None]:
        _terminal_state(state)
        opened = self._opened()
        entry = opened.get(task_id)
        raw = entry.get("start") if isinstance(entry, dict) else None
        if not isinstance(raw, dict):
            raise TaskLedgerError("canonical task start is unavailable")
        current = entry.get("terminal")
        if isinstance(current, dict) and current.get("queued"):
            return opened, raw, dict(entry), current, TaskRecord.from_dict(
                current["record"],
            )
        intent = current if isinstance(current, dict) else {
            "state": state, "status": _terminal_summary(state), "queued": False,
        }
        if intent.get("state") != state:
            raise TaskLedgerError("task already has a different terminal intent")
        entry = dict(entry)
        entry["terminal"] = intent
        opened[task_id] = entry
        return opened, raw, entry, intent, None

    def _build_terminal(
        self, raw: dict, entry: dict, intent: dict,
    ) -> tuple[TaskRecord, str, dict]:
        terminal = _terminal_state(str(intent.get("state")))
        started = TaskRecord.from_dict(raw)
        authority(self.mesh, self.mesh.user, started.meta.chat_id)
        ns = max(next_ns(), int(entry.get("last_ns") or 0) + 1)
        epoch, _key = self.mesh.keys.ensure(
            started.meta.chat_id, self.mesh.snapshot(started.meta.chat_id),
        )
        result = str(intent["status"])
        record = replace(
            started,
            meta=replace(started.meta, id=new_id("task-event", ns), ns=ns,
                         key_epoch=epoch),
            state=terminal, progress=result, result=result,
        )
        target, payload = self._payload(record, terminal=True)
        return record, target, payload

    def _opened(self) -> dict[str, dict]:
        value = self.mesh.store.cached_doc(self.OPEN_PATH, default={})
        return dict(value) if isinstance(value, dict) else {}

    def recover_open(self) -> int:
        recovered = 0
        for task_id, entry in list(self._opened().items()):
            try:
                terminal = entry.get("terminal") if isinstance(entry, dict) else None
                if isinstance(terminal, dict) and terminal.get("queued"):
                    continue
                state = (str(terminal.get("state"))
                         if isinstance(terminal, dict) else "interrupted")
                start = entry.get("start") if isinstance(entry, dict) else None
                task = TaskRecord.from_dict(start) if isinstance(start, dict) else None
                run_id = task.meta.run_id if task is not None else None
                run_entry = self.run_ledger._opened().get(run_id or "")
                run_terminal = (run_entry.get("terminal")
                                if isinstance(run_entry, dict) else None)
                if run_entry is not None:
                    self.finish_with_run(
                        task_id, run_id or "", state,
                        str(run_terminal.get("status"))
                        if isinstance(run_terminal, dict)
                        else "Interrupted - the app or agent restarted mid-run",
                    )
                else:
                    self.finish(task_id, state)
                recovered += 1
            except (AuthorityError, TaskLedgerError):
                continue
        return recovered

    def retry_terminals(self) -> int:
        retried = 0
        for task_id, entry in list(self._opened().items()):
            terminal = entry.get("terminal") if isinstance(entry, dict) else None
            if not isinstance(terminal, dict) or terminal.get("queued"):
                continue
            try:
                start = entry.get("start") if isinstance(entry, dict) else None
                task = TaskRecord.from_dict(start) if isinstance(start, dict) else None
                run_id = task.meta.run_id if task is not None else None
                run_entry = self.run_ledger._opened().get(run_id or "")
                run_terminal = (run_entry.get("terminal")
                                if isinstance(run_entry, dict) else None)
                if isinstance(run_terminal, dict) and not run_terminal.get("queued"):
                    self.finish_with_run(
                        task_id, run_id or "", str(terminal.get("state")),
                        str(run_terminal.get("status")),
                    )
                else:
                    self.finish(task_id, str(terminal.get("state")))
                retried += 1
            except Exception:  # noqa: BLE001 - retry failure cannot stop harness
                continue
        return retried

    def has_terminal_intent(self, task_id: str) -> bool:
        with self._lock:
            entry = self._opened().get(task_id)
            return bool(isinstance(entry, dict) and entry.get("terminal"))

    def read(self, chat_id: str, run_id: str = "",
             task_id: str = "") -> list[TaskRecord]:
        if task_id and not run_id:
            raise TaskLedgerError("exact task lookup requires its run id")
        snap = self.mesh.snapshot(chat_id)
        if not snap.is_member(self.mesh.user):
            raise AuthorityError("viewer is not a current room member")
        run_starts = {
            record.meta.run_id: record
            for record in self.run_ledger.read(chat_id, run_id)
            if record.state is RunState.RUNNING
        }
        records: list[TaskRecord] = []
        prefix = task_prefix(chat_id, run_id, task_id) + "/"
        paths = (sorted(path for path in self.read_snapshot
                        if path.startswith(prefix))
                 if self.read_snapshot is not None else
                 self.mesh.tx.list_docs(prefix) if self.fresh_reads else
                 self.mesh.tx.list_cached_docs(prefix))
        for path in paths:
            try:
                doc = (self.read_snapshot.get(path)
                       if self.read_snapshot is not None
                       else self.mesh.tx.get_doc(path))
                record = open_record(
                    self.mesh, snap, doc,
                    RecordKind.TASK, TaskRecord.from_dict,
                )
                meta = record.meta
                if meta.chat_id != chat_id or meta.root_run_id != meta.run_id:
                    raise TaskLedgerError("task envelope routing mismatch")
                if run_id and meta.run_id != run_id:
                    raise TaskLedgerError("task run id does not match exact lookup")
                if task_id and meta.task_id != task_id:
                    raise TaskLedgerError("task id does not match exact lookup")
                if path != task_event_path(
                        chat_id, meta.run_id or "", meta.task_id or "", meta.id):
                    raise TaskLedgerError("task envelope path mismatch")
                if not (record.assigned_agent == record.assigning_agent
                        == record.return_to_agent == meta.actor):
                    raise TaskLedgerError("root task agent chain differs from signer")
                current = authority(self.mesh, meta.actor, chat_id)
                if record.responsible_member != current["owner"]:
                    raise TaskLedgerError("task responsible member is stale")
                latest_key = self.mesh.keys.latest(chat_id)
                if latest_key is None or meta.key_epoch != latest_key[0]:
                    raise TaskLedgerError("task key_epoch is stale")
                for name in ("policy_revision", "membership_epoch",
                             "ownership_epoch"):
                    if getattr(meta, name) != int(current[name]):
                        raise TaskLedgerError(f"task {name} is stale")
                run = run_starts.get(meta.run_id)
                if (run is None or meta.task_id not in run.active_task_ids
                        or meta.ns <= run.meta.ns
                        or meta.policy_revision != run.meta.policy_revision
                        or meta.membership_epoch != run.meta.membership_epoch
                        or meta.ownership_epoch != run.meta.ownership_epoch):
                    raise TaskLedgerError("task is not coherently bound to its run")
                if (meta.actor != run.manager_agent or meta.call_id is not None
                        or record.parent_task_id is not None
                        or record.objective != "Respond to the triggering message"
                        or record.success_criteria != (
                            "Produce one policy-compliant response or explicit terminal outcome",
                        )
                        or record.context_digest != _context_digest(run)
                        or record.grant_ids or record.dependency_ids):
                    raise TaskLedgerError("record exceeds the root-task contract")
                if record.state is TaskState.ACTIVE:
                    if record.result is not None or record.progress not in {
                            "Working", "Model work completed"}:
                        raise TaskLedgerError("root-task progress is not content-free")
                elif (record.state not in _TERMINAL
                      or record.result != _terminal_summary({
                          TaskState.COMPLETED: "done",
                          TaskState.FAILED: "error",
                          TaskState.STOPPED: "stopped",
                          TaskState.INTERRUPTED: "interrupted",
                      }[record.state])
                      or record.progress != record.result):
                    raise TaskLedgerError("root-task terminal is incoherent")
                records.append(record)
            except (AuthorityError, RuntimeContractError, TypeError, ValueError):
                continue
        return self._fold(records)

    @staticmethod
    def _fold(records: list[TaskRecord]) -> list[TaskRecord]:
        accepted: list[TaskRecord] = []
        grouped: dict[str, list[TaskRecord]] = {}
        for record in records:
            grouped.setdefault(record.meta.task_id or "", []).append(record)
        for group in grouped.values():
            starts = [record for record in group
                      if record.state is TaskState.ACTIVE
                      and record.progress == "Working"
                      and record.result is None]
            if len(starts) != 1:
                continue
            started = starts[0]
            accepted.append(started)
            static = (
                "objective", "assigned_agent", "assigning_agent",
                "responsible_member", "parent_task_id", "success_criteria",
                "context_digest", "grant_ids", "dependency_ids",
                "return_to_agent",
            )
            coherent = [
                record for record in group
                if record is not started
                and record.meta.ns > started.meta.ns
                and record.meta.actor == started.meta.actor
                and record.meta.signer == started.meta.signer
                and all(getattr(record, name) == getattr(started, name)
                        for name in static)
            ]
            progress = [record for record in coherent
                        if record.state is TaskState.ACTIVE
                        and record.progress == "Model work completed"
                        and record.result is None]
            terminals = [record for record in coherent
                         if record.state in _TERMINAL]
            if len(progress) > 1 or len(terminals) > 1:
                continue
            if progress:
                accepted.append(progress[0])
            if terminals and (not progress
                              or terminals[0].meta.ns > progress[0].meta.ns):
                accepted.append(terminals[0])
        accepted.sort(key=lambda item: (item.meta.ns, item.meta.id))
        return accepted
