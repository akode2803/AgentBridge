"""V157 manager-retained same-room agent-tool orchestration tests."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agentbridge.harness.adapters.cli import ChildResult
from agentbridge.harness.runtime.delegation import (
    DelegationCoordinator, DelegationError,
)
from agentbridge.harness.runtime.handoffs import HandoffLedger
from agentbridge.harness.runtime.models import HandoffState
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger
from agentbridge.mesh.service import Mesh


@pytest.fixture()
def delegation_meshes(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent(
        "manager", harness={"agent_tools_enabled": True},
    )
    owner.accounts.create_agent(
        "specialist", harness={"agent_tools_enabled": True,
                               "routing": {"agents": {"enabled": True}}},
    )
    manager = Mesh(root, "manager", "box", encrypt=True, home=home,
                   store_path=tmp_path / "manager.sqlite")
    specialist = Mesh(root, "specialist", "box", encrypt=True, home=home,
                      store_path=tmp_path / "specialist.sqlite")
    chat = owner.create_chat(
        "Delegation proof", members=["manager", "specialist"],
    )
    owner.outbox.flush_once()
    manager.sync.sync_once([chat.id])
    specialist.sync.sync_once([chat.id])
    try:
        yield owner, manager, specialist, chat.id
    finally:
        specialist.close()
        manager.close()
        owner.close()


def _coordinator(mesh):
    runs = RunLedger(mesh)
    tasks = TaskLedger(mesh, runs)
    handoffs = HandoffLedger(mesh, tasks)
    return runs, tasks, handoffs, DelegationCoordinator(
        mesh, handoffs, machine="box",
    )


class _ChildResponder:
    def __init__(self, text="Specialist evidence"):
        self.text = text
        self.requests = []

    def prepare_child(self, request, *, chat_id=""):
        self.requests.append((request, chat_id))
        return SimpleNamespace(
            prompt_digest="d" * 64, provider="fake-text", model="model-a",
        )

    def respond_child(self, prepared, *, cancelled=None):
        assert cancelled is None or not cancelled()
        return ChildResult(
            text=self.text, provider=prepared.provider, model=prepared.model,
            prompt_digest=prepared.prompt_digest,
        )


def _authorized_work(manager, specialist, chat_id, suffix):
    _runs, tasks, source_handoffs, source = _coordinator(manager)
    run_id = f"run-{suffix}"
    task_id = f"task-{suffix}"
    tasks.start_with_run(
        run_id=run_id, task_id=task_id, chat_id=chat_id,
        trigger_id=f"message-{suffix}", provider="codex", model="gpt-test",
    )
    offered = source_handoffs.offer(
        chat_id=chat_id, run_id=run_id, parent_task_id=task_id,
        destination_agent="specialist", objective="Review", reason="Review",
        success_criteria=("Return a finding",),
    )
    _r, _t, destination_handoffs, destination = _coordinator(specialist)
    handoff_id = offered.events[0].meta.call_id or ""
    destination_handoffs.decide(
        chat_id=chat_id, run_id=run_id, handoff_id=handoff_id, accept=True,
    )
    source_handoffs.authorize(
        chat_id=chat_id, run_id=run_id, handoff_id=handoff_id,
    )
    work = destination.claim_ready(exclude=set())[0]
    return source_handoffs, destination_handoffs, source, destination, work


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_manager_retained_agent_tool_returns_once_without_room_post(
        delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _runs, source_tasks, source_handoffs, source = _coordinator(manager)
    source_tasks.start_with_run(
        run_id="run-1", task_id="task-1", chat_id=chat_id,
        trigger_id="message-1", provider="codex", model="gpt-test",
    )
    _dr, _dt, destination_handoffs, destination = _coordinator(specialist)
    before = [(m.id, m.from_, m.body) for m in manager.messages_for(chat_id)]
    outcome = {}

    def call_manager():
        outcome["value"] = source.delegate(
            chat_id=chat_id, run_id="run-1", parent_task_id="task-1",
            destination_agent="specialist", objective="Check the evidence",
            reason="Independent review",
            success_criteria=("Return one concise finding",),
        )

    thread = threading.Thread(target=call_manager)
    thread.start()
    _wait_until(lambda: source_handoffs.read(chat_id, "run-1"))

    # First pass accepts; the manager then authorizes while its provider call
    # remains blocked. A later pass atomically claims the child.
    def accepted():
        destination.claim_ready(exclude=set())
        views = source_handoffs.read(chat_id, "run-1")
        return bool(views and views[0].events[-1].state
                    is HandoffState.ACCEPTED)

    _wait_until(accepted)
    work = _wait_until(lambda: destination.claim_ready(exclude=set()))[0]
    responder = _ChildResponder()
    destination.execute(work, responder)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert outcome["value"] == "Specialist evidence"
    assert source_handoffs.read(
        chat_id, "run-1",
    )[0].events[-1].state is HandoffState.RETURNED
    assert source.mark_provider_completed("run-1") == 1
    assert source.consume_for_run("run-1") == 1
    assert len(responder.requests) == 1
    request, request_chat = responder.requests[0]
    assert request_chat == chat_id
    assert request.objective == "Check the evidence"
    assert request.max_output_chars == destination.RESULT_CHARS
    assert [(m.id, m.from_, m.body) for m in manager.messages_for(chat_id)] == before
    view = source_handoffs.read(chat_id, "run-1")[0]
    assert [event.state for event in view.events] == [
        HandoffState.OFFERED, HandoffState.ACCEPTED,
        HandoffState.AUTHORIZED, HandoffState.ACTIVE,
        HandoffState.RETURNED, HandoffState.CONSUMED,
    ]


def test_destination_flag_off_refuses_offer(delegation_meshes):
    owner, manager, _specialist, chat_id = delegation_meshes
    owner.accounts.set_agent_harness(
        "specialist", {"agent_tools_enabled": False},
    )
    _runs, tasks, _handoffs, source = _coordinator(manager)
    tasks.start_with_run(
        run_id="run-flag", task_id="task-flag", chat_id=chat_id,
        trigger_id="message-flag", provider="codex", model="gpt-test",
    )
    with pytest.raises(DelegationError, match="not accepting"):
        source.delegate(
            chat_id=chat_id, run_id="run-flag", parent_task_id="task-flag",
            destination_agent="specialist", objective="Review", reason="Review",
            success_criteria=("Return a finding",),
        )


def test_authorized_work_drains_after_destination_flag_is_disabled(
        delegation_meshes):
    owner, manager, specialist, chat_id = delegation_meshes
    source_handoffs, _destination_handoffs, _source, destination, work = \
        _authorized_work(manager, specialist, chat_id, "flag-drain")
    account = owner.directory.get("specialist")
    harness = dict(account.agent.harness or {})
    owner.accounts.set_agent_harness(
        "specialist", {**harness, "agent_tools_enabled": False},
    )
    owner.outbox.flush_once()
    manager.sync.sync_once([chat_id])
    specialist.sync.sync_once([chat_id])

    responder = _ChildResponder("Drained specialist result")
    destination.execute(work, responder)

    view = source_handoffs.read(chat_id, work.run_id, work.handoff_id)[0]
    assert [event.state for event in view.events] == [
        HandoffState.OFFERED, HandoffState.ACCEPTED,
        HandoffState.AUTHORIZED, HandoffState.ACTIVE,
        HandoffState.RETURNED,
    ]
    assert len(responder.requests) == 1


def test_destination_policy_drift_interrupts_without_model_invocation(
        delegation_meshes):
    owner, manager, specialist, chat_id = delegation_meshes
    source_handoffs, _destination_handoffs, _source, destination, work = \
        _authorized_work(manager, specialist, chat_id, "policy-drift")
    account = owner.directory.get("specialist")
    harness = dict(account.agent.harness or {})
    owner.accounts.set_agent_harness(
        "specialist", {**harness, "model": "changed-after-authorization"},
    )
    owner.outbox.flush_once()
    manager.sync.sync_once([chat_id])
    specialist.sync.sync_once([chat_id])

    class MustNotRun(_ChildResponder):
        def respond_child(self, prepared, *, cancelled=None):
            raise AssertionError("policy drift must stop before model invocation")

    responder = MustNotRun()
    destination.execute(work, responder)

    view = source_handoffs.read(chat_id, work.run_id, work.handoff_id)[0]
    assert [event.state for event in view.events] == [
        HandoffState.OFFERED, HandoffState.ACCEPTED,
        HandoffState.AUTHORIZED, HandoffState.INTERRUPTED,
    ]
    journal = specialist.store.cached_doc(
        destination._journal_path(work.handoff_id), default={},
    )
    assert journal["state"] == "interrupted"
    assert len(responder.requests) == 1


def test_wrong_destination_host_never_claims(delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _runs, tasks, source_handoffs, _source = _coordinator(manager)
    tasks.start_with_run(
        run_id="run-host", task_id="task-host", chat_id=chat_id,
        trigger_id="message-host", provider="codex", model="gpt-test",
    )
    offered = source_handoffs.offer(
        chat_id=chat_id, run_id="run-host", parent_task_id="task-host",
        destination_agent="specialist", objective="Review", reason="Review",
        success_criteria=("Return a finding",),
    )
    _r, _t, destination_handoffs, _destination = _coordinator(specialist)
    handoff_id = offered.events[0].meta.call_id or ""
    destination_handoffs.decide(
        chat_id=chat_id, run_id="run-host", handoff_id=handoff_id, accept=True,
    )
    source_handoffs.authorize(
        chat_id=chat_id, run_id="run-host", handoff_id=handoff_id,
    )
    wrong_host = DelegationCoordinator(
        specialist, destination_handoffs, machine="another-machine",
    )
    assert wrong_host.claim_ready(exclude=set()) == []
    assert specialist.store.cached_doc(
        wrong_host._journal_path(handoff_id), default=None,
    ) is None


def test_restart_during_executing_settles_interrupted_without_reinvoke(
        delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _runs, tasks, source_handoffs, _source = _coordinator(manager)
    tasks.start_with_run(
        run_id="run-crash", task_id="task-crash", chat_id=chat_id,
        trigger_id="message-crash", provider="codex", model="gpt-test",
    )
    offered = source_handoffs.offer(
        chat_id=chat_id, run_id="run-crash", parent_task_id="task-crash",
        destination_agent="specialist", objective="Review", reason="Review",
        success_criteria=("Return a finding",),
    )
    _r, _t, destination_handoffs, destination = _coordinator(specialist)
    handoff_id = offered.events[0].meta.call_id or ""
    destination_handoffs.decide(
        chat_id=chat_id, run_id="run-crash", handoff_id=handoff_id, accept=True,
    )
    source_handoffs.authorize(
        chat_id=chat_id, run_id="run-crash", handoff_id=handoff_id,
    )
    work = destination.claim_ready(exclude=set())[0]
    context, manifest = destination._context(chat_id)
    assert context
    destination_handoffs.activate(
        chat_id=chat_id, run_id="run-crash", handoff_id=handoff_id,
        manifest=manifest,
    )
    journal_path = destination._journal_path(handoff_id)
    journal = specialist.store.cached_doc(journal_path)
    journal["state"] = "executing"
    specialist.store.cache_doc(journal_path, journal)

    assert destination.claim_ready(exclude=set()) == []
    view = destination_handoffs.read(chat_id, "run-crash", handoff_id)[0]
    assert view.events[-1].state is HandoffState.INTERRUPTED
    assert specialist.store.cached_doc(journal_path)["state"] == "interrupted"
    assert work.handoff_id == handoff_id


def test_child_preflight_failure_settles_without_retry(delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _runs, tasks, source_handoffs, _source = _coordinator(manager)
    tasks.start_with_run(
        run_id="run-preflight", task_id="task-preflight", chat_id=chat_id,
        trigger_id="message-preflight", provider="codex", model="gpt-test",
    )
    offered = source_handoffs.offer(
        chat_id=chat_id, run_id="run-preflight",
        parent_task_id="task-preflight", destination_agent="specialist",
        objective="Review", reason="Review",
        success_criteria=("Return a finding",),
    )
    _r, _t, destination_handoffs, destination = _coordinator(specialist)
    handoff_id = offered.events[0].meta.call_id or ""
    destination_handoffs.decide(
        chat_id=chat_id, run_id="run-preflight", handoff_id=handoff_id,
        accept=True,
    )
    source_handoffs.authorize(
        chat_id=chat_id, run_id="run-preflight", handoff_id=handoff_id,
    )
    work = destination.claim_ready(exclude=set())[0]

    class Unsafe:
        def prepare_child(self, *_args, **_kwargs):
            raise DelegationError("adapter is not text-only")

    destination.execute(work, Unsafe())
    view = destination_handoffs.read(
        chat_id, "run-preflight", handoff_id,
    )[0]
    assert [event.state for event in view.events][-2:] == [
        HandoffState.AUTHORIZED, HandoffState.INTERRUPTED,
    ]
    assert destination.claim_ready(exclude=set()) == []
    assert specialist.store.cached_doc(
        destination._journal_path(handoff_id),
    )["state"] == "interrupted"


def test_two_local_coordinators_cannot_claim_one_child(delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    source_handoffs, destination_handoffs, _source, destination, work = (
        _authorized_work(manager, specialist, chat_id, "claim-race")
    )
    rival = DelegationCoordinator(
        specialist, destination_handoffs, machine="box",
    )
    assert rival.claim_ready(exclude=set()) == []
    assert destination.claim_ready(exclude={work.handoff_id}) == []
    view = source_handoffs.read(chat_id, "run-claim-race")[0]
    assert view.events[-1].state is HandoffState.AUTHORIZED


def test_restart_after_active_before_executing_safely_reclaims(
        delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _source_handoffs, destination_handoffs, _source, destination, work = (
        _authorized_work(manager, specialist, chat_id, "active-window")
    )
    _context, manifest = destination._context(chat_id)
    destination_handoffs.activate(
        chat_id=chat_id, run_id=work.run_id, handoff_id=work.handoff_id,
        manifest=manifest,
    )
    path = destination._journal_path(work.handoff_id)
    journal = specialist.store.cached_doc(path)
    assert journal["state"] == "claimed"
    journal["claim_lease_ns"] = 0
    specialist.store.cache_doc(path, journal)

    restarted = DelegationCoordinator(
        specialist, destination_handoffs, machine="box",
    )
    resumed = restarted.claim_ready(exclude=set())
    assert len(resumed) == 1
    restarted.execute(resumed[0], _ChildResponder("Recovered contribution"))
    view = destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0]
    assert view.events[-1].state is HandoffState.RETURNED
    assert specialist.store.cached_doc(path)["state"] == "committed"


def test_result_ready_restart_publishes_without_provider_reinvoke(
        delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _source_handoffs, destination_handoffs, _source, destination, work = (
        _authorized_work(manager, specialist, chat_id, "result-window")
    )
    _context, manifest = destination._context(chat_id)
    destination_handoffs.activate(
        chat_id=chat_id, run_id=work.run_id, handoff_id=work.handoff_id,
        manifest=manifest,
    )
    path = destination._journal_path(work.handoff_id)
    journal = specialist.store.cached_doc(path)
    journal.update({
        "state": "result_ready", "result": "Persisted contribution",
        "prompt_digest": "e" * 64,
    })
    specialist.store.cache_doc(path, journal)

    restarted = DelegationCoordinator(
        specialist, destination_handoffs, machine="box",
    )
    assert restarted.claim_ready(exclude=set()) == []
    view = destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0]
    assert view.events[-1].state is HandoffState.RETURNED
    assert specialist.store.cached_doc(path)["state"] == "committed"


@pytest.mark.parametrize("closure", ["expired", "parent-finished"])
def test_result_ready_restart_settles_when_return_is_no_longer_allowed(
        delegation_meshes, monkeypatch, closure):
    _owner, manager, specialist, chat_id = delegation_meshes
    source_handoffs, destination_handoffs, _source, destination, work = (
        _authorized_work(manager, specialist, chat_id, f"blocked-{closure}")
    )
    _context, manifest = destination._context(chat_id)
    destination_handoffs.activate(
        chat_id=chat_id, run_id=work.run_id, handoff_id=work.handoff_id,
        manifest=manifest,
    )
    path = destination._journal_path(work.handoff_id)
    journal = specialist.store.cached_doc(path)
    journal.update({
        "state": "result_ready", "result": "Persisted contribution",
        "prompt_digest": "e" * 64,
    })
    specialist.store.cache_doc(path, journal)
    if closure == "expired":
        view = destination_handoffs.read(
            chat_id, work.run_id, work.handoff_id,
        )[0]
        expiry = int(view.events[2].meta.expires_ns or 0)
        monkeypatch.setattr(
            "agentbridge.harness.runtime.handoffs.time",
            SimpleNamespace(time_ns=lambda: expiry + 1),
        )
    else:
        runs = source_handoffs.run_ledger
        tasks = source_handoffs.task_ledger
        parent_task_id = destination_handoffs.read(
            chat_id, work.run_id, work.handoff_id,
        )[0].task.parent_task_id or ""
        tasks.finish_with_run(
            parent_task_id, work.run_id, "done", "Manager completed",
        )
        assert runs.read(chat_id, work.run_id)[-1].state.value == "completed"

    restarted = DelegationCoordinator(
        specialist, destination_handoffs, machine="box",
    )
    assert restarted.claim_ready(exclude=set()) == []
    view = destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0]
    assert view.events[-1].state is HandoffState.INTERRUPTED
    assert specialist.store.cached_doc(path)["state"] == "interrupted"


def test_shutdown_cancels_an_executing_child(delegation_meshes):
    _owner, manager, specialist, chat_id = delegation_meshes
    _source_handoffs, destination_handoffs, _source, _destination, work = (
        _authorized_work(manager, specialist, chat_id, "shutdown")
    )
    stopping = threading.Event()
    destination = DelegationCoordinator(
        specialist, destination_handoffs, machine="box",
        stopping=stopping.is_set,
    )
    # Reclaim the lease created by the helper coordinator.
    path = destination._journal_path(work.handoff_id)
    journal = specialist.store.cached_doc(path)
    journal["claim_lease_ns"] = 0
    specialist.store.cache_doc(path, journal)
    work = destination.claim_ready(exclude=set())[0]

    class BlockingResponder(_ChildResponder):
        def respond_child(self, prepared, *, cancelled=None):
            while not cancelled():
                time.sleep(0.01)
            raise DelegationError("cancelled")

    thread = threading.Thread(
        target=destination.execute, args=(work, BlockingResponder()),
    )
    thread.start()
    _wait_until(lambda: specialist.store.cached_doc(path)["state"] == "executing")
    stopping.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    view = destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0]
    assert view.events[-1].state is HandoffState.INTERRUPTED


def test_source_shutdown_releases_blocking_delegation(delegation_meshes):
    _owner, manager, _specialist, chat_id = delegation_meshes
    _runs, tasks, source_handoffs, _source = _coordinator(manager)
    tasks.start_with_run(
        run_id="run-source-stop", task_id="task-source-stop", chat_id=chat_id,
        trigger_id="message-source-stop", provider="codex", model="gpt-test",
    )
    stopping = threading.Event()
    source = DelegationCoordinator(
        manager, source_handoffs, machine="box", stopping=stopping.is_set,
    )
    source.POLL_S = 0.01
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(value=source.delegate(
        chat_id=chat_id, run_id="run-source-stop",
        parent_task_id="task-source-stop", destination_agent="specialist",
        objective="Review", reason="Review",
        success_criteria=("Return a finding",),
    )))
    thread.start()
    _wait_until(lambda: source_handoffs.read(chat_id, "run-source-stop"))
    stopping.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert "runner is stopping" in outcome["value"]


def test_completed_provider_consumption_retries_without_provider_replay(
        delegation_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = delegation_meshes
    source_handoffs, _destination_handoffs, source, destination, work = (
        _authorized_work(manager, specialist, chat_id, "consume-retry")
    )
    destination.execute(work, _ChildResponder())
    returned = source_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0].events[-1]
    source._remember_consumption(
        run_id=work.run_id, chat_id=chat_id, handoff_id=work.handoff_id,
        returned_id=returned.meta.id,
    )
    assert source.mark_provider_completed(work.run_id) == 1
    real_consume = source_handoffs.consume
    monkeypatch.setattr(
        source_handoffs, "consume",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    assert source.consume_for_run(work.run_id) == 0
    monkeypatch.setattr(source_handoffs, "consume", real_consume)

    assert source.retry_consumptions() == 1
    assert source_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0].events[-1].state is HandoffState.CONSUMED


def test_result_ready_reuses_pending_offline_return(delegation_meshes,
                                                    monkeypatch):
    _owner, manager, specialist, chat_id = delegation_meshes
    _source_handoffs, destination_handoffs, _source, destination, work = (
        _authorized_work(manager, specialist, chat_id, "return-offline")
    )
    _context, manifest = destination._context(chat_id)
    destination_handoffs.activate(
        chat_id=chat_id, run_id=work.run_id, handoff_id=work.handoff_id,
        manifest=manifest,
    )
    path = destination._journal_path(work.handoff_id)
    journal = specialist.store.cached_doc(path)
    journal.update({
        "state": "result_ready", "result": "Persisted contribution",
        "prompt_digest": "f" * 64,
    })
    specialist.store.cache_doc(path, journal)
    original = specialist.tx.create_doc
    monkeypatch.setattr(
        specialist.tx, "create_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    first = destination_handoffs.return_result(
        chat_id=chat_id, run_id=work.run_id, handoff_id=work.handoff_id,
        contribution="Persisted contribution", prompt_digest="f" * 64,
    )
    restarted = DelegationCoordinator(
        specialist, destination_handoffs, machine="box",
    )
    assert restarted.claim_ready(exclude=set()) == []
    pending = destination_handoffs._pending_record(work.handoff_id)
    assert pending is not None and pending.meta.id == first.meta.id

    monkeypatch.setattr(specialist.tx, "create_doc", original)
    specialist.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    specialist.outbox.flush_once()
    view = destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0]
    assert view.events[-1].state is HandoffState.RETURNED
    assert sum(event.state is HandoffState.RETURNED for event in view.events) == 1


def test_offline_activation_waits_for_canonical_record_before_provider(
        delegation_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = delegation_meshes
    _source_handoffs, destination_handoffs, _source, destination, work = (
        _authorized_work(manager, specialist, chat_id, "active-offline")
    )

    class CountingResponder(_ChildResponder):
        def __init__(self):
            super().__init__()
            self.invocations = 0

        def respond_child(self, prepared, *, cancelled=None):
            self.invocations += 1
            return super().respond_child(prepared, cancelled=cancelled)

    responder = CountingResponder()
    original = specialist.tx.create_doc
    monkeypatch.setattr(
        specialist.tx, "create_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    destination.execute(work, responder)
    path = destination._journal_path(work.handoff_id)
    assert specialist.store.cached_doc(path)["state"] == "awaiting_active"
    assert responder.invocations == 0
    assert destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0].events[-1].state is HandoffState.AUTHORIZED
    assert destination.claim_ready(exclude=set()) == []

    monkeypatch.setattr(specialist.tx, "create_doc", original)
    specialist.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    specialist.outbox.flush_once()
    resumed = destination.claim_ready(exclude=set())
    assert len(resumed) == 1
    destination.execute(resumed[0], responder)

    assert responder.invocations == 1
    view = destination_handoffs.read(
        chat_id, work.run_id, work.handoff_id,
    )[0]
    assert view.events[-1].state is HandoffState.RETURNED


@pytest.mark.parametrize("accept", [True, False])
def test_late_synced_decision_settles_once_without_timeout_loop(
        delegation_meshes, monkeypatch, accept):
    _owner, manager, specialist, chat_id = delegation_meshes
    _runs, tasks, source_handoffs, source = _coordinator(manager)
    tasks.start_with_run(
        run_id="run-late", task_id="task-late", chat_id=chat_id,
        trigger_id="message-late", provider="codex", model="gpt-test",
    )
    source.ACCEPTANCE_S = 2.0
    source.POLL_S = 0.01
    _r, _t, destination_handoffs, _destination = _coordinator(specialist)
    outcome = {}
    real_view = source._view
    release_at = time.monotonic() + 2.2

    def delayed_view(*args):
        view = real_view(*args)
        if time.monotonic() < release_at:
            return type(view)(view.task, (view.events[0],))
        return view

    monkeypatch.setattr(source, "_view", delayed_view)
    calls = 0
    real_timeout = source_handoffs.timeout

    def counted_timeout(**kwargs):
        nonlocal calls
        calls += 1
        return real_timeout(**kwargs)

    monkeypatch.setattr(source_handoffs, "timeout", counted_timeout)

    thread = threading.Thread(target=lambda: outcome.update(value=source.delegate(
        chat_id=chat_id, run_id="run-late", parent_task_id="task-late",
        destination_agent="specialist", objective="Review", reason="Review",
        success_criteria=("Return a finding",),
    )))
    thread.start()
    offered = _wait_until(lambda: source_handoffs.read(chat_id, "run-late"))[0]
    destination_handoffs.decide(
        chat_id=chat_id, run_id="run-late",
        handoff_id=offered.events[0].meta.call_id or "", accept=accept,
    )
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert calls == 1
    assert "deadline" in outcome["value"]
