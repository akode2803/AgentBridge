"""Inactive transport-to-Store recovery executor checks."""

import sqlite3

import pytest

from agentbridge.node.admission import NodeCaptureRequest, NodeGenerationChanged
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.recovery import InactiveRecoveryExecutor
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
            {"root": "team", "path": "presence/user@mac.json", "seq": 3,
             "deleted": False, "data": {"online": True}},
        ],
        "ab_logs": [{
            "root": "team", "id": 7, "chat_id": "c1",
            "log_name": "main", "line": "sealed",
        }],
    })
    tx = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    return tx, client


def run_to_seal(executor, recovery_id=None):
    actions = []
    for attempted in range(10, 40):
        result = executor.step(recovery_id, attempted_ns=attempted)
        recovery_id = result.recovery_id
        actions.append(result.action)
        if result.state == "sealed":
            return result, actions
        executor = InactiveRecoveryExecutor(executor.store, executor.transport)
    raise AssertionError("recovery did not seal within its bounded fixture work")


def test_executor_resumes_one_bounded_step_at_a_time_and_keeps_result_private(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    first = InactiveRecoveryExecutor(owner, tx).step(None, attempted_ns=10)
    assert first.action == "started" and first.state == "building"

    # A new executor instance resumes entirely from Store-owned progress.
    sealed, actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), first.recovery_id)
    assert actions == [
        "manifest:documents", "manifest:chats", "manifest:streams",
        "stream", "events", "sealed",
    ]
    assert owner.capture(NodeCaptureRequest(max_bytes=1024)).generation == 0
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM provider_recoveries WHERE recovery_id=?",
            (sealed.recovery_id,),
        ).fetchone() == ("sealed",)

    owner.admit_candidate(sealed.generation)
    captured = owner.capture(NodeCaptureRequest(
        document_prefixes=("",), include_visibility=True,
        visibility_limit=10, max_documents=10, max_bytes=8192,
    ))
    assert [row.path for row in captured.documents] == [
        "chats/c1/meta.json", "users/a.json"]
    assert captured.visibility == ("c1",)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT id,payload FROM remote_log_rows").fetchall() == [(7, b"sealed")]


def test_executor_replays_change_arriving_during_inventory(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, client = transport()
    executor = InactiveRecoveryExecutor(owner, tx)
    started = executor.step(None, attempted_ns=10)
    executor.step(started.recovery_id, attempted_ns=11)  # documents at cursor 0
    client.db["ab_docs"][0]["seq"] = 4
    client.db["ab_docs"][0]["data"] = {"members": {"user": True}, "name": "new"}
    client.db["ab_change_events"].append({
        "root": "team", "id": 1, "stream_kind": "chat", "stream_id": "c1",
        "domain": "docs", "source_key": "chats/c1/meta.json",
        "doc_head": 4, "log_head": None,
    })
    sealed, actions = run_to_seal(executor, started.recovery_id)
    assert "repair" in actions and actions[-1] == "sealed"
    owner.admit_candidate(sealed.generation)
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=("chats/c1/meta.json",), max_bytes=8192))
    assert captured.documents[0].seq == 4


def test_executor_resumes_after_empty_event_page_then_advanced_close_fence(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, client = transport()
    executor = InactiveRecoveryExecutor(owner, tx)
    started = executor.step(None, attempted_ns=10)
    recovery_id = started.recovery_id
    for attempted in range(11, 30):
        result = executor.step(recovery_id, attempted_ns=attempted)
        if result.action == "events":
            break
    assert result.action == "events"

    original_fence = tx.recovery_fence
    fence_calls = 0

    def advancing_fence():
        nonlocal fence_calls
        fence_calls += 1
        if fence_calls == 2:
            client.db["ab_docs"][0]["seq"] = 4
            client.db["ab_docs"][0]["data"] = {
                "members": {"user": True}, "name": "late",
            }
            client.db["ab_change_events"].append({
                "root": "team", "id": 1, "stream_kind": "chat",
                "stream_id": "c1", "domain": "docs",
                "source_key": "chats/c1/meta.json", "doc_head": 4,
                "log_head": None,
            })
        return original_fence()

    monkeypatch.setattr(tx, "recovery_fence", advancing_fence)
    advanced = executor.step(recovery_id, attempted_ns=30)
    assert advanced.action == "target-advanced"
    sealed, actions = run_to_seal(executor, recovery_id)
    assert actions[:2] == ["events", "repair"]
    owner.admit_candidate(sealed.generation)
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=("chats/c1/meta.json",), max_bytes=8192))
    assert captured.documents[0].seq == 4


def test_executor_resumes_stream_larger_than_one_store_page(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, client = transport()
    client.db["ab_logs"] = [{
        "root": "team", "id": row_id, "chat_id": "c1",
        "log_name": "main", "line": f"row-{row_id}",
    } for row_id in range(1, 258)]
    sealed, actions = run_to_seal(InactiveRecoveryExecutor(owner, tx))
    assert actions.count("stream") == 2
    owner.admit_candidate(sealed.generation)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT count(*),min(id),max(id) FROM remote_log_rows"
        ).fetchone() == (257, 1, 257)


def test_executor_missing_exact_repair_abandons_private_generation(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, client = transport()
    executor = InactiveRecoveryExecutor(owner, tx)
    started = executor.step(None, attempted_ns=10)
    client.db["ab_change_events"].append({
        "root": "team", "id": 1, "stream_kind": "chat", "stream_id": "c1",
        "domain": "docs", "source_key": "chats/c1/missing.json",
        "doc_head": 1, "log_head": None,
    })
    recovery_id = started.recovery_id
    for attempted in range(11, 30):
        result = executor.step(recovery_id, attempted_ns=attempted)
        if result.action == "events":
            break
    with pytest.raises(NodeGenerationChanged, match="unavailable"):
        executor.step(recovery_id, attempted_ns=30)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (started.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries").fetchone() == (0,)
