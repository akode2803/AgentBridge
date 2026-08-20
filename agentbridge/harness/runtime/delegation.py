"""Depth-one, same-room, manager-retained agent-tool orchestration."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace

from ...core.errors import ValidationError
from ...core.jsonkit import canonical_json_bytes
from ..adapters.cli import ChildRequest
from ..prompt import TRANSCRIPT_TAIL, render_message
from ..settings import HarnessSettings
from .authority import authority
from .handoffs import HandoffLedger, HandoffLedgerError, HandoffView
from .models import HandoffState, HandoffType, RunState

__all__ = ["ChildWork", "DelegationCoordinator", "DelegationError"]


class DelegationError(ValidationError):
    """A delegation cannot execute safely or its result is unavailable."""


@dataclass(frozen=True, slots=True)
class ChildWork:
    chat_id: str
    run_id: str
    task_id: str
    handoff_id: str
    source_agent: str
    objective: str
    success_criteria: tuple[str, ...]
    claim_token: str


class DelegationCoordinator:
    """Bridge one blocking manager tool call to one destination sidecar."""

    JOURNAL_PREFIX = "runtime/delegation-execution"
    CONSUME_PATH = "runtime/delegation-consume-pending"
    CLAIM_SCOPE = "runtime-delegation-execution"
    ACCEPTANCE_S = 60.0
    EXECUTION_S = 900.0
    RESULT_CHARS = 6_000
    POLL_S = 0.25
    CLAIM_LEASE_S = 30.0

    def __init__(self, mesh, ledger: HandoffLedger, *, machine: str,
                 stopping=None) -> None:
        self.mesh = mesh
        self.ledger = ledger
        self.machine = machine
        self.instance_id = hashlib.sha256(
            f"{machine}|{id(self)}|{time.time_ns()}".encode("utf-8"),
        ).hexdigest()[:24]
        self._lock = threading.RLock()
        self._stopped: set[str] = set()
        self._stopping = stopping

    def delegate(self, *, chat_id: str, run_id: str, parent_task_id: str,
                 destination_agent: str, objective: str, reason: str,
                 success_criteria: tuple[str, ...], cancelled=None) -> str:
        """Offer, authorize, and wait inside the manager's live MCP call."""
        source_settings = HarnessSettings.from_account(
            self.mesh.directory.get(self.mesh.user),
        )
        runs = self.ledger.run_ledger.read(chat_id, run_id)
        if (len(runs) != 1 or runs[0].state is not RunState.RUNNING
                or "delegate_agent" not in runs[0].capability_ceiling):
            raise DelegationError(
                "this canonical run is not authorized to delegate")
        destination = self.mesh.directory.get(destination_agent)
        destination_settings = HarnessSettings.from_account(destination)
        if not source_settings.agent_tools_enabled:
            raise DelegationError("agent tools are disabled for this manager")
        if not destination_settings.agent_tools_enabled:
            raise DelegationError(
                f"@{destination_agent} is not accepting agent-tool work",
            )
        view = self.ledger.offer(
            chat_id=chat_id, run_id=run_id,
            parent_task_id=parent_task_id,
            destination_agent=destination_agent, objective=objective,
            reason=reason, success_criteria=success_criteria,
            requested_capabilities=(), handoff_type=HandoffType.AGENT_TOOL,
            timeout_s=self.ACCEPTANCE_S,
        )
        handoff_id = view.events[0].meta.call_id or ""
        execution_deadline = 0
        while True:
            if callable(self._stopping) and self._stopping():
                return "delegation stopped because the manager runner is stopping"
            if callable(cancelled) and cancelled():
                return "delegation stopped because the manager run ended"
            self._sync(chat_id)
            current = self._view(chat_id, run_id, handoff_id)
            states = [event.state for event in current.events]
            if states[-1] is HandoffState.DECLINED:
                detail = current.events[-1].result or "declined"
                return f"@{destination_agent} declined the task: {detail}"
            if states[-1] is HandoffState.TIMED_OUT:
                return f"@{destination_agent} did not accept before the deadline"
            if states[-1] is HandoffState.ACCEPTED:
                authorized = self.ledger.authorize(
                    chat_id=chat_id, run_id=run_id, handoff_id=handoff_id,
                    execution_timeout_s=self.EXECUTION_S,
                )
                execution_deadline = int(authorized.meta.expires_ns or 0)
                continue
            if HandoffState.AUTHORIZED in states and not execution_deadline:
                authorized = next(event for event in current.events
                                  if event.state is HandoffState.AUTHORIZED)
                execution_deadline = int(authorized.meta.expires_ns or 0)
            if states[-1] is HandoffState.RETURNED:
                payload = self._payload(current.events[-1])
                contribution = str(payload.get("contribution") or "").strip()
                if not contribution:
                    raise DelegationError("the specialist result is unavailable")
                self._remember_consumption(
                    run_id=run_id, chat_id=chat_id, handoff_id=handoff_id,
                    returned_id=current.events[-1].meta.id,
                )
                return contribution
            if states[-1] in {HandoffState.INTERRUPTED, HandoffState.STOPPED}:
                detail = self._payload(current.events[-1]).get("reason")
                return (f"@{destination_agent}'s task was "
                        f"{states[-1].value}: {detail or 'no result was returned'}")

            now = time.time_ns()
            acceptance_deadline = int(current.events[0].meta.expires_ns or 0)
            if len(current.events) == 1 and now >= acceptance_deadline:
                try:
                    self.ledger.timeout(
                        chat_id=chat_id, run_id=run_id,
                        handoff_id=handoff_id,
                    )
                except HandoffLedgerError:
                    return (f"@{destination_agent}'s decision arrived after "
                            "the acceptance window; it was not authorized")
                return f"@{destination_agent} did not accept before the deadline"
            if execution_deadline and now >= execution_deadline:
                return (f"@{destination_agent}'s task exceeded its execution "
                        "deadline; no contribution was consumed")
            time.sleep(self.POLL_S)

    def consume_for_run(self, run_id: str) -> int:
        """Consume only results durably marked after provider completion."""
        with self._lock:
            pending = self._pending_consumptions()
            entries = list(pending.get(run_id) or [])
        consumed = 0
        remaining = []
        for entry in entries:
            if not int(entry.get("provider_completed_ns") or 0):
                remaining.append(entry)
                continue
            try:
                self.ledger.consume(
                    chat_id=str(entry["chat_id"]), run_id=run_id,
                    handoff_id=str(entry["handoff_id"]),
                )
            except Exception:
                remaining.append(entry)
            else:
                consumed += 1
        if entries != remaining:
            with self._lock:
                pending = self._pending_consumptions()
                if remaining:
                    pending[run_id] = remaining
                else:
                    pending.pop(run_id, None)
                self.mesh.store.cache_doc(self.CONSUME_PATH, pending)
        return consumed

    def mark_provider_completed(self, run_id: str) -> int:
        """Durably make this run's returned tool results consumable."""
        with self._lock:
            pending = self._pending_consumptions()
            entries = list(pending.get(run_id) or [])
            completed_ns = time.time_ns()
            changed = 0
            for entry in entries:
                if not int(entry.get("provider_completed_ns") or 0):
                    entry["provider_completed_ns"] = completed_ns
                    changed += 1
            if changed:
                pending[run_id] = entries
                self.mesh.store.cache_doc(self.CONSUME_PATH, pending)
            return changed

    def retry_consumptions(self) -> int:
        """Retry post-provider acknowledgements without replaying providers."""
        with self._lock:
            run_ids = tuple(self._pending_consumptions())
        return sum(self.consume_for_run(run_id) for run_id in run_ids)

    def claim_ready(self, *, exclude: set[str]) -> list[ChildWork]:
        """Accept eligible offers and claim authorized work for this host."""
        settings = HarnessSettings.from_account(
            self.mesh.directory.get(self.mesh.user),
        )
        work: list[ChildWork] = []
        for snap in self.mesh.membership.chats_for():
            try:
                views = self.ledger.read(snap.id)
            except Exception:
                continue
            for view in views:
                offer = view.events[0]
                if (offer.destination_agent != self.mesh.user
                        or offer.handoff_type is not HandoffType.AGENT_TOOL
                        or offer.requested_capabilities):
                    continue
                if (len(view.events) == 1 and settings.agent_tools_enabled
                        and time.time_ns() < int(offer.meta.expires_ns or 0)):
                    try:
                        self.ledger.decide(
                            chat_id=snap.id, run_id=offer.meta.run_id or "",
                            handoff_id=offer.meta.call_id or "", accept=True,
                            result="Accepted for text-only specialist work",
                        )
                    except HandoffLedgerError:
                        pass
                    continue
                handoff_id = offer.meta.call_id or ""
                if handoff_id in exclude or not self._host_current():
                    continue
                item = self._recover_or_claim(view)
                if item is not None:
                    work.append(item)
        return work

    def execute(self, work: ChildWork, responder) -> None:
        """Run one claimed child and settle its encrypted lifecycle."""
        path = self._journal_path(work.handoff_id)
        journal = self.mesh.store.cached_doc(path, default={}) or {}
        if (journal.get("state") != "claimed"
                or journal.get("claim_token") != work.claim_token
                or not self._host_current()):
            return
        try:
            context, manifest = self._context(work.chat_id)
            request = ChildRequest(
                objective=work.objective,
                success_criteria=work.success_criteria,
                rendered_context=context,
                max_output_chars=self.RESULT_CHARS,
            )
            prepared = responder.prepare_child(request, chat_id=work.chat_id)
            view = self._view(work.chat_id, work.run_id, work.handoff_id)
            authorized = next(event for event in view.events
                              if event.state is HandoffState.AUTHORIZED)
            remaining_s = max(
                0.1, (int(authorized.meta.expires_ns or 0) - time.time_ns()) / 1e9,
            )
            if hasattr(prepared, "timeout_s"):
                prepared = replace(
                    prepared, timeout_s=min(float(prepared.timeout_s), remaining_s),
                )
            manifest = {**manifest, "prompt_digest": prepared.prompt_digest,
                        "provider": prepared.provider, "model": prepared.model}
            active = self.ledger.activate(
                chat_id=work.chat_id, run_id=work.run_id,
                handoff_id=work.handoff_id, manifest=manifest,
            )
            current = self._view(work.chat_id, work.run_id, work.handoff_id)
            if (current.events[-1].state is not HandoffState.ACTIVE
                    or current.events[-1].meta.id != active.meta.id):
                pending = self.ledger._pending_record(work.handoff_id)
                if (pending is not None
                        and pending.state is HandoffState.ACTIVE
                        and pending.meta.id == active.meta.id):
                    journal.update({
                        "state": "awaiting_active",
                        "active_record_id": active.meta.id,
                        "manifest": manifest,
                        "prompt_digest": prepared.prompt_digest,
                        "updated_ns": time.time_ns(),
                    })
                    self.mesh.store.cache_doc(path, journal)
                    return
                raise DelegationError(
                    "child activation became ambiguous before invocation",
                )
            executing = {**journal,
                "state": "executing", "manifest": manifest,
                "active_record_id": active.meta.id,
                "prompt_digest": prepared.prompt_digest,
                "updated_ns": time.time_ns(),
            }
            if not self.mesh.store.replace_cached_doc_if(
                    path, {"state": "claimed",
                           "claim_token": work.claim_token}, executing):
                return
            journal = executing
            if not self._execution_allowed(work):
                raise DelegationError("child authority changed before execution")
            result = responder.respond_child(
                prepared, cancelled=lambda: not self._execution_allowed(work),
            )
            if not self._execution_allowed(work):
                raise DelegationError("child authority changed during execution")
            journal = {**journal,
                "state": "result_ready", "result": result.text,
                "prompt_digest": result.prompt_digest,
                "updated_ns": time.time_ns(),
            }
            self.mesh.store.cache_doc(path, journal)
            self.ledger.return_result(
                chat_id=work.chat_id, run_id=work.run_id,
                handoff_id=work.handoff_id, contribution=result.text,
                prompt_digest=result.prompt_digest,
            )
            journal.update({"state": "committed", "updated_ns": time.time_ns()})
            self.mesh.store.cache_doc(path, journal)
        except Exception as exc:
            journal.update({
                "state": "interrupted", "error": type(exc).__name__,
                "updated_ns": time.time_ns(),
            })
            self.mesh.store.cache_doc(path, journal)
            try:
                self.ledger.interrupt(
                    chat_id=work.chat_id, run_id=work.run_id,
                    handoff_id=work.handoff_id,
                    reason=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
            except Exception:
                pass

    def _recover_or_claim(self, view: HandoffView) -> ChildWork | None:
        offer = view.events[0]
        handoff_id = offer.meta.call_id or ""
        path = self._journal_path(handoff_id)
        journal = self.mesh.store.cached_doc(path, default={}) or {}
        last = view.events[-1].state
        if last is HandoffState.RETURNED:
            if journal.get("state") == "result_ready":
                journal.update({"state": "committed", "updated_ns": time.time_ns()})
                self.mesh.store.cache_doc(path, journal)
            return None
        if last is HandoffState.ACTIVE and journal.get("state") == "result_ready":
            try:
                self.ledger.return_result(
                    chat_id=offer.meta.chat_id, run_id=offer.meta.run_id or "",
                    handoff_id=handoff_id,
                    contribution=str(journal.get("result") or ""),
                    prompt_digest=str(journal.get("prompt_digest") or ""),
                )
                journal.update({"state": "committed",
                                "updated_ns": time.time_ns()})
                self.mesh.store.cache_doc(path, journal)
            except Exception as exc:
                journal.update({
                    "state": "interrupted", "updated_ns": time.time_ns(),
                    "error": f"result_recovery:{type(exc).__name__}",
                })
                self.mesh.store.cache_doc(path, journal)
                try:
                    self.ledger.interrupt(
                        chat_id=offer.meta.chat_id,
                        run_id=offer.meta.run_id or "",
                        handoff_id=handoff_id,
                        reason="Saved child result could no longer be returned",
                    )
                except Exception:
                    pass
            return None
        if last is HandoffState.ACTIVE and journal.get("state") == "executing":
            journal.update({"state": "interrupted", "updated_ns": time.time_ns(),
                            "error": "runner_restarted_during_execution"})
            self.mesh.store.cache_doc(path, journal)
            try:
                self.ledger.interrupt(
                    chat_id=offer.meta.chat_id, run_id=offer.meta.run_id or "",
                    handoff_id=handoff_id,
                    reason="Runner restarted during child execution",
                )
            except Exception:
                pass
            return None
        if last not in {HandoffState.AUTHORIZED, HandoffState.ACTIVE}:
            return None

        item_values = {
            "chat_id": offer.meta.chat_id, "run_id": offer.meta.run_id or "",
            "task_id": offer.meta.task_id or "", "handoff_id": handoff_id,
            "source_agent": offer.source_agent,
            "objective": view.task.objective,
            "success_criteria": view.task.success_criteria,
        }
        if not journal:
            prepared = {
                "state": "prepared", "machine": self.machine,
                **item_values,
                "success_criteria": list(view.task.success_criteria),
                "updated_ns": time.time_ns(),
            }
            self.mesh.store.claim_with_doc(
                self.CLAIM_SCOPE, handoff_id, offer.meta.ns, path, prepared,
            )
            journal = self.mesh.store.cached_doc(path, default={}) or {}
        if journal.get("machine") != self.machine:
            return None
        now = time.time_ns()
        state = journal.get("state")
        if state == "awaiting_active":
            pending = self.ledger._pending_record(handoff_id)
            if (last is HandoffState.AUTHORIZED and pending is not None
                    and pending.state is HandoffState.ACTIVE
                    and pending.meta.id == journal.get("active_record_id")):
                return None
            journal.update({"state": "prepared", "updated_ns": now})
            self.mesh.store.cache_doc(path, journal)
            state = "prepared"
        expected = {"state": state}
        if state == "claimed":
            if int(journal.get("claim_lease_ns") or 0) > now:
                return None
            expected["claim_token"] = journal.get("claim_token")
        elif state != "prepared":
            return None
        token = hashlib.sha256(
            f"{self.instance_id}|{handoff_id}|{now}".encode("utf-8"),
        ).hexdigest()[:32]
        claimed = {**journal, "state": "claimed", "claim_token": token,
                   "claim_lease_ns": now + int(self.CLAIM_LEASE_S * 1e9),
                   "updated_ns": now}
        if not self.mesh.store.replace_cached_doc_if(
                path, expected, claimed):
            return None
        return ChildWork(**item_values, claim_token=token)

    def _execution_allowed(self, work: ChildWork) -> bool:
        if callable(self._stopping) and self._stopping():
            return False
        if not self._host_current():
            return False
        with self._lock:
            if work.handoff_id in self._stopped:
                return False
        try:
            from .controls import consume_owner_command

            stopped = consume_owner_command(
                self.mesh, target=self.mesh.user, action="stop",
                chat_id=work.chat_id, run_id=work.run_id,
            )
            if stopped is not None:
                with self._lock:
                    self._stopped.add(work.handoff_id)
                return False
        except Exception:
            pass
        try:
            view = self._view(work.chat_id, work.run_id, work.handoff_id)
            authorized = next(
                event for event in view.events
                if event.state is HandoffState.AUTHORIZED
            )
            active = next(
                event for event in view.events
                if event.state is HandoffState.ACTIVE
            )
            current = authority(self.mesh, self.mesh.user, work.chat_id)
            return (time.time_ns() < int(authorized.meta.expires_ns or 0)
                    and view.events[-1].state is HandoffState.ACTIVE
                    and active.meta.policy_revision
                        == int(current["policy_revision"])
                    and self.ledger._parent_open(view))
        except Exception:
            return False

    def _context(self, chat_id: str) -> tuple[str, dict]:
        messages = self.mesh.messages_for(chat_id, breadcrumbs=True)
        selected = messages[-TRANSCRIPT_TAIL:]
        lines = [render_message(message, self.mesh.user) for message in selected]
        lines = [line for line in lines if line]
        entries = []
        for message, line in zip(selected, [render_message(
                item, self.mesh.user) for item in selected]):
            if not line:
                continue
            entries.append({
                "id": message.id, "ns": message.ns,
                "edit_ns": int((message.edited or {}).get("ns") or 0),
                "deleted": bool(message.deleted),
                "line_digest": hashlib.sha256(
                    line.encode("utf-8")).hexdigest(),
            })
        context = "\n".join(lines)
        manifest = {
            "version": 1, "history_policy": "destination_tail_exact",
            "messages": entries,
            "omitted_count": max(0, len(messages) - len(selected)),
            "context_digest": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        }
        manifest["manifest_digest"] = hashlib.sha256(
            canonical_json_bytes(manifest),
        ).hexdigest()
        return context, manifest

    def _host_current(self) -> bool:
        account = self.mesh.directory.get(self.mesh.user)
        return bool(account and account.active and account.agent
                    and account.agent.machine == self.machine)

    def _view(self, chat_id: str, run_id: str,
              handoff_id: str) -> HandoffView:
        views = self.ledger.read(chat_id, run_id, handoff_id)
        if len(views) != 1:
            raise DelegationError("the canonical handoff became unavailable")
        return views[0]

    def _sync(self, chat_id: str) -> None:
        try:
            self.mesh.sync.sync_once([chat_id])
        except Exception:
            pass

    @staticmethod
    def _payload(record) -> dict:
        try:
            value = json.loads(record.result or "")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _journal_path(self, handoff_id: str) -> str:
        return f"{self.JOURNAL_PREFIX}/{handoff_id}"

    def _remember_consumption(self, *, run_id: str, chat_id: str,
                              handoff_id: str, returned_id: str) -> None:
        with self._lock:
            pending = self._pending_consumptions()
            entries = list(pending.get(run_id) or [])
            entry = {
                "chat_id": chat_id, "handoff_id": handoff_id,
                "returned_id": returned_id,
                "provider_completed_ns": 0,
            }
            if entry not in entries:
                entries.append(entry)
            pending[run_id] = entries
            self.mesh.store.cache_doc(self.CONSUME_PATH, pending)

    def _pending_consumptions(self) -> dict:
        value = self.mesh.store.cached_doc(self.CONSUME_PATH, default={})
        return dict(value) if isinstance(value, dict) else {}
