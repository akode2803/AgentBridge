"""Disabled-product composition for local-node recovery evidence."""

import json
import sqlite3

import pytest

from agentbridge.node.diagnostic_runtime import (
    LOCAL_NODE_DIAGNOSTIC_ENV, LocalNodeDiagnosticRuntime,
    diagnostic_runtime_enabled,
)
from agentbridge.node.admission import (
    NodeCandidateCapacityError, NodeGenerationChanged, NodeInputBatch,
    NodeInputError,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.recovery import InactiveRecoveryExecutor
from agentbridge.node.reference import InactiveReferenceExecutor
from agentbridge.node.store import (
    DIAGNOSTIC_REFERENCE_PLAN_SELECTION, NodeStore,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

EPOCH = "12345678-1234-5678-9234-567812345678"


def identity():
    return ReplicaIdentity("https://x.test", "supabase://team", "user", "mac")


def transport():
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_scoped_source_version": 1,
        "_reference_version": 1,
        "ab_change_epochs": [{
            "root": "team", "epoch": EPOCH, "minimum_cursor": 0,
            "source_schema_version": 2,
        }],
        "ab_change_events": [],
        "ab_docs": [
            {"root": "team", "path": "chats/c1/meta.json", "seq": 1,
             "deleted": False, "data": {"members": {"user": True}}},
            {"root": "team", "path": "users/a.json", "seq": 2,
             "deleted": False, "data": {"name": "a"}},
        ],
        "ab_logs": [{
            "root": "team", "id": 7, "chat_id": "c1",
            "log_name": "main", "line": "sealed",
        }],
        "ab_log_stream_heads": [{
            "root": "team", "chat_id": "c1", "log_name": "main", "head": 7,
        }],
    })
    source = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"}, client=client)
    return source, client


def runtime(path, evidence, source, *, enabled=True):
    return LocalNodeDiagnosticRuntime(
        NodeStore(path, identity()), source, evidence, enabled=enabled)


def test_feature_gate_is_exact_and_disabled_step_touches_no_provider(tmp_path, monkeypatch):
    source, _client = transport()
    monkeypatch.setattr(
        source, "recovery_fence",
        lambda: (_ for _ in ()).throw(AssertionError("provider was touched")),
    )
    assert not diagnostic_runtime_enabled({})
    assert not diagnostic_runtime_enabled({LOCAL_NODE_DIAGNOSTIC_ENV: "true"})
    assert diagnostic_runtime_enabled({LOCAL_NODE_DIAGNOSTIC_ENV: "1"})
    result = runtime(
        tmp_path / "node.sqlite3", tmp_path / "evidence", source,
        enabled=False,
    ).step(attempted_ns=1)
    assert (result.state, result.phase, result.action) == (
        "disabled", "disabled", "disabled")


def test_runtime_resumes_each_bounded_step_and_records_private_equivalence(
    tmp_path, monkeypatch,
):
    path = tmp_path / "node.sqlite3"
    evidence = tmp_path / "evidence"
    source, _client = transport()
    initial_status = NodeStore(path, identity()).status(
        node_epoch="node", started_ns=1)
    data_calls = 0
    for name in (
        "recovery_page", "source_documents", "source_log_page",
        "reference_documents", "reference_streams", "reference_log_page",
    ):
        original = getattr(source, name)

        def counted(*args, _original=original, **kwargs):
            nonlocal data_calls
            data_calls += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(source, name, counted)
    actions = []
    completed = None
    for attempted in range(10, 40):
        # Reconstruct every time: no process-local continuation is required.
        before = data_calls
        result = runtime(path, evidence, source).step(attempted_ns=attempted)
        assert data_calls - before <= 1
        actions.append((result.phase, result.action))
        if result.state == "completed":
            completed = result
            break
    assert completed is not None
    assert completed.outcome == "equal"
    assert ("recovery", "manifest:documents") in actions
    assert ("reference", "manifest:documents") in actions
    payload = json.loads(open(completed.evidence_path, encoding="utf-8").read())
    assert payload["version"] == 2
    assert payload["outcome"] == "equal"
    assert payload["candidate_counts"] == payload["reference_counts"] == [2, 1, 1, 2]
    final_status = NodeStore(path, identity()).status(
        node_epoch="node", started_ns=1)
    assert (
        final_status.health,
        final_status.last_attempt_ns,
        final_status.last_success_ns,
    ) == (
        initial_status.health,
        initial_status.last_attempt_ns,
        initial_status.last_success_ns,
    )
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT admitted_generation FROM node_meta WHERE singleton=1"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT state,count(*) FROM node_generations GROUP BY state"
        ).fetchall() == [("abandoned", 2)]
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries"
        ).fetchone() == (0,)


def test_diagnostic_recovery_is_health_neutral_and_kind_isolated(tmp_path):
    path = tmp_path / "node.sqlite3"
    source, _client = transport()
    store = NodeStore(path, identity())
    initial = store.status(node_epoch="node", started_ns=1)
    started = InactiveRecoveryExecutor(
        store, source, diagnostic=True,
    ).step(None, attempted_ns=10)
    after = store.status(node_epoch="node", started_ns=1)
    assert (after.health, after.last_attempt_ns, after.last_success_ns) == (
        initial.health, initial.last_attempt_ns, initial.last_success_ns,
    )
    with pytest.raises(NodeInputError, match="different run kind"):
        InactiveRecoveryExecutor(store, source).step(
            started.recovery_id, attempted_ns=11)


def test_diagnostic_exact_repair_restart_abandons_without_health_token(tmp_path):
    path = tmp_path / "node.sqlite3"
    source, client = transport()
    store = NodeStore(path, identity())
    initial = store.status(node_epoch="node", started_ns=1)
    executor = InactiveRecoveryExecutor(store, source, diagnostic=True)
    started = executor.step(None, attempted_ns=10)
    client.db["ab_change_events"].append({
        "root": "team", "id": 1, "stream_kind": "chat", "stream_id": "c1",
        "domain": "docs", "source_key": "chats/c1/missing.json",
        "doc_head": 1, "log_head": None,
    })
    for attempted in range(11, 30):
        result = executor.step(started.recovery_id, attempted_ns=attempted)
        if result.action == "events":
            break
    else:
        raise AssertionError("diagnostic recovery did not reach event replay")
    with pytest.raises(NodeGenerationChanged, match="unavailable"):
        executor.step(started.recovery_id, attempted_ns=30)
    after = store.status(node_epoch="node", started_ns=1)
    assert (after.health, after.last_attempt_ns, after.last_success_ns) == (
        initial.health, initial.last_attempt_ns, initial.last_success_ns,
    )
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (started.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries"
        ).fetchone() == (0,)


def test_diagnostic_requires_reference_capability_before_candidate(tmp_path):
    path = tmp_path / "node.sqlite3"
    source, client = transport()
    client.db["_reference_version"] = None
    source._reference_source_ready = None
    with pytest.raises(NotImplementedError, match="diagnostic sources"):
        runtime(path, tmp_path / "evidence", source).step(attempted_ns=10)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_generations"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries"
        ).fetchone() == (0,)


def test_diagnostic_start_defers_when_pair_capacity_is_unavailable(tmp_path):
    path = tmp_path / "node.sqlite3"
    source, _client = transport()
    store = NodeStore(path, identity())
    for created in (1, 2, 3):
        store.begin_candidate(created_ns=created)
    result = LocalNodeDiagnosticRuntime(
        store, source, tmp_path / "evidence", enabled=True,
    ).step(attempted_ns=10)
    assert (result.state, result.phase, result.action) == (
        "running", "recovery", "deferred")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_generations WHERE state IN "
            "('building','sealed')"
        ).fetchone() == (3,)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries"
        ).fetchone() == (0,)


def test_reference_capacity_race_cleans_up_diagnostic_recovery(
    tmp_path, monkeypatch,
):
    path = tmp_path / "node.sqlite3"
    evidence = tmp_path / "evidence"
    source, _client = transport()
    original = NodeStore.begin_recovery

    def fail_reference(self, plan, **kwargs):
        if plan.work[0].selection == DIAGNOSTIC_REFERENCE_PLAN_SELECTION:
            raise NodeCandidateCapacityError(
                "too many active node candidates")
        return original(self, plan, **kwargs)

    monkeypatch.setattr(NodeStore, "begin_recovery", fail_reference)
    for attempted in range(10, 40):
        result = runtime(path, evidence, source).step(attempted_ns=attempted)
        if result.phase == "reference":
            break
    else:
        raise AssertionError("recovery did not reach the reference phase")
    assert (result.state, result.action) == ("running", "restarted")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state,count(*) FROM node_generations GROUP BY state"
        ).fetchall() == [("abandoned", 1)]
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries"
        ).fetchone() == (0,)


def test_capacity_retry_reclaims_reference_orphaned_by_stale_recovery(tmp_path):
    path = tmp_path / "node.sqlite3"
    evidence = tmp_path / "evidence"
    source, _client = transport()
    for attempted in range(10, 40):
        result = runtime(path, evidence, source).step(attempted_ns=attempted)
        if result.phase == "reference":
            break
    else:
        raise AssertionError("recovery did not reach the reference phase")

    store = NodeStore(path, identity())
    admitted = store.begin_candidate(created_ns=50)
    store.seal_candidate(admitted, NodeInputBatch(), observed_ns=51)
    store.admit_candidate(admitted)
    store.begin_candidate(created_ns=52)
    store.begin_candidate(created_ns=53)

    reclaimed = runtime(path, evidence, source).step(attempted_ns=60)
    assert (reclaimed.state, reclaimed.phase, reclaimed.action) == (
        "running", "recovery", "restarted")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_generations WHERE state IN "
            "('building','sealed')"
        ).fetchone() == (2,)

    restarted = runtime(path, evidence, source).step(attempted_ns=61)
    assert (restarted.phase, restarted.action) == ("recovery", "started")


def test_diagnostic_capacity_cleanup_preserves_ordinary_reference(tmp_path):
    path = tmp_path / "node.sqlite3"
    source, _client = transport()
    store = NodeStore(path, identity())
    InactiveRecoveryExecutor(store, source).step(None, attempted_ns=10)
    reference = InactiveReferenceExecutor(store, source).step(
        None, attempted_ns=11)
    store.begin_candidate(created_ns=12)

    deferred = LocalNodeDiagnosticRuntime(
        store, source, tmp_path / "evidence", enabled=True,
    ).step(attempted_ns=13)
    assert (deferred.phase, deferred.action) == ("recovery", "deferred")
    resumed = InactiveReferenceExecutor(store, source).step(
        reference.reference_id, attempted_ns=14)
    assert resumed.reference_id == reference.reference_id
    assert resumed.action == "manifest:documents"


def test_provider_movement_restarts_both_private_runs_without_admission(tmp_path):
    path = tmp_path / "node.sqlite3"
    evidence = tmp_path / "evidence"
    source, client = transport()
    reference_seen = False
    for attempted in range(10, 40):
        result = runtime(path, evidence, source).step(attempted_ns=attempted)
        if result.phase == "reference":
            reference_seen = True
            break
    assert reference_seen
    client.db["ab_change_events"].append({
        "root": "team", "id": 1, "stream_kind": "root", "stream_id": "",
        "domain": "docs", "source_key": "users/a.json",
        "doc_head": 2, "log_head": None,
    })
    restarted = runtime(path, evidence, source).step(attempted_ns=50)
    assert (restarted.state, restarted.phase, restarted.action) == (
        "running", "reference", "restarted")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT admitted_generation FROM node_meta WHERE singleton=1"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT state,count(*) FROM node_generations GROUP BY state"
        ).fetchall() == [("abandoned", 2)]
    assert not evidence.exists()


def test_reference_started_at_newer_cut_is_rejected_before_its_first_data_page(
    tmp_path, monkeypatch,
):
    path = tmp_path / "node.sqlite3"
    evidence = tmp_path / "evidence"
    source, client = transport()
    reference_reads = 0
    original = source.reference_documents

    def counted(*args, **kwargs):
        nonlocal reference_reads
        reference_reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(source, "reference_documents", counted)
    for attempted in range(10, 40):
        result = runtime(path, evidence, source).step(attempted_ns=attempted)
        if result.phase == "reference":
            break
    else:
        raise AssertionError("recovery did not reach the reference phase")
    # Recreate the exact between-phases race by discarding the just-started
    # reference while retaining the sealed recovery, then advance the source.
    with sqlite3.connect(path) as conn:
        reference_generation = conn.execute(
            "SELECT r.generation FROM provider_recoveries r "
            "JOIN recovery_work w ON w.recovery_id=r.recovery_id "
            "WHERE w.selection=?",
            (DIAGNOSTIC_REFERENCE_PLAN_SELECTION,),
        ).fetchone()[0]
    NodeStore(path, identity()).abandon_candidate(reference_generation)
    client.db["ab_change_events"].append({
        "root": "team", "id": 1, "stream_kind": "root", "stream_id": "",
        "domain": "docs", "source_key": "users/a.json",
        "doc_head": 2, "log_head": None,
    })

    restarted = runtime(path, evidence, source).step(attempted_ns=50)
    assert (restarted.phase, restarted.action) == ("reference", "restarted")
    assert reference_reads == 0
