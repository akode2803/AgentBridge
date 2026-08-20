"""R139 current-CLI compatibility contract and rollback tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentbridge.harness.runtime.cli_compat import prepare_cli_contract
from agentbridge.harness.runtime.contracts import (
    AgentContractError, AgentResultStatus, StreamEventKind,
)
from agentbridge.harness.runtime.fakes import (
    FakeClock, FakeEventStore, ScriptedProvider,
)
from agentbridge.harness.runtime.models import (
    RecordKind, RecordMeta, RunRecord, RunState, TaskRecord, TaskState,
)
from agentbridge.harness.settings import HarnessSettings, runtime_policy_revision


def _records() -> tuple[RunRecord, TaskRecord]:
    run = RunRecord(
        meta=RecordMeta(
            schema_version=1, kind=RecordKind.RUN, id="run-event", ns=10,
            actor="helper", chat_id="chat-1", signer="helper",
            root_run_id="run-1", run_id="run-1", task_id=None, call_id=None,
            key_epoch=2, policy_revision=3, membership_epoch=4,
            ownership_epoch=5, expires_ns=None,
        ),
        state=RunState.RUNNING, trigger_id="message-1", manager_agent="helper",
        responsible_member="aryan", execution_level="foreground",
        provider="codex", model="gpt-test", capability_ceiling=("read",),
        active_task_ids=("task-1",), status="running", outcome=None,
    )
    task = TaskRecord(
        meta=RecordMeta(
            schema_version=1, kind=RecordKind.TASK, id="task-event", ns=11,
            actor="helper", chat_id="chat-1", signer="helper",
            root_run_id="run-1", run_id="run-1", task_id="task-1", call_id=None,
            key_epoch=2, policy_revision=3, membership_epoch=4,
            ownership_epoch=5, expires_ns=None,
        ),
        state=TaskState.ACTIVE, objective="Answer the triggering message",
        assigned_agent="helper", assigning_agent="helper",
        responsible_member="aryan", parent_task_id=None,
        success_criteria=("Reply once",), context_digest="a" * 64,
        grant_ids=("grant-1",), dependency_ids=(), progress="working",
        result=None, return_to_agent="helper",
    )
    return run, task


def _delivery(run: RunRecord | None = None, task: TaskRecord | None = None):
    canonical_run, canonical_task = _records()
    return SimpleNamespace(
        run_id="run-1", task_id="task-1", chat_id="chat-1", agent="helper",
        canonical_run=run or canonical_run,
        canonical_task=task or canonical_task,
    )


def _prepare(delivery=None, **overrides):
    values = {
        "delivery": delivery or _delivery(),
        "provider": "codex", "model": "gpt-test", "effort": "high",
        "timeout_s": 30.0, "minimal": False, "bridge_attached": True,
        "prompt": "private prompt", "argv": ("codex", "private prompt"),
        "fallback_argv": None,
        "env": {"PATH": "/bin", "AGENTBRIDGE_MCP_TOKEN": "top-secret"},
        "now_ns": lambda: 1_000, "event_clock": iter(range(10, 100)).__next__,
    }
    values.update(overrides)
    return prepare_cli_contract(**values)


@pytest.mark.parametrize("raw", [False, 0, 1, "true", "yes", None, {}])
def test_contract_cli_flag_requires_exact_true_and_does_not_change_policy(raw) -> None:
    account = SimpleNamespace(agent=SimpleNamespace(harness={
        "adapter": "codex", "contract_cli_enabled": raw,
    }))
    parsed = HarnessSettings.from_account(account)
    assert parsed.contract_cli_enabled is False
    assert runtime_policy_revision({"adapter": "codex", "contract_cli_enabled": raw}) \
        == runtime_policy_revision({"adapter": "codex"})


def test_contract_cli_flag_accepts_true_without_changing_policy() -> None:
    account = SimpleNamespace(agent=SimpleNamespace(harness={
        "adapter": "codex", "contract_cli_enabled": True,
    }))
    assert HarnessSettings.from_account(account).contract_cli_enabled is True
    assert runtime_policy_revision(account.agent.harness) \
        == runtime_policy_revision({"adapter": "codex"})


def test_prepare_binds_exact_prompt_settings_launch_and_signed_authority() -> None:
    session = _prepare(fallback_argv=("codex", "--minimal", "private prompt"))
    invocation = session.invocation
    assert invocation.input.to_value() == {"prompt": "private prompt"}
    assert invocation.model_settings.to_value() == {
        "bridge_attached": True, "effort": "high", "initial_minimal": False,
        "timeout_s": 30.0,
    }
    assert invocation.authority.capability_ids == ("read",)
    assert invocation.authority.grant_ids == ("grant-1",)
    extensions = invocation.extensions.to_value()
    assert len(extensions["x.agentbridge.argv_digests"]) == 2
    assert extensions["x.agentbridge.env_names_digest"]
    serialized = str(invocation.to_dict())
    assert "top-secret" not in serialized and "/bin" not in serialized
    session.launch(("codex", "private prompt"))
    session.launch(("codex", "--minimal", "private prompt"))
    with pytest.raises(AgentContractError, match="changed"):
        session.launch(("codex", "--unsafe", "private prompt"))
    trace = session.complete("answer")
    assert sum("launch_digest" in event.payload.to_value()
               for event in trace.events) == 2


def test_prepare_rejects_substituted_or_mutated_canonical_records() -> None:
    run, task = _records()
    bad_task = replace(task, meta=replace(task.meta, run_id="run-other"))
    with pytest.raises(AgentContractError, match="task run"):
        _prepare(_delivery(run, bad_task))
    bad_run = replace(run, active_task_ids=("task-other",))
    with pytest.raises(AgentContractError, match="task active"):
        _prepare(_delivery(bad_run, task))
    child_task = replace(task, parent_task_id="task-parent")
    with pytest.raises(AgentContractError, match="root task"):
        _prepare(_delivery(run, child_task))


def test_terminal_race_is_idempotent_immutable_and_events_hide_secrets() -> None:
    session = _prepare()
    session.launch(("codex", "private prompt"))
    session.activity("  Reading   context  ")
    session.activity("A bounded public activity line " + "x" * 200)
    with ThreadPoolExecutor(max_workers=3) as pool:
        traces = list(pool.map(lambda action: action(), (
            lambda: session.complete("answer"),
            session.stop,
            lambda: session.fail(RuntimeError("raw top-secret failure")),
        )))
    assert traces[0] is traces[1] is traces[2]
    trace = traces[0]
    terminals = [event for event in trace.events if event.kind in {
        StreamEventKind.COMPLETED, StreamEventKind.FAILED,
        StreamEventKind.STOPPED, StreamEventKind.INTERRUPTED,
    }]
    assert len(terminals) == 1
    assert trace.result.status in {
        AgentResultStatus.COMPLETED, AgentResultStatus.FAILED,
        AgentResultStatus.STOPPED,
    }
    assert trace.result.last_event_sequence == trace.events[-1].sequence
    payloads = str([event.payload.to_value() for event in trace.events])
    assert "raw top-secret failure" not in payloads
    assert all(len(event.payload.to_value().get("text", "")) <= 120
               for event in trace.events)


def test_current_cli_success_matches_c21_fake_result_contract() -> None:
    session = _prepare()
    session.launch(("codex", "private prompt"))
    trace = session.complete("answer")
    sink = FakeEventStore(session.invocation.invocation_id)
    fake = ScriptedProvider((), final_text="answer")
    fake_result = fake.run(session.invocation, sink, FakeClock())
    sink.assert_settled(fake_result)
    assert (trace.result.status, trace.result.final_text, trace.result.usage) == (
        fake_result.status, fake_result.final_text, fake_result.usage,
    )
    assert trace.events[-1].kind is sink.events[-1].kind
