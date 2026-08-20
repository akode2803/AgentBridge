"""Deterministic C2 fixtures for adapter contract and fault-injection tests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import threading
from typing import Any, Callable

from .contracts import (
    AGENT_CONTRACT_VERSION,
    AgentContractError,
    AgentInterruption,
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    AgentStreamEvent,
    CanonicalValue,
    ContinuationBinding,
    ContinuationMode,
    InterruptionKind,
    ProviderError,
    ProviderErrorCategory,
    StreamEventKind,
    TERMINAL_EVENT_KINDS,
    UsageRecord,
    contract_digest,
    invocation_context_digest,
)


_UNSET = object()


@dataclass(slots=True)
class FakeClock:
    """A monotonic nanosecond clock advanced only by the fixture."""

    value: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise AgentContractError("fake clock must start at a positive ns")

    def now_ns(self) -> int:
        return self.value

    def advance(self, amount: int = 1) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise AgentContractError("fake clock advance must be positive")
        self.value += amount
        return self.value


class FakeEventStore:
    """Strict in-memory event store with an explicit settlement proof."""

    def __init__(self, invocation_id: str) -> None:
        if not isinstance(invocation_id, str) or not invocation_id.strip():
            raise AgentContractError("fake event store needs an invocation id")
        self.invocation_id = invocation_id
        self.events: list[AgentStreamEvent] = []
        self._terminal = False

    def append(self, event: AgentStreamEvent) -> None:
        if event.invocation_id != self.invocation_id:
            raise AgentContractError("event belongs to another invocation")
        if self._terminal:
            raise AgentContractError("events cannot follow a terminal event")
        expected = len(self.events) + 1
        if event.sequence != expected:
            raise AgentContractError(
                f"event sequence must be contiguous; expected {expected}")
        if self.events and event.ns <= self.events[-1].ns:
            raise AgentContractError("event ns must increase monotonically")
        if not self.events and event.kind is not StreamEventKind.STARTED:
            raise AgentContractError("the first event must be started")
        if self.events and event.kind is StreamEventKind.STARTED:
            raise AgentContractError("started can appear only once")
        self.events.append(event)
        self._terminal = event.kind in TERMINAL_EVENT_KINDS

    @property
    def terminal_count(self) -> int:
        return sum(event.kind in TERMINAL_EVENT_KINDS for event in self.events)

    def assert_settled(self, result: AgentResult) -> None:
        expected = {
            AgentResultStatus.COMPLETED: StreamEventKind.COMPLETED,
            AgentResultStatus.FAILED: StreamEventKind.FAILED,
            AgentResultStatus.STOPPED: StreamEventKind.STOPPED,
            AgentResultStatus.INTERRUPTED: StreamEventKind.INTERRUPTED,
        }[result.status]
        if self.terminal_count != 1 or not self.events:
            raise AgentContractError("a settled run needs exactly one terminal event")
        if self.events[-1].kind is not expected:
            raise AgentContractError("terminal event does not match result status")
        if result.invocation_id != self.invocation_id:
            raise AgentContractError("result belongs to another invocation")
        if result.last_event_sequence != self.events[-1].sequence:
            raise AgentContractError("result does not bind the terminal event")
        terminal = self.events[-1].payload.to_value()
        if (not isinstance(terminal, dict)
                or terminal.get("result_digest") != contract_digest(result)):
            raise AgentContractError("terminal event does not bind the result digest")


# The older test-facing name remains a clear description of the same fake
# store; C2.2 adapters can type against FakeEventStore.
RecordingEventSink = FakeEventStore


class FakeCancellationGate:
    """Lock-shared cancellation and terminal-commit arbiter for race tests."""

    def __init__(self, checker: Callable[[], bool] | None = None) -> None:
        self._checker = checker
        self._cancelled = False
        self._lock = threading.Lock()

    def _observed_locked(self) -> bool:
        return self._cancelled or bool(self._checker and self._checker())

    def cancelled(self) -> bool:
        with self._lock:
            return self._observed_locked()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def commit_terminal(
        self,
        sink: FakeEventStore,
        active_event: AgentStreamEvent,
        stopped_event: AgentStreamEvent,
    ) -> bool:
        """Choose cancellation or result and append while holding the same lock."""
        with self._lock:
            was_cancelled = self._observed_locked()
            sink.append(stopped_event if was_cancelled else active_event)
            return was_cancelled


class FakeContinuationStore:
    """Authenticated one-use continuation state for reload and replay tests."""

    def __init__(self, secret: bytes = b"agentbridge-c2-fake-secret") -> None:
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise AgentContractError("fake continuation secret is too short")
        self._secret = secret
        self._states: dict[str, tuple[ContinuationBinding, CanonicalValue]] = {}
        self._consumed: set[str] = set()
        self._counter = 0
        self._generation = 0

    @staticmethod
    def _tag_payload(binding: ContinuationBinding) -> bytes:
        raw = binding.to_dict()
        raw["state_auth_tag"] = None
        return CanonicalValue.from_value(raw).encoded

    @staticmethod
    def _snapshot_tag_payload(raw: dict[str, Any]) -> bytes:
        unsigned = {**raw, "snapshot_auth_tag": None}
        return CanonicalValue.from_value(unsigned).encoded

    def save(
        self,
        invocation: AgentInvocationSpec,
        state: CanonicalValue,
        *,
        mode: ContinuationMode = ContinuationMode.PAUSED_RUN,
    ) -> ContinuationBinding:
        if mode is ContinuationMode.NONE or not isinstance(state, CanonicalValue):
            raise AgentContractError("saved continuation needs canonical paused state")
        self._counter += 1
        state_ref = f"local:fake-continuation-{self._counter}"
        unsigned = ContinuationBinding(
            mode=mode,
            state_ref=state_ref,
            state_digest=state.digest(),
            state_auth_tag="0" * 64,
            invocation_id=invocation.invocation_id,
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            chat_id=invocation.chat_id,
            agent=invocation.agent,
            responsible_member=invocation.responsible_member,
            definition_digest=invocation.definition_digest,
            provider=invocation.provider,
            model=invocation.model,
            authority_digest=contract_digest(invocation.authority),
            invocation_context_digest=invocation_context_digest(invocation),
        )
        tag = hmac.new(
            self._secret, self._tag_payload(unsigned), hashlib.sha256,
        ).hexdigest()
        binding = ContinuationBinding.from_dict({
            **unsigned.to_dict(), "state_auth_tag": tag,
        })
        self._states[state_ref] = (binding, state)
        self._generation += 1
        return binding

    def interruption(
        self,
        invocation: AgentInvocationSpec,
        state: CanonicalValue,
        *,
        kind: InterruptionKind,
        request_ids: tuple[str, ...],
        expires_ns: int | None = None,
    ) -> tuple[ContinuationBinding, AgentInterruption]:
        binding = self.save(invocation, state)
        return binding, AgentInterruption(
            schema_version=AGENT_CONTRACT_VERSION,
            interruption_id=f"fake-interruption-{self._counter}",
            invocation_id=invocation.invocation_id,
            kind=kind,
            request_ids=request_ids,
            state_ref=binding.state_ref or "",
            state_digest=binding.state_digest or "",
            state_auth_tag=binding.state_auth_tag or "",
            expires_ns=expires_ns,
        )

    def snapshot(self) -> CanonicalValue:
        """Serialize opaque test state without serializing its authentication key."""
        raw = {
            "counter": self._counter,
            "generation": self._generation,
            "consumed": sorted(self._consumed),
            "entries": [
                {"binding": binding.to_dict(), "state": state.to_value()}
                for binding, state in self._states.values()
            ],
            "snapshot_auth_tag": None,
        }
        raw["snapshot_auth_tag"] = hmac.new(
            self._secret, self._snapshot_tag_payload(raw), hashlib.sha256,
        ).hexdigest()
        return CanonicalValue.from_value(raw)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CanonicalValue,
        *,
        minimum_generation: int,
        secret: bytes = b"agentbridge-c2-fake-secret",
    ) -> FakeContinuationStore:
        if not isinstance(snapshot, CanonicalValue):
            raise AgentContractError("continuation snapshot must be canonical JSON")
        if (isinstance(minimum_generation, bool)
                or not isinstance(minimum_generation, int)
                or minimum_generation < 0):
            raise AgentContractError("minimum continuation generation is malformed")
        store = cls(secret)
        raw = snapshot.to_value()
        if not isinstance(raw, dict) or set(raw) != {
                "counter", "generation", "consumed", "entries",
                "snapshot_auth_tag"}:
            raise AgentContractError("continuation snapshot has invalid fields")
        snapshot_auth_tag = raw["snapshot_auth_tag"]
        if (not isinstance(snapshot_auth_tag, str)
                or not hmac.compare_digest(snapshot_auth_tag, hmac.new(
                    store._secret, cls._snapshot_tag_payload(raw), hashlib.sha256,
                ).hexdigest())):
            raise AgentContractError("continuation snapshot authentication failed")
        counter = raw["counter"]
        generation = raw["generation"]
        consumed = raw["consumed"]
        entries = raw["entries"]
        if (isinstance(counter, bool) or not isinstance(counter, int) or counter < 0
                or isinstance(generation, bool) or not isinstance(generation, int)
                or generation < minimum_generation or generation < counter
                or not isinstance(consumed, list) or not isinstance(entries, list)
                or any(not isinstance(item, str) or not item for item in consumed)
                or len(consumed) != len(set(consumed))
                or len(consumed) + len(entries) != counter):
            raise AgentContractError("continuation snapshot is malformed")
        store._counter = counter
        store._generation = generation
        store._consumed = set(consumed)
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"binding", "state"}:
                raise AgentContractError("continuation snapshot entry is malformed")
            binding = ContinuationBinding.from_dict(entry["binding"])
            state = CanonicalValue.from_value(entry["state"])
            expected_tag = hmac.new(
                store._secret, store._tag_payload(binding), hashlib.sha256,
            ).hexdigest()
            if (not hmac.compare_digest(expected_tag, binding.state_auth_tag or "")
                    or state.digest() != binding.state_digest
                    or binding.state_ref in store._states
                    or binding.state_ref in store._consumed):
                raise AgentContractError("continuation snapshot authentication failed")
            store._states[binding.state_ref or ""] = (binding, state)
        return store

    def consume(
        self,
        binding: ContinuationBinding,
        invocation: AgentInvocationSpec,
    ) -> CanonicalValue:
        # Reconstructing the invocation is the canonical cross-binding check.
        AgentInvocationSpec.from_dict({
            **invocation.to_dict(), "continuation": binding.to_dict(),
        })
        expected_tag = hmac.new(
            self._secret, self._tag_payload(binding), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_tag, binding.state_auth_tag or ""):
            raise AgentContractError("continuation authentication failed")
        state_ref = binding.state_ref or ""
        if state_ref in self._consumed:
            raise AgentContractError("continuation is unavailable or already consumed")
        saved = self._states.get(state_ref)
        if saved is None or saved[0] != binding:
            raise AgentContractError("continuation is unavailable or already consumed")
        if saved[1].digest() != binding.state_digest:
            raise AgentContractError("continuation state digest mismatch")
        self._states.pop(state_ref)
        self._consumed.add(state_ref)
        self._generation += 1
        return saved[1]


@dataclass(frozen=True, slots=True)
class ScriptStep:
    kind: StreamEventKind
    payload: CanonicalValue
    advance_ns: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StreamEventKind):
            raise AgentContractError("script step kind must be a StreamEventKind")
        if self.kind in TERMINAL_EVENT_KINDS:
            raise AgentContractError("script steps are non-terminal provider events")
        if not isinstance(self.payload, CanonicalValue):
            raise AgentContractError("script step payload must be canonical JSON")
        if (isinstance(self.advance_ns, bool)
                or not isinstance(self.advance_ns, int)
                or self.advance_ns < 1):
            raise AgentContractError("script step advance must be positive")


class ScriptedProvider:
    """A provider backend with deterministic events and injected failure points."""

    def __init__(
        self,
        steps: tuple[ScriptStep, ...],
        *,
        final_output: CanonicalValue | None = None,
        final_text: str | None = None,
        usage: UsageRecord | None = None,
        failure: ProviderError | None = None,
        interruption: AgentInterruption | None = None,
        raw_final_output: Any = _UNSET,
        fail_before_step: int | None = None,
    ) -> None:
        outcomes = sum((
            failure is not None or fail_before_step is not None,
            interruption is not None,
            final_output is not None or final_text is not None,
            raw_final_output is not _UNSET,
        ))
        if outcomes != 1:
            raise AgentContractError("a scripted provider needs exactly one outcome")
        if final_output is not None and not isinstance(final_output, CanonicalValue):
            raise AgentContractError("final_output must be canonical JSON")
        if final_text is not None and not isinstance(final_text, str):
            raise AgentContractError("final_text must be text")
        if (failure is None and interruption is None and fail_before_step is None
                and final_output is None and final_text is None
                and raw_final_output is _UNSET):
            raise AgentContractError("successful scripted provider needs output")
        self.steps = tuple(steps)
        self.final_output = final_output
        self.final_text = final_text
        self.usage = usage or UsageRecord()
        self.failure = failure
        self.interruption = interruption
        self.raw_final_output = raw_final_output
        if (fail_before_step is not None
                and (isinstance(fail_before_step, bool)
                     or not isinstance(fail_before_step, int)
                     or not 1 <= fail_before_step <= len(self.steps))):
            raise AgentContractError("fault step must identify a scripted step")
        self.fail_before_step = fail_before_step

    def run(
        self,
        invocation: AgentInvocationSpec,
        sink: FakeEventStore,
        clock: FakeClock,
        *,
        cancelled: FakeCancellationGate | None = None,
    ) -> AgentResult:
        if sink.invocation_id != invocation.invocation_id:
            raise AgentContractError("event sink belongs to another invocation")
        if (self.interruption is not None
                and self.interruption.invocation_id != invocation.invocation_id):
            raise AgentContractError("scripted interruption belongs to another invocation")
        if cancelled is not None and not isinstance(cancelled, FakeCancellationGate):
            raise AgentContractError("cancellation must use a FakeCancellationGate")
        cancellation = cancelled or FakeCancellationGate()
        sequence = 0

        def emit(kind: StreamEventKind, payload: CanonicalValue) -> None:
            nonlocal sequence
            sequence += 1
            sink.append(AgentStreamEvent(
                schema_version=AGENT_CONTRACT_VERSION,
                invocation_id=invocation.invocation_id,
                sequence=sequence,
                ns=clock.advance(),
                kind=kind,
                payload=payload,
            ))

        emit(StreamEventKind.STARTED, CanonicalValue.from_value({}))

        def settle(result: AgentResult, kind: StreamEventKind,
                   payload: CanonicalValue | None = None) -> AgentResult:
            nonlocal sequence
            terminal_payload = {} if payload is None else payload.to_value()
            if not isinstance(terminal_payload, dict):
                raise AgentContractError("terminal payload must be an object")
            terminal_payload = {
                **terminal_payload,
                "result_digest": contract_digest(result),
            }
            sequence += 1
            active_event = AgentStreamEvent(
                schema_version=AGENT_CONTRACT_VERSION,
                invocation_id=invocation.invocation_id,
                sequence=sequence,
                ns=clock.advance(),
                kind=kind,
                payload=CanonicalValue.from_value(terminal_payload),
            )
            if result.status is not AgentResultStatus.STOPPED:
                stopped_result = AgentResult(
                    schema_version=AGENT_CONTRACT_VERSION,
                    invocation_id=invocation.invocation_id,
                    status=AgentResultStatus.STOPPED,
                    last_event_sequence=sequence,
                    final_output=None,
                    final_text=None,
                    usage=self.usage,
                )
                stopped_event = AgentStreamEvent(
                    schema_version=AGENT_CONTRACT_VERSION,
                    invocation_id=invocation.invocation_id,
                    sequence=sequence,
                    ns=active_event.ns,
                    kind=StreamEventKind.STOPPED,
                    payload=CanonicalValue.from_value({
                        "result_digest": contract_digest(stopped_result),
                    }),
                )
                if cancellation.commit_terminal(sink, active_event, stopped_event):
                    result = stopped_result
            else:
                sink.append(active_event)
            sink.assert_settled(result)
            return result

        for index, step in enumerate(self.steps, start=1):
            if cancellation.cancelled():
                result = AgentResult(
                    schema_version=AGENT_CONTRACT_VERSION,
                    invocation_id=invocation.invocation_id,
                    status=AgentResultStatus.STOPPED,
                    last_event_sequence=sequence + 1,
                    final_output=None,
                    final_text=None,
                    usage=self.usage,
                )
                return settle(result, StreamEventKind.STOPPED)
            if self.fail_before_step == index:
                error = self.failure or ProviderError.normalized(
                    ProviderErrorCategory.INTERNAL, "fake_fault", False,
                )
                result = AgentResult(
                    schema_version=AGENT_CONTRACT_VERSION,
                    invocation_id=invocation.invocation_id,
                    status=AgentResultStatus.FAILED,
                    last_event_sequence=sequence + 1,
                    final_output=None,
                    final_text=None,
                    usage=self.usage,
                    error=error,
                )
                return settle(
                    result, StreamEventKind.FAILED,
                    CanonicalValue.from_value(error.to_dict()),
                )
            clock.advance(step.advance_ns - 1) if step.advance_ns > 1 else None
            emit(step.kind, step.payload)
        if cancellation.cancelled():
            result = AgentResult(
                schema_version=AGENT_CONTRACT_VERSION,
                invocation_id=invocation.invocation_id,
                status=AgentResultStatus.STOPPED,
                last_event_sequence=sequence + 1,
                final_output=None,
                final_text=None,
                usage=self.usage,
            )
            return settle(result, StreamEventKind.STOPPED)
        if self.interruption is not None:
            result = AgentResult(
                schema_version=AGENT_CONTRACT_VERSION,
                invocation_id=invocation.invocation_id,
                status=AgentResultStatus.INTERRUPTED,
                last_event_sequence=sequence + 1,
                final_output=None,
                final_text=None,
                usage=self.usage,
                interruption=self.interruption,
            )
            return settle(result, StreamEventKind.INTERRUPTED)
        if self.failure is not None:
            result = AgentResult(
                schema_version=AGENT_CONTRACT_VERSION,
                invocation_id=invocation.invocation_id,
                status=AgentResultStatus.FAILED,
                last_event_sequence=sequence + 1,
                final_output=None,
                final_text=None,
                usage=self.usage,
                error=self.failure,
            )
            return settle(
                result, StreamEventKind.FAILED,
                CanonicalValue.from_value(self.failure.to_dict()),
            )
        final_output = self.final_output
        if self.raw_final_output is not _UNSET:
            try:
                final_output = CanonicalValue.from_value(self.raw_final_output)
            except AgentContractError:
                error = ProviderError.normalized(
                    ProviderErrorCategory.OUTPUT,
                    "invalid_structured_output",
                    False,
                )
                result = AgentResult(
                    schema_version=AGENT_CONTRACT_VERSION,
                    invocation_id=invocation.invocation_id,
                    status=AgentResultStatus.FAILED,
                    last_event_sequence=sequence + 1,
                    final_output=None,
                    final_text=None,
                    usage=self.usage,
                    error=error,
                )
                return settle(
                    result, StreamEventKind.FAILED,
                    CanonicalValue.from_value(error.to_dict()),
                )
        result = AgentResult(
            schema_version=AGENT_CONTRACT_VERSION,
            invocation_id=invocation.invocation_id,
            status=AgentResultStatus.COMPLETED,
            last_event_sequence=sequence + 1,
            final_output=final_output,
            final_text=self.final_text,
            usage=self.usage,
        )
        return settle(result, StreamEventKind.COMPLETED)
