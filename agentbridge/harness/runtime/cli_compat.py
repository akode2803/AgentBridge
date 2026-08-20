"""Provider-neutral contract observation for the existing CLI responder.

The CLI responder remains the sole executor and the signed run/task ledgers
remain canonical. This module validates the exact launch against those records
and exposes only an immutable in-process trace for tests and staged rollout.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
import time
from typing import Callable, Mapping, Sequence

from .contracts import (
    AGENT_CONTRACT_VERSION,
    AgentDefinition,
    AgentContractError,
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    AgentStreamEvent,
    AuthorityBinding,
    CanonicalValue,
    ContinuationBinding,
    ContinuationMode,
    PromptSpec,
    ProviderError,
    ProviderErrorCategory,
    StreamEventKind,
    UsageRecord,
    contract_digest,
    verify_invocation,
)
from .models import RunRecord, TaskRecord, TaskState

__all__ = [
    "CliContractSession", "CliContractTrace", "prepare_cli_contract",
]

_INSTRUCTIONS = "Execute the exact canonical CLI prompt in input.prompt."
_MAX_ACTIVITY_CHARS = 120


@dataclass(frozen=True, slots=True)
class CliContractTrace:
    """Immutable, non-authoritative observation retained only on Delivery."""

    definition: AgentDefinition
    invocation: AgentInvocationSpec
    events: tuple[AgentStreamEvent, ...]
    result: AgentResult


class CliContractSession:
    """Thread-safe event normalization with exactly one terminal settlement."""

    def __init__(
        self,
        definition: AgentDefinition,
        invocation: AgentInvocationSpec,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.definition = definition
        self.invocation = invocation
        self._clock = clock
        self._lock = threading.Lock()
        self._events: list[AgentStreamEvent] = []
        self._trace: CliContractTrace | None = None
        self._last_ns = 0
        self._append(StreamEventKind.STARTED, {"adapter": "current_cli"})

    def _next_ns(self) -> int:
        value = int(self._clock())
        value = max(value, self._last_ns + 1)
        self._last_ns = value
        return value

    def _append(self, kind: StreamEventKind, payload: dict) -> None:
        self._events.append(AgentStreamEvent(
            schema_version=AGENT_CONTRACT_VERSION,
            invocation_id=self.invocation.invocation_id,
            sequence=len(self._events) + 1,
            ns=self._next_ns(),
            kind=kind,
            payload=CanonicalValue.from_value(payload),
        ))

    def activity(self, line: str) -> None:
        normalized = " ".join((line or "").split())[:_MAX_ACTIVITY_CHARS]
        if not normalized:
            return
        with self._lock:
            if self._trace is None:
                self._append(StreamEventKind.ACTIVITY, {"text": normalized})

    def launch(self, argv: Sequence[str]) -> None:
        """Bind each actual subprocess attempt to the prepared launch set."""
        digest = hashlib.sha256(
            CanonicalValue.from_value(list(argv)).encoded,
        ).hexdigest()
        allowed = self.invocation.extensions.to_value().get(
            "x.agentbridge.argv_digests", [])
        if digest not in allowed:
            raise AgentContractError(
                "CLI launch arguments changed after contract validation")
        with self._lock:
            if self._trace is not None:
                raise AgentContractError(
                    "CLI launch attempted after terminal settlement")
            self._append(StreamEventKind.ACTIVITY, {"launch_digest": digest})

    def complete(self, text: str) -> CliContractTrace:
        if not isinstance(text, str) or not text.strip():
            return self.fail(ValueError("empty CLI output"), output_error=True)
        return self._settle(AgentResultStatus.COMPLETED, final_text=text)

    def fail(
        self,
        error: BaseException,
        *,
        timeout: bool = False,
        output_error: bool = False,
    ) -> CliContractTrace:
        category = (ProviderErrorCategory.TIMEOUT if timeout else
                    ProviderErrorCategory.OUTPUT if output_error else
                    ProviderErrorCategory.INTERNAL)
        code = ("timeout" if timeout else
                "invalid_output" if output_error else "internal")
        normalized = ProviderError.normalized(
            category, code, retryable=timeout,
            evidence_digest=hashlib.sha256(
                str(error).encode("utf-8", errors="replace"),
            ).hexdigest(),
        )
        return self._settle(AgentResultStatus.FAILED, error=normalized)

    def stop(self) -> CliContractTrace:
        return self._settle(AgentResultStatus.STOPPED)

    def _settle(
        self,
        status: AgentResultStatus,
        *,
        final_text: str | None = None,
        error: ProviderError | None = None,
    ) -> CliContractTrace:
        with self._lock:
            if self._trace is not None:
                return self._trace
            if status is AgentResultStatus.COMPLETED:
                self._append(StreamEventKind.OUTPUT, {"text": final_text})
            terminal_kind = {
                AgentResultStatus.COMPLETED: StreamEventKind.COMPLETED,
                AgentResultStatus.FAILED: StreamEventKind.FAILED,
                AgentResultStatus.STOPPED: StreamEventKind.STOPPED,
            }[status]
            result = AgentResult(
                schema_version=AGENT_CONTRACT_VERSION,
                invocation_id=self.invocation.invocation_id,
                status=status,
                last_event_sequence=len(self._events) + 1,
                final_output=None,
                final_text=final_text,
                usage=UsageRecord(),
                error=error,
            )
            self._append(terminal_kind, {"result_digest": contract_digest(result)})
            self._trace = CliContractTrace(
                definition=self.definition,
                invocation=self.invocation,
                events=tuple(self._events),
                result=result,
            )
            return self._trace


def prepare_cli_contract(
    *,
    delivery,
    provider: str,
    model: str,
    effort: str,
    timeout_s: float,
    minimal: bool,
    bridge_attached: bool,
    prompt: str,
    argv: Sequence[str],
    fallback_argv: Sequence[str] | None = None,
    env: Mapping[str, str],
    now_ns: Callable[[], int] = time.time_ns,
    event_clock: Callable[[], int] = time.monotonic_ns,
) -> CliContractSession:
    """Build and verify one exact CLI launch against signed canonical truth."""
    run = delivery.canonical_run
    task = delivery.canonical_task
    if not isinstance(run, RunRecord) or not isinstance(task, TaskRecord):
        raise AgentContractError(
            "signed run and root task are required for CLI contracts")
    settings = CanonicalValue.from_value({
        "bridge_attached": bool(bridge_attached),
        "effort": effort or "",
        "initial_minimal": bool(minimal),
        "timeout_s": float(timeout_s),
    })
    definition = AgentDefinition(
        schema_version=AGENT_CONTRACT_VERSION,
        definition_id=f"agentbridge.cli.{provider}",
        revision=1,
        name=f"AgentBridge current CLI ({provider})",
        provider=provider,
        model=model,
        prompt=PromptSpec(dynamic_resolver_id="agentbridge.cli.prompt.v1"),
        model_settings=settings,
        input_schema=CanonicalValue.from_value({"type": "object"}),
        output_schema=CanonicalValue.from_value({"type": "string"}),
        requested_tool_ids=(),
        approval_policy_id="agentbridge.current-cli",
        max_turns=1,
    )
    if task.parent_task_id is not None or task.state is not TaskState.ACTIVE:
        raise AgentContractError(
            "CLI contracts require the active canonical root task")
    authority = AuthorityBinding(
        run_id=delivery.run_id,
        task_id=delivery.task_id,
        chat_id=delivery.chat_id,
        agent=delivery.agent,
        responsible_member=run.responsible_member,
        run_record_id=run.meta.id,
        run_record_digest=contract_digest(run),
        task_record_id=task.meta.id,
        task_record_digest=contract_digest(task),
        key_epoch=run.meta.key_epoch,
        policy_revision=run.meta.policy_revision,
        membership_epoch=run.meta.membership_epoch,
        ownership_epoch=run.meta.ownership_epoch,
        capability_ids=run.capability_ceiling,
        grant_ids=task.grant_ids,
    )
    invocation = AgentInvocationSpec(
        schema_version=AGENT_CONTRACT_VERSION,
        invocation_id=f"{delivery.run_id}:current-cli",
        run_id=delivery.run_id,
        task_id=delivery.task_id,
        chat_id=delivery.chat_id,
        agent=delivery.agent,
        responsible_member=run.responsible_member,
        definition_id=definition.definition_id,
        definition_revision=definition.revision,
        definition_digest=contract_digest(definition),
        provider=provider,
        model=model,
        input=CanonicalValue.from_value({"prompt": prompt}),
        resolved_instructions=_INSTRUCTIONS,
        model_settings=settings,
        max_turns=1,
        authority=authority,
        continuation=ContinuationBinding(ContinuationMode.NONE),
        deadline_ns=int(now_ns() + timeout_s * 1_000_000_000),
        extensions=CanonicalValue.from_value({
            "x.agentbridge.argv_digests": [
                hashlib.sha256(CanonicalValue.from_value(list(candidate)).encoded)
                .hexdigest()
                for candidate in (argv, fallback_argv)
                if candidate is not None
            ],
            "x.agentbridge.env_names_digest": hashlib.sha256(
                CanonicalValue.from_value(sorted(env)).encoded,
            ).hexdigest(),
        }),
    )
    verify_invocation(definition, invocation, run, task)
    return CliContractSession(definition, invocation, clock=event_clock)
