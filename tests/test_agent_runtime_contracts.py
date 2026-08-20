from dataclasses import fields, replace
import hashlib
import threading

import pytest

from agentbridge.harness.runtime.contracts import (
    AGENT_CONTRACT_VERSION,
    AgentContractError,
    AgentDefinition,
    AgentInterruption,
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    AgentStreamEvent,
    AuthorityBinding,
    CanonicalValue,
    ContinuationBinding,
    ContinuationMode,
    InterruptionKind,
    PromptSpec,
    ProviderError,
    ProviderErrorCategory,
    StreamEventKind,
    UsageRecord,
    contract_digest,
    verify_invocation,
)
from agentbridge.harness.runtime.fakes import (
    FakeClock,
    FakeCancellationGate,
    FakeContinuationStore,
    FakeEventStore,
    RecordingEventSink,
    ScriptedProvider,
    ScriptStep,
)
from agentbridge.harness.runtime.models import (
    RecordKind,
    RecordMeta,
    RunRecord,
    RunState,
    TaskRecord,
    TaskState,
)


DIGEST = "a" * 64


def definition() -> AgentDefinition:
    return AgentDefinition(
        schema_version=AGENT_CONTRACT_VERSION,
        definition_id="agent:researcher",
        revision=3,
        name="Researcher",
        provider="test-provider",
        model="test-model",
        prompt=PromptSpec(
            static_instructions="Research carefully.",
            template_id="research",
            template_revision=2,
            dynamic_resolver_id="room-context-v1",
        ),
        model_settings=CanonicalValue.from_value({"temperature": 0}),
        input_schema=CanonicalValue.from_value({"type": "object"}),
        output_schema=CanonicalValue.from_value({"type": "object"}),
        requested_tool_ids=("search",),
        requested_handoff_definition_ids=("agent:reviewer",),
        hook_ids=("audit",),
        approval_policy_id="human-side-effects",
        max_turns=12,
    )


def signed_records() -> tuple[RunRecord, TaskRecord]:
    run = RunRecord(
        meta=RecordMeta(
            schema_version=1, kind=RecordKind.RUN, id="run-event-1", ns=10,
            actor="researcher", chat_id="chat-1", signer="researcher",
            root_run_id="run-1", run_id="run-1", task_id=None, call_id=None,
            key_epoch=2, policy_revision=3, membership_epoch=4,
            ownership_epoch=5, expires_ns=None,
        ),
        state=RunState.RUNNING, trigger_id="trigger-1",
        manager_agent="researcher", responsible_member="aryan",
        execution_level="foreground", provider="test-provider", model="test-model",
        capability_ceiling=("search",), active_task_ids=("task-1",),
        status="running", outcome=None,
    )
    task = TaskRecord(
        meta=RecordMeta(
            schema_version=1, kind=RecordKind.TASK, id="task-event-1", ns=11,
            actor="researcher", chat_id="chat-1", signer="researcher",
            root_run_id="run-1", run_id="run-1", task_id="task-1", call_id=None,
            key_epoch=2, policy_revision=3, membership_epoch=4,
            ownership_epoch=5, expires_ns=None,
        ),
        state=TaskState.ACTIVE, objective="Answer the question",
        assigned_agent="researcher", assigning_agent="researcher",
        responsible_member="aryan", parent_task_id=None,
        success_criteria=("Grounded answer",), context_digest=DIGEST,
        grant_ids=(), dependency_ids=(), progress="working", result=None,
        return_to_agent="researcher",
    )
    return run, task


def invocation(*, continuation: ContinuationBinding | None = None) -> AgentInvocationSpec:
    run, task = signed_records()
    return AgentInvocationSpec(
        schema_version=AGENT_CONTRACT_VERSION,
        invocation_id="inv-1",
        run_id="run-1",
        task_id="task-1",
        chat_id="chat-1",
        agent="researcher",
        responsible_member="aryan",
        definition_id="agent:researcher",
        definition_revision=3,
        definition_digest=contract_digest(definition()),
        provider="test-provider",
        model="test-model",
        input=CanonicalValue.from_value({"question": "why?"}),
        resolved_instructions="Research carefully for this room.",
        model_settings=CanonicalValue.from_value({"temperature": 0}),
        max_turns=12,
        authority=AuthorityBinding(
            run_id="run-1",
            task_id="task-1",
            chat_id="chat-1",
            agent="researcher",
            responsible_member="aryan",
            run_record_id="run-event-1",
            run_record_digest=contract_digest(run),
            task_record_id="task-event-1",
            task_record_digest=contract_digest(task),
            key_epoch=2,
            policy_revision=3,
            membership_epoch=4,
            ownership_epoch=5,
            capability_ids=("search",),
            grant_ids=(),
        ),
        continuation=continuation or ContinuationBinding(ContinuationMode.NONE),
        deadline_ns=99,
    )


def test_canonical_value_is_deeply_immutable_and_canonical() -> None:
    original = {"b": [2], "a": 1}
    value = CanonicalValue.from_value(original)
    original["b"].append(3)
    assert value.encoded == b'{"a":1,"b":[2]}'
    assert value.to_value() == {"a": 1, "b": [2]}
    assert value.digest() == hashlib.sha256(value.encoded).hexdigest()
    with pytest.raises(AgentContractError, match="canonical JSON"):
        CanonicalValue(b'{"b":2,"a":1}')


def test_definition_round_trip_and_clone_cannot_copy_authority() -> None:
    raw = definition().to_dict()
    raw["extensions"] = {"x.provider.local_hint": "do-not-clone"}
    source = AgentDefinition.from_dict(raw)
    assert AgentDefinition.from_dict(source.to_dict()) == source
    clone = source.clone(definition_id="agent:researcher-copy", name="Researcher copy")
    assert clone.definition_id == "agent:researcher-copy"
    assert clone.revision == 1
    assert clone.requested_tool_ids == source.requested_tool_ids
    assert clone.extensions == CanonicalValue.from_value({})
    names = {field.name for field in fields(clone)}
    assert names.isdisjoint({
        "authority", "grant_ids", "credentials", "continuation", "run_id", "session",
    })


def test_definition_requires_prompt_and_rejects_unknown_fields_and_versions() -> None:
    with pytest.raises(AgentContractError, match="prompt needs"):
        PromptSpec()
    raw = definition().to_dict()
    raw["unexpected"] = True
    with pytest.raises(AgentContractError, match="extra=.*unexpected"):
        AgentDefinition.from_dict(raw)
    raw = definition().to_dict()
    raw["schema_version"] = 2
    with pytest.raises(AgentContractError, match="unsupported"):
        AgentDefinition.from_dict(raw)


def test_namespaced_extensions_round_trip_but_cannot_masquerade_as_fields() -> None:
    raw = definition().to_dict()
    raw["extensions"] = {"x.openai.trace_hint": "local-only"}
    parsed = AgentDefinition.from_dict(raw)
    assert parsed.extensions.to_value() == {"x.openai.trace_hint": "local-only"}
    raw["extensions"] = {"authority": {"capability_ids": ["shell"]}}
    with pytest.raises(AgentContractError, match=r"x\.\*"):
        AgentDefinition.from_dict(raw)


def test_definition_collections_are_normalized_and_duplicates_rejected() -> None:
    raw = definition().to_dict()
    raw["requested_tool_ids"] = ["search", "search"]
    with pytest.raises(AgentContractError, match="duplicates"):
        AgentDefinition.from_dict(raw)
    raw = definition().to_dict()
    raw["requested_tool_ids"] = ["search", "fetch"]
    parsed = AgentDefinition.from_dict(raw)
    assert parsed.requested_tool_ids == ("search", "fetch")


def test_invocation_round_trip_binds_signed_authority_and_opaque_continuation() -> None:
    base = invocation()
    continuation = FakeContinuationStore().save(
        base, CanonicalValue.from_value({"turn": 2}),
    )
    spec = invocation(continuation=continuation)
    assert AgentInvocationSpec.from_dict(spec.to_dict()) == spec
    assert spec.authority.run_record_id == "run-event-1"
    assert spec.continuation.state_ref == "local:fake-continuation-1"
    assert "provider_object" not in spec.to_dict()
    assert len(contract_digest(spec)) == 64


@pytest.mark.parametrize("raw", [
    {**ContinuationBinding(ContinuationMode.NONE).to_dict(), "state_ref": "leak"},
    {**ContinuationBinding(ContinuationMode.NONE).to_dict(),
     "mode": "provider_session"},
    {**ContinuationBinding(ContinuationMode.NONE).to_dict(), "mode": "future"},
])
def test_continuation_fails_closed(raw: dict) -> None:
    with pytest.raises(AgentContractError):
        ContinuationBinding.from_dict(raw)


def test_authority_binding_requires_signed_record_digest() -> None:
    with pytest.raises(AgentContractError, match="sha256"):
        AuthorityBinding(
            "run-1", "task-1", "chat-1", "researcher", "aryan", "event",
            "mutable-settings", "task-event", DIGEST, 1, 1, 1, 1, (), (),
        )
    with pytest.raises(AgentContractError, match="duplicates"):
        AuthorityBinding(
            "run-1", "task-1", "chat-1", "researcher", "aryan", "event", DIGEST,
            "task-event", DIGEST, 1, 1, 1, 1, ("search", "search"), (),
        )


def test_invocation_rejects_authority_substitution() -> None:
    raw = invocation().to_dict()
    raw["authority"]["chat_id"] = "other-room"
    with pytest.raises(AgentContractError, match="authority chat_id"):
        AgentInvocationSpec.from_dict(raw)


def test_invocation_verifier_binds_definition_run_task_and_authority() -> None:
    run, task = signed_records()
    spec = invocation()
    verify_invocation(definition(), spec, run, task)

    substitutions = (
        (replace(definition(), model="other-model"), spec, run, task),
        (definition(), replace(spec, model="other-model"), run, task),
        (definition(), replace(
            spec, model_settings=CanonicalValue.from_value({"temperature": 1}),
        ), run, task),
        (definition(), spec, replace(run, model="other-model"), task),
        (definition(), spec, run, replace(task, assigned_agent="other-agent")),
        (definition(), spec, run, replace(task, objective="substituted objective")),
        (definition(), replace(
            spec, authority=replace(spec.authority, policy_revision=99),
        ), run, task),
    )
    for candidate in substitutions:
        with pytest.raises(AgentContractError):
            verify_invocation(*candidate)


def test_continuation_store_authenticates_context_and_consumes_once() -> None:
    base = invocation()
    store = FakeContinuationStore()
    state = CanonicalValue.from_value({"provider_state": "opaque"})
    binding = store.save(base, state)
    resumed = invocation(continuation=binding)

    assert store.consume(binding, resumed) == state
    with pytest.raises(AgentContractError, match="already consumed"):
        store.consume(binding, resumed)


def test_continuation_rejects_replay_and_authentication_tampering() -> None:
    base = invocation()
    store = FakeContinuationStore()
    binding = store.save(base, CanonicalValue.from_value({"turn": 2}))

    raw = base.to_dict()
    raw["chat_id"] = "other-chat"
    raw["authority"]["chat_id"] = "other-chat"
    raw["continuation"] = binding.to_dict()
    with pytest.raises(AgentContractError, match="continuation chat_id"):
        AgentInvocationSpec.from_dict(raw)

    tampered = ContinuationBinding.from_dict({
        **binding.to_dict(), "state_auth_tag": "f" * 64,
    })
    with pytest.raises(AgentContractError, match="authentication"):
        store.consume(tampered, invocation(continuation=tampered))


@pytest.mark.parametrize(("field", "value"), (
    ("input", {"question": "substituted"}),
    ("resolved_instructions", "Substituted instructions"),
    ("model_settings", {"temperature": 1}),
    ("max_turns", 2),
    ("deadline_ns", 1000),
    ("extensions", {"x.test.substituted": True}),
))
def test_continuation_binds_complete_execution_context(field: str, value: object) -> None:
    base = invocation()
    binding = FakeContinuationStore().save(
        base, CanonicalValue.from_value({"turn": 2}),
    )
    raw = base.to_dict()
    raw[field] = value
    raw["continuation"] = binding.to_dict()
    with pytest.raises(AgentContractError, match="invocation_context_digest"):
        AgentInvocationSpec.from_dict(raw)


def test_interruption_state_survives_reload_and_resumes_exact_invocation() -> None:
    base = invocation()
    store = FakeContinuationStore()
    state = CanonicalValue.from_value({"pending_approval": "ask-1"})
    binding, interruption = store.interruption(
        base, state, kind=InterruptionKind.APPROVAL, request_ids=("ask-1",),
    )
    reloaded = AgentInterruption.from_dict(interruption.to_dict())
    reloaded_store = FakeContinuationStore.from_snapshot(
        store.snapshot(), minimum_generation=1,
    )
    assert reloaded.state_ref == binding.state_ref
    assert reloaded.state_auth_tag == binding.state_auth_tag
    assert reloaded_store.consume(binding, invocation(continuation=binding)) == state


def test_continuation_snapshot_rejects_rollback_after_consumption() -> None:
    base = invocation()
    store = FakeContinuationStore()
    binding = store.save(base, CanonicalValue.from_value({"turn": 2}))
    before_consumption = store.snapshot()
    store.consume(binding, invocation(continuation=binding))
    after_consumption = store.snapshot()

    with pytest.raises(AgentContractError, match="malformed"):
        FakeContinuationStore.from_snapshot(
            before_consumption, minimum_generation=2,
        )
    tampered = before_consumption.to_value()
    tampered["generation"] = 2
    with pytest.raises(AgentContractError, match="snapshot authentication"):
        FakeContinuationStore.from_snapshot(
            CanonicalValue.from_value(tampered), minimum_generation=2,
        )
    reloaded = FakeContinuationStore.from_snapshot(
        after_consumption, minimum_generation=2,
    )
    with pytest.raises(AgentContractError, match="already consumed"):
        reloaded.consume(binding, invocation(continuation=binding))


def test_schemas_and_model_settings_must_be_json_objects() -> None:
    raw = definition().to_dict()
    raw["output_schema"] = ["not", "a", "schema"]
    with pytest.raises(AgentContractError, match="output_schema"):
        AgentDefinition.from_dict(raw)
    raw = invocation().to_dict()
    raw["model_settings"] = ["not", "settings"]
    with pytest.raises(AgentContractError, match="model_settings"):
        AgentInvocationSpec.from_dict(raw)


def test_stream_event_round_trip_and_unknown_kind_failure() -> None:
    event = AgentStreamEvent(
        schema_version=AGENT_CONTRACT_VERSION,
        invocation_id="inv-1",
        sequence=1,
        ns=10,
        kind=StreamEventKind.ACTIVITY,
        payload=CanonicalValue.from_value({"label": "Searching"}),
    )
    assert AgentStreamEvent.from_dict(event.to_dict()) == event
    raw = event.to_dict()
    raw["kind"] = "provider_private_kind"
    with pytest.raises(AgentContractError, match="unknown stream event kind"):
        AgentStreamEvent.from_dict(raw)


def test_recording_sink_enforces_invocation_order_and_single_terminal() -> None:
    sink = RecordingEventSink("inv-1")
    first = AgentStreamEvent(
        AGENT_CONTRACT_VERSION, "inv-1", 1, 10,
        StreamEventKind.STARTED, CanonicalValue.from_value({}),
    )
    terminal = AgentStreamEvent(
        AGENT_CONTRACT_VERSION, "inv-1", 2, 11,
        StreamEventKind.COMPLETED, CanonicalValue.from_value({}),
    )
    sink.append(first)
    sink.append(terminal)
    assert sink.terminal_count == 1
    with pytest.raises(AgentContractError, match="follow"):
        sink.append(AgentStreamEvent(
            AGENT_CONTRACT_VERSION, "inv-1", 3, 12,
            StreamEventKind.ACTIVITY, CanonicalValue.from_value({}),
        ))
    with pytest.raises(AgentContractError, match="another invocation"):
        RecordingEventSink("other").append(first)
    with pytest.raises(AgentContractError, match="first event"):
        FakeEventStore("inv-1").append(AgentStreamEvent(
            AGENT_CONTRACT_VERSION, "inv-1", 1, 10,
            StreamEventKind.COMPLETED, CanonicalValue.from_value({}),
        ))


def test_event_store_settlement_binds_terminal_kind_and_result_sequence() -> None:
    sink = FakeEventStore("inv-1")
    sink.append(AgentStreamEvent(
        AGENT_CONTRACT_VERSION, "inv-1", 1, 10,
        StreamEventKind.STARTED, CanonicalValue.from_value({}),
    ))
    result = AgentResult(
        AGENT_CONTRACT_VERSION, "inv-1", AgentResultStatus.COMPLETED, 2,
        CanonicalValue.from_value({"ok": True}), None, UsageRecord(),
    )
    with pytest.raises(AgentContractError, match="exactly one"):
        sink.assert_settled(result)
    sink.append(AgentStreamEvent(
        AGENT_CONTRACT_VERSION, "inv-1", 2, 11,
        StreamEventKind.COMPLETED,
        CanonicalValue.from_value({"result_digest": "0" * 64}),
    ))
    with pytest.raises(AgentContractError, match="result digest"):
        sink.assert_settled(result)


def test_interruption_round_trip_contains_reference_not_provider_state() -> None:
    interruption = AgentInterruption(
        schema_version=AGENT_CONTRACT_VERSION,
        interruption_id="interrupt-1",
        invocation_id="inv-1",
        kind=InterruptionKind.APPROVAL,
        request_ids=("ask-1",),
        state_ref="encrypted-local-state:1",
        state_digest=DIGEST,
        state_auth_tag="b" * 64,
        expires_ns=100,
    )
    assert AgentInterruption.from_dict(interruption.to_dict()) == interruption
    assert set(interruption.to_dict()) == {
        "schema_version", "interruption_id", "invocation_id", "kind",
        "request_ids", "state_ref", "state_digest", "state_auth_tag",
        "expires_ns", "extensions",
    }


def test_result_status_shapes_fail_closed() -> None:
    usage = UsageRecord(input_tokens=5, output_tokens=2)
    completed = AgentResult(
        AGENT_CONTRACT_VERSION, "inv-1", AgentResultStatus.COMPLETED, 2,
        CanonicalValue.from_value({"answer": 42}), None, usage,
    )
    assert AgentResult.from_dict(completed.to_dict()) == completed
    with pytest.raises(AgentContractError, match="needs output"):
        AgentResult(
            AGENT_CONTRACT_VERSION, "inv-1", AgentResultStatus.COMPLETED, 2,
            None, None, usage,
        )
    with pytest.raises(AgentContractError, match="provider error"):
        AgentResult(
            AGENT_CONTRACT_VERSION, "inv-1", AgentResultStatus.FAILED, 2,
            None, None, usage,
        )
    with pytest.raises(AgentContractError, match=">= 1"):
        AgentResult(
            AGENT_CONTRACT_VERSION, "inv-1", AgentResultStatus.COMPLETED, 0,
            CanonicalValue.from_value({"answer": 42}), None, usage,
        )


def test_interrupted_result_must_bind_same_invocation() -> None:
    interruption = AgentInterruption(
        AGENT_CONTRACT_VERSION, "interrupt-1", "other",
        InterruptionKind.USER_INPUT, ("question-1",), "state:1", DIGEST, "b" * 64,
    )
    with pytest.raises(AgentContractError, match="bind this invocation"):
        AgentResult(
            AGENT_CONTRACT_VERSION, "inv-1", AgentResultStatus.INTERRUPTED, 1,
            None, None, UsageRecord(), interruption=interruption,
        )


def test_usage_accepts_only_provider_reported_cost_and_consistent_cache() -> None:
    assert UsageRecord(input_tokens=10, cached_input_tokens=4).currency is None
    with pytest.raises(AgentContractError, match="cannot exceed"):
        UsageRecord(input_tokens=2, cached_input_tokens=3)
    with pytest.raises(AgentContractError, match="currency requires"):
        UsageRecord(currency="USD")
    assert UsageRecord(
        provider_reported_cost_micros=123, currency="USD",
    ).provider_reported_cost_micros == 123


def test_provider_errors_are_code_owned_and_carry_only_private_evidence_digest() -> None:
    evidence = hashlib.sha256(
        b"Bearer secret-token /Users/aryan/private prompt stack trace",
    ).hexdigest()
    error = ProviderError.normalized(
        ProviderErrorCategory.RATE_LIMIT, "rate_limit", True,
        evidence_digest=evidence,
    )
    assert ProviderError.from_dict(error.to_dict()) == error
    assert "secret" not in str(error.to_dict())
    with pytest.raises(AgentContractError, match="code-owned"):
        ProviderError(
            ProviderErrorCategory.INTERNAL, "raw", "secret\ntrace", False,
        )
    with pytest.raises(AgentContractError, match="normalized identifier"):
        ProviderError(
            ProviderErrorCategory.INTERNAL, "raw/bearer-token",
            "Provider execution failed", False,
        )
    with pytest.raises(AgentContractError, match="code-owned"):
        ProviderError(
            ProviderErrorCategory.INTERNAL, "sk_live_secret",
            "Provider execution failed", False,
        )


def test_scripted_provider_success_has_contiguous_events_and_one_terminal() -> None:
    provider = ScriptedProvider(
        (
            ScriptStep(StreamEventKind.ACTIVITY,
                       CanonicalValue.from_value({"label": "Working"})),
            ScriptStep(StreamEventKind.TEXT_DELTA,
                       CanonicalValue.from_value({"text": "answer"}), 3),
        ),
        final_output=CanonicalValue.from_value({"answer": 42}),
        usage=UsageRecord(input_tokens=5, output_tokens=2),
    )
    sink = RecordingEventSink("inv-1")
    result = provider.run(invocation(), sink, FakeClock())
    assert result.status is AgentResultStatus.COMPLETED
    assert [event.sequence for event in sink.events] == [1, 2, 3, 4]
    assert sink.terminal_count == 1
    assert result.last_event_sequence == 4


def test_scripted_provider_fault_is_normalized_and_terminal() -> None:
    error = ProviderError.normalized(
        ProviderErrorCategory.UNAVAILABLE, "offline", True,
    )
    provider = ScriptedProvider(
        (ScriptStep(StreamEventKind.ACTIVITY, CanonicalValue.from_value({})),),
        failure=error,
        fail_before_step=1,
    )
    sink = RecordingEventSink("inv-1")
    result = provider.run(invocation(), sink, FakeClock())
    assert result.status is AgentResultStatus.FAILED
    assert result.error == error
    assert sink.events[-1].kind is StreamEventKind.FAILED
    assert sink.terminal_count == 1


def test_scripted_provider_malformed_output_becomes_failed_terminal() -> None:
    provider = ScriptedProvider((), raw_final_output={"not-json": {1, 2}})
    sink = FakeEventStore("inv-1")
    result = provider.run(invocation(), sink, FakeClock())
    assert result.status is AgentResultStatus.FAILED
    assert result.error is not None
    assert result.error.category is ProviderErrorCategory.OUTPUT
    assert sink.events[-1].kind is StreamEventKind.FAILED
    assert sink.terminal_count == 1


def test_scripted_provider_interruption_is_resumable_and_terminal() -> None:
    interruption = AgentInterruption(
        AGENT_CONTRACT_VERSION, "interrupt-1", "inv-1",
        InterruptionKind.APPROVAL, ("ask-1",), "local:state-1", DIGEST, "b" * 64,
    )
    provider = ScriptedProvider((), interruption=interruption)
    sink = FakeEventStore("inv-1")
    result = provider.run(invocation(), sink, FakeClock())
    assert result.status is AgentResultStatus.INTERRUPTED
    assert result.interruption == interruption
    assert sink.events[-1].kind is StreamEventKind.INTERRUPTED
    sink.assert_settled(result)


def test_scripted_provider_cancellation_wins_before_more_work() -> None:
    provider = ScriptedProvider(
        (ScriptStep(StreamEventKind.ACTIVITY, CanonicalValue.from_value({})),),
        final_text="unused",
    )
    sink = RecordingEventSink("inv-1")
    result = provider.run(
        invocation(), sink, FakeClock(),
        cancelled=FakeCancellationGate(lambda: True),
    )
    assert result.status is AgentResultStatus.STOPPED
    assert [event.kind for event in sink.events] == [
        StreamEventKind.STARTED, StreamEventKind.STOPPED,
    ]
    assert sink.terminal_count == 1


def test_scripted_provider_cancellation_wins_after_last_nonterminal_event() -> None:
    provider = ScriptedProvider(
        (ScriptStep(StreamEventKind.ACTIVITY, CanonicalValue.from_value({})),),
        final_text="must not commit",
    )
    polls = iter((False, True))
    sink = RecordingEventSink("inv-1")
    result = provider.run(
        invocation(), sink, FakeClock(),
        cancelled=FakeCancellationGate(lambda: next(polls)),
    )
    assert result.status is AgentResultStatus.STOPPED
    assert sink.events[-1].kind is StreamEventKind.STOPPED
    assert sink.terminal_count == 1


def test_cancellation_wins_at_final_precommit_boundary() -> None:
    provider = ScriptedProvider(
        (ScriptStep(StreamEventKind.ACTIVITY, CanonicalValue.from_value({})),),
        final_text="must not commit",
    )
    polls = iter((False, False, True))
    sink = FakeEventStore("inv-1")
    result = provider.run(
        invocation(), sink, FakeClock(),
        cancelled=FakeCancellationGate(lambda: next(polls)),
    )
    assert result.status is AgentResultStatus.STOPPED
    assert sink.events[-1].kind is StreamEventKind.STOPPED
    sink.assert_settled(result)


def test_cancellation_wins_over_failure_at_final_precommit_boundary() -> None:
    provider = ScriptedProvider(
        (),
        failure=ProviderError.normalized(
            ProviderErrorCategory.UNAVAILABLE, "offline", True,
        ),
    )
    polls = iter((False, True))
    sink = FakeEventStore("inv-1")
    result = provider.run(
        invocation(), sink, FakeClock(),
        cancelled=FakeCancellationGate(lambda: next(polls)),
    )
    assert result.status is AgentResultStatus.STOPPED
    assert sink.events[-1].kind is StreamEventKind.STOPPED


def test_cancellation_after_terminal_commit_cannot_rewrite_result() -> None:
    cancellation = FakeCancellationGate()
    provider = ScriptedProvider((), final_text="committed")
    sink = FakeEventStore("inv-1")
    result = provider.run(
        invocation(), sink, FakeClock(), cancelled=cancellation,
    )
    committed_events = tuple(sink.events)
    cancellation.cancel()
    assert result.status is AgentResultStatus.COMPLETED
    assert tuple(sink.events) == committed_events
    sink.assert_settled(result)


def test_terminal_commit_and_concurrent_cancellation_share_one_lock() -> None:
    commit_entered = threading.Event()
    release_commit = threading.Event()
    cancel_started = threading.Event()
    cancel_finished = threading.Event()
    polls = 0

    def checker() -> bool:
        nonlocal polls
        polls += 1
        if polls == 1:
            return False
        commit_entered.set()
        assert release_commit.wait(timeout=2)
        return False

    cancellation = FakeCancellationGate(checker)
    sink = FakeEventStore("inv-1")
    results: list[AgentResult] = []
    runner = threading.Thread(target=lambda: results.append(ScriptedProvider(
        (), final_text="committed",
    ).run(invocation(), sink, FakeClock(), cancelled=cancellation)))
    runner.start()
    assert commit_entered.wait(timeout=2)

    def cancel() -> None:
        cancel_started.set()
        cancellation.cancel()
        cancel_finished.set()

    canceller = threading.Thread(target=cancel)
    canceller.start()
    assert cancel_started.wait(timeout=2)
    assert not cancel_finished.is_set()
    release_commit.set()
    runner.join(timeout=2)
    canceller.join(timeout=2)
    assert not runner.is_alive() and not canceller.is_alive()
    assert results[0].status is AgentResultStatus.COMPLETED
    sink.assert_settled(results[0])


def test_fake_fixture_configuration_is_strict() -> None:
    with pytest.raises(AgentContractError, match="advance"):
        ScriptStep(StreamEventKind.ACTIVITY, CanonicalValue.from_value({}), "soon")
    with pytest.raises(AgentContractError, match="fault step"):
        ScriptedProvider((), fail_before_step=1)
    with pytest.raises(AgentContractError, match="positive ns"):
        FakeClock(0)


def test_scripted_provider_rejects_success_without_output() -> None:
    with pytest.raises(AgentContractError, match="exactly one outcome"):
        ScriptedProvider(())
