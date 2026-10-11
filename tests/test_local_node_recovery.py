"""Inactive transport-to-Store recovery and equivalence checks."""

import json
import sqlite3

import pytest

from agentbridge.node.admission import (
    NodeCaptureRequest, NodeDocument, NodeFrontier, NodeGenerationChanged,
    NodeInputBatch, NodeInputError, NodeLogRow, NodeVisibility,
)
from agentbridge.node.equivalence import (
    RecoveryEquivalenceRecorder, RecoveryEquivalenceReference,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.recovery import (
    ClaimedRecoveryExecutor, InactiveRecoveryExecutor, _provider_cut,
)
from agentbridge.node.store import NodeStore
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store import source_selectors
from agentbridge.store.local_source import SourceChanged
from agentbridge.transport.local_mutations import root_identity
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


def exact_reference(*, log_payload=b"sealed"):
    return NodeInputBatch(
        documents=(
            NodeDocument(
                "chats/c1/meta.json", 1, False,
                b'{"members":{"user":true}}',
            ),
            NodeDocument("users/a.json", 2, False, b'{"name":"a"}'),
        ),
        log_rows=(NodeLogRow(7, "c1", "main", log_payload),),
        visibility=(NodeVisibility("c1", True),),
        frontiers=(
            NodeFrontier("provider-source-ledger", EPOCH, 0, 0),
            NodeFrontier("provider-source-replay", EPOCH, 0, 0),
        ),
        documents_mode="replace", logs_mode="replace",
        visibility_mode="replace", frontiers_mode="replace",
    )


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


def test_equivalence_recorder_preserves_private_equal_and_mismatch_evidence(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    sealed, _actions = run_to_seal(InactiveRecoveryExecutor(owner, tx))
    recorder = RecoveryEquivalenceRecorder(owner, tmp_path / "evidence")
    current = _provider_cut(tx.recovery_fence())

    equal_reference = RecoveryEquivalenceReference(
        current, current, exact_reference(), "deterministic-current-path")
    equal = recorder.compare(sealed.recovery_id, equal_reference, observed_ns=100)
    assert equal.outcome == "equal"
    assert equal.candidate_digest == equal.reference_digest
    assert json.loads(open(equal.evidence_path, encoding="utf-8").read())[
        "outcome"] == "equal"

    mismatch_reference = RecoveryEquivalenceReference(
        current, current, exact_reference(log_payload=b"different"),
        "deterministic-current-path")
    mismatch = recorder.compare(
        sealed.recovery_id, mismatch_reference, observed_ns=101)
    assert mismatch.outcome == "mismatch"
    assert mismatch.mismatched_families == ("logs",)
    evidence = json.loads(open(mismatch.evidence_path, encoding="utf-8").read())
    assert evidence["candidate_digest"] is None
    assert set(evidence["mismatch_tokens"]) == {"logs"}

    # Diagnostic evidence cannot publish the recovery candidate.
    assert owner.capture(NodeCaptureRequest(max_bytes=1024)).generation == 0


def test_recovery_executor_discovers_durable_run_after_restart(tmp_path):
    path = tmp_path / "node.sqlite3"
    source, _client = transport()
    started = InactiveRecoveryExecutor(NodeStore(path, identity()), source).step(
        None, attempted_ns=10)
    resumed = InactiveRecoveryExecutor(NodeStore(path, identity()), source).step(
        None, attempted_ns=11)
    assert resumed.recovery_id == started.recovery_id
    assert resumed.generation == started.generation
    assert resumed.action == "resumed"


def test_claimed_executor_commits_claim_before_provider_fence_and_resumes(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    original = tx.recovery_fence
    calls = 0

    def checked_fence():
        nonlocal calls
        calls += 1
        with sqlite3.connect(owner.path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM node_recovery_claims"
            ).fetchone() == (1,)
        return original()

    monkeypatch.setattr(tx, "recovery_fence", checked_fence)
    started = ClaimedRecoveryExecutor(owner, tx, root).step(
        None, attempted_ns=10)
    resumed = ClaimedRecoveryExecutor(owner, tx, root).step(
        None, attempted_ns=11)

    assert calls == 2
    assert started.action == "started"
    assert resumed.recovery_id == started.recovery_id
    assert resumed.action == "manifest:documents"
    assert owner.status(node_epoch="node", started_ns=1).health == "inactive"


def test_mutation_between_claim_and_provider_fence_prevents_recovery_start(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    original = tx.recovery_fence

    def moving_fence():
        root.begin((source_selectors.Selector("doc_exact", "users/later.json"),))
        return original()

    monkeypatch.setattr(tx, "recovery_fence", moving_fence)
    with pytest.raises(SourceChanged, match="root_mutation_pending"):
        ClaimedRecoveryExecutor(owner, tx, root).step(None, attempted_ns=10)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)


def test_claimed_recovery_seals_but_cannot_use_ordinary_admission(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    executor = ClaimedRecoveryExecutor(owner, tx, root)
    result = executor.step(None, attempted_ns=10)
    for attempted in range(11, 30):
        result = executor.step(result.recovery_id, attempted_ns=attempted)
        if result.state == "sealed":
            break
    assert result.state == "sealed"
    with pytest.raises(NodeInputError, match="coordinator admission"):
        owner.admit_candidate(result.generation)
    assert owner.capture(NodeCaptureRequest(max_bytes=1024)).generation == 0


def test_database_guard_blocks_version_nine_writer_from_admitting_claim(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    executor = ClaimedRecoveryExecutor(owner, tx, root)
    result = executor.step(None, attempted_ns=10)
    for attempted in range(11, 30):
        result = executor.step(result.recovery_id, attempted_ns=attempted)
        if result.state == "sealed":
            break
    assert result.state == "sealed"

    # The v9 admission implementation ultimately performs this same state
    # transition and does not know about claim mappings.  The database must
    # reject it even for a valid empty-fence claim.
    with sqlite3.connect(owner.path) as conn, pytest.raises(
            sqlite3.IntegrityError, match="requires coordinator admission"):
        conn.execute(
            "UPDATE node_generations SET state='admitted' "
            "WHERE generation=? AND state='sealed'", (result.generation,),
        )
    assert owner.capture(NodeCaptureRequest(max_bytes=1024)).generation == 0


def test_missing_claim_mapping_cannot_downgrade_claimed_recovery(tmp_path):
    path = tmp_path / "node.sqlite3"
    owner = NodeStore(path, identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    started = ClaimedRecoveryExecutor(owner, tx, root).step(None, attempted_ns=10)

    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM claimed_recoveries WHERE recovery_id=?",
            (started.recovery_id,),
        )

    with pytest.raises(sqlite3.DatabaseError, match="claimed local recovery"):
        owner.recovery_state(
            started.recovery_id, _provider_cut(tx.recovery_fence()))
    with pytest.raises(sqlite3.DatabaseError, match="claimed local generation"):
        NodeStore(path, identity())
    with sqlite3.connect(path) as conn, pytest.raises(
            sqlite3.IntegrityError, match="claim tag is immutable"):
        conn.execute(
            "UPDATE node_generations SET claim_id=NULL,claim_digest=NULL "
            "WHERE generation=?", (started.generation,),
        )


def test_v9_migration_backfills_active_claim_generation_tag(tmp_path):
    path = tmp_path / "node.sqlite3"
    owner = NodeStore(path, identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    started = ClaimedRecoveryExecutor(owner, tx, root).step(None, attempted_ns=10)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER node_claimed_generation_guard")
        conn.execute("DROP TRIGGER node_claim_tag_immutable_guard")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_digest")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_id")
        conn.execute("DROP TABLE node_schema")
        conn.execute("""CREATE TABLE node_schema(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=9),
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""")
        conn.execute("INSERT INTO node_schema VALUES(1,9,1)")

    reopened = NodeStore(path, identity())

    assert reopened.claimed_recovery_id() == started.recovery_id
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version FROM node_schema WHERE singleton=1"
        ).fetchone() == (10,)
        assert conn.execute(
            "SELECT claim_id,claim_digest FROM node_generations "
            "WHERE generation=?", (started.generation,),
        ).fetchone() == conn.execute(
            "SELECT claim_id,claim_digest FROM claimed_recoveries "
            "WHERE recovery_id=?", (started.recovery_id,),
        ).fetchone()


def test_v9_migration_retires_unmapped_active_claim_work(tmp_path):
    path = tmp_path / "node.sqlite3"
    owner = NodeStore(path, identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    executor = ClaimedRecoveryExecutor(owner, tx, root)
    result = executor.step(None, attempted_ns=10)
    for attempted in range(11, 30):
        result = executor.step(result.recovery_id, attempted_ns=attempted)
        if result.state == "sealed":
            break
    assert result.state == "sealed"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM claimed_recoveries WHERE recovery_id=?",
            (result.recovery_id,),
        )
        conn.execute("DROP TRIGGER node_claimed_generation_guard")
        conn.execute("DROP TRIGGER node_claim_tag_immutable_guard")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_digest")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_id")
        conn.execute("DROP TABLE node_schema")
        conn.execute("""CREATE TABLE node_schema(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=9),
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""")
        conn.execute("INSERT INTO node_schema VALUES(1,9,1)")

    reopened = NodeStore(path, identity())

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (result.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM node_recovery_claims").fetchone() == (1,)
    with pytest.raises(NodeGenerationChanged, match="candidate is not sealed"):
        reopened.admit_candidate(result.generation)


def test_equivalence_reference_rejects_a_moving_provider_cut():
    cut = _provider_cut(transport()[0].recovery_fence())
    moved = type(cut)(
        cut.schema_version, cut.index_epoch, cut.source_epoch, cut.account_id,
        cut.role, cut.minimum_cursor, cut.cursor + 1,
    )
    with pytest.raises(NodeInputError, match="moved"):
        RecoveryEquivalenceReference(
            cut, moved, exact_reference(), "moving-current-path")


def test_equivalence_stale_binding_cleanup_commits_before_rejection(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    sealed, _actions = run_to_seal(InactiveRecoveryExecutor(owner, tx))
    recorder = RecoveryEquivalenceRecorder(owner, tmp_path / "evidence")
    old = _provider_cut(tx.recovery_fence())
    changed = type(old)(
        old.schema_version, old.index_epoch, "replacement-source-epoch",
        old.account_id, old.role, old.minimum_cursor, old.cursor,
    )
    stale = RecoveryEquivalenceReference(
        changed, changed, exact_reference(), "stale-current-path")
    with pytest.raises(NodeGenerationChanged, match="binding changed"):
        recorder.compare(sealed.recovery_id, stale, observed_ns=102)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (sealed.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries").fetchone() == (0,)
    prior = RecoveryEquivalenceReference(
        old, old, exact_reference(), "old-current-path")
    with pytest.raises(NodeGenerationChanged, match="unknown recovery"):
        recorder.compare(sealed.recovery_id, prior, observed_ns=103)
    assert not (tmp_path / "evidence").exists()


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
