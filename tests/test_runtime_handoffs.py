"""R127 same-room handoff authority, pairing, and decision tests."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness.runtime.handoffs import (
    HandoffLedger, HandoffLedgerError, handoff_event_path, handoff_prefix,
)
from agentbridge.harness.runtime.models import (
    HandoffState, HandoffType, TaskState,
)
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger, task_event_path
from agentbridge.mesh.service import Mesh
from agentbridge.harness.runner import AgentRunner


@pytest.fixture()
def handoff_meshes(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "owner", "box", encrypt=True, home=home,
                 store_path=tmp_path / "owner.sqlite")
    owner.accounts.create_human("owner", "correct-horse")
    owner.accounts.create_agent("manager")
    owner.accounts.create_agent("specialist")
    manager = Mesh(root, "manager", "box", encrypt=True, home=home,
                   store_path=tmp_path / "manager.sqlite")
    specialist = Mesh(root, "specialist", "box", encrypt=True, home=home,
                      store_path=tmp_path / "specialist.sqlite")
    chat = owner.create_chat(
        "Runtime handoffs", members=["manager", "specialist"],
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


def _ledgers(mesh):
    runs = RunLedger(mesh)
    tasks = TaskLedger(mesh, runs)
    return runs, tasks, HandoffLedger(mesh, tasks)


def _root(manager, chat_id, suffix="1", capabilities=()):
    runs, tasks, handoffs = _ledgers(manager)
    run, task = tasks.start_with_run(
        run_id=f"run-{suffix}", task_id=f"task-{suffix}", chat_id=chat_id,
        trigger_id=f"message-{suffix}", provider="codex", model="gpt-test",
    )
    if capabilities:
        raise AssertionError("fixture capabilities must be set at run creation")
    return runs, tasks, handoffs, run, task


def _offer(handoffs, chat_id, suffix="1", **kwargs):
    return handoffs.offer(
        chat_id=chat_id, run_id=f"run-{suffix}",
        parent_task_id=f"task-{suffix}", destination_agent="specialist",
        objective="Review the proposed answer", reason="Independent review",
        success_criteria=("Return a concise review",), **kwargs,
    )


def test_offer_pairs_encrypted_child_task_and_handoff(handoff_meshes):
    owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]

    task_doc = manager.tx.get_doc(task_event_path(
        chat_id, "run-1", view.task.meta.task_id or "", view.task.meta.id,
    ))
    handoff_doc = manager.tx.get_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", offer.meta.id,
    ))
    assert set(task_doc) == {"meta", "nonce", "ct", "sig"}
    assert set(handoff_doc) == {"meta", "nonce", "ct", "sig"}
    assert "Independent review" not in json.dumps(handoff_doc)
    assert view.task.state is TaskState.OFFERED
    assert view.task.parent_task_id == "task-1"
    assert view.task.assigning_agent == "manager"
    assert view.task.assigned_agent == "specialist"
    assert view.task.return_to_agent == "manager"

    for reader in (owner, manager, specialist):
        _r, _t, ledger = _ledgers(reader)
        assert ledger.read(chat_id, "run-1", offer.meta.call_id or "") == [view]


def test_only_destination_can_accept_and_decision_is_destination_signed(
        handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, manager_handoffs, _run, _task = _root(manager, chat_id)
    offered = _offer(manager_handoffs, chat_id)
    handoff_id = offered.events[0].meta.call_id or ""

    with pytest.raises(HandoffLedgerError, match="destination"):
        manager_handoffs.decide(
            chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
            accept=True,
        )
    _r, _t, specialist_handoffs = _ledgers(specialist)
    accepted = specialist_handoffs.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        accept=True, result="Review accepted",
    )
    assert accepted.state is HandoffState.ACCEPTED
    assert accepted.meta.actor == accepted.meta.signer == "specialist"
    assert manager_handoffs.read(chat_id, "run-1", handoff_id)[0].events == (
        offered.events[0], accepted,
    )


def test_destination_can_decline_and_same_decision_is_idempotent(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    offered = _offer(handoffs, chat_id)
    hid = offered.events[0].meta.call_id or ""
    _r, _t, destination = _ledgers(specialist)
    declined = destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=hid,
        accept=False, result="Unavailable",
    )
    assert destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=hid,
        accept=False, result="Ignored retry text",
    ) == declined
    with pytest.raises(HandoffLedgerError, match="different decision"):
        destination.decide(
            chat_id=chat_id, run_id="run-1", handoff_id=hid, accept=True,
        )


def test_self_non_agent_and_capability_escalation_are_rejected(handoff_meshes):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    common = dict(
        chat_id=chat_id, run_id="run-1", parent_task_id="task-1",
        objective="Review", reason="Review", success_criteria=("Review",),
    )
    with pytest.raises(HandoffLedgerError, match="distinct"):
        handoffs.offer(destination_agent="manager", **common)
    with pytest.raises(HandoffLedgerError, match="active agent"):
        handoffs.offer(destination_agent="owner", **common)
    with pytest.raises(HandoffLedgerError, match="root ceiling"):
        handoffs.offer(
            destination_agent="specialist",
            requested_capabilities=("filesystem.read",), **common,
        )


def test_offline_pair_has_one_durable_intent_and_retries(handoff_meshes,
                                                          monkeypatch):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    original = manager.tx.create_doc

    def offline(path, doc):
        if "/runtime/handoffs/" in path or "/runtime/tasks/" in path:
            raise OSError("offline")
        return original(path, doc)

    monkeypatch.setattr(manager.tx, "create_doc", offline)
    view = _offer(handoffs, chat_id)
    assert view.events[0].meta.call_id in manager.store.cached_doc(
        "runtime/handoff-open",
    )
    assert manager.store.outbox_counts().get("pending") == 1
    with pytest.raises(HandoffLedgerError, match="pending child"):
        _offer(handoffs, chat_id)

    monkeypatch.setattr(manager.tx, "create_doc", original)
    manager.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    manager.outbox.flush_once()
    assert manager.store.cached_doc("runtime/handoff-open") == {}
    assert handoffs.read(
        chat_id, "run-1", view.events[0].meta.call_id or "",
    ) == [view]
    assert len(handoffs.read(chat_id, "run-1")) == 1


def test_forged_source_acceptance_and_conflicting_decisions_fail_closed(
        handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    forged = replace(
        offer,
        meta=replace(offer.meta, id="handoff-event-forged",
                     ns=offer.meta.ns + 1),
        state=HandoffState.ACCEPTED, result="forged",
    )
    manager.tx.create_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", forged.meta.id,
    ), handoffs._sealed(forged))
    assert handoffs.read(chat_id, "run-1", offer.meta.call_id or "") == [view]

    _r, _t, destination = _ledgers(specialist)
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=offer.meta.call_id or "",
        accept=True,
    )
    declined = replace(
        accepted,
        meta=replace(accepted.meta, id="handoff-event-conflict",
                     ns=accepted.meta.ns + 1),
        state=HandoffState.DECLINED, result="conflict",
    )
    specialist.tx.create_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", declined.meta.id,
    ), destination._sealed(declined))
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer,)


def test_missing_or_retargeted_child_pair_hides_handoff(handoff_meshes):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    task_path = task_event_path(
        chat_id, "run-1", view.task.meta.task_id or "", view.task.meta.id,
    )
    raw = manager.tx.get_doc(task_path)
    manager.tx.delete_doc(task_path)
    assert handoffs.read(chat_id, "run-1", offer.meta.call_id or "") == []
    manager.tx.create_doc(task_path, raw)
    assert handoffs.read(chat_id, "run-1", offer.meta.call_id or "") == [view]


def test_expiry_allows_source_timeout_but_rejects_late_acceptance(
        handoff_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id, timeout_s=1)
    offer = view.events[0]
    expiry = int(offer.meta.expires_ns or 0)
    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.time.time_ns", lambda: expiry + 1,
    )
    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.next_ns", lambda: expiry + 1,
    )
    _r, _t, destination = _ledgers(specialist)
    with pytest.raises(HandoffLedgerError, match="expired"):
        destination.decide(
            chat_id=chat_id, run_id="run-1",
            handoff_id=offer.meta.call_id or "", accept=True,
        )
    timed_out = handoffs.timeout(
        chat_id=chat_id, run_id="run-1", handoff_id=offer.meta.call_id or "",
    )
    assert timed_out.state is HandoffState.TIMED_OUT
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer, timed_out)


def test_future_dated_timeout_is_hidden_before_wall_expiry(handoff_meshes):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id, timeout_s=3600)
    offer = view.events[0]
    future = replace(
        offer,
        meta=replace(
            offer.meta, id="handoff-event-future-timeout",
            ns=(offer.meta.expires_ns or offer.meta.ns) + 1,
            expires_ns=None,
        ),
        state=HandoffState.TIMED_OUT, result="future",
    )
    manager.tx.create_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", future.meta.id,
    ), handoffs._sealed(future))
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer,)


def test_destination_decision_is_hidden_after_wall_expiry(
        handoff_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id, timeout_s=3600)
    offer = view.events[0]
    _r, _t, destination = _ledgers(specialist)
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1",
        handoff_id=offer.meta.call_id or "", accept=True,
    )
    repeated = destination.decide(
        chat_id=chat_id, run_id="run-1",
        handoff_id=offer.meta.call_id or "", accept=True,
    )
    assert repeated.meta.id == accepted.meta.id
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer, accepted)

    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.time.time_ns",
        lambda: int(offer.meta.expires_ns or 0) + 1,
    )
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer,)


def test_outbox_offer_requires_one_bound_task_and_handoff(handoff_meshes):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    path = handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", offer.meta.id,
    )
    payload = {
        "phase": "offer", "chat_id": chat_id, "run_id": "run-1",
        "handoff_id": offer.meta.call_id, "record_id": offer.meta.id,
        "docs": [{"path": path, "doc": manager.tx.get_doc(path)}],
    }
    with pytest.raises(ValidationError, match="pair"):
        handoffs._deliver(
            handoff_prefix(chat_id, "run-1", offer.meta.call_id or ""),
            payload,
        )


def test_execution_transfer_type_is_visible_but_not_activated(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id, handoff_type=HandoffType.HANDOFF)
    offer = view.events[0]
    _r, _t, destination = _ledgers(specialist)
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1",
        handoff_id=offer.meta.call_id or "", accept=True,
    )
    assert accepted.handoff_type is HandoffType.HANDOFF
    assert accepted.state is HandoffState.ACCEPTED
    assert view.task.state is TaskState.OFFERED


def test_authorized_agent_tool_runs_paired_child_lifecycle(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id)
    handoff_id = offered.events[0].meta.call_id or ""
    _r, _t, destination = _ledgers(specialist)
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        accept=True,
    )
    authorized = source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        execution_timeout_s=60,
    )
    assert json.loads(authorized.result or "")["after"] == accepted.meta.id

    active = destination.activate(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        manifest={"version": 1, "messages": ["m-1"], "omitted": 0},
    )
    returned = destination.return_result(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        contribution="The proposal is coherent.", prompt_digest="a" * 64,
    )
    consumed = source.consume(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
    )
    view = source.read(chat_id, "run-1", handoff_id)[0]
    assert [event.state for event in view.events] == [
        HandoffState.OFFERED, HandoffState.ACCEPTED,
        HandoffState.AUTHORIZED, HandoffState.ACTIVE,
        HandoffState.RETURNED, HandoffState.CONSUMED,
    ]
    assert json.loads(active.result or "")["after"] == authorized.meta.id
    assert json.loads(returned.result or "")["after"] == active.meta.id
    assert json.loads(returned.result or "")["contribution"] \
        == "The proposal is coherent."
    assert json.loads(consumed.result or "")["after"] == returned.meta.id

    child_docs = []
    prefix = f"chats/{chat_id}/runtime/tasks/run-1/{view.task.meta.task_id}/"
    for path in manager.tx.list_docs(prefix):
        child_docs.append(manager.tx.get_doc(path))
    assert len(child_docs) == 3


def test_causal_predecessors_survive_cross_machine_clock_skew(
        handoff_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id)
    offer = offered.events[0]
    handoff_id = offer.meta.call_id or ""
    _r, _t, destination = _ledgers(specialist)

    # The destination is one second ahead, then both writers appear far
    # behind. Exact predecessor IDs, not comparable wall clocks, define order.
    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.next_ns",
        lambda: offer.meta.ns + 1_000_000_000,
    )
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id, accept=True,
    )
    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.next_ns", lambda: 1,
    )
    authorized = source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        execution_timeout_s=60,
    )
    active = destination.activate(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        manifest={"version": 1},
    )
    returned = destination.return_result(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        contribution="Clock-independent result", prompt_digest="b" * 64,
    )
    consumed = source.consume(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
    )

    assert accepted.meta.ns < authorized.meta.ns < active.meta.ns
    assert active.meta.ns < returned.meta.ns < consumed.meta.ns
    assert source.read(chat_id, "run-1", handoff_id)[0].events[-1].state \
        is HandoffState.CONSUMED


def test_authorization_freezes_in_window_acceptance_after_expiry(
        handoff_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id, timeout_s=3600)
    offer = offered.events[0]
    _r, _t, destination = _ledgers(specialist)
    destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=offer.meta.call_id or "",
        accept=True,
    )
    source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=offer.meta.call_id or "",
        execution_timeout_s=7200,
    )
    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.time.time_ns",
        lambda: int(offer.meta.expires_ns or 0) + 1,
    )
    assert [event.state for event in source.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events] == [
        HandoffState.OFFERED, HandoffState.ACCEPTED, HandoffState.AUTHORIZED,
    ]


def test_forged_authorization_predecessor_fails_closed(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id)
    offer = offered.events[0]
    _r, _t, destination = _ledgers(specialist)
    destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=offer.meta.call_id or "",
        accept=True,
    )
    authorized = source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=offer.meta.call_id or "",
    )
    forged = replace(
        authorized,
        meta=replace(authorized.meta, id="handoff-event-wrong-parent",
                     ns=authorized.meta.ns + 1),
        result=json.dumps({"after": "not-the-acceptance"}),
    )
    manager.tx.create_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", forged.meta.id,
    ), source._sealed(forged))
    assert [event.state for event in source.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events] == [HandoffState.OFFERED, HandoffState.ACCEPTED]


def test_agent_tool_execution_rejects_declared_capabilities(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    runs = RunLedger(manager)
    tasks = TaskLedger(manager, runs)
    source = HandoffLedger(manager, tasks)
    run, _task = tasks.start_with_run(
        run_id="run-cap", task_id="task-cap", chat_id=chat_id,
        trigger_id="message-cap", provider="codex", model="gpt-test",
    )
    # The root contract currently starts with an empty ceiling. Constructing
    # a capable offer is therefore rejected before execution authorization.
    assert run.capability_ceiling == ()
    with pytest.raises(HandoffLedgerError, match="root ceiling"):
        source.offer(
            chat_id=chat_id, run_id="run-cap", parent_task_id="task-cap",
            destination_agent="specialist", objective="Review", reason="Review",
            success_criteria=("Review",),
            requested_capabilities=("filesystem.read",),
        )


def test_root_allows_only_one_child_even_after_return(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id)
    handoff_id = offered.events[0].meta.call_id or ""
    _r, _t, destination = _ledgers(specialist)
    destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id, accept=True,
    )
    source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
    )
    destination.activate(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        manifest={"version": 1},
    )
    destination.return_result(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        contribution="done", prompt_digest="f" * 64,
    )
    with pytest.raises(HandoffLedgerError, match="one allowed child"):
        _offer(source, chat_id)


def test_result_after_execution_deadline_is_rejected(
        handoff_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id)
    handoff_id = offered.events[0].meta.call_id or ""
    _r, _t, destination = _ledgers(specialist)
    destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id, accept=True,
    )
    authorized = source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        execution_timeout_s=60,
    )
    destination.activate(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
        manifest={"version": 1},
    )
    monkeypatch.setattr(
        "agentbridge.harness.runtime.handoffs.time.time_ns",
        lambda: int(authorized.meta.expires_ns or 0) + 1,
    )
    with pytest.raises(HandoffLedgerError, match="execution window"):
        destination.return_result(
            chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
            contribution="late", prompt_digest="f" * 64,
        )


def test_offer_local_intent_and_outbox_commit_atomically(handoff_meshes,
                                                          monkeypatch):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    before = manager.store.outbox_counts()
    monkeypatch.setattr(
        manager.store, "cache_doc_and_outbox_add",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        _offer(handoffs, chat_id)
    assert manager.store.cached_doc("runtime/handoff-open", {}) == {}
    assert manager.store.outbox_counts() == before
    assert handoffs.read(chat_id, "run-1") == []


def test_destination_policy_or_room_key_drift_invalidates_projection(
        handoff_meshes):
    owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    assert handoffs.read(chat_id, "run-1") == [view]

    owner.accounts.set_agent_harness("specialist", {"web": True})
    assert handoffs.read(chat_id, "run-1") == []

    owner.accounts.set_agent_harness("specialist", {})
    # A current-key read never revives records from an older room epoch.
    manager.keys.rotate(chat_id, sorted(manager.snapshot(chat_id).members))
    assert handoffs.read(chat_id, "run-1") == []


def test_removed_destination_hides_offer_from_remaining_members(handoff_meshes):
    owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    _offer(handoffs, chat_id)
    owner.remove_member(chat_id, "specialist")
    owner.outbox.flush_once()
    manager.sync.sync_once([chat_id])
    assert handoffs.read(chat_id, "run-1") == []


def test_immutable_handoff_conflict_is_dead_not_retried(handoff_meshes,
                                                        monkeypatch):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    original = manager.tx.create_doc

    def offline(path, doc):
        if "/runtime/handoffs/" in path or "/runtime/tasks/" in path:
            raise OSError("offline")
        return original(path, doc)

    monkeypatch.setattr(manager.tx, "create_doc", offline)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    monkeypatch.setattr(manager.tx, "create_doc", original)
    path = handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", offer.meta.id,
    )
    manager.tx.put_doc(path, {"conflict": True})
    manager.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    manager.outbox.flush_once()
    counts = manager.store.outbox_counts()
    assert counts.get("dead") == 1
    assert not counts.get("pending")
    assert manager.store.cached_doc("runtime/handoff-open") == {}


def test_destination_cannot_decide_after_parent_terminal(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    runs, tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    tasks.finish_with_run("task-1", "run-1", "done", "Reply posted")
    assert runs.read(chat_id, "run-1")[-1].state.value == "completed"
    _r, _t, destination = _ledgers(specialist)
    with pytest.raises(HandoffLedgerError, match="no longer active"):
        destination.decide(
            chat_id=chat_id, run_id="run-1",
            handoff_id=view.events[0].meta.call_id or "", accept=True,
        )


def test_backdated_signed_decision_after_parent_terminal_fails_closed(
        handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    task_terminal, run_terminal = tasks.finish_with_run(
        "task-1", "run-1", "done", "Reply posted",
    )
    _r, _t, destination = _ledgers(specialist)
    assert max(task_terminal.meta.ns, run_terminal.meta.ns) > offer.meta.ns + 1
    late_ns = offer.meta.ns + 1
    late = replace(
        offer,
        meta=replace(
            offer.meta, id="handoff-event-late", ns=late_ns,
            actor="specialist", signer="specialist",
        ),
        state=HandoffState.ACCEPTED, result="late",
    )
    specialist.tx.create_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", late.meta.id,
    ), destination._sealed(late))
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer,)


def test_ambiguous_parent_terminals_do_not_reopen_handoff(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    runs, tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    task_terminal, run_terminal = tasks.finish_with_run(
        "task-1", "run-1", "done", "Reply posted",
    )
    task_duplicate = replace(
        task_terminal,
        meta=replace(task_terminal.meta, id="task-terminal-duplicate",
                     ns=task_terminal.meta.ns + 2),
    )
    run_duplicate = replace(
        run_terminal,
        meta=replace(run_terminal.meta, id="run-terminal-duplicate",
                     ns=run_terminal.meta.ns + 2),
    )
    for ledger, record in ((tasks, task_duplicate), (runs, run_duplicate)):
        _target, payload = ledger._payload(record, terminal=True)
        manager.tx.create_doc(payload["path"], payload["doc"])
    assert [record.state for record in tasks.read(
        chat_id, "run-1", "task-1",
    )] == [TaskState.ACTIVE]
    _r, _t, destination = _ledgers(specialist)
    with pytest.raises(HandoffLedgerError, match="no longer active"):
        destination.decide(
            chat_id=chat_id, run_id="run-1",
            handoff_id=view.events[0].meta.call_id or "", accept=True,
        )


def test_root_progress_checkpoint_keeps_offer_decidable(handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    tasks.progress("task-1")
    manager.outbox.flush_once()
    assert handoffs.read(chat_id, "run-1") == [view]
    _r, _t, destination = _ledgers(specialist)
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1",
        handoff_id=view.events[0].meta.call_id or "", accept=True,
    )
    assert accepted.state is HandoffState.ACCEPTED


def test_retargeted_child_metadata_and_decision_expiry_fail_closed(
        handoff_meshes):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    original_path = task_event_path(
        chat_id, "run-1", view.task.meta.task_id or "", view.task.meta.id,
    )
    manager.tx.delete_doc(original_path)
    retargeted = replace(
        view.task,
        meta=replace(view.task.meta, run_id="other-run", task_id="other-task",
                     expires_ns=(view.task.meta.expires_ns or 0) + 1),
    )
    manager.tx.create_doc(original_path, handoffs._sealed(retargeted))
    assert handoffs.read(chat_id, "run-1", offer.meta.call_id or "") == []

    manager.tx.put_doc(original_path, handoffs._sealed(view.task))
    _r, _t, destination = _ledgers(specialist)
    wrong_expiry = replace(
        offer,
        meta=replace(
            offer.meta, id="handoff-event-expiry", ns=offer.meta.ns + 1,
            actor="specialist", signer="specialist",
            expires_ns=(offer.meta.expires_ns or 0) + 1,
        ),
        state=HandoffState.ACCEPTED, result="wrong expiry",
    )
    specialist.tx.create_doc(handoff_event_path(
        chat_id, "run-1", offer.meta.call_id or "", wrong_expiry.meta.id,
    ), destination._sealed(wrong_expiry))
    assert handoffs.read(
        chat_id, "run-1", offer.meta.call_id or "",
    )[0].events == (offer,)


@pytest.mark.parametrize("field,value", [
    ("timeout_s", math.nan),
    ("timeout_s", math.inf),
    ("handoff_type", "agent_tool"),
    ("objective", None),
    ("reason", None),
    ("success_criteria", ["not immutable"]),
    ("requested_capabilities", ("",)),
])
def test_malformed_offer_inputs_raise_stable_ledger_error(
        handoff_meshes, field, value):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    kwargs = dict(
        chat_id=chat_id, run_id="run-1", parent_task_id="task-1",
        destination_agent="specialist", objective="Review", reason="Review",
        success_criteria=("Return a review",),
    )
    kwargs[field] = value
    with pytest.raises(HandoffLedgerError):
        handoffs.offer(**kwargs)


def test_partial_remote_pair_converges_on_retry(handoff_meshes, monkeypatch):
    _owner, manager, _specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    original = manager.tx.create_doc
    failed = False

    def fail_second(path, doc):
        nonlocal failed
        if "/runtime/handoffs/" in path and not failed:
            failed = True
            raise OSError("lost after child task")
        return original(path, doc)

    monkeypatch.setattr(manager.tx, "create_doc", fail_second)
    view = _offer(handoffs, chat_id)
    task_path = task_event_path(
        chat_id, "run-1", view.task.meta.task_id or "", view.task.meta.id,
    )
    assert manager.tx.get_doc(task_path) is not None
    assert handoffs.read(chat_id, "run-1") == []

    monkeypatch.setattr(manager.tx, "create_doc", original)
    manager.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    manager.outbox.flush_once()
    assert handoffs.read(chat_id, "run-1") == [view]


def test_offline_destination_decision_retries(handoff_meshes, monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, handoffs, _run, _task = _root(manager, chat_id)
    view = _offer(handoffs, chat_id)
    offer = view.events[0]
    _r, _t, destination = _ledgers(specialist)
    original = specialist.tx.create_doc
    monkeypatch.setattr(
        specialist.tx, "create_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    accepted = destination.decide(
        chat_id=chat_id, run_id="run-1",
        handoff_id=offer.meta.call_id or "", accept=True,
    )
    assert destination.retry_open() == 1
    monkeypatch.setattr(specialist.tx, "create_doc", original)
    specialist.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    specialist.outbox.flush_once()
    assert destination.retry_open() == 0
    assert handoffs.read(chat_id, "run-1")[0].events == (offer, accepted)


def test_offline_authorization_reuses_exact_pending_event(handoff_meshes,
                                                          monkeypatch):
    _owner, manager, specialist, chat_id = handoff_meshes
    _runs, _tasks, source, _run, _task = _root(manager, chat_id)
    offered = _offer(source, chat_id)
    handoff_id = offered.events[0].meta.call_id or ""
    _r, _t, destination = _ledgers(specialist)
    destination.decide(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id, accept=True,
    )
    original = manager.tx.create_doc
    monkeypatch.setattr(
        manager.tx, "create_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    first = source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
    )
    repeated = source.authorize(
        chat_id=chat_id, run_id="run-1", handoff_id=handoff_id,
    )
    assert repeated.meta.id == first.meta.id

    monkeypatch.setattr(manager.tx, "create_doc", original)
    manager.store._conn().execute(
        "UPDATE outbox SET next_ns=0, lease_ns=0 WHERE state='pending'",
    )
    manager.outbox.flush_once()
    view = source.read(chat_id, "run-1", handoff_id)[0]
    assert view.events[-1].state is HandoffState.AUTHORIZED
    assert sum(event.state is HandoffState.AUTHORIZED for event in view.events) == 1


def test_real_runner_registers_handoff_recovery_handler(handoff_meshes):
    _owner, manager, _specialist, _chat_id = handoff_meshes
    runner = AgentRunner(
        manager.tx, "manager", home=manager.home, machine="runner-test",
        poll_s=0.01,
    )
    try:
        assert runner.handoff_ledger.OUTBOX_KIND in runner.mesh.outbox.handlers
        assert runner.mesh.outbox.handlers[
            runner.handoff_ledger.OUTBOX_KIND
        ] == runner.handoff_ledger._deliver
    finally:
        runner.close()
