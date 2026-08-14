"""Durable signed and room-encrypted canonical run events.

The current ``status/*_live.json`` and ``status/*_runs.json`` documents remain
compatibility projections. This ledger is the immutable authority for a run's
start and terminal outcome; task, handoff, and effect records land separately.
"""

from __future__ import annotations

import threading
from dataclasses import replace

from ...core.errors import ValidationError
from ...core.timekit import new_id, next_ns
from .authority import AuthorityError, authority
from .eventio import deliver_immutable, open_record, seal_record
from .models import (
    RecordKind, RecordMeta, RunRecord, RunState, RuntimeContractError,
)

__all__ = ["RunLedger", "RunLedgerError", "run_event_path", "run_prefix"]


class RunLedgerError(RuntimeContractError):
    """A canonical run event is malformed, forged, stale, or unavailable."""


_TERMINAL = {
    RunState.COMPLETED, RunState.FAILED, RunState.STOPPED,
    RunState.INTERRUPTED,
}


def run_prefix(chat_id: str, run_id: str = "") -> str:
    base = f"chats/{chat_id}/runtime/runs"
    return f"{base}/{run_id}" if run_id else base


def run_event_path(chat_id: str, run_id: str, record_id: str) -> str:
    return f"{run_prefix(chat_id, run_id)}/{record_id}.json"


def _state(value: str) -> RunState:
    mapping = {
        "running": RunState.RUNNING,
        "done": RunState.COMPLETED,
        "error": RunState.FAILED,
        "stopped": RunState.STOPPED,
        "interrupted": RunState.INTERRUPTED,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise RunLedgerError(f"unsupported compatibility run state: {value}") from exc


class RunLedger:
    """Write and read one chat's canonical run-event stream."""

    OUTBOX_KIND = "runtime-run-event"

    def __init__(self, mesh, *, fresh_reads: bool = True,
                 register_outbox: bool = True,
                 read_snapshot: dict | None = None) -> None:
        self.mesh = mesh
        self.fresh_reads = fresh_reads
        self.read_snapshot = read_snapshot
        self._lock = threading.RLock()
        if register_outbox:
            mesh.outbox.handlers[self.OUTBOX_KIND] = self._deliver

    def _deliver(self, _target: str, payload: dict) -> None:
        path = payload.get("path")
        doc = payload.get("doc")
        if not isinstance(path, str) or not path or not isinstance(doc, dict):
            raise ValidationError("runtime run outbox payload is malformed")
        deliver_immutable(self.mesh.tx, path, doc)
        # Cleanup is part of delivery: the outbox row remains retryable until
        # the matching terminal intent has durably disappeared.
        self._delivered(_target, payload)

    def _payload(self, record: RunRecord, *, terminal: bool) -> tuple[str, dict]:
        try:
            doc = seal_record(self.mesh, record)
        except RuntimeContractError as exc:
            raise RunLedgerError(str(exc)) from exc
        path = run_event_path(record.meta.chat_id, record.meta.run_id or "",
                              record.meta.id)
        target = run_prefix(record.meta.chat_id, record.meta.run_id or "")
        payload = {
            "path": path, "doc": doc, "chat_id": record.meta.chat_id,
            "run_id": record.meta.run_id, "record_id": record.meta.id,
            "terminal": terminal,
        }
        return target, payload

    def _attempt(self, seq: int, target: str, payload: dict) -> None:
        try:
            self._deliver(target, payload)
        except ValidationError as exc:
            self.mesh.store.outbox_dead(seq, f"{type(exc).__name__}: {exc}")
            raise RunLedgerError("canonical run event conflicts with its ledger") from exc
        except Exception as exc:
            self.mesh.store.outbox_retry(seq, f"{type(exc).__name__}: {exc}", 1.0)
            self.mesh.outbox.notify()
        else:
            try:
                self.mesh.store.outbox_done(seq)
            except Exception:  # remote event and local cleanup already landed
                # Leave the idempotent row for the ordinary worker. Raising
                # here would make the runner execute an already-finished
                # trigger again merely because local queue cleanup was late.
                self.mesh.outbox.notify()

    def _delivered(self, _target: str, payload: dict) -> None:
        if not payload.get("terminal"):
            return
        run_id = str(payload.get("run_id") or "")
        record_id = str(payload.get("record_id") or "")
        with self._lock:
            opened = self._opened()
            entry = opened.get(run_id)
            terminal = entry.get("terminal") if isinstance(entry, dict) else None
            if (isinstance(terminal, dict)
                    and terminal.get("record_id") == record_id):
                opened.pop(run_id, None)
                self.mesh.store.cache_doc("runtime/run-open", opened)

    def start(self, *, run_id: str, chat_id: str, trigger_id: str,
              provider: str, model: str,
              execution_level: str = "brokered_native",
              capability_ceiling: tuple[str, ...] = (),
              native_policy_digest: str = "",
              provider_policy_digest: str = "",
              native_provider_version: str = "",
              native_enabled: tuple[str, ...] = (),
              native_approval_gated: tuple[str, ...] = (),
              native_blocked: tuple[str, ...] = (),
              active_task_ids: tuple[str, ...] = (),
              policy_revision: int | None = None) -> RunRecord:
        with self._lock:
            record, target, payload, opened = self._prepare_start(
                run_id=run_id, chat_id=chat_id, trigger_id=trigger_id,
                provider=provider, model=model, execution_level=execution_level,
                capability_ceiling=capability_ceiling,
                native_policy_digest=native_policy_digest,
                provider_policy_digest=provider_policy_digest,
                native_provider_version=native_provider_version,
                native_enabled=native_enabled,
                native_approval_gated=native_approval_gated,
                native_blocked=native_blocked,
                active_task_ids=active_task_ids,
                policy_revision=policy_revision,
            )
            seq = self.mesh.store.cache_doc_and_outbox_add(
                "runtime/run-open", opened, self.OUTBOX_KIND, target, payload,
            )
        self._attempt(seq, target, payload)
        return record

    def _prepare_start(self, *, run_id: str, chat_id: str, trigger_id: str,
                       provider: str, model: str,
                       execution_level: str = "brokered_native",
                       capability_ceiling: tuple[str, ...] = (),
                       native_policy_digest: str = "",
                       provider_policy_digest: str = "",
                       native_provider_version: str = "",
                       native_enabled: tuple[str, ...] = (),
                       native_approval_gated: tuple[str, ...] = (),
                       native_blocked: tuple[str, ...] = (),
                       active_task_ids: tuple[str, ...] = (),
                       policy_revision: int | None = None,
                       ) -> tuple[RunRecord, str, dict, dict]:
        """Build a start and updated recovery map without committing either."""
        auth = authority(self.mesh, self.mesh.user, chat_id)
        if (policy_revision is not None
                and int(auth["policy_revision"]) != int(policy_revision)):
            raise RunLedgerError("harness policy changed before run start")
        ns = next_ns()
        epoch, _key = self.mesh.keys.ensure(chat_id, self.mesh.snapshot(chat_id))
        meta = RecordMeta(
            schema_version=1, kind=RecordKind.RUN, id=new_id("run-event", ns),
            ns=ns, actor=self.mesh.user, chat_id=chat_id,
            signer=self.mesh.user, root_run_id=run_id, run_id=run_id,
            task_id=None, call_id=None, key_epoch=epoch,
            policy_revision=int(auth["policy_revision"]),
            membership_epoch=int(auth["membership_epoch"]),
            ownership_epoch=int(auth["ownership_epoch"]), expires_ns=None,
        )
        record = RunRecord(
            meta=meta, state=RunState.RUNNING, trigger_id=trigger_id,
            manager_agent=self.mesh.user,
            responsible_member=str(auth["owner"]),
            execution_level=execution_level, provider=provider, model=model,
            capability_ceiling=capability_ceiling, active_task_ids=active_task_ids,
            status="Starting up", outcome=None,
            native_policy_digest=native_policy_digest,
            provider_policy_digest=provider_policy_digest,
            native_provider_version=native_provider_version,
            native_enabled=native_enabled,
            native_approval_gated=native_approval_gated,
            native_blocked=native_blocked,
        )
        target, payload = self._payload(record, terminal=False)
        opened = self._opened()
        opened[run_id] = {"start": record.to_dict(), "terminal": None}
        return record, target, payload, opened

    def finish(self, run_id: str, state: str, status: str) -> RunRecord:
        with self._lock:
            opened, raw, intent, queued = self._terminal_intent(
                run_id, state, status,
            )
            if queued is not None:
                return queued
            self.mesh.store.cache_doc("runtime/run-open", opened)
            record, target, payload = self._build_terminal(raw, intent)
            opened = self._opened()
            opened[run_id] = {
                "start": raw,
                "terminal": {
                    **intent, "queued": True, "record_id": record.meta.id,
                    "record": record.to_dict(),
                },
            }
            seq = self.mesh.store.cache_doc_and_outbox_add(
                "runtime/run-open", opened, self.OUTBOX_KIND, target, payload,
            )
        self._attempt(seq, target, payload)
        return record

    def _terminal_intent(
        self, run_id: str, state: str, status: str,
    ) -> tuple[dict, dict, dict, RunRecord | None]:
        terminal = _state(state)
        if terminal not in _TERMINAL:
            raise RunLedgerError("run finish must be terminal")
        opened = self._opened()
        entry = opened.get(run_id)
        raw = entry.get("start") if isinstance(entry, dict) else None
        if isinstance(entry, dict) and "meta" in entry:
            raw = entry  # pre-R125 development fixture compatibility
        if not isinstance(raw, dict):
            raise RunLedgerError("canonical run start is unavailable")
        current = entry.get("terminal") if isinstance(entry, dict) else None
        if isinstance(current, dict) and current.get("queued"):
            return opened, raw, current, RunRecord.from_dict(current["record"])
        intent = current if isinstance(current, dict) else {
            "state": state, "status": " ".join(status.split())[:160],
            "queued": False,
        }
        if intent.get("state") != state:
            raise RunLedgerError("run already has a different terminal intent")
        opened[run_id] = {"start": raw, "terminal": intent}
        return opened, raw, intent, None

    def _build_terminal(
        self, raw: dict, intent: dict,
    ) -> tuple[RunRecord, str, dict]:
        terminal = _state(str(intent.get("state")))
        if terminal not in _TERMINAL:
            raise RunLedgerError("run finish must be terminal")
        started = RunRecord.from_dict(raw)
        authority(self.mesh, self.mesh.user, started.meta.chat_id)
        ns = max(next_ns(), started.meta.ns + 1)
        epoch, _key = self.mesh.keys.ensure(
            started.meta.chat_id, self.mesh.snapshot(started.meta.chat_id),
        )
        meta = replace(
            started.meta, id=new_id("run-event", ns), ns=ns, key_epoch=epoch,
        )
        status = " ".join(str(intent.get("status") or "").split())[:160]
        record = replace(
            started, meta=meta, state=terminal,
            status=status or terminal.value, outcome=terminal.value,
        )
        target, payload = self._payload(record, terminal=True)
        return record, target, payload

    def _opened(self) -> dict[str, dict]:
        value = self.mesh.store.cached_doc("runtime/run-open", default={})
        return dict(value) if isinstance(value, dict) else {}

    def recover_open(self) -> int:
        """Close runs left open by a previous process before accepting work."""
        recovered = 0
        for run_id, entry in list(self._opened().items()):
            try:
                terminal = entry.get("terminal") if isinstance(entry, dict) else None
                if isinstance(terminal, dict) and terminal.get("queued"):
                    continue
                self.finish(
                    run_id,
                    str(terminal.get("state")) if isinstance(terminal, dict)
                    else "interrupted",
                    str(terminal.get("status")) if isinstance(terminal, dict)
                    else "Interrupted - the app or agent restarted mid-run",
                )
                recovered += 1
            except (AuthorityError, RunLedgerError):
                # Membership/ownership drift intentionally invalidates the
                # continuation. Keep the local evidence for a future explicit
                # retention/quarantine policy instead of forging a terminal.
                continue
        return recovered

    def retry_terminals(self) -> int:
        """Retry terminal intents that failed before reaching the outbox."""
        retried = 0
        for run_id, entry in list(self._opened().items()):
            terminal = entry.get("terminal") if isinstance(entry, dict) else None
            if not isinstance(terminal, dict) or terminal.get("queued"):
                continue
            try:
                self.finish(run_id, str(terminal.get("state")),
                            str(terminal.get("status")))
                retried += 1
            except Exception:  # noqa: BLE001 - a retry failure cannot stop the harness
                continue
        return retried

    def has_terminal_intent(self, run_id: str) -> bool:
        with self._lock:
            entry = self._opened().get(run_id)
            return bool(isinstance(entry, dict) and entry.get("terminal"))

    def read(self, chat_id: str, run_id: str = "") -> list[RunRecord]:
        snap = self.mesh.snapshot(chat_id)
        if not snap.is_member(self.mesh.user):
            raise AuthorityError("viewer is not a current room member")
        records: list[RunRecord] = []
        prefix = run_prefix(chat_id, run_id) + "/"
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
                    self.mesh, snap, doc, RecordKind.RUN, RunRecord.from_dict,
                )
                meta = record.meta
                if meta.chat_id != chat_id:
                    raise RunLedgerError("run envelope routing mismatch")
                if run_id and meta.run_id != run_id:
                    raise RunLedgerError("run id does not match exact lookup")
                if path != run_event_path(chat_id, meta.run_id or "", meta.id):
                    raise RunLedgerError("run envelope path mismatch")
                if record.manager_agent != meta.actor:
                    raise RunLedgerError("run manager and signer differ")
                current = authority(self.mesh, meta.actor, chat_id)
                if record.responsible_member != current["owner"]:
                    raise RunLedgerError("run responsible member is stale")
                latest_key = self.mesh.keys.latest(chat_id)
                if latest_key is None or meta.key_epoch != latest_key[0]:
                    raise RunLedgerError("run key_epoch is stale")
                for name in ("policy_revision", "membership_epoch",
                             "ownership_epoch"):
                    if getattr(meta, name) != int(current[name]):
                        raise RunLedgerError(f"run {name} is stale")
                records.append(record)
            except (AuthorityError, RunLedgerError, RuntimeContractError,
                    TypeError, ValueError):
                continue
        return self._fold(records)

    @staticmethod
    def _fold(records: list[RunRecord]) -> list[RunRecord]:
        """Accept one unambiguous start and at most one coherent terminal."""
        accepted: list[RunRecord] = []
        grouped: dict[str, list[RunRecord]] = {}
        for record in records:
            grouped.setdefault(record.meta.run_id or "", []).append(record)
        for group in grouped.values():
            starts = [record for record in group
                      if record.state is RunState.RUNNING
                      and record.manager_agent == record.meta.actor
                      and record.outcome is None
                      and record.status == "Starting up"]
            if len(starts) != 1:
                continue
            start = starts[0]
            accepted.append(start)
            static = (
                "trigger_id", "manager_agent", "responsible_member",
                "execution_level", "provider", "model", "capability_ceiling",
                "active_task_ids", "native_policy_digest",
                "provider_policy_digest",
                "native_provider_version", "native_enabled",
                "native_approval_gated", "native_blocked",
            )
            terminals = [
                record for record in group
                if record.state in _TERMINAL
                and record.meta.ns > start.meta.ns
                and record.meta.actor == start.meta.actor
                and record.meta.signer == start.meta.signer
                and record.outcome == record.state.value
                and all(getattr(record, name) == getattr(start, name)
                        for name in static)
            ]
            if len(terminals) == 1:
                accepted.append(terminals[0])
        accepted.sort(key=lambda item: (item.meta.ns, item.meta.id))
        return accepted
