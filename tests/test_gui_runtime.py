"""GUI projection tests for canonical, room-private runtime handoffs."""

from __future__ import annotations

import urllib.error

import pytest

from agentbridge.harness.runtime.handoffs import HandoffLedgerError
from agentbridge.harness.runtime.runs import RunLedgerError

from agentbridge.harness.runtime.handoffs import (
    HandoffLedger,
    handoff_prefix,
)
from agentbridge.harness.runtime.runs import RunLedger
from agentbridge.harness.runtime.tasks import TaskLedger
from agentbridge.harness.adapters.native import codex_native_policy
from agentbridge.gui.api_runtime import authority_rows, contributor_rows
from agentbridge.mesh.service import Mesh


def _ledgers(mesh):
    runs = RunLedger(mesh)
    tasks = TaskLedger(mesh, runs)
    return runs, tasks, HandoffLedger(mesh, tasks)


def test_latency_diagnostics_are_bounded_local_metadata(rig):
    rig.signup()
    out = rig.get("/api/mesh/latency", limit=5)
    assert out["clock_scope"] == "per-process"
    assert len(out["runner"]) <= 5 and len(out["gui"]) <= 5
    assert set(out["stats"]) == {"runner", "gui", "transport"}
    assert isinstance(out["agents"], dict)
    assert "chat_id" not in str(out["runner"])


def test_runtime_aux_requires_auth_and_current_room_membership(rig):
    assert rig.get("/api/mesh/chat_aux", id="missing")["error"] == "Sign in first"
    rig.signup()
    rig.peer_account("fable")
    with rig.peer_mesh("fable") as fable:
        private = fable.create_chat("Private", members=[])
    out = rig.aux(private.id)
    assert out["status"] == "forbidden"
    assert "tasks" not in out and "runs" not in out


def test_runtime_http_wrappers_are_retired(rig):
    for route in ("/api/mesh/runtime_tasks", "/api/mesh/runtime_authority",
                  "/api/mesh/chat", "/api/mesh/chat_info", "/api/mesh/livefeed"):
        with pytest.raises(urllib.error.HTTPError) as error:
            rig.get(route, id="missing")
        assert error.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as error:
        rig.post("/api/mesh/runtime_authority", chat_id="missing", run_ids=["r-1-abcdef12"])
    assert error.value.code == 404


def _tasks(rig, chat):
    rig.prepare(chat)
    return contributor_rows(rig.app.mesh, chat)


def _authority(rig, chat, **params):
    rig.prepare(chat)
    return authority_rows(rig.app.mesh, chat, **params)


def test_runtime_authority_projects_only_signed_nonsecret_facts(
        rig, monkeypatch):
    rig.signup()
    rig.app.mesh.accounts.create_agent("manager")
    chat_id = rig.post(
        "/api/mesh/create_chat", name="Authority projection",
        members=["manager"],
    )["chat"]["id"]
    manager = Mesh(
        rig.clouds.bare(rig.root), "manager", "guibox", encrypt=True, home=rig.home,
        store_path=rig.home / "manager-authority.sqlite",
    )
    try:
        manager.sync.sync_once([chat_id])
        policy = codex_native_policy(bridge_attached=True)
        ledger = RunLedger(manager)
        ledger.start(
            run_id="r-1-abcdef12", chat_id=chat_id,
            trigger_id="message-authority", provider="codex", model="gpt-test",
            capability_ceiling=("delegate_agent",),
            native_policy_digest=policy.authority_digest("codex-cli 0.147.0"),
            provider_policy_digest="b" * 64,
            native_provider_version="codex-cli 0.147.0",
            native_enabled=policy.enabled,
            native_approval_gated=policy.approval_gated,
            native_blocked=policy.blocked,
        )
        # A correctly signed manager record with internally inconsistent
        # effective facts is still not a reportable authority attestation.
        RunLedger(manager).start(
            run_id="run-forged-authority", chat_id=chat_id,
            trigger_id="message-forged", provider="codex", model="gpt-test",
            native_policy_digest=policy.authority_digest("codex-cli 0.147.0"),
            provider_policy_digest="b" * 64,
            native_provider_version="codex-cli 0.147.0",
            native_enabled=policy.enabled,
            native_approval_gated=policy.approval_gated,
            native_blocked=policy.blocked[:-1],
        )
        RunLedger(manager).start(
            run_id="run-old", chat_id=chat_id,
            trigger_id="message-old", provider="codex", model="gpt-test",
        )
        RunLedger(manager).start(
            run_id="run-claude", chat_id=chat_id,
            trigger_id="message-claude", provider="claude", model="gpt-test",
        )
        handlers = dict(rig.app.mesh.outbox.handlers)
        dead_hooks = dict(rig.app.mesh.outbox.dead_hooks)
        rows = _authority(rig, chat_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == "r-1-abcdef12"
        assert row["provider_version"] == "codex-cli 0.147.0"
        assert row["authority_digest"] == "b" * 64
        assert row["approval_gated"] == ["codex.agentbridge_mcp"]
        assert {tuple(item) for item in row["capabilities"]} == {
            ("id", "label", "state", "surface", "effect", "risk"),
        }
        capability = next(
            item for item in row["capabilities"]
            if item["id"] == "codex.agentbridge_mcp"
        )
        assert capability == {
            "id": "codex.agentbridge_mcp",
            "label": "Use AgentBridge tools",
            "state": "approval_gated",
            "surface": "provider-tool-transport",
            "effect": "agentbridge-broker-tools",
            "risk": "high",
        }
        encoded = str(row).lower()
        assert "/users/" not in encoded and "token" not in encoded
        assert "launch_args" not in row and "workspace" not in row
        assert "executable" not in row and "environment" not in row
        assert all(
            "controls" not in item and "evidence" not in item
            for item in row["capabilities"]
        )
        assert rig.app.mesh.outbox.handlers == handlers
        assert rig.app.mesh.outbox.dead_hooks == dead_hooks

        current = _authority(rig, chat_id, run_ids=("r-1-abcdef12",), current_only=True)
        assert [(item["run_id"], item["manager"], item["state"])
                for item in current] == [
            ("r-1-abcdef12", "manager", "running"),
        ]
        # Captured active projection reads only the requested mirror prefix.
        cached = rig.clouds.cached(rig.root, auto_refresh=False)
        cached.refresh()
        viewer = Mesh(
            cached, "aryan", "guibox", encrypt=True, home=rig.home,
            store_path=rig.home / "authority-cache-viewer.sqlite",
        )
        try:
            monkeypatch.setattr(
                cached.inner, "list_docs",
                lambda _prefix: (_ for _ in ()).throw(
                    AssertionError("authority GUI projection read through"),
                ),
            )
            monkeypatch.setattr(
                cached.inner, "get_doc",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("authority GUI document read through"),
                ),
            )
            assert [item["run_id"] for item in authority_rows(
                viewer, chat_id, run_ids=("r-1-abcdef12",),
                current_only=True,
            )] == ["r-1-abcdef12"]
        finally:
            viewer.close()

        ledger.finish("r-1-abcdef12", "done", "Reply posted")
        assert _authority(rig, chat_id, run_ids=("r-1-abcdef12",), current_only=True) == []
    finally:
        manager.close()


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
    with pytest.raises(HandoffLedgerError, match="bounded GUI projection"):
        contributor_rows(rig.app.mesh, chat_id)
    with pytest.raises(RunLedgerError, match="bounded GUI projection"):
        authority_rows(rig.app.mesh, chat_id)


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
        rig.clouds.bare(rig.root), "manager", "guibox", encrypt=True, home=rig.home,
        store_path=rig.home / "manager-runtime.sqlite",
    )
    specialist = Mesh(
        rig.clouds.bare(rig.root), "specialist", "guibox", encrypt=True, home=rig.home,
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
            rows = _tasks(rig, chat_id)
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
        assert len(_tasks(rig, chat_id)) == 2
        rig.post("/api/mesh/clear_chat", chat_id=chat_id)
        assert _tasks(rig, chat_id) == []
        specialist_handoffs.return_result(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
            contribution="late secret", prompt_digest="sha256:late",
        )
        manager_handoffs.consume(
            chat_id=chat_id, run_id="run-clear", handoff_id=second_id,
        )
        assert _tasks(rig, chat_id) == []

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
        visible = _tasks(rig, chat_id)
        assert [(row["id"], row["state"]) for row in visible] == [
            (declined_id, "declined"),
        ]

        # An unrelated malformed wire document is not projected beside the
        # canonical fold, and a caller cannot request an unbounded response.
        manager.tx.put_doc(
            f"{handoff_prefix(chat_id, 'run-forged', 'handoff-9999999999999999999-forged')}/x.json",
            {"plaintext": "secret forged body"},
        )
        rows = _tasks(rig, chat_id)
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
        assert contributor_rows(rig.app.mesh, chat_id) == rows
        assert rig.app.mesh.outbox.handlers == handlers
        assert rig.app.mesh.outbox.dead_hooks == dead_hooks
    finally:
        specialist.close()
        manager.close()
