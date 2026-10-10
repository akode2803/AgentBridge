import sqlite3

import pytest

from agentbridge.node.admission import (
    NodeCaptureOverflow,
    NodeCaptureRequest,
    NodeDocument,
    NodeFrontier,
    NodeGenerationChanged,
    NodeInputBatch,
    NodeInputError,
    NodeLogRequest,
    NodeLogRow,
    NodeVisibility,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.store import NodeStore


def identity():
    return ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="member", machine="desktop",
    )


def store(tmp_path):
    return NodeStore(tmp_path / "node" / "replica.sqlite3", identity())


def admit(owner, batch, *, created=1, observed=2):
    generation = owner.begin_candidate(created_ns=created)
    owner.seal_candidate(generation, batch, observed_ns=observed)
    owner.admit_candidate(generation)
    return generation


def whole_capture(owner, *, expected=None, max_bytes=8 * 1024 * 1024):
    return owner.capture(NodeCaptureRequest(
        document_prefixes=("",),
        log_requests=(NodeLogRequest("chat-a", "alice", limit=100),),
        include_visibility=True,
        visibility_limit=100,
        expected_generation=expected,
        max_documents=100,
        max_log_rows=100,
        max_bytes=max_bytes,
    ))


def first_batch():
    return NodeInputBatch(
        documents=(
            NodeDocument("accounts/alice.json", 1, False, b'{"name":"Alice"}'),
            NodeDocument("chats/chat-a/meta.json", 2, False, b'{"title":"A"}'),
        ),
        log_rows=(NodeLogRow(10, "chat-a", "alice", b'{"id":"m1"}'),),
        visibility=(NodeVisibility("chat-a", True),),
        frontiers=(NodeFrontier("documents", "epoch-1", 7, 0),),
        documents_mode="replace",
        visibility_mode="replace",
        frontiers_mode="replace",
    )


def test_candidate_is_invisible_until_all_families_admit(tmp_path):
    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=10)
    owner.seal_candidate(generation, first_batch(), observed_ns=20)

    before = whole_capture(owner)
    assert before.generation == 0
    assert before.documents == ()
    assert before.log_pages[0].rows == ()
    assert before.visibility == ()
    assert before.frontiers == ()

    assert owner.admit_candidate(generation) == generation
    after = whole_capture(owner, expected=generation)
    assert [row.path for row in after.documents] == [
        "accounts/alice.json", "chats/chat-a/meta.json",
    ]
    assert [row.id for row in after.log_pages[0].rows] == [10]
    assert after.visibility == ("chat-a",)
    assert after.frontiers == (NodeFrontier("documents", "epoch-1", 7, 0),)
    assert after.health == "ready"
    assert after.last_success_ns == 20


def test_delta_and_replace_preserve_log_history_but_replace_other_families(tmp_path):
    owner = store(tmp_path)
    first = admit(owner, NodeInputBatch(
        documents=(
            NodeDocument("chats/chat-a/meta.json", 1, False, b"a"),
            NodeDocument("chats/chat-b/meta.json", 1, False, b"b"),
        ),
        log_rows=(NodeLogRow(1, "chat-a", "alice", b"one"),),
        visibility=(NodeVisibility("chat-a", True), NodeVisibility("chat-b", True)),
        frontiers=(NodeFrontier("logs", None, 1, 0),),
        documents_mode="replace", visibility_mode="replace", frontiers_mode="replace",
    ))
    second = admit(owner, NodeInputBatch(
        documents=(NodeDocument("chats/chat-a/meta.json", 2, False, b"a2"),),
        log_rows=(NodeLogRow(2, "chat-a", "alice", b"two"),),
        visibility=(NodeVisibility("chat-a", True),),
        frontiers=(NodeFrontier("logs", None, 2, 0),),
        documents_mode="replace", visibility_mode="replace", frontiers_mode="replace",
    ), created=3, observed=4)
    assert second > first
    captured = whole_capture(owner, expected=second)
    assert [(row.path, row.payload) for row in captured.documents] == [
        ("chats/chat-a/meta.json", b"a2"),
    ]
    assert [row.id for row in captured.log_pages[0].rows] == [1, 2]
    assert captured.visibility == ("chat-a",)
    assert captured.frontiers[0].cursor == 2


def test_stale_candidate_is_abandoned_without_replacing_newer_cut(tmp_path):
    owner = store(tmp_path)
    first = owner.begin_candidate(created_ns=1)
    second = owner.begin_candidate(created_ns=2)
    owner.seal_candidate(first, first_batch(), observed_ns=3)
    owner.seal_candidate(second, NodeInputBatch(
        documents=(NodeDocument("accounts/bob.json", 1, False, b"bob"),),
    ), observed_ns=4)
    owner.admit_candidate(first)
    with pytest.raises(NodeGenerationChanged, match="base generation changed"):
        owner.admit_candidate(second)
    captured = whole_capture(owner, expected=first)
    assert "accounts/bob.json" not in {row.path for row in captured.documents}
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?", (second,)
        ).fetchone() == ("abandoned",)


def test_conflicting_log_identity_rolls_back_every_family(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, first_batch())
    candidate = owner.begin_candidate(created_ns=3)
    owner.seal_candidate(candidate, NodeInputBatch(
        documents=(NodeDocument("accounts/bob.json", 3, False, b"bob"),),
        log_rows=(NodeLogRow(10, "chat-a", "alice", b"different"),),
        visibility=(NodeVisibility("chat-b", True),),
    ), observed_ns=4)
    with pytest.raises(NodeInputError, match="identity conflict"):
        owner.admit_candidate(candidate)
    captured = whole_capture(owner, expected=generation)
    assert "accounts/bob.json" not in {row.path for row in captured.documents}
    assert captured.visibility == ("chat-a",)
    assert captured.log_pages[0].rows[0].payload == b'{"id":"m1"}'
    assert owner.abandon_candidate(candidate)
    assert whole_capture(owner, expected=generation).health == "degraded"


def test_capture_is_generation_bound_and_enforces_row_and_byte_budgets(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, first_batch())
    with pytest.raises(NodeGenerationChanged):
        whole_capture(owner, expected=generation + 1)
    with pytest.raises(NodeCaptureOverflow, match="document"):
        owner.capture(NodeCaptureRequest(
            document_prefixes=("",), max_documents=1, max_bytes=1_000,
        ))
    with pytest.raises(NodeCaptureOverflow, match="byte"):
        whole_capture(owner, expected=generation, max_bytes=1)


def test_document_prefix_is_case_sensitive_and_segment_exact(tmp_path):
    owner = store(tmp_path)
    admit(owner, NodeInputBatch(documents=(
        NodeDocument("chats/A/meta.json", 1, False, b"upper"),
        NodeDocument("chats/a/meta.json", 1, False, b"lower"),
        NodeDocument("chats/AA/meta.json", 1, False, b"longer"),
    )))
    captured = owner.capture(NodeCaptureRequest(
        document_prefixes=("chats/A",), max_documents=10, max_bytes=1_000,
    ))
    assert [(row.path, row.payload) for row in captured.documents] == [
        ("chats/A/meta.json", b"upper"),
    ]


def test_absent_chat_selector_carries_scope_and_root_fences(tmp_path):
    owner = store(tmp_path)
    request = NodeCaptureRequest(
        exact_document_paths=("chats/missing/meta.json",), max_bytes=1_000,
    )
    before = owner.capture(request)
    assert {(row.scope_kind, row.scope_id, row.generation)
            for row in before.scope_positions} == {
                ("chat", "missing", 0), ("root", "", 0),
            }
    generation = admit(owner, NodeInputBatch(documents=(
        NodeDocument("chats/missing/meta.json", 1, False, b"now-present"),
    )))
    after = owner.capture(request)
    assert {(row.scope_kind, row.scope_id, row.generation)
            for row in after.scope_positions} == {
                ("chat", "missing", generation),
                ("root", "", generation),
            }


def test_frontier_delta_cannot_make_default_capture_permanently_overflow(tmp_path):
    owner = store(tmp_path)
    frontiers = tuple(NodeFrontier(f"frontier-{index:03d}", None, index, 0)
                      for index in range(128))
    admitted = admit(owner, NodeInputBatch(frontiers=frontiers))
    candidate = owner.begin_candidate(created_ns=3)
    owner.seal_candidate(candidate, NodeInputBatch(frontiers=(
        NodeFrontier("overflow", None, 1, 0),
    )), observed_ns=4)
    with pytest.raises(NodeInputError, match="frontiers exceed"):
        owner.admit_candidate(candidate)
    captured = owner.capture(NodeCaptureRequest(
        expected_generation=admitted, max_bytes=20_000,
    ))
    assert len(captured.frontiers) == 128


def test_change_pages_are_scoped_and_foreign_or_future_cursors_reset(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, first_batch())
    incarnation = whole_capture(owner).database_incarnation
    page = owner.capture_changes(
        database_incarnation=incarnation, after_cursor=0, limit=1)
    assert page.status == "ok"
    assert page.has_more
    assert len(page.changes) == 1
    assert page.changes[0].generation == generation
    rest = owner.capture_changes(
        database_incarnation=incarnation, after_cursor=page.after_cursor, limit=10)
    assert {(row.scope_kind, row.scope_id) for row in page.changes + rest.changes} == {
        ("chat", "chat-a"), ("root", ""),
    }
    assert owner.capture_changes(
        database_incarnation="other", after_cursor=0).status == "reset_required"
    assert owner.capture_changes(
        database_incarnation=incarnation,
        after_cursor=rest.current_cursor + 1,
    ).status == "reset_required"


def test_compacted_change_cursor_requires_reset(tmp_path):
    owner = store(tmp_path)
    values = tuple(NodeVisibility(f"chat-{index:05d}", True) for index in range(4_100))
    admit(owner, NodeInputBatch(visibility=values, visibility_mode="replace"))
    captured = whole_capture(owner)
    page = owner.capture_changes(
        database_incarnation=captured.database_incarnation, after_cursor=0)
    assert page.status == "reset_required"
    assert page.minimum_cursor == 4
    resumed = owner.capture_changes(
        database_incarnation=captured.database_incarnation,
        after_cursor=page.minimum_cursor,
    )
    assert resumed.status == "ok"
    assert resumed.changes[0].seq == page.minimum_cursor + 1


def test_old_candidates_are_reclaimable_and_admitted_cut_survives(tmp_path):
    owner = store(tmp_path)
    admitted = admit(owner, first_batch(), created=5, observed=6)
    building = owner.begin_candidate(created_ns=10)
    sealed = owner.begin_candidate(created_ns=11)
    owner.seal_candidate(sealed, NodeInputBatch(
        documents=(NodeDocument("accounts/later.json", 1, False, b"later"),),
    ), observed_ns=12)
    assert owner.reclaim_candidates(before_ns=12) == 2
    assert whole_capture(owner, expected=admitted).generation == admitted
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation IN (?,?) "
            "ORDER BY generation", (building, sealed)).fetchall() == [
                ("abandoned",), ("abandoned",),
            ]


def test_candidate_and_generation_metadata_are_bounded(tmp_path, monkeypatch):
    from agentbridge.node import store as store_module

    owner = store(tmp_path)
    monkeypatch.setattr(store_module, "MAX_ACTIVE_CANDIDATES", 2)
    owner.begin_candidate(created_ns=1)
    owner.begin_candidate(created_ns=2)
    with pytest.raises(NodeInputError, match="too many active"):
        owner.begin_candidate(created_ns=3)

    # Reclaim the probes, then prove pruning old generation metadata does not
    # discard a current row last updated by that old generation.
    assert owner.reclaim_candidates(before_ns=3) == 2
    monkeypatch.setattr(store_module, "MAX_GENERATION_HISTORY", 2)
    first = admit(owner, NodeInputBatch(
        documents=(NodeDocument("accounts/stable.json", 1, False, b"stable"),),
    ), created=4, observed=5)
    for index in range(3):
        admit(owner, NodeInputBatch(), created=6 + index * 2,
              observed=7 + index * 2)
    assert whole_capture(owner).documents[0].payload == b"stable"
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT 1 FROM node_generations WHERE generation=?", (first,)
        ).fetchone() is None


def test_batch_rejects_duplicates_and_invalid_tombstones():
    with pytest.raises(NodeInputError, match="absent payload"):
        NodeDocument("accounts/a.json", 1, True, b"payload")
    row = NodeDocument("accounts/a.json", 1, False, b"a")
    with pytest.raises(NodeInputError, match="duplicate document"):
        NodeInputBatch(documents=(row, row))


def test_store_revalidates_mutated_frozen_values(tmp_path):
    owner = store(tmp_path)
    batch = NodeInputBatch(documents=(
        NodeDocument("accounts/a.json", 1, False, b"a"),
    ))
    object.__setattr__(batch.documents[0], "payload", bytearray(b"changed"))
    candidate = owner.begin_candidate(created_ns=1)
    with pytest.raises(NodeInputError, match="payload"):
        owner.seal_candidate(candidate, batch, observed_ns=2)

    request = NodeCaptureRequest(max_bytes=10)
    object.__setattr__(request, "max_bytes", True)
    with pytest.raises(NodeInputError, match="byte limit"):
        owner.capture(request)


def test_empty_inactive_v1_database_migrates_without_rebinding(tmp_path):
    path = tmp_path / "node" / "replica.sqlite3"
    path.parent.mkdir()
    wanted = identity()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE node_schema(singleton INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,protocol_version INTEGER NOT NULL);
            CREATE TABLE node_identity(singleton INTEGER PRIMARY KEY,
                provider_endpoint TEXT,root TEXT,principal TEXT,machine TEXT,
                identity_digest TEXT);
            CREATE TABLE node_meta(singleton INTEGER PRIMARY KEY,
                database_incarnation TEXT,admitted_generation INTEGER,health TEXT,
                last_attempt_ns INTEGER,last_success_ns INTEGER,pending_mutations INTEGER);
            CREATE TABLE node_generations(generation INTEGER PRIMARY KEY,state TEXT,
                created_ns INTEGER);
            CREATE UNIQUE INDEX one_admitted_node_generation
                ON node_generations(state) WHERE state='admitted';
            CREATE TABLE remote_docs(generation INTEGER,path TEXT,seq INTEGER,
                deleted INTEGER,payload BLOB);
            CREATE TABLE remote_log_rows(generation INTEGER,id INTEGER,chat_id TEXT,
                log_name TEXT,payload BLOB);
            CREATE INDEX remote_log_chat
                ON remote_log_rows(generation,chat_id,log_name,id);
            CREATE TABLE remote_chat_visibility(generation INTEGER,chat_id TEXT);
            CREATE TABLE remote_frontiers(generation INTEGER,name TEXT,epoch TEXT,
                cursor INTEGER,minimum_cursor INTEGER);
            CREATE TABLE scope_versions(scope_kind TEXT,scope_id TEXT,
                generation INTEGER,pending_reason TEXT);
            CREATE TABLE local_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                generation INTEGER,scope_kind TEXT,scope_id TEXT,kind TEXT);
        """)
        conn.execute("INSERT INTO node_schema VALUES(1,1,1)")
        conn.execute("INSERT INTO node_identity VALUES(1,?,?,?,?,?)", (
            wanted.provider_endpoint, wanted.root, wanted.principal,
            wanted.machine, wanted.digest,
        ))
        conn.execute(
            "INSERT INTO node_meta VALUES(1,'incarnation-v1',0,'inactive',0,0,0)")
    owner = NodeStore(path, wanted)
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.database_incarnation == "incarnation-v1"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version,protocol_version FROM node_schema").fetchone() == (2, 1)
        assert conn.execute(
            "SELECT minimum_cursor FROM local_change_state").fetchone() == (0,)

