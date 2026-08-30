"""R126 canonical root-task security, durability, and run binding."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness.adapters.cli import CliResponder
from agentbridge.harness.conversation import Delivery
from agentbridge.harness.runtime.authority import AuthorityError
from agentbridge.harness.runtime.eventio import deliver_immutable
from agentbridge.harness.runtime.models import RunState, TaskState
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import (
    TaskLedger, TaskLedgerError, task_event_path,
)
from agentbridge.mesh.service import Mesh


@pytest.fixture()
def task_meshes(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "box", encrypt=True, home=home,
                 store_path=tmp_path / "agent.sqlite")
    chat = owner.create_chat("Runtime tasks", members=["helper"])
    owner.outbox.flush_once()
    agent.sync.sync_once([chat.id])
    try:
        yield owner, agent, chat.id
    finally:
        agent.close()
        owner.close()


def _ledgers(agent):
    runs = RunLedger(agent)
    return runs, TaskLedger(agent, runs)


def _start(tasks, chat_id, suffix="1"):
    return tasks.start_with_run(
        run_id=f"run-{suffix}", task_id=f"task-{suffix}", chat_id=chat_id,
        trigger_id=f"message-{suffix}", provider="codex", model="gpt-test",
    )


def test_immutable_delivery_skips_preflight_only_for_exclusive_transport():
    class Exclusive:
        supports_exclusive_create = True

        def __init__(self):
            self.created = []

        def get_doc(self, *_args, **_kwargs):
            raise AssertionError("exclusive create must not preflight")

        def create_doc(self, path, doc):
            self.created.append((path, doc))

    tx = Exclusive()
    deliver_immutable(tx, "runtime/run.json", {"signed": True})
    assert tx.created == [("runtime/run.json", {"signed": True})]

    class ResponseLost(Exclusive):
        def __init__(self, current):
            super().__init__()
            self.current = current

        def get_doc(self, _path, default=None):
            return self.current if self.current is not None else default

        def create_doc(self, path, doc):
            self.created.append((path, doc))
            raise OSError("response lost")

    same = ResponseLost({"signed": True})
    deliver_immutable(same, "runtime/run.json", {"signed": True})

    conflict = ResponseLost({"signed": False})
    with pytest.raises(OSError, match="response lost"):
        deliver_immutable(conflict, "runtime/run.json", {"signed": True})


def test_immutable_delivery_retains_nonexclusive_conflict_check():
    class NonExclusive:
        supports_exclusive_create = False

        def __init__(self, current):
            self.current = current
            self.created = []

        def get_doc(self, _path, default=None):
            return self.current if self.current is not None else default

        def create_doc(self, path, doc):
            self.created.append((path, doc))

    same = NonExclusive({"signed": True})
    deliver_immutable(same, "runtime/run.json", {"signed": True})
    assert same.created == []

    conflict = NonExclusive({"signed": False})
    with pytest.raises(ValidationError, match="already differs"):
        deliver_immutable(conflict, "runtime/run.json", {"signed": True})
    assert conflict.created == []


def test_atomic_root_task_is_encrypted_signed_and_bound_to_run(task_meshes):
    owner, agent, chat_id = task_meshes
    runs, tasks = _ledgers(agent)
    run, started = _start(tasks, chat_id)
    finished = tasks.finish("task-1", "done")
    runs.finish("run-1", "done", "Reply posted")

    raw = agent.tx.get_doc(task_event_path(
        chat_id, "run-1", "task-1", started.meta.id,
    ))
    encoded = json.dumps(raw, sort_keys=True)
    assert set(raw) == {"meta", "nonce", "ct", "sig"}
    assert "Respond to the triggering message" not in encoded
    assert "message-1" not in encoded
    assert run.active_task_ids == ("task-1",)

    owner_runs = RunLedger(owner)
    owner_tasks = TaskLedger(owner, owner_runs)
    assert tasks.read(chat_id, "run-1", "task-1") == [started, finished]
    assert owner_tasks.read(chat_id, "run-1", "task-1") == [started, finished]


def test_atomic_root_task_freezes_run_capability_ceiling(task_meshes):
    owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    run, _task = tasks.start_with_run(
        run_id="run-cap", task_id="task-cap", chat_id=chat_id,
        trigger_id="message-cap", provider="codex", model="gpt-test",
        capability_ceiling=("delegate_agent",),
    )
    assert run.capability_ceiling == ("delegate_agent",)
    owner_run = RunLedger(owner).read(chat_id, "run-cap")[0]
    assert owner_run.capability_ceiling == ("delegate_agent",)
    delivery = Delivery(
        agent="helper", chat_id=chat_id, chat_name="Runtime tasks",
        chat_kind="group", kind="message", rule="tagged",
        run_id="run-cap", task_id="task-cap",
        capability_ceiling=("delegate_agent",), canonical_run=run,
    )
    assert CliResponder._canonical_capability_ceiling(delivery) == (
        "delegate_agent",)
    delivery.capability_ceiling = ()
    with pytest.raises(ValidationError, match="changed after signing"):
        CliResponder._canonical_capability_ceiling(delivery)


def test_run_and_task_recovery_intents_commit_atomically(task_meshes, monkeypatch):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    before = agent.store.outbox_counts()
    original = agent.store.cache_docs_and_outbox_add_many
    monkeypatch.setattr(
        agent.store, "cache_docs_and_outbox_add_many",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        _start(tasks, chat_id, "atomic")
    assert agent.store.cached_doc("runtime/run-open", {}) == {}
    assert agent.store.cached_doc("runtime/task-open", {}) == {}
    assert agent.store.outbox_counts() == before
    monkeypatch.setattr(agent.store, "cache_docs_and_outbox_add_many", original)


def test_both_starts_remain_durable_when_transport_is_offline(
        task_meshes, monkeypatch):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    original_create = agent.tx.create_doc

    def fail_runtime(path, value):
        if "/runtime/" in path:
            raise OSError("offline")
        return original_create(path, value)

    monkeypatch.setattr(agent.tx, "create_doc", fail_runtime)
    run, task = _start(tasks, chat_id, "offline")
    assert run.state is RunState.RUNNING
    assert task.state is TaskState.ACTIVE
    assert "run-offline" in agent.store.cached_doc("runtime/run-open")
    assert "task-offline" in agent.store.cached_doc("runtime/task-open")
    assert agent.store.outbox_counts().get("pending") == 2


def test_first_progress_is_content_free_async_and_ordered(task_meshes):
    _owner, agent, chat_id = task_meshes
    runs, tasks = _ledgers(agent)
    _run, started = _start(tasks, chat_id, "progress")
    progress = tasks.progress("task-progress")
    assert progress is not None
    assert progress.progress == "Model work completed"
    assert tasks.progress("task-progress") is None
    terminal = tasks.finish("task-progress", "done")
    runs.finish("run-progress", "done", "private provider detail")
    agent.outbox.flush_once()

    events = tasks.read(chat_id, "run-progress", "task-progress")
    assert events == [started, progress, terminal]
    assert terminal.result == "Completed"
    assert "private provider detail" not in json.dumps(terminal.to_dict())


def test_progress_only_queues_transport_work(task_meshes, monkeypatch):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _start(tasks, chat_id, "async-progress")
    calls = []
    monkeypatch.setattr(
        agent.tx, "create_doc",
        lambda path, doc: calls.append((path, doc)),
    )

    progress = tasks.progress("task-async-progress")

    assert progress is not None
    assert calls == []
    assert agent.store.outbox_counts().get("pending") == 1


def test_invalid_terminal_does_not_poison_recovery_intent(task_meshes):
    _owner, agent, chat_id = task_meshes
    runs, tasks = _ledgers(agent)
    _start(tasks, chat_id, "invalid-terminal")

    with pytest.raises(TaskLedgerError, match="unsupported"):
        tasks.finish("task-invalid-terminal", "running")
    assert not tasks.has_terminal_intent("task-invalid-terminal")

    finished = tasks.finish("task-invalid-terminal", "done")
    runs.finish("run-invalid-terminal", "done", "Reply posted")
    assert finished.state is TaskState.COMPLETED


def test_paired_terminal_intents_survive_crash_before_outbox(task_meshes, monkeypatch):
    _owner, agent, chat_id = task_meshes
    runs, tasks = _ledgers(agent)
    _start(tasks, chat_id, "paired-retry")
    original = tasks._build_terminal
    monkeypatch.setattr(
        tasks, "_build_terminal",
        lambda *_args: (_ for _ in ()).throw(OSError("seal interrupted")),
    )

    with pytest.raises(OSError, match="seal interrupted"):
        tasks.finish_with_run(
            "task-paired-retry", "run-paired-retry", "done", "Reply posted",
        )
    assert tasks.has_terminal_intent("task-paired-retry")
    assert runs.has_terminal_intent("run-paired-retry")

    monkeypatch.setattr(tasks, "_build_terminal", original)
    assert tasks.retry_terminals() == 1
    assert [event.state for event in tasks.read(
        chat_id, "run-paired-retry", "task-paired-retry",
    )] == [TaskState.ACTIVE, TaskState.COMPLETED]
    assert [event.state for event in runs.read(chat_id, "run-paired-retry")] == [
        RunState.RUNNING, RunState.COMPLETED,
    ]


def test_exact_task_lookup_requires_run_and_first_terminal_wins(task_meshes):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _run, started = _start(tasks, chat_id, "terminal-fold")
    completed = replace(
        started,
        meta=replace(started.meta, id="task-event-completed",
                     ns=started.meta.ns + 1),
        state=TaskState.COMPLETED, progress="Completed", result="Completed",
    )
    failed = replace(
        started,
        meta=replace(started.meta, id="task-event-failed",
                     ns=started.meta.ns + 2),
        state=TaskState.FAILED, progress="Failed", result="Failed",
    )

    with pytest.raises(TaskLedgerError, match="requires its run"):
        tasks.read(chat_id, task_id="task-terminal-fold")
    assert TaskLedger._fold([failed, started, completed]) == [started]
    for record in (completed, failed):
        _target, payload = tasks._payload(record, terminal=True)
        agent.tx.create_doc(payload["path"], payload["doc"])
    assert tasks.read(chat_id, "run-terminal-fold", "task-terminal-fold") == [
        started,
    ]

    equal_ns = replace(completed, meta=replace(
        completed.meta, id="task-event-equal", ns=started.meta.ns,
    ))
    assert TaskLedger._fold([started, equal_ns, completed]) == [started, completed]


def test_rotated_room_key_invalidates_old_task_projection(task_meshes):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _run, started = _start(tasks, chat_id, "old-key")
    assert tasks.read(chat_id, "run-old-key", "task-old-key") == [started]

    agent.keys.rotate(chat_id, sorted(agent.snapshot(chat_id).members))

    assert tasks.read(chat_id, "run-old-key", "task-old-key") == []


def test_restart_recovers_task_then_run_as_interrupted(task_meshes):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _start(tasks, chat_id, "crashed")

    reopened = Mesh(agent.tx, "helper", "box", encrypt=True, home=agent.home,
                    store_path=agent.store.path)
    try:
        reopened_runs, reopened_tasks = _ledgers(reopened)
        assert reopened_tasks.recover_open() == 1
        assert reopened_runs.recover_open() == 0
        task_events = reopened_tasks.read(chat_id, "run-crashed", "task-crashed")
        run_events = reopened_runs.read(chat_id, "run-crashed")
        assert task_events[-1].state is TaskState.INTERRUPTED
        assert run_events[-1].state is RunState.INTERRUPTED
        assert reopened_tasks.recover_open() == 0
    finally:
        reopened.close()


def test_removed_member_cannot_enumerate_task_or_run(task_meshes):
    owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _start(tasks, chat_id, "removed")
    owner.remove_member(chat_id, "helper")
    owner.outbox.flush_once()
    agent.sync.sync_once([chat_id])

    with pytest.raises(AuthorityError):
        tasks.read(chat_id)
    assert TaskLedger(owner, RunLedger(owner)).read(chat_id) == []


def test_forged_agent_chain_and_cross_run_task_are_rejected(task_meshes):
    _owner, agent, chat_id = task_meshes
    runs, tasks = _ledgers(agent)
    run, started = _start(tasks, chat_id, "bound")
    forged = replace(
        started,
        meta=replace(started.meta, id="task-event-signed-forgery",
                     ns=started.meta.ns + 1),
        assigned_agent="other-agent",
    )
    wrong_run = replace(
        started,
        meta=replace(started.meta, id="task-event-wrong-run",
                     ns=started.meta.ns + 2, run_id="run-other",
                     root_run_id="run-other"),
    )
    for record in (forged, wrong_run):
        _target, payload = tasks._payload(record, terminal=False)
        agent.tx.create_doc(payload["path"], payload["doc"])

    assert started.meta.task_id in run.active_task_ids
    assert tasks.read(chat_id, "run-bound", "task-bound") == [started]
    assert tasks.read(chat_id) == [started]
    assert runs.read(chat_id, "run-bound")[0] == run


def test_tamper_and_exact_prefix_collision_fail_closed(task_meshes):
    _owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _run, started = _start(tasks, chat_id, "1")
    _start(tasks, chat_id, "10")
    path = task_event_path(chat_id, "run-1", "task-1", started.meta.id)
    raw = agent.tx.get_doc(path)
    tampered = dict(raw)
    tampered["ct"] = ("A" if raw["ct"][:1] != "A" else "B") + raw["ct"][1:]
    agent.tx.put_doc(path, tampered)
    assert tasks.read(chat_id, "run-1", "task-1") == []

    agent.tx.put_doc(path, raw)
    events = tasks.read(chat_id, "run-1", "task-1")
    assert events == [started]
    assert all(event.meta.run_id == "run-1" for event in events)


def test_parallel_root_tasks_keep_independent_recovery_state(task_meshes):
    _owner, agent, chat_id = task_meshes
    runs, tasks = _ledgers(agent)

    with ThreadPoolExecutor(max_workers=4) as pool:
        pairs = list(pool.map(
            lambda i: _start(tasks, chat_id, f"parallel-{i}"), range(4),
        ))
    assert set(agent.store.cached_doc("runtime/task-open")) == {
        f"task-parallel-{i}" for i in range(4)
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        terminals = list(pool.map(
            lambda pair: tasks.finish(pair[1].meta.task_id or "", "done"),
            pairs,
        ))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            lambda pair: runs.finish(pair[0].meta.run_id or "", "done", "posted"),
            pairs,
        ))
    assert agent.store.cached_doc("runtime/task-open") == {}
    assert all(record.state is TaskState.COMPLETED for record in terminals)


def test_policy_drift_invalidates_task_without_relabelling_terminal(task_meshes):
    owner, agent, chat_id = task_meshes
    _runs, tasks = _ledgers(agent)
    _run, started = _start(tasks, chat_id, "policy")
    owner.accounts.set_agent_harness("helper", {"web": True})
    terminal = tasks.finish("task-policy", "done")

    assert terminal.meta.policy_revision == started.meta.policy_revision
    assert tasks.read(chat_id, "run-policy", "task-policy") == []
