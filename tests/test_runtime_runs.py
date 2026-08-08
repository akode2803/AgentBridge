"""C1.4 canonical run-ledger security and compatibility behavior."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agentbridge.harness.runtime.authority import AuthorityError
from agentbridge.harness.runtime.models import RunState
from agentbridge.harness.runtime.runs import RunLedger, RunLedgerError, run_event_path
from agentbridge.mesh.service import Mesh


@pytest.fixture()
def run_meshes(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "box", encrypt=True, home=home,
                 store_path=tmp_path / "agent.sqlite")
    chat = owner.create_chat("Runtime", members=["helper"])
    owner.outbox.flush_once()
    agent.sync.sync_once([chat.id])
    try:
        yield owner, agent, chat.id
    finally:
        agent.close()
        owner.close()


def test_run_events_are_encrypted_signed_and_visible_to_current_members(run_meshes):
    owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-1", chat_id=chat_id, trigger_id="message-7",
        provider="codex", model="gpt-test",
    )
    finished = ledger.finish("run-1", "done", "Reply posted")

    raw = agent.tx.get_doc(run_event_path(chat_id, "run-1", started.meta.id))
    encoded = json.dumps(raw, sort_keys=True)
    assert "message-7" not in encoded
    assert "gpt-test" not in encoded
    assert "Reply posted" not in encoded

    agent_view = ledger.read(chat_id, "run-1")
    owner_view = RunLedger(owner).read(chat_id, "run-1")
    assert agent_view == owner_view == [started, finished]
    assert [event.state for event in owner_view] == [
        RunState.RUNNING, RunState.COMPLETED,
    ]
    assert owner_view[-1].outcome == "completed"


def test_tamper_and_cross_room_retarget_fail_closed(run_meshes):
    owner, agent, chat_id = run_meshes
    other = owner.create_chat("Other", members=["helper"])
    owner.outbox.flush_once()
    agent.sync.sync_once([other.id])
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-2", chat_id=chat_id, trigger_id="message-8",
        provider="codex", model="gpt-test",
    )
    path = run_event_path(chat_id, "run-2", started.meta.id)
    raw = agent.tx.get_doc(path)
    tampered = dict(raw)
    tampered["ct"] = ("A" if raw["ct"][:1] != "A" else "B") + raw["ct"][1:]
    agent.tx.put_doc(path, tampered)
    assert ledger.read(chat_id, "run-2") == []

    agent.tx.put_doc(path, raw)
    agent.tx.put_doc(run_event_path(other.id, "run-2", started.meta.id), raw)
    assert ledger.read(other.id, "run-2") == []


def test_removed_member_cannot_enumerate_or_publish_runs(run_meshes):
    owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-before", chat_id=chat_id, trigger_id="message-9",
        provider="codex", model="gpt-test",
    )
    owner.remove_member(chat_id, "helper")
    owner.outbox.flush_once()
    agent.sync.sync_once([chat_id])

    with pytest.raises(AuthorityError):
        ledger.read(chat_id)
    with pytest.raises((AuthorityError, RunLedgerError)):
        ledger.start(
            run_id="run-after", chat_id=chat_id, trigger_id="message-10",
            provider="codex", model="gpt-test",
        )
    assert RunLedger(owner).read(chat_id, "run-before") == []


def test_start_is_durable_before_transport_and_retries_offline(run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    original_create = agent.tx.create_doc

    def fail_runtime(path, value):
        if "/runtime/runs/" in path:
            raise OSError("offline")
        return original_create(path, value)

    monkeypatch.setattr(agent.tx, "create_doc", fail_runtime)
    started = ledger.start(
        run_id="run-offline", chat_id=chat_id, trigger_id="message-11",
        provider="codex", model="gpt-test",
    )
    assert started.state is RunState.RUNNING
    assert agent.store.outbox_counts().get("pending") == 1


def test_terminal_outbox_success_clears_local_intent(run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-terminal-offline", chat_id=chat_id,
        trigger_id="message-terminal", provider="codex", model="gpt-test",
    )
    original_create = agent.tx.create_doc

    def fail_terminal(path, value):
        if "/runtime/runs/run-terminal-offline/" in path:
            raise OSError("offline")
        return original_create(path, value)

    monkeypatch.setattr(agent.tx, "create_doc", fail_terminal)
    original_retry = agent.store.outbox_retry
    monkeypatch.setattr(
        agent.store, "outbox_retry",
        lambda seq, error, _delay: original_retry(seq, error, 0),
    )
    finished = ledger.finish("run-terminal-offline", "done", "Reply posted")
    assert finished.state is RunState.COMPLETED
    assert ledger.has_terminal_intent("run-terminal-offline")

    monkeypatch.setattr(agent.tx, "create_doc", original_create)
    assert agent.outbox.flush_once() == 1
    assert not ledger.has_terminal_intent("run-terminal-offline")


def test_terminal_outbox_retries_until_local_cleanup_succeeds(run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-cleanup-retry", chat_id=chat_id,
        trigger_id="message-cleanup", provider="codex", model="gpt-test",
    )
    original_cache = agent.store.cache_doc

    def fail_cleanup(path, value):
        if path == "runtime/run-open" and "run-cleanup-retry" not in value:
            raise OSError("local cleanup failed")
        return original_cache(path, value)

    monkeypatch.setattr(agent.store, "cache_doc", fail_cleanup)
    original_retry = agent.store.outbox_retry
    monkeypatch.setattr(
        agent.store, "outbox_retry",
        lambda seq, error, _delay: original_retry(seq, error, 0),
    )
    ledger.finish("run-cleanup-retry", "done", "Reply posted")
    assert ledger.has_terminal_intent("run-cleanup-retry")
    assert agent.store.outbox_counts().get("pending") == 1

    monkeypatch.setattr(agent.store, "cache_doc", original_cache)
    assert agent.outbox.flush_once() == 1
    assert not ledger.has_terminal_intent("run-cleanup-retry")


def test_delivered_terminal_does_not_fail_run_when_outbox_delete_is_late(
        run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-late-delete", chat_id=chat_id,
        trigger_id="message-late-delete", provider="codex", model="gpt-test",
    )
    original_done = agent.store.outbox_done
    monkeypatch.setattr(
        agent.store, "outbox_done",
        lambda _seq: (_ for _ in ()).throw(OSError("sqlite busy")),
    )

    finished = ledger.finish("run-late-delete", "done", "Reply posted")
    assert finished.state is RunState.COMPLETED
    assert not ledger.has_terminal_intent("run-late-delete")
    assert ledger.read(chat_id, "run-late-delete")[-1] == finished

    monkeypatch.setattr(agent.store, "outbox_done", original_done)
    assert agent.outbox.flush_once() >= 1


def test_open_run_recovers_as_interrupted_after_restart(run_meshes):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-crashed", chat_id=chat_id, trigger_id="message-12",
        provider="codex", model="gpt-test",
    )

    reopened = Mesh(agent.tx, "helper", "box", encrypt=True, home=agent.home,
                    store_path=agent.store.path)
    try:
        recovery = RunLedger(reopened)
        assert recovery.recover_open() == 1
        events = recovery.read(chat_id, "run-crashed")
        assert events[0] == started
        assert events[-1].state is RunState.INTERRUPTED
        assert recovery.recover_open() == 0
    finally:
        reopened.close()


def test_parallel_runs_keep_independent_recovery_state(run_meshes):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)

    def start(i):
        return ledger.start(
            run_id=f"parallel-{i}", chat_id=chat_id,
            trigger_id=f"message-{i}", provider="codex", model="gpt-test",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(pool.map(start, range(4)))
    assert set(agent.store.cached_doc("runtime/run-open")) == {
        f"parallel-{i}" for i in range(4)
    }

    with ThreadPoolExecutor(max_workers=4) as pool:
        terminals = list(pool.map(
            lambda record: ledger.finish(
                record.meta.run_id or "", "done", "Reply posted"),
            starts,
        ))
    assert agent.store.cached_doc("runtime/run-open") == {}
    assert all(record.state is RunState.COMPLETED for record in terminals)


def test_overlapping_terminal_retry_enqueues_one_terminal(run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-overlap", chat_id=chat_id, trigger_id="message-overlap",
        provider="codex", model="gpt-test",
    )
    original_payload = ledger._payload
    entered = threading.Event()
    release = threading.Event()

    def slow_payload(record, **kwargs):
        if kwargs.get("terminal"):
            entered.set()
            release.wait(timeout=2)
        return original_payload(record, **kwargs)

    monkeypatch.setattr(ledger, "_payload", slow_payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ledger.finish, "run-overlap", "done", "Reply posted")
        assert entered.wait(timeout=2)
        second = pool.submit(ledger.retry_terminals)
        release.set()
        assert first.result().state is RunState.COMPLETED
        assert second.result() in {0, 1}

    events = ledger.read(chat_id, "run-overlap")
    assert [event.state for event in events] == [
        RunState.RUNNING, RunState.COMPLETED,
    ]


def test_terminal_must_match_starting_agent_and_exact_run_id(run_meshes):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    run_one = ledger.start(
        run_id="run-1", chat_id=chat_id, trigger_id="message-a",
        provider="codex", model="gpt-test",
    )
    run_ten = ledger.start(
        run_id="run-10", chat_id=chat_id, trigger_id="message-b",
        provider="codex", model="gpt-test",
    )
    terminal = ledger.finish("run-1", "done", "Reply posted")
    forged = replace(
        terminal,
        meta=replace(terminal.meta, actor="other-agent", signer="other-agent"),
    )

    assert RunLedger._fold([run_one, forged]) == [run_one]
    assert ledger.read(chat_id, "run-1") == [run_one, terminal]
    assert run_ten not in ledger.read(chat_id, "run-1")


def test_signed_backdated_and_competing_run_terminals_fail_closed(run_meshes):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-ambiguous", chat_id=chat_id, trigger_id="message-ambiguous",
        provider="codex", model="gpt-test",
    )
    equal_ns = replace(
        started,
        meta=replace(started.meta, id="run-event-equal"),
        state=RunState.COMPLETED, status="Completed", outcome="completed",
    )
    completed = replace(
        started,
        meta=replace(started.meta, id="run-event-completed",
                     ns=started.meta.ns + 1),
        state=RunState.COMPLETED, status="Completed", outcome="completed",
    )
    failed = replace(
        started,
        meta=replace(started.meta, id="run-event-failed",
                     ns=started.meta.ns + 2),
        state=RunState.FAILED, status="Failed", outcome="failed",
    )
    assert RunLedger._fold([equal_ns, started, completed]) == [started, completed]
    assert RunLedger._fold([started, completed, failed]) == [started]

    for record in (completed, failed):
        _target, payload = ledger._payload(record, terminal=True)
        agent.tx.create_doc(payload["path"], payload["doc"])
    assert ledger.read(chat_id, "run-ambiguous") == [started]

    incoherent = replace(completed, outcome="failed")
    assert RunLedger._fold([started, incoherent]) == [started]


def test_rotated_room_key_invalidates_old_run_projection(run_meshes):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-old-key", chat_id=chat_id, trigger_id="message-old-key",
        provider="codex", model="gpt-test",
    )
    assert ledger.read(chat_id, "run-old-key") == [started]

    agent.keys.rotate(chat_id, sorted(agent.snapshot(chat_id).members))

    assert ledger.read(chat_id, "run-old-key") == []


def test_terminal_intent_survives_pre_outbox_failure(run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-terminal-retry", chat_id=chat_id, trigger_id="message-c",
        provider="codex", model="gpt-test",
    )
    original_payload = ledger._payload
    monkeypatch.setattr(ledger, "_payload", lambda _record, **_kwargs: (
        _ for _ in ()).throw(OSError("local failure")))
    with pytest.raises(OSError, match="local failure"):
        ledger.finish("run-terminal-retry", "done", "Reply posted")
    assert ledger.has_terminal_intent("run-terminal-retry")

    monkeypatch.setattr(ledger, "_payload", original_payload)
    assert ledger.retry_terminals() == 1
    assert ledger.read(chat_id, "run-terminal-retry")[-1].state is RunState.COMPLETED


def test_invalid_terminal_does_not_poison_run_recovery_intent(run_meshes):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    ledger.start(
        run_id="run-invalid-terminal", chat_id=chat_id,
        trigger_id="message-invalid", provider="codex", model="gpt-test",
    )

    with pytest.raises(RunLedgerError, match="must be terminal"):
        ledger.finish("run-invalid-terminal", "running", "Still working")
    assert not ledger.has_terminal_intent("run-invalid-terminal")

    finished = ledger.finish("run-invalid-terminal", "done", "Reply posted")
    assert finished.state is RunState.COMPLETED


def test_recovery_terminal_ns_stays_after_start_on_clock_regression(
        run_meshes, monkeypatch):
    _owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-clock", chat_id=chat_id, trigger_id="message-d",
        provider="codex", model="gpt-test",
    )
    monkeypatch.setattr(
        "agentbridge.harness.runtime.runs.next_ns",
        lambda: started.meta.ns - 100,
    )
    finished = ledger.finish("run-clock", "interrupted", "Restarted")
    assert finished.meta.ns == started.meta.ns + 1
    assert ledger.read(chat_id, "run-clock") == [started, finished]


def test_stale_settings_snapshot_cannot_start_a_run(run_meshes):
    owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    current = ledger.mesh.directory.get("helper").agent.harness
    from agentbridge.harness.settings import HarnessSettings

    revision = HarnessSettings.from_account(
        ledger.mesh.directory.get("helper"),
    ).policy_revision
    assert current == {}
    owner.accounts.set_agent_harness("helper", {"model": "changed"})
    with pytest.raises(RunLedgerError, match="policy changed"):
        ledger.start(
            run_id="run-stale-policy", chat_id=chat_id,
            trigger_id="message-e", provider="codex", model="old-model",
            policy_revision=revision,
        )


def test_terminal_preserves_the_policy_that_execution_started_under(run_meshes):
    owner, agent, chat_id = run_meshes
    ledger = RunLedger(agent)
    started = ledger.start(
        run_id="run-policy-attribution", chat_id=chat_id,
        trigger_id="message-policy", provider="codex", model="gpt-test",
    )
    owner.accounts.set_agent_harness("helper", {"web": True})
    finished = ledger.finish("run-policy-attribution", "done", "Reply posted")

    assert finished.meta.policy_revision == started.meta.policy_revision
    assert finished.meta.membership_epoch == started.meta.membership_epoch
    assert finished.meta.ownership_epoch == started.meta.ownership_epoch
    assert ledger.read(chat_id, "run-policy-attribution") == []
