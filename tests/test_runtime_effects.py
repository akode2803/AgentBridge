"""R142 canonical one-use effect claims and outcome records."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agentbridge.harness.runtime.effects import (
    EffectLedger, EffectLedgerError, effect_claim_path,
)
from agentbridge.harness.runtime.models import EffectState
from agentbridge.harness.runtime.permissions import PermissionLane, answer, digest
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger
from agentbridge.mesh.service import Mesh


@pytest.fixture()
def effect_meshes(tmp_path, monkeypatch):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "box", encrypt=True, home=home,
                 store_path=tmp_path / "agent.sqlite")
    chat = owner.create_chat("Effects", members=["helper"])
    owner.outbox.flush_once()
    agent.sync.sync_once([chat.id])

    # Model Supabase's unique (root,path) INSERT while retaining the fast local
    # mesh fixture. FolderTransport deliberately does not claim this capability.
    original = agent.tx.create_doc
    claim_lock = threading.Lock()

    def exclusive(path, data, *, ask_envelope=None, decision_envelope=None):
        if "/runtime/effects/" not in path:
            return original(path, data)
        with claim_lock:
            current = agent.tx.get_doc(path, default=None)
            if current is not None:
                if current == data:
                    return None
                raise FileExistsError(path)
            base = path.rsplit("/", 1)[0]
            if ask_envelope is not None:
                original(f"{base}/grant-ask.json", ask_envelope)
            if decision_envelope is not None:
                original(f"{base}/grant-decision.json", decision_envelope)
            return original(path, data)

    monkeypatch.setattr(agent.tx, "effect_claims_ready", lambda: True)
    monkeypatch.setattr(agent.tx, "create_effect_doc", exclusive)
    try:
        yield owner, agent, chat.id
    finally:
        agent.close()
        owner.close()


def _parent(agent, chat_id):
    runs = RunLedger(agent)
    tasks = TaskLedger(agent, runs)
    return tasks.start_with_run(
        run_id="run-effect", task_id="task-effect", chat_id=chat_id,
        trigger_id="message-effect", provider="codex", model="gpt-test",
        capability_ceiling=("clear_chat",),
    )


def _grant(owner, agent, chat_id, run, task, ledger):
    arguments = {"keep_starred": True}
    argument_digest = digest({
        "schema_version": 1, "capability_id": "clear_chat",
        "arguments": arguments,
    })
    lane = PermissionLane(agent, "helper")
    ask = lane.publish_ask(
        chat_id=chat_id, kind="permission", tool="clear_chat",
        detail="clear its own view", input_digest=argument_digest,
        timeout_s=30, run_id=run.meta.run_id or "", call_id="call-effect",
    )
    answer(owner, chat_id=chat_id, agent="helper", ask_id=ask.id,
           verdict="allow")
    claimed = {}

    def claim(ask_record, ask_envelope, decision, decision_envelope):
        claimed["value"] = ledger.claim(
            ask=ask_record, ask_envelope=ask_envelope,
            decision=decision, decision_envelope=decision_envelope,
            run=run, task=task,
            capability_id="clear_chat", argument_digest=argument_digest,
        )
        return True

    decision = lane.read_decision(ask, claim=claim)
    assert decision and decision["verdict"] == "allow"
    return lane, ask, claimed["value"]


def test_effect_claim_is_global_one_use_and_signed(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    lane, ask, claim = _grant(owner, agent, chat_id, run, task, ledger)

    path = effect_claim_path(chat_id, "run-effect", "call-effect")
    raw = agent.tx.get_doc(path)
    assert "clear_chat" not in str(raw)
    assert ledger.read(chat_id, "run-effect", "call-effect") == [claim.prepared]
    assert claim.prepared.state is EffectState.PREPARED

    with pytest.raises(EffectLedgerError, match="already claimed"):
        lane.read_decision(ask, claim=lambda ask_record, ask_env,
                           decision, decision_env: (
            ledger.claim(
                ask=ask_record, ask_envelope=ask_env,
                decision=decision, decision_envelope=decision_env,
                run=run, task=task,
                capability_id="clear_chat",
                argument_digest=ask_record["input_digest"],
            ), True,
        )[1])


def test_effect_history_requires_retained_owner_grant_evidence(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    _lane, _ask, _claim = _grant(owner, agent, chat_id, run, task, ledger)
    agent.tx.delete_doc(
        f"chats/{chat_id}/runtime/effects/run-effect/"
        "call-effect/grant-ask.json")
    with pytest.raises(EffectLedgerError, match="unavailable"):
        ledger.read(chat_id, "run-effect", "call-effect")


def test_effect_records_known_success(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)

    assert ledger.execute(claim, run, task, lambda: "cleared") == "cleared"
    states = [event.state for event in
              ledger.read(chat_id, "run-effect", "call-effect")]
    assert states == [EffectState.PREPARED, EffectState.EXECUTING,
                      EffectState.COMMITTED]


def test_post_dispatch_failure_is_unknown_without_raw_error(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)
    mutated = []

    def response_lost():
        mutated.append(True)
        raise RuntimeError("private failure")

    with pytest.raises(RuntimeError, match="private failure"):
        ledger.execute(claim, run, task, response_lost)
    assert mutated == [True]
    history = ledger.read(chat_id, "run-effect", "call-effect")
    assert [event.state for event in history] == [
        EffectState.PREPARED, EffectState.EXECUTING, EffectState.UNKNOWN,
    ]
    assert "private failure" not in str([event.to_dict() for event in history])


def test_two_workers_cannot_both_advance_or_execute_one_claim(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)
    calls = 0
    lock = threading.Lock()

    def action():
        nonlocal calls
        with lock:
            calls += 1
        return "done"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda _index: _capture(lambda: ledger.execute(
                claim, run, task, action)),
            range(2),
        ))
    assert calls == 1
    assert sorted(kind for kind, _value in outcomes) == ["error", "result"]
    assert [event.state for event in
            ledger.read(chat_id, "run-effect", "call-effect")] == [
        EffectState.PREPARED, EffectState.EXECUTING, EffectState.COMMITTED,
    ]


def test_executing_effect_recovers_as_unknown_without_retry(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    ledger.RECOVERY_GRACE_S = 0
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)
    ledger._transition(
        claim.prepared, EffectState.EXECUTING,
        cancellation_state="dispatching", receipt_digest=None,
    )
    assert ledger.recover_incomplete() == 1
    unknown = ledger.read(chat_id, "run-effect", "call-effect")[-1]
    assert unknown.state is EffectState.UNKNOWN
    with pytest.raises(EffectLedgerError, match="invalid effect state"):
        ledger._transition(
            unknown, EffectState.COMMITTED,
            cancellation_state="completed", receipt_digest="a" * 64,
        )


def test_claimed_immediate_attempt_is_not_retroactively_revoked(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)
    owner.set_agent_harness("helper", {"max_replies_per_hour": 7})
    assert ledger.execute(claim, run, task, lambda: "done") == "done"
    assert [event.state for event in
            ledger.read(chat_id, "run-effect", "call-effect")] == [
        EffectState.PREPARED, EffectState.EXECUTING, EffectState.COMMITTED,
    ]


def test_recovery_preserves_history_after_policy_drift(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    ledger.RECOVERY_GRACE_S = 0
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)
    ledger._transition(
        claim.prepared, EffectState.EXECUTING,
        cancellation_state="dispatch_committed", receipt_digest=None,
    )
    owner.set_agent_harness("helper", {"max_replies_per_hour": 9})
    assert ledger.recover_incomplete() == 1
    assert [event.state for event in
            ledger.read(chat_id, "run-effect", "call-effect")] == [
        EffectState.PREPARED, EffectState.EXECUTING, EffectState.UNKNOWN,
    ]


def test_folder_transport_and_authority_drift_fail_closed(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home)
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("helper")
    agent = Mesh(root, "helper", "box", encrypt=True, home=home)
    try:
        chat = owner.create_chat("Folder effect", members=["helper"])
        owner.outbox.flush_once()
        agent.sync.sync_once([chat.id])
        run, task = _parent(agent, chat.id)
        ledger = EffectLedger(agent)
        assert ledger.available is False
        with pytest.raises(EffectLedgerError, match="unavailable on this transport"):
            ledger.claim(
                ask={}, ask_envelope={}, decision={}, decision_envelope={},
                run=run, task=task,
                capability_id="clear_chat", argument_digest="a" * 64,
            )

        stale = replace(run, meta=replace(run.meta, policy_revision=99))
        with pytest.raises(EffectLedgerError, match="stale policy_revision"):
            ledger._validate_parent(stale, task, "clear_chat")
    finally:
        agent.close()
        owner.close()


def test_removed_agent_cannot_read_effect_history(effect_meshes):
    owner, agent, chat_id = effect_meshes
    run, task = _parent(agent, chat_id)
    ledger = EffectLedger(agent)
    _lane, _ask, claim = _grant(owner, agent, chat_id, run, task, ledger)
    ledger.execute(claim, run, task, lambda: "done")
    owner.remove_member(chat_id, "helper")
    owner.outbox.flush_once()
    agent.sync.sync_once([chat_id])
    with pytest.raises(EffectLedgerError, match="unavailable"):
        ledger.read(chat_id, "run-effect", "call-effect")


def _capture(fn):
    try:
        return "result", fn()
    except EffectLedgerError as exc:
        return "error", str(exc)
