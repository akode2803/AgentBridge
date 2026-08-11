"""GUI projection tests for canonical, room-private runtime handoffs."""

from __future__ import annotations

from agentbridge.harness.runtime.handoffs import (
    HandoffLedger,
    handoff_prefix,
)
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger
from agentbridge.mesh.service import Mesh


def _ledgers(mesh):
    runs = RunLedger(mesh)
    tasks = TaskLedger(mesh, runs)
    return runs, tasks, HandoffLedger(mesh, tasks)


def test_runtime_tasks_requires_auth_and_current_room_membership(rig):
    assert rig.get("/api/mesh/runtime_tasks", id="missing")["error"] == (
        "Sign in first"
    )
    rig.signup()
    rig.peer_account("fable")
    with rig.peer_mesh("fable") as fable:
        private = fable.create_chat("Private", members=[])
    out = rig.get("/api/mesh/runtime_tasks", id=private.id)
    assert "error" in out


def test_runtime_tasks_fail_closed_when_room_snapshot_exceeds_budget(
        rig, monkeypatch):
    rig.signup()
    chat_id = rig.post(
        "/api/mesh/create_chat", name="Budget", members=[],
    )["chat"]["id"]
    monkeypatch.setattr(
        rig.app.mesh.tx, "cached_docs_bounded",
        lambda _prefix, _limit: (_ for _ in ()).throw(OverflowError("full")),
    )
    out = rig.get("/api/mesh/runtime_tasks", id=chat_id)
    assert "bounded GUI projection" in out["error"]


def test_runtime_tasks_projects_only_canonical_minimized_lifecycle(
        rig, monkeypatch):
    rig.signup()
    rig.app.mesh.accounts.create_agent("manager")
    rig.app.mesh.accounts.create_agent("specialist")
    chat_id = rig.post(
        "/api/mesh/create_chat",
        name="Runtime projection",
        members=["manager", "specialist"],
    )["chat"]["id"]

    manager = Mesh(
        rig.root, "manager", "guibox", encrypt=True, home=rig.home,
        store_path=rig.home / "manager-runtime.sqlite",
    )
    specialist = Mesh(
        rig.root, "specialist", "guibox", encrypt=True, home=rig.home,
        store_path=rig.home / "specialist-runtime.sqlite",
    )
    try:
        manager.sync.sync_once([chat_id])
        specialist.sync.sync_once([chat_id])
        _runs, manager_tasks, manager_handoffs = _ledgers(manager)
        manager_tasks.start_with_run(
            run_id="run-gui", task_id="task-gui", chat_id=chat_id,
            trigger_id="message-gui", provider="codex", model="gpt-test",
        )
        offered = manager_handoffs.offer(
            chat_id=chat_id, run_id="run-gui", parent_task_id="task-gui",
            destination_agent="specialist", objective="secret objective",
            reason="secret reason", success_criteria=("secret criterion",),
        )
        handoff_id = offered.events[0].meta.call_id or ""
        expected_keys = {
            "id", "run_id", "manager", "contributor", "kind", "state",
            "started_ns", "updated_ns",
        }

        def projected_state():
            rows = rig.get("/api/mesh/runtime_tasks", id=chat_id)["tasks"]
            assert len(rows) == 1 and set(rows[0]) == expected_keys
            assert "secret" not in str(rows).lower()
            assert rows[0]["manager"] == "manager"
            assert rows[0]["contributor"] == "specialist"
            return rows[0]["state"]

        assert projected_state() == "offered"
        _sr, _st, specialist_handoffs = _ledgers(specialist)
        specialist_handoffs.decide(
            chat_id=chat_id, run_id="run-gui", handoff_id=handoff_id,
            accept=True, result="secret acceptance",
        )
        assert projected_state() == "accepted"
        manager_handoffs.authorize(
            chat_id=chat_id, run_id="run-gui", handoff_id=handoff_id,
        )
        assert projected_state() == "authorized"
        specialist_handoffs.activate(
            chat_id=chat_id, run_id="run-gui", handoff_id=handoff_id,
            manifest={"secret_manifest": "must not project"},
        )
        assert projected_state() == "active"
        specialist_handoffs.return_result(
            chat_id=chat_id, run_id="run-gui", handoff_id=handoff_id,
            contribution="secret contribution", prompt_digest="sha256:test",
        )
        assert projected_state() == "returned"
        manager_handoffs.consume(
            chat_id=chat_id, run_id="run-gui", handoff_id=handoff_id,
        )
        assert projected_state() == "consumed"
        manager_tasks.finish_with_run(
            "task-gui", "run-gui", "done", "Completed",
        )
        # Historical visibility must survive closure of the parent root.
        assert projected_state() == "consumed"

        manager_tasks.start_with_run(
            run_id="run-clear", task_id="task-clear", chat_id=chat_id,
            trigger_id="message-clear", provider="codex", model="gpt-test",
        )
        second = manager_handoffs.offer(
            chat_id=chat_id, run_id="run-clear", parent_task_id="task-clear",
            destination_agent="specialist", objective="clear me",
            reason="clear test", success_criteria=("stay hidden",),
        )
        second_id = second.events[0].meta.call_id or ""
        specialist_handoffs.decide(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
            accept=True,
        )
        manager_handoffs.authorize(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
        )
        specialist_handoffs.activate(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
            manifest={"messages": []},
        )
        assert len(rig.get("/api/mesh/runtime_tasks", id=chat_id)["tasks"]) == 2
        rig.post("/api/mesh/clear_chat", chat_id=chat_id)
        assert rig.get("/api/mesh/runtime_tasks", id=chat_id)["tasks"] == []
        specialist_handoffs.return_result(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
            contribution="late secret", prompt_digest="sha256:late",
        )
        manager_handoffs.consume(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
        )
        assert rig.get("/api/mesh/runtime_tasks", id=chat_id)["tasks"] == []

        # Clear stores exact row ids, not a wall-clock cutoff: future rows
        # remain visible, and a declined terminal survives parent completion.
        manager_tasks.start_with_run(
            run_id="run-decline", task_id="task-decline", chat_id=chat_id,
            trigger_id="message-decline", provider="codex", model="gpt-test",
        )
        declined = manager_handoffs.offer(
            chat_id=chat_id, run_id="run-decline",
            parent_task_id="task-decline", destination_agent="specialist",
            objective="decline me", reason="decline test",
            success_criteria=("decline",),
        )
        declined_id = declined.events[0].meta.call_id or ""
        specialist_handoffs.decide(
            chat_id=chat_id, run_id="run-decline", handoff_id=declined_id,
            accept=False,
        )
        manager_tasks.finish_with_run(
            "task-decline", "run-decline", "done", "Completed",
        )
        visible = rig.get("/api/mesh/runtime_tasks", id=chat_id)["tasks"]
        assert [(row["id"], row["state"]) for row in visible] == [
            (declined_id, "declined"),
        ]

        # An unrelated malformed wire document is not projected beside the
        # canonical fold, and a caller cannot request an unbounded response.
        manager.tx.put_doc(
            f"{handoff_prefix(chat_id, 'run-forged', 'handoff-9999999999999999999-forged')}/x.json",
            {"plaintext": "secret forged body"},
        )
        rows = rig.get(
            "/api/mesh/runtime_tasks", id=chat_id, limit=9999,
        )["tasks"]
        assert [(row["id"], row["state"]) for row in rows] == [
            (declined_id, "declined"),
        ]

        # GUI polling is an observational local-mirror read. It must neither
        # query the cloud driver nor replace this process's outbox handlers.
        handlers = dict(rig.app.mesh.outbox.handlers)
        dead_hooks = dict(rig.app.mesh.outbox.dead_hooks)
        inner = getattr(rig.app.mesh.tx, "inner", None)
        if inner is not None:
            monkeypatch.setattr(
                inner, "list_docs",
                lambda _prefix: (_ for _ in ()).throw(
                    AssertionError("runtime GUI projection read through"),
                ),
            )
            monkeypatch.setattr(
                inner, "get_doc",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("runtime GUI document read through"),
                ),
            )
        assert rig.get("/api/mesh/runtime_tasks", id=chat_id)["tasks"] == rows
        assert rig.app.mesh.outbox.handlers == handlers
        assert rig.app.mesh.outbox.dead_hooks == dead_hooks
    finally:
        specialist.close()
        manager.close()
