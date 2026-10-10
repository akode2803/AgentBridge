import os
import sqlite3
import stat

import pytest

import agentbridge.node.store as node_store
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.store import NodeStore
from agentbridge.node.admission import (
    NodeDocument, NodeGenerationChanged, NodeInputBatch, NodeInputError,
    NodeLogRow, NodeProviderCut, NodeRecoveryPlan, NodeRecoveryScope,
    NodeRecoveryStreamHead, NodeRecoveryWork,
)


def identity(**changes):
    values = dict(provider_endpoint="https://example.test",
                  root="supabase://mesh", principal="user", machine="mac")
    values.update(changes)
    return ReplicaIdentity(**values)


def recovery_plan(owner):
    status = owner.status(node_epoch="node", started_ns=1)
    cut = NodeProviderCut(1, "index-1", "source-1", "account-1", "member", 0, 5)
    plan = NodeRecoveryPlan(
        status.database_incarnation, owner.identity.digest,
        status.admitted_generation, 0, cut,
        (NodeRecoveryScope("root", "", 0),),
        (NodeRecoveryWork("documents", "docs", "root", "", b"all"),
         NodeRecoveryWork("source-frontier", "frontiers", "root", "", b"ledger")),
    )
    return plan, cut


def test_v3_migration_preserves_admitted_and_resumable_generic_candidate(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    owner = NodeStore(path, identity())
    admitted = owner.begin_candidate(created_ns=1)
    owner.seal_candidate(admitted, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b"a"),)), observed_ns=2)
    owner.admit_candidate(admitted)
    building = owner.begin_candidate(created_ns=3)
    owner.stage_candidate_batch(building, "first", NodeInputBatch(
        documents=(NodeDocument("users/b.json", 2, False, b"b"),)))
    sealed = owner.begin_candidate(created_ns=4)
    owner.seal_candidate(sealed, NodeInputBatch(), observed_ns=5)
    with sqlite3.connect(path) as conn:
        for table in ("recovery_proof_pages", "recovery_streams",
                      "recovery_manifests", "recovery_pages", "recovery_work",
                      "recovery_scopes", "provider_recoveries"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DROP TABLE node_schema")
        conn.execute("CREATE TABLE node_schema(singleton INTEGER PRIMARY KEY,"
                     "version INTEGER CHECK(version=3),protocol_version INTEGER)")
        conn.execute("INSERT INTO node_schema VALUES(1,3,1)")
    reopened = NodeStore(path, identity())
    assert reopened.stage_candidate_batch(building, "first", NodeInputBatch(
        documents=(NodeDocument("users/b.json", 2, False, b"b"),))) is False
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT payload FROM remote_docs WHERE path='users/a.json'")\
            .fetchone() == (b"a",)
        assert conn.execute("SELECT state FROM node_generations WHERE generation=?",
                            (sealed,)).fetchone() == ("sealed",)
        assert conn.execute("SELECT version FROM node_schema").fetchone() == (5,)


def test_v4_migration_retires_unprovable_recovery_only(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    owner = NodeStore(path, identity())
    admitted = owner.begin_candidate(created_ns=1)
    owner.seal_candidate(admitted, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b"admitted"),)),
        observed_ns=2)
    owner.admit_candidate(admitted)
    generic = owner.begin_candidate(created_ns=3)
    generic_batch = NodeInputBatch(
        documents=(NodeDocument("users/b.json", 2, False, b"generic"),))
    owner.stage_candidate_batch(generic, "generic", generic_batch)
    status = owner.status(node_epoch="node", started_ns=1)
    with sqlite3.connect(path) as conn:
        root_generation = conn.execute(
            "SELECT generation FROM scope_versions WHERE scope_kind='root' "
            "AND scope_id=''"
        ).fetchone()[0]
    cut = NodeProviderCut(1, "index", "source", "account", "member", 0, 5)
    plan = NodeRecoveryPlan(
        status.database_incarnation, owner.identity.digest,
        status.admitted_generation, 0, cut,
        (NodeRecoveryScope("root", "", root_generation),),
        (NodeRecoveryWork("frontier", "frontiers", "root", "", b"ledger"),),
    )
    recovery = owner.begin_recovery(plan, created_ns=4)
    owner.stage_recovery_documents(
        recovery.recovery_id, cut, "recovery-docs", 0, b"",
        (NodeDocument("users/recovery.json", 3, False, b"private"),),
        has_more=False)
    with sqlite3.connect(path) as conn:
        for table in ("recovery_proof_pages", "recovery_streams",
                      "recovery_manifests"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DROP TABLE node_schema")
        conn.execute("CREATE TABLE node_schema(singleton INTEGER PRIMARY KEY,"
                     "version INTEGER CHECK(version=4),protocol_version INTEGER)")
        conn.execute("INSERT INTO node_schema VALUES(1,4,1)")

    reopened = NodeStore(path, identity())
    assert reopened.stage_candidate_batch(generic, "generic", generic_batch) is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT payload FROM remote_docs WHERE path='users/a.json'"
        ).fetchone() == (b"admitted",)
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (recovery.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM candidate_docs WHERE generation=?",
            (recovery.generation,),
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (0,)
        assert conn.execute("SELECT version FROM node_schema").fetchone() == (5,)


def test_recovery_reopens_retries_and_rejects_generic_bypass(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    owner = NodeStore(path, identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    resumed = NodeStore(path, identity()).recovery_state(state.recovery_id, cut)
    assert resumed.generation == state.generation
    documents = (NodeDocument("users/a.json", 1, False, b"a"),)
    args = (state.recovery_id, cut, "page-1", 0, b"", documents)
    assert owner.stage_recovery_documents(*args, has_more=True) is True
    assert owner.stage_recovery_documents(*args, has_more=True) is False
    with pytest.raises(NodeInputError, match="reused"):
        owner.stage_recovery_documents(
            state.recovery_id, cut, "page-1", 0, b"",
            (NodeDocument("users/b.json", 2, False, b"b"),), has_more=True)
    resumed_page = owner.recovery_state(state.recovery_id, cut)
    documents_state = {item.family: item for item in resumed_page.manifests}["documents"]
    assert documents_state.checkpoint == b"users/a.json"
    assert documents_state.outcome == "pending"
    assert resumed_page.proof_revision == 1
    with pytest.raises(NodeInputError, match="recovery candidate"):
        owner.stage_candidate_batch(state.generation, "generic", NodeInputBatch())
    with pytest.raises(NodeInputError, match="recovery candidate"):
        owner.seal_staged_candidate(state.generation, observed_ns=12)
    with pytest.raises(NodeInputError, match="replay proof"):
        owner.seal_recovery(state.recovery_id, cut, observed_ns=13)
    with pytest.raises(NodeInputError, match="typed recovery pages"):
        owner.stage_recovery_page(
            state.recovery_id, cut, "legacy", "documents", 0, b"", b"next",
            "pending", 0, NodeInputBatch())


def test_typed_recovery_manifests_derive_and_complete_stream_obligations(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    assert owner.stage_recovery_documents(
        state.recovery_id, cut, "documents-empty", 0, b"", (), has_more=False)
    assert owner.stage_recovery_chats(
        state.recovery_id, cut, "chats", 0, b"", ("a", "b"), has_more=False)
    assert owner.stage_recovery_streams(
        state.recovery_id, cut, "streams", 0, b"",
        (NodeRecoveryStreamHead("a", "main", 7),
         NodeRecoveryStreamHead("b", "main", 1)), has_more=False)
    assert owner.stage_recovery_log_page(
        state.recovery_id, cut, "a-log", "a", "main", 0, 0,
        (NodeLogRow(2, "a", "main", b"two"),
         NodeLogRow(7, "a", "main", b"seven")), has_more=False)
    assert owner.stage_recovery_log_page(
        state.recovery_id, cut, "b-log", "b", "main", 0, 0, (),
        has_more=False)
    # The positive provider head can name a physically deleted historical row.
    # Exact terminal exhaustion closes the payload scan, but sealing remains
    # disabled until root-delete event replay is also proven.

    resumed = owner.recovery_state(state.recovery_id, cut)
    manifests = {item.family: item for item in resumed.manifests}
    assert manifests["documents"].outcome == "complete_empty"
    assert manifests["chats"].outcome == "complete"
    assert manifests["streams"].outcome == "complete"
    assert resumed.stream_count == 2
    assert resumed.pending_stream_count == 0
    streams = owner.recovery_streams(state.recovery_id, cut, limit=1)
    assert [(item.chat_id, item.cursor, item.outcome) for item in streams] == [
        ("a", 7, "complete")]
    streams = owner.recovery_streams(
        state.recovery_id, cut, after=("a", "main"), limit=1)
    assert [(item.chat_id, item.cursor, item.outcome) for item in streams] == [
        ("b", 1, "complete_empty")]
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT chat_id FROM candidate_visibility ORDER BY chat_id"
        ).fetchall() == [("a",), ("b",)]
        assert conn.execute(
            "SELECT id FROM candidate_log_rows ORDER BY id"
        ).fetchall() == [(2,), (7,)]


def test_stream_manifest_maximum_raw_identity_checkpoint_is_resumable(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    first = NodeRecoveryStreamHead("\x01" * 1024, '"' * 4096, 1)
    assert owner.stage_recovery_streams(
        state.recovery_id, cut, "first", 0, b"", (first,), has_more=True)
    checkpoint = {item.family: item for item in owner.recovery_state(
        state.recovery_id, cut).manifests}["streams"].checkpoint
    assert len(checkpoint) <= node_store.MAX_RECOVERY_OPAQUE_BYTES
    assert owner.stage_recovery_streams(
        state.recovery_id, cut, "second", 1, checkpoint,
        (NodeRecoveryStreamHead("z", "last", 1),), has_more=False)
    with pytest.raises(NodeInputError, match="stream head"):
        NodeRecoveryStreamHead("room", "main", 0)


@pytest.mark.parametrize("changed", ["schema", "index", "source", "account",
                                      "role", "compaction",
                                      "scope", "pending", "base"])
def test_recovery_stale_binding_abandons_without_touching_admitted(tmp_path, changed):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    current = cut
    if changed == "schema":
        current = NodeProviderCut(2, cut.index_epoch, cut.source_epoch,
                                  cut.account_id, cut.role, 0, 5)
    elif changed == "index":
        current = NodeProviderCut(1, "other", cut.source_epoch,
                                  cut.account_id, cut.role, 0, 5)
    elif changed == "source":
        current = NodeProviderCut(1, cut.index_epoch, "other",
                                  cut.account_id, cut.role, 0, 5)
    elif changed == "account":
        current = NodeProviderCut(1, cut.index_epoch, cut.source_epoch,
                                  "other", cut.role, 0, 5)
    elif changed == "role":
        current = NodeProviderCut(1, cut.index_epoch, cut.source_epoch,
                                  cut.account_id, "other", 0, 5)
    elif changed == "compaction":
        current = NodeProviderCut(1, cut.index_epoch, cut.source_epoch,
                                  cut.account_id, cut.role, 1, 5)
    else:
        with sqlite3.connect(owner.path) as conn:
            if changed == "scope":
                conn.execute("INSERT INTO scope_versions VALUES('root','',1,NULL)")
            elif changed == "pending":
                conn.execute("UPDATE node_meta SET pending_mutations=1")
            else:
                conn.execute("UPDATE node_meta SET admitted_generation=1")
    with pytest.raises(NodeGenerationChanged):
        owner.recovery_state(state.recovery_id, current)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT state FROM node_generations WHERE generation=?",
                            (state.generation,)).fetchone() == ("abandoned",)
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (0,)


def test_delayed_older_provider_cut_does_not_abandon_newer_recovery(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    newer = NodeProviderCut(1, cut.index_epoch, cut.source_epoch,
                            cut.account_id, cut.role, 0, 8)
    owner.extend_recovery_target(state.recovery_id, newer)
    with pytest.raises(NodeGenerationChanged, match="precedes"):
        owner.recovery_state(state.recovery_id, cut)
    assert owner.recovery_state(state.recovery_id, newer).target_cursor == 8
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT state FROM node_generations WHERE generation=?",
                            (state.generation,)).fetchone() == ("building",)
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (1,)


def test_recovery_target_extension_and_reclaim_cleanup(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    newer = NodeProviderCut(1, cut.index_epoch, cut.source_epoch,
                            cut.account_id, cut.role, 0, 8)
    assert owner.extend_recovery_target(state.recovery_id, newer) == 8
    resumed = owner.recovery_state(state.recovery_id, newer)
    assert resumed.plan.provider_cut.cursor == 5
    assert resumed.target_cursor == 8
    page_cut = NodeProviderCut(1, cut.index_epoch, cut.source_epoch,
                               cut.account_id, cut.role, 0, 9)
    assert owner.stage_recovery_documents(
        state.recovery_id, page_cut, "page", 0, b"", (), has_more=False)
    assert owner.recovery_state(state.recovery_id, page_cut).target_cursor == 9
    assert owner.reclaim_candidates(before_ns=11) == 1
    with sqlite3.connect(owner.path) as conn:
        for table in ("provider_recoveries", "recovery_scopes", "recovery_work",
                      "recovery_pages", "recovery_manifests", "recovery_streams",
                      "recovery_proof_pages"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_recovery_page_and_checkpoint_roll_back_together(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    owner.stage_recovery_streams(
        state.recovery_id, cut, "streams", 0, b"",
        (NodeRecoveryStreamHead("a", "one", 1),
         NodeRecoveryStreamHead("b", "two", 1)), has_more=False)
    owner.stage_recovery_log_page(
        state.recovery_id, cut, "first", "a", "one", 0, 0,
        (NodeLogRow(1, "a", "one", b"a"),), has_more=False)
    with pytest.raises(NodeInputError, match="overlap"):
        owner.stage_recovery_log_page(
            state.recovery_id, cut, "second", "b", "two", 0, 0,
            (NodeLogRow(1, "b", "two", b"changed"),), has_more=False)
    resumed = NodeStore(owner.path, identity()).recovery_state(state.recovery_id, cut)
    streams = owner.recovery_streams(state.recovery_id, cut)
    assert [(item.chat_id, item.cursor, item.page_count) for item in streams] == [
        ("a", 1, 1), ("b", 0, 0)]
    assert resumed.proof_revision == 2
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM recovery_proof_pages").fetchone() == (2,)
        assert conn.execute("SELECT payload FROM candidate_log_rows").fetchone() == (b"a",)


def test_recovery_page_revision_rejects_checkpoint_aba_and_old_retry_is_inert(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    first = (state.recovery_id, cut, "first", 0, b"",
             (NodeDocument("a", 1, False, b"a"),))
    assert owner.stage_recovery_documents(*first, has_more=True)
    assert owner.stage_recovery_documents(
        state.recovery_id, cut, "second", 1, b"a",
        (NodeDocument("b", 2, False, b"b"),), has_more=False)
    assert owner.stage_recovery_documents(*first, has_more=True) is False
    with pytest.raises(NodeInputError, match="checkpoint changed"):
        owner.stage_recovery_documents(
            state.recovery_id, cut, "delayed", 0, b"",
            (NodeDocument("c", 3, False, b"c"),), has_more=False)
    resumed = owner.recovery_state(state.recovery_id, cut)
    manifest = {item.family: item for item in resumed.manifests}["documents"]
    assert manifest.checkpoint == b"b"
    assert manifest.page_count == 2
    assert manifest.outcome == "complete"


def test_stale_recovery_cleanup_prunes_generation_history(tmp_path, monkeypatch):
    monkeypatch.setattr(node_store, "MAX_GENERATION_HISTORY", 2)
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    for index in range(10):
        plan, cut = recovery_plan(owner)
        state = owner.begin_recovery(plan, created_ns=10 + index)
        changed = NodeProviderCut(2, cut.index_epoch, cut.source_epoch,
                                  cut.account_id, cut.role, 0, 5)
        with pytest.raises(NodeGenerationChanged):
            owner.recovery_state(state.recovery_id, changed)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM node_generations").fetchone() == (2,)


def test_recovery_failure_health_is_owned_by_its_refresh_token(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    admitted = owner.begin_candidate(created_ns=1)
    owner.seal_candidate(admitted, NodeInputBatch(), observed_ns=2)
    owner.admit_candidate(admitted)
    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=10)
    assert state.health_owner_token == 10
    assert owner.abandon_candidate(state.generation)
    assert owner.status(node_epoch="node", started_ns=1).health == "degraded"

    plan, cut = recovery_plan(owner)
    state = owner.begin_recovery(plan, created_ns=11)
    token = owner.begin_refresh(attempted_ns=20, expected_generation=admitted)
    assert owner.note_refresh_state(
        "ready", attempted_ns=token, expected_generation=admitted,
        expected_attempt_ns=token,
    ) == "ready"
    # Short refresh attempts do not invalidate the durable private work.
    assert owner.recovery_state(state.recovery_id, cut).generation == state.generation
    assert owner.abandon_candidate(
        state.generation, expected_generation=admitted,
        expected_attempt_ns=token,
    )
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.health == "ready"
    assert status.last_attempt_ns == token


def test_recovery_metadata_budget_counts_utf8_bytes(tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    status = owner.status(node_epoch="node", started_ns=1)
    cut = NodeProviderCut(1, "index", "source", "account", "member", 0, 5)
    chat = "é" * 400  # 400 code points, 800 encoded bytes.
    checkpoint = b"x" * 1_500
    plan = NodeRecoveryPlan(
        status.database_incarnation, owner.identity.digest, 0, 0, cut,
        (NodeRecoveryScope("root", "", 0), NodeRecoveryScope("chat", chat, 0)),
        (NodeRecoveryWork("docs", "docs", "chat", chat, b"all", checkpoint),
         NodeRecoveryWork("frontier", "frontiers", "root", "", b"ledger")),
    )
    state = owner.begin_recovery(plan, created_ns=10)
    monkeypatch.setattr(node_store, "MAX_RECOVERY_METADATA_BYTES", 100)
    with pytest.raises(NodeInputError, match="metadata exceeds"):
        owner.stage_recovery_documents(
            state.recovery_id, cut, "page", 0, b"", (), has_more=False)


def test_recovery_begin_counts_mandatory_manifest_metadata_atomically(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, _cut = recovery_plan(owner)
    monkeypatch.setattr(node_store, "MAX_RECOVERY_METADATA_BYTES", 500)
    with pytest.raises(NodeInputError, match="metadata exceeds"):
        owner.begin_recovery(plan, created_ns=10)
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.health == "inactive"
    assert status.last_attempt_ns == 0
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM node_generations").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM provider_recoveries").fetchone() == (0,)


def test_recovery_input_bounds_and_family_gate(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    plan, cut = recovery_plan(owner)
    with pytest.raises(NodeInputError, match="whole-root"):
        NodeRecoveryPlan(plan.database_incarnation, plan.identity_digest,
                         plan.base_generation, 0, cut,
                         (NodeRecoveryScope("chat", "a", 0),),
                         (NodeRecoveryWork("frontier", "frontiers", "chat", "a",
                                           b"ledger"),))
    with pytest.raises(NodeInputError, match="budget"):
        NodeRecoveryWork("oversized", "docs", "root", "", b"x" * 8193)
    with pytest.raises(NodeInputError, match="work id"):
        NodeRecoveryWork("é" * 129, "docs", "root", "", b"all")
    state = owner.begin_recovery(plan, created_ns=10)
    with pytest.raises(NodeInputError, match="strictly ordered"):
        owner.stage_recovery_documents(
            state.recovery_id, cut, "wrong", 0, b"",
            (NodeDocument("b", 1, False, b"b"),
             NodeDocument("a", 2, False, b"a")), has_more=False)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM recovery_pages").fetchone() == (0,)


def test_store_persists_identity_and_incarnation(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    first = NodeStore(path, identity()).status(node_epoch="one", started_ns=1)
    second = NodeStore(path, identity()).status(node_epoch="two", started_ns=2)
    assert first.database_incarnation == second.database_incarnation
    assert first.node_epoch != second.node_epoch
    assert second.identity_digest == identity().digest
    assert second.health == "inactive"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version,protocol_version FROM node_schema").fetchone() == (5, 1)
        assert conn.execute("SELECT count(*) FROM remote_docs").fetchone() == (0,)


def test_store_rejects_rebinding_existing_database(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    NodeStore(path, identity())
    with pytest.raises(sqlite3.DatabaseError, match="identity mismatch"):
        NodeStore(path, identity(principal="someone-else"))


def test_v2_store_migration_retires_nonresumable_private_candidates(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    NodeStore(path, identity())
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO node_generations(generation,base_generation,state,created_ns) "
            "VALUES(1,0,'building',1)"
        )
        conn.execute(
            "INSERT INTO candidate_docs VALUES(1,'users/a.json',1,0,?)",
            (b"{}",),
        )
        for table in ("recovery_proof_pages", "recovery_streams",
                      "recovery_manifests", "recovery_pages", "recovery_work",
                      "recovery_scopes", "provider_recoveries"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DROP TABLE candidate_chunks")
        conn.execute("ALTER TABLE node_generations DROP COLUMN chunk_count")
        conn.execute("ALTER TABLE node_generations DROP COLUMN logs_mode")
        conn.execute("DROP TABLE node_schema")
        conn.execute("""
            CREATE TABLE node_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL CHECK(version=2),
                protocol_version INTEGER NOT NULL CHECK(protocol_version=1)
            )
        """)
        conn.execute("INSERT INTO node_schema VALUES(1,2,1)")

    NodeStore(path, identity())
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version,protocol_version FROM node_schema"
        ).fetchone() == (5, 1)
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=1"
        ).fetchone() == ("abandoned",)
        assert conn.execute("SELECT count(*) FROM candidate_docs").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM candidate_chunks").fetchone() == (0,)


@pytest.mark.parametrize("table", ["node_identity", "node_meta"])
def test_store_never_repairs_missing_identity_or_incarnation(tmp_path, table):
    path = tmp_path / "private" / "node.sqlite3"
    original = NodeStore(path, identity()).status(node_epoch="one", started_ns=1)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO node_generations("
            "generation,base_generation,state,created_ns,observed_ns,"
            "documents_mode,visibility_mode,frontiers_mode) "
            "VALUES(1,0,'admitted',1,1,'replace','replace','replace')")
        conn.execute(
            "INSERT INTO remote_docs VALUES('members/user.json',1,0,?,1)",
            (b"{}",),
        )
        conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.DatabaseError):
        NodeStore(path, identity(principal="someone-else"))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM remote_docs").fetchone() == (1,)
        if table == "node_meta":
            assert conn.execute("SELECT count(*) FROM node_meta").fetchone() == (0,)
        else:
            assert conn.execute("SELECT count(*) FROM node_identity").fetchone() == (0,)
    assert original.database_incarnation


def test_store_rejects_partial_existing_schema(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(sqlite3.DatabaseError, match="incomplete local node schema"):
        NodeStore(path, identity())


@pytest.mark.skipif(os.name == "nt", reason="symlink setup differs on Windows")
def test_store_rejects_database_symlink(tmp_path):
    target = tmp_path / "target.sqlite3"
    sqlite3.connect(target).close()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)
    with pytest.raises(sqlite3.DatabaseError, match="cannot be a symlink"):
        NodeStore(linked, identity())


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_store_files_are_owner_only(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    NodeStore(path, identity())
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
