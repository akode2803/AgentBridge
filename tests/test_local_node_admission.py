import sqlite3
import threading

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
from agentbridge.node.protocol import ProtocolError, ReplicaIdentity
from agentbridge.node.store import NodeStore
from agentbridge.node.wire import capture_response, parse_capture_request


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


def test_candidate_can_require_the_captured_admitted_generation(tmp_path):
    owner = store(tmp_path)
    first = admit(owner, NodeInputBatch(), created=1, observed=2)
    with pytest.raises(NodeGenerationChanged, match="admitted generation changed"):
        owner.begin_candidate(created_ns=3, expected_generation=0)
    candidate = owner.begin_candidate(
        created_ns=4, expected_generation=first,
    )
    assert candidate == first + 1


def test_refresh_health_preserves_admission_and_success_time(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(), created=1, observed=2)
    assert owner.note_refresh_state("catching_up", attempted_ns=7) == "catching_up"
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.admitted_generation == generation
    assert status.last_success_ns == 2
    assert status.last_attempt_ns == 7
    assert status.health == "catching_up"
    assert owner.note_refresh_state("ready", attempted_ns=9) == "ready"
    ready = owner.status(node_epoch="node", started_ns=1)
    assert ready.admitted_generation == generation
    assert ready.last_success_ns == 9
    assert ready.health == "ready"

    next_generation = admit(
        owner, NodeInputBatch(), created=10, observed=11,
    )
    with pytest.raises(NodeGenerationChanged, match="admitted generation changed"):
        owner.note_refresh_state(
            "ready", attempted_ns=12, expected_generation=generation,
        )
    current = owner.status(node_epoch="node", started_ns=1)
    assert current.admitted_generation == next_generation
    assert current.last_success_ns == 11


def test_refresh_token_orders_attempts_with_the_same_generation(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(), created=1, observed=2)
    older = owner.begin_refresh(
        attempted_ns=10, expected_generation=generation,
    )
    newer = owner.begin_refresh(
        attempted_ns=20, expected_generation=generation,
    )
    assert (older, newer) == (10, 20)
    with pytest.raises(NodeGenerationChanged, match="refresh changed"):
        owner.note_refresh_state(
            "ready", attempted_ns=older, expected_generation=generation,
            expected_attempt_ns=older,
        )
    assert owner.note_refresh_state(
        "catching_up", attempted_ns=newer, expected_generation=generation,
        expected_attempt_ns=newer,
    ) == "catching_up"
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.last_attempt_ns == newer
    assert status.last_success_ns == 2
    assert status.health == "catching_up"


def test_newer_refresh_invalidates_an_older_candidate(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(), created=1, observed=2)
    older = owner.begin_refresh(
        attempted_ns=10, expected_generation=generation,
    )
    candidate = owner.begin_candidate(
        created_ns=older, expected_generation=generation,
        expected_attempt_ns=older,
    )
    owner.seal_candidate(candidate, NodeInputBatch(), observed_ns=11)
    newer = owner.begin_refresh(
        attempted_ns=20, expected_generation=generation,
    )
    with pytest.raises(NodeGenerationChanged, match="refresh changed"):
        owner.admit_candidate(candidate, expected_attempt_ns=older)
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.admitted_generation == generation
    assert status.last_attempt_ns == newer
    assert status.health == "catching_up"


def test_partial_admission_keeps_catching_up_health(tmp_path):
    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=1, expected_generation=0)
    owner.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b"{}"),),
    ), observed_ns=2)
    owner.admit_candidate(generation, health="catching_up")
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.admitted_generation == generation
    assert status.health == "catching_up"
    assert status.last_success_ns == 2


def test_provider_sized_log_names_are_valid_node_identities(tmp_path):
    owner = store(tmp_path)
    log_name = "x" * 4_096
    generation = admit(owner, NodeInputBatch(log_rows=(
        NodeLogRow(1, "chat-a", log_name, b"one"),
    )))
    captured = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("chat-a", log_name, limit=1),),
        expected_generation=generation, max_log_rows=1,
        max_bytes=16 * 1024,
    ))
    assert captured.log_pages[0].rows == (
        NodeLogRow(1, "chat-a", log_name, b"one"),
    )


def test_provider_valid_unicode_raw_identities_round_trip(tmp_path):
    owner = store(tmp_path)
    joiner = "\u200d"
    path = f"users/a{joiner}.json"
    chat_id = f"chat{joiner}a"
    log_name = f"a{joiner}b.jsonl"
    generation = admit(owner, NodeInputBatch(
        documents=(NodeDocument(path, 1, False, b"{}"),),
        log_rows=(NodeLogRow(1, chat_id, log_name, b"one"),),
        visibility=(NodeVisibility(chat_id, True),),
    ))
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=(path,),
        log_requests=(NodeLogRequest(chat_id, log_name, limit=1),),
        include_visibility=True, expected_generation=generation,
        max_documents=1, max_log_rows=1, max_bytes=16 * 1024,
    ))
    assert captured.documents[0].path == path
    assert captured.log_pages[0].rows[0].log_name == log_name
    assert captured.visibility == (chat_id,)


def test_provider_sized_names_produce_bounded_reusable_wire_cursors(tmp_path):
    owner = store(tmp_path)
    chat_id = "\n" * 1_024
    log_name = '"' * 4_096
    generation = admit(owner, NodeInputBatch(
        log_rows=(NodeLogRow(1, chat_id, log_name, b"one"),),
        visibility=(NodeVisibility(chat_id, True),),
    ))
    captured = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest(chat_id, log_name, limit=1),),
        include_visibility=True, expected_generation=generation,
        max_log_rows=1, max_bytes=32 * 1024,
    ))
    response = capture_response(captured)
    log_cursor = response["logs"][0]["cursor"]
    visibility_cursor = response["visibility_cursor"]
    assert len(log_cursor.encode()) < 8 * 1024
    assert len(visibility_cursor.encode()) < 8 * 1024
    request = parse_capture_request({
        "protocol_version": 1,
        "expected_capture": response["capture"],
        "exact_document_paths": [],
        "document_prefixes": [],
        "logs": [{
            "chat_id": chat_id, "log_name": log_name,
            "cursor": log_cursor, "limit": 1,
        }],
        "include_visibility": True,
        "visibility_cursor": visibility_cursor,
        "visibility_limit": 1,
        "frontier_names": [],
        "max_documents": 0,
        "max_log_rows": 1,
        "max_bytes": 32 * 1024,
    }, database_incarnation=captured.database_incarnation)
    assert request.log_requests[0].after_id == 1
    assert request.visibility_after == chat_id

    bad = {
        "protocol_version": 1,
        "expected_capture": response["capture"],
        "exact_document_paths": [], "document_prefixes": [],
        "logs": [{
            "chat_id": "\ud800", "log_name": log_name,
            "cursor": log_cursor, "limit": 1,
        }],
        "include_visibility": False, "visibility_cursor": None,
        "visibility_limit": 1, "frontier_names": [],
        "max_documents": 0, "max_log_rows": 1, "max_bytes": 32 * 1024,
    }
    with pytest.raises(ProtocolError, match="invalid log request"):
        parse_capture_request(
            bad, database_incarnation=captured.database_incarnation,
        )


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


def test_staged_candidate_chunks_remain_private_and_resume_after_reopen(tmp_path):
    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=10)
    first = NodeInputBatch(
        documents=(NodeDocument("accounts/alice.json", 1, False, b"alice"),),
        log_rows=(NodeLogRow(1, "chat-a", "alice", b"one"),),
        documents_mode="replace", logs_mode="replace",
        visibility_mode="replace", frontiers_mode="replace",
    )
    assert owner.stage_candidate_batch(generation, "docs:0001", first)
    assert not owner.stage_candidate_batch(generation, "docs:0001", first)

    reopened = NodeStore(owner.path, identity())
    before = whole_capture(reopened)
    assert before.generation == 0
    assert before.documents == ()
    assert before.log_pages[0].rows == ()

    second = NodeInputBatch(
        documents=(NodeDocument("chats/chat-a/meta.json", 2, False, b"meta"),),
        visibility=(NodeVisibility("chat-a", True),),
        frontiers=(NodeFrontier("provider-source-ledger", "epoch", 8, 0),),
        documents_mode="replace", logs_mode="replace",
        visibility_mode="replace", frontiers_mode="replace",
    )
    assert reopened.stage_candidate_batch(generation, "docs:0002", second)
    reopened.seal_staged_candidate(generation, observed_ns=20)
    reopened.admit_candidate(generation)
    after = whole_capture(reopened, expected=generation)
    assert [row.path for row in after.documents] == [
        "accounts/alice.json", "chats/chat-a/meta.json",
    ]
    assert [row.id for row in after.log_pages[0].rows] == [1]
    assert after.visibility == ("chat-a",)


def test_staged_chunk_id_and_overlap_fail_without_partial_stage(tmp_path):
    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=1)
    first = NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b"one"),),
    )
    owner.stage_candidate_batch(generation, "chunk", first)
    with pytest.raises(NodeInputError, match="reused"):
        owner.stage_candidate_batch(generation, "chunk", NodeInputBatch(
            documents=(NodeDocument("users/b.json", 1, False, b"two"),),
        ))
    with pytest.raises(NodeInputError, match="overlap"):
        owner.stage_candidate_batch(generation, "other", NodeInputBatch(
            documents=(NodeDocument("users/a.json", 1, False, b"one"),),
        ))
    owner.seal_staged_candidate(generation, observed_ns=2)
    owner.admit_candidate(generation)
    captured = owner.capture(NodeCaptureRequest(
        document_prefixes=("users",), max_documents=10, max_bytes=1_000,
    ))
    assert [(row.path, row.payload) for row in captured.documents] == [
        ("users/a.json", b"one"),
    ]


def test_staged_chunks_cannot_change_replacement_contract(tmp_path):
    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=1)
    owner.stage_candidate_batch(generation, "first", NodeInputBatch(
        documents_mode="replace", logs_mode="replace",
        visibility_mode="replace", frontiers_mode="replace",
    ))
    with pytest.raises(NodeInputError, match="modes changed"):
        owner.stage_candidate_batch(generation, "second", NodeInputBatch())


def test_staged_frontier_updates_are_cumulative_and_chunk_retries_are_inert(tmp_path):
    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=1)
    first = NodeInputBatch(frontiers=(
        NodeFrontier("provider-source-ledger", "epoch", 10, 2),
    ))
    second = NodeInputBatch(frontiers=(
        NodeFrontier("provider-source-ledger", "epoch", 20, 3),
        NodeFrontier("provider-documents", "epoch", 7, 1),
    ))
    assert owner.stage_candidate_batch(generation, "ledger:0001", first)
    assert owner.stage_candidate_batch(generation, "ledger:0002", second)
    assert not owner.stage_candidate_batch(generation, "ledger:0001", first)

    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT frontier_count FROM node_generations WHERE generation=?",
            (generation,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT name,cursor,minimum_cursor FROM candidate_frontiers "
            "WHERE generation=? ORDER BY name", (generation,),
        ).fetchall() == [
            ("provider-documents", 7, 1),
            ("provider-source-ledger", 20, 3),
        ]


def test_staged_budget_failure_rolls_back_the_whole_chunk(tmp_path, monkeypatch):
    from agentbridge.node import store as store_module

    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=1)
    monkeypatch.setattr(store_module, "MAX_STAGED_DOCUMENTS", 1)
    first = NodeInputBatch(documents=(
        NodeDocument("users/a.json", 1, False, b"a"),
    ))
    owner.stage_candidate_batch(generation, "first", first)
    with pytest.raises(NodeInputError, match="staged candidate exceeds"):
        owner.stage_candidate_batch(generation, "overflow", NodeInputBatch(
            documents=(NodeDocument("users/b.json", 1, False, b"b"),),
            frontiers=(NodeFrontier("must-rollback", None, 2, 0),),
        ))

    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT path FROM candidate_docs WHERE generation=?", (generation,),
        ).fetchall() == [("users/a.json",)]
        assert conn.execute(
            "SELECT name FROM candidate_frontiers WHERE generation=?", (generation,),
        ).fetchall() == []
        assert conn.execute(
            "SELECT chunk_id FROM candidate_chunks WHERE generation=?", (generation,),
        ).fetchall() == [("first",)]
        assert conn.execute(
            "SELECT document_count,frontier_count FROM node_generations "
            "WHERE generation=?", (generation,),
        ).fetchone() == (1, 0)
    assert not owner.stage_candidate_batch(generation, "first", first)


def test_empty_staged_chunks_have_a_durable_candidate_limit(tmp_path, monkeypatch):
    from agentbridge.node import store as store_module

    owner = store(tmp_path)
    generation = owner.begin_candidate(created_ns=1)
    monkeypatch.setattr(store_module, "MAX_STAGED_CHUNKS", 1)
    empty = NodeInputBatch()
    assert owner.stage_candidate_batch(generation, "empty:0001", empty)
    assert not owner.stage_candidate_batch(generation, "empty:0001", empty)
    with pytest.raises(NodeInputError, match="staged candidate exceeds"):
        owner.stage_candidate_batch(generation, "empty:0002", empty)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT chunk_count FROM node_generations WHERE generation=?",
            (generation,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT chunk_id FROM candidate_chunks WHERE generation=?",
            (generation,),
        ).fetchall() == [("empty:0001",)]


def test_staged_candidate_cannot_publish_after_a_newer_refresh(tmp_path):
    owner = store(tmp_path)
    admitted = admit(owner, NodeInputBatch(), created=1, observed=2)
    older = owner.begin_refresh(attempted_ns=10, expected_generation=admitted)
    candidate = owner.begin_candidate(
        created_ns=older, expected_generation=admitted,
        expected_attempt_ns=older,
    )
    owner.stage_candidate_batch(candidate, "part:0001", NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b"a"),),
    ))
    owner.seal_staged_candidate(candidate, observed_ns=11)
    newer = owner.begin_refresh(attempted_ns=20, expected_generation=admitted)

    with pytest.raises(NodeGenerationChanged, match="refresh changed"):
        owner.admit_candidate(candidate, expected_attempt_ns=older)
    assert whole_capture(owner, expected=admitted).documents == ()
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?", (candidate,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM candidate_chunks WHERE generation=?", (candidate,),
        ).fetchone() == (0,)
    assert owner.status(node_epoch="node", started_ns=1).last_attempt_ns == newer


def test_capture_sees_only_the_old_cut_during_staged_promotion(tmp_path, monkeypatch):
    owner = store(tmp_path)
    admitted = admit(owner, NodeInputBatch(documents=(
        NodeDocument("users/current.json", 1, False, b"old"),
    )), created=1, observed=2)
    candidate = owner.begin_candidate(created_ns=3, expected_generation=admitted)
    owner.stage_candidate_batch(candidate, "all", NodeInputBatch(
        documents=(NodeDocument("users/current.json", 2, False, b"new"),),
        documents_mode="replace", logs_mode="replace",
        visibility_mode="replace", frontiers_mode="replace",
    ))
    owner.seal_staged_candidate(candidate, observed_ns=4)

    applied = threading.Event()
    release = threading.Event()
    failures = []
    original = NodeStore._apply_candidate

    def blocking_apply(conn, *args):
        original(conn, *args)
        applied.set()
        if not release.wait(5):
            raise RuntimeError("test did not release promotion")

    monkeypatch.setattr(NodeStore, "_apply_candidate", staticmethod(blocking_apply))

    def promote():
        try:
            owner.admit_candidate(candidate)
        except BaseException as exc:  # pragma: no cover - reported by assertion
            failures.append(exc)

    worker = threading.Thread(target=promote, daemon=True)
    worker.start()
    try:
        assert applied.wait(5)
        during = owner.capture(NodeCaptureRequest(
            document_prefixes=("users",), max_documents=10, max_bytes=1_000,
        ))
        assert during.generation == admitted
        assert during.documents[0].payload == b"old"
    finally:
        release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    after = owner.capture(NodeCaptureRequest(
        document_prefixes=("users",), max_documents=10, max_bytes=1_000,
    ))
    assert after.generation == candidate
    assert after.documents[0].payload == b"new"


def test_log_replacement_removes_rows_absent_from_complete_candidate(tmp_path):
    owner = store(tmp_path)
    first = admit(owner, NodeInputBatch(log_rows=(
        NodeLogRow(1, "chat-a", "alice", b"one"),
        NodeLogRow(2, "chat-b", "bob", b"two"),
    )))
    candidate = owner.begin_candidate(created_ns=3, expected_generation=first)
    owner.stage_candidate_batch(candidate, "all", NodeInputBatch(
        log_rows=(NodeLogRow(1, "chat-a", "alice", b"one"),),
        logs_mode="replace",
    ))
    owner.seal_staged_candidate(candidate, observed_ns=4)
    owner.admit_candidate(candidate)
    assert owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("chat-b", "bob", limit=10),),
        max_log_rows=10, max_bytes=1_000,
    )).log_pages[0].rows == ()


def test_incremental_admission_uses_the_full_staged_replica_caps(
        tmp_path, monkeypatch):
    from agentbridge.node import store as store_module

    owner = store(tmp_path)
    monkeypatch.setattr(store_module, "MAX_STAGED_DOCUMENTS", 2)
    monkeypatch.setattr(store_module, "MAX_STAGED_VISIBILITY", 2)
    first = admit(owner, NodeInputBatch(
        documents=(
            NodeDocument("users/a.json", 1, False, b"a"),
            NodeDocument("users/b.json", 1, False, b"b"),
        ),
        visibility=(
            NodeVisibility("chat-a", True),
            NodeVisibility("chat-b", True),
        ),
        documents_mode="replace", visibility_mode="replace",
    ))
    candidate = owner.begin_candidate(created_ns=3, expected_generation=first)
    owner.seal_candidate(candidate, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 2, False, b"new"),),
        visibility=(NodeVisibility("chat-a", True),),
    ), observed_ns=4)
    owner.admit_candidate(candidate)
    captured = owner.capture(NodeCaptureRequest(
        document_prefixes=("users",), max_documents=10, max_bytes=1_000,
        include_visibility=True, visibility_limit=10,
    ))
    assert [(row.path, row.payload) for row in captured.documents] == [
        ("users/a.json", b"new"), ("users/b.json", b"b"),
    ]
    assert captured.visibility == ("chat-a", "chat-b")


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
            "SELECT version,protocol_version FROM node_schema").fetchone() == (4, 1)
        assert conn.execute(
            "SELECT minimum_cursor FROM local_change_state").fetchone() == (0,)

