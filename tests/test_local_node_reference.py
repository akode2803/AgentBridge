"""Resumable, private current-path reference collection."""

import sqlite3
import stat

import pytest

from agentbridge.node.admission import NodeGenerationChanged, NodeInputError
from agentbridge.node.equivalence import RecoveryEquivalenceRecorder
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.recovery import InactiveRecoveryExecutor, _provider_cut
from agentbridge.node.reference import InactiveReferenceExecutor
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
            {"root": "team", "path": "presence/user@mac.json", "seq": 3,
             "deleted": False, "data": {"online": True}},
        ],
        "ab_logs": [{
            "root": "team", "id": 7, "chat_id": "c1",
            "log_name": "main", "line": "sealed",
        }],
        "ab_log_stream_heads": [{
            "root": "team", "chat_id": "c1", "log_name": "main", "head": 7,
        }],
    })
    owner = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"}, client=client)
    return owner, client


def run_to_seal(executor, reference_id=None):
    actions = []
    for attempted in range(10, 40):
        result = executor.step(reference_id, attempted_ns=attempted)
        reference_id = result.reference_id
        actions.append(result.action)
        if result.state == "sealed":
            return result, actions
        executor = InactiveReferenceExecutor(executor.store, executor.transport)
    raise AssertionError("reference did not seal")


def run_recovery_to_seal(executor, recovery_id=None):
    for attempted in range(50, 90):
        result = executor.step(recovery_id, attempted_ns=attempted)
        recovery_id = result.recovery_id
        if result.state == "sealed":
            return result
        executor = InactiveRecoveryExecutor(executor.store, executor.transport)
    raise AssertionError("recovery did not seal")


def test_reference_resumes_one_page_at_a_time_and_remains_private(tmp_path):
    store = NodeStore(tmp_path / "node.sqlite3", identity())
    source, _client = transport()
    sealed, actions = run_to_seal(InactiveReferenceExecutor(store, source))
    assert actions == [
        "started", "manifest:documents", "manifest:chats",
        "manifest:streams", "stream", "terminal", "sealed",
    ]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (sealed.generation,),
        ).fetchone() == ("sealed",)
        assert conn.execute(
            "SELECT path FROM candidate_docs WHERE generation=? ORDER BY path",
            (sealed.generation,),
        ).fetchall() == [("chats/c1/meta.json",), ("users/a.json",)]
        assert conn.execute(
            "SELECT id,chat_id,log_name,payload FROM candidate_log_rows "
            "WHERE generation=?", (sealed.generation,),
        ).fetchall() == [(7, "c1", "main", b"sealed")]
        assert conn.execute(
            "SELECT chat_id FROM candidate_visibility WHERE generation=?",
            (sealed.generation,),
        ).fetchall() == [("c1",)]
        assert conn.execute("SELECT count(*) FROM remote_docs").fetchone() == (0,)
    with pytest.raises(NodeInputError, match="reference candidate cannot be admitted"):
        store.admit_candidate(sealed.generation)


def test_reference_derives_visibility_from_exact_meta_paths(tmp_path):
    store = NodeStore(tmp_path / "node.sqlite3", identity())
    source, client = transport()
    client.db["ab_docs"] = [
        {"root": "team", "path": "chats/a-/meta.json", "seq": 1,
         "deleted": False, "data": {"members": {"user": True}}},
        {"root": "team", "path": "chats/a/meta.json", "seq": 2,
         "deleted": True, "data": None},
        {"root": "team", "path": "presence0", "seq": 3,
         "deleted": False, "data": {"durable": True}},
    ]
    client.db["ab_logs"] = []
    client.db["ab_log_stream_heads"] = []
    sealed, _actions = run_to_seal(InactiveReferenceExecutor(store, source))
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT chat_id FROM candidate_visibility WHERE generation=? "
            "ORDER BY chat_id", (sealed.generation,),
        ).fetchall() == [("a",), ("a-",)]


def test_reference_abandons_when_source_moves_after_page_read(tmp_path, monkeypatch):
    store = NodeStore(tmp_path / "node.sqlite3", identity())
    source, client = transport()
    executor = InactiveReferenceExecutor(store, source)
    started = executor.step(None, attempted_ns=10)
    original = source.reference_documents

    def moving(*args, **kwargs):
        page = original(*args, **kwargs)
        client.db["ab_change_events"].append({
            "root": "team", "id": 1, "stream_kind": "root", "stream_id": "",
            "domain": "docs", "source_key": "users/a.json",
            "doc_head": 2, "log_head": None,
        })
        return page

    monkeypatch.setattr(source, "reference_documents", moving)
    with pytest.raises(NodeGenerationChanged, match="moved during collection"):
        executor.step(started.reference_id, attempted_ns=11)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (started.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (0,)


def test_reference_and_recovery_executors_reject_each_others_runs(tmp_path):
    source, _client = transport()
    reference_store = NodeStore(tmp_path / "reference.sqlite3", identity())
    reference = InactiveReferenceExecutor(reference_store, source).step(
        None, attempted_ns=10)
    with pytest.raises(NodeInputError, match="different run kind"):
        InactiveRecoveryExecutor(reference_store, source).step(
            reference.reference_id, attempted_ns=11)

    recovery_store = NodeStore(tmp_path / "recovery.sqlite3", identity())
    recovery = InactiveRecoveryExecutor(recovery_store, source).step(
        None, attempted_ns=10)
    with pytest.raises(NodeInputError, match="different run kind"):
        InactiveReferenceExecutor(recovery_store, source).step(
            recovery.recovery_id, attempted_ns=11)
    with pytest.raises(NodeInputError, match="require a reference run"):
        recovery_store.stage_reference_documents(
            recovery.recovery_id, _provider_cut(source.recovery_fence()),
            "wrong-run-kind", 0, b"", (), has_more=False,
        )


def test_store_owned_reference_compares_without_in_memory_batch(tmp_path):
    store = NodeStore(tmp_path / "node.sqlite3", identity())
    source, _client = transport()
    recovery = run_recovery_to_seal(InactiveRecoveryExecutor(store, source))
    reference, _actions = run_to_seal(InactiveReferenceExecutor(store, source))
    current = _provider_cut(source.recovery_fence())
    result = RecoveryEquivalenceRecorder(
        store, tmp_path / "evidence",
    ).compare_candidates(
        recovery.recovery_id, reference.reference_id, current,
        observed_ns=100, label="independent-current-path-v1",
    )
    assert result.outcome == "equal"
    assert result.candidate_counts == result.reference_counts == (2, 1, 1, 2)
    assert result.candidate_digest == result.reference_digest
    mode = stat.S_IMODE((tmp_path / "evidence").stat().st_mode)
    assert mode & 0o077 == 0


def test_store_owned_reference_reports_first_raw_family_mismatch(tmp_path):
    store = NodeStore(tmp_path / "node.sqlite3", identity())
    source, _client = transport()
    recovery = run_recovery_to_seal(InactiveRecoveryExecutor(store, source))
    reference, _actions = run_to_seal(InactiveReferenceExecutor(store, source))
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE candidate_log_rows SET payload=? WHERE generation=? AND id=7",
            (b"tampered-private-reference", reference.generation),
        )
    result = RecoveryEquivalenceRecorder(
        store, tmp_path / "evidence",
    ).compare_candidates(
        recovery.recovery_id, reference.reference_id,
        _provider_cut(source.recovery_fence()), observed_ns=101,
        label="mismatch-fixture",
    )
    assert result.outcome == "mismatch"
    assert result.mismatched_families == ("logs",)
    assert result.candidate_digest is None

