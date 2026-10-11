"""Disabled-product composition for local-node recovery evidence."""

import json
import sqlite3

from agentbridge.node.diagnostic_runtime import (
    LOCAL_NODE_DIAGNOSTIC_ENV, LocalNodeDiagnosticRuntime,
    diagnostic_runtime_enabled,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.store import NodeStore
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
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT admitted_generation FROM node_meta WHERE singleton=1"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT state,count(*) FROM node_generations GROUP BY state"
        ).fetchall() == [("sealed", 2)]


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
            "SELECT generation FROM provider_recoveries WHERE "
            "health_owner_token=0"
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
