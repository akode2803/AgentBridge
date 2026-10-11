"""Inactive transport-to-Store recovery and equivalence checks."""

import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

import agentbridge.node.store as node_store
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


def sealed_claimed_recovery(tmp_path, *, with_fence=False):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    if with_fence:
        intent = root.begin((source_selectors.Selector(
            "doc_exact", "users/earlier.json"),))
        root.complete(intent)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)
    return owner, root, claim, sealed


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


def test_coordinator_admission_consumes_claim_and_is_idempotent(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    intent = root.begin((source_selectors.Selector(
        "doc_exact", "users/earlier.json"),))
    root.complete(intent)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)

    admitted = root.admit_node_recovery(owner, claim, sealed.recovery_id)
    repeated = root.admit_node_recovery(owner, claim, sealed.recovery_id)

    assert admitted == sealed.generation == repeated
    assert owner.capture(NodeCaptureRequest(
        document_prefixes=("",), include_visibility=True,
        visibility_limit=10, max_documents=10, max_bytes=8192,
    )).generation == admitted
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.health == "ready" and status.pending_mutations == 0
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM claimed_recoveries").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM node_mutation_fences").fetchone() == (0,)
        assert conn.execute(
            "SELECT admitted_generation,claim_digest FROM node_recovery_receipts "
            "WHERE recovery_id=?", (sealed.recovery_id,),
        ).fetchone() == (admitted, claim.claim_digest)


def test_coordinator_admission_failure_rolls_back_permit_and_publication(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)

    def fail_publish(*_args, **_kwargs):
        raise OSError("injected publication failure")

    monkeypatch.setattr(NodeStore, "_apply_candidate", staticmethod(fail_publish))
    with pytest.raises(OSError, match="injected publication failure"):
        root.admit_node_recovery(owner, claim, sealed.recovery_id)

    assert owner.capture(NodeCaptureRequest(max_bytes=1024)).generation == 0
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute("SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (sealed.generation,),
        ).fetchone() == ("sealed",)
        assert conn.execute("SELECT count(*) FROM node_recovery_claims").fetchone() == (1,)


@pytest.mark.parametrize("with_fence", (False, True))
@pytest.mark.parametrize(
    "failure",
    (
        "permit_insert", "candidate_apply", "generation_transition",
        "permit_consume", "fence_consumption", "receipt_insert",
        "post_receipt",
    ),
)
def test_admission_failure_matrix_rolls_back_and_retries(
        tmp_path, monkeypatch, with_fence, failure):
    owner, root, claim, sealed = sealed_claimed_recovery(
        tmp_path, with_fence=with_fence)
    original_prune = NodeStore._prune_generation_history
    original_apply = NodeStore._apply_candidate
    original_record = NodeStore._record_changes
    original_clear = NodeStore._clear_candidate_rows

    if failure == "permit_insert":
        def inject(conn):
            original_prune(conn)
            conn.execute("""CREATE TEMP TRIGGER injected_permit_insert_failure
                BEFORE INSERT ON node_admission_permits BEGIN
                    SELECT RAISE(ABORT,'injected permit insert failure');
                END""")

        monkeypatch.setattr(
            NodeStore, "_prune_generation_history", staticmethod(inject))
    elif failure == "candidate_apply":
        def inject(*_args, **_kwargs):
            raise OSError("injected candidate apply failure")

        monkeypatch.setattr(NodeStore, "_apply_candidate", staticmethod(inject))
    elif failure == "generation_transition":
        def inject(conn, *args):
            original_apply(conn, *args)
            conn.execute("""CREATE TEMP TRIGGER injected_generation_failure
                BEFORE UPDATE OF state ON node_generations
                WHEN NEW.state='admitted' BEGIN
                    SELECT RAISE(ABORT,'injected generation failure');
                END""")

        monkeypatch.setattr(NodeStore, "_apply_candidate", staticmethod(inject))
    elif failure == "permit_consume":
        def inject(conn, *args):
            original_apply(conn, *args)
            conn.execute("""CREATE TEMP TRIGGER injected_permit_consume_failure
                BEFORE DELETE ON node_admission_permits BEGIN
                    SELECT RAISE(ABORT,'injected permit consume failure');
                END""")

        monkeypatch.setattr(NodeStore, "_apply_candidate", staticmethod(inject))
    elif failure == "fence_consumption":
        def inject(*_args, **_kwargs):
            raise OSError("injected fence consumption failure")

        monkeypatch.setattr(NodeStore, "_record_changes", staticmethod(inject))
    elif failure == "receipt_insert":
        def inject(conn, *args):
            original_record(conn, *args)
            conn.execute("""CREATE TEMP TRIGGER injected_receipt_failure
                BEFORE INSERT ON node_recovery_receipts BEGIN
                    SELECT RAISE(ABORT,'injected receipt failure');
                END""")

        monkeypatch.setattr(NodeStore, "_record_changes", staticmethod(inject))
    else:
        def inject(*_args, **_kwargs):
            raise OSError("injected post receipt failure")

        monkeypatch.setattr(
            NodeStore, "_clear_candidate_rows", staticmethod(inject))

    with pytest.raises((OSError, sqlite3.IntegrityError), match="injected"):
        root.admit_node_recovery(owner, claim, sealed.recovery_id)

    status = owner.status(node_epoch="node", started_ns=1)
    assert status.admitted_generation == 0
    assert status.pending_mutations == int(with_fence)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (sealed.generation,),
        ).fetchone() == ("sealed",)
        assert conn.execute(
            "SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_receipts").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM node_mutation_fences").fetchone() == (
                int(with_fence),)

    monkeypatch.setattr(
        NodeStore, "_prune_generation_history", staticmethod(original_prune))
    monkeypatch.setattr(
        NodeStore, "_apply_candidate", staticmethod(original_apply))
    monkeypatch.setattr(
        NodeStore, "_record_changes", staticmethod(original_record))
    monkeypatch.setattr(
        NodeStore, "_clear_candidate_rows", staticmethod(original_clear))
    assert root.admit_node_recovery(
        owner, claim, sealed.recovery_id) == sealed.generation


@pytest.mark.parametrize(
    "mutation",
    (
        "recovery_id='ffffffffffffffffffffffffffffffffffffffffffffffff'",
        "database_incarnation='replacement'",
        "base_generation=base_generation+1",
        "destination_generation=destination_generation+1",
        "claim_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
        "root_revision=root_revision+1",
        "old_pending_count=old_pending_count+1",
    ),
)
def test_exact_permit_binding_rejects_each_mismatch(
        tmp_path, monkeypatch, mutation):
    owner, root, claim, sealed = sealed_claimed_recovery(
        tmp_path, with_fence=True)
    original_apply = NodeStore._apply_candidate

    def corrupt_permit(conn, *args):
        original_apply(conn, *args)
        conn.execute(f"UPDATE node_admission_permits SET {mutation}")

    monkeypatch.setattr(
        NodeStore, "_apply_candidate", staticmethod(corrupt_permit))
    with pytest.raises(
            sqlite3.IntegrityError, match="requires coordinator admission"):
        root.admit_node_recovery(owner, claim, sealed.recovery_id)

    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (sealed.generation,),
        ).fetchone() == ("sealed",)
        assert conn.execute(
            "SELECT count(*) FROM node_mutation_fences").fetchone() == (1,)

    monkeypatch.setattr(
        NodeStore, "_apply_candidate", staticmethod(original_apply))
    assert root.admit_node_recovery(
        owner, claim, sealed.recovery_id) == sealed.generation


def test_late_admission_failure_preserves_existing_admitted_cut(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    base = owner.begin_candidate(created_ns=1)
    owner.seal_candidate(base, NodeInputBatch(documents=(
        NodeDocument("users/base.json", 1, False, b"base"),
    )), observed_ns=2)
    owner.admit_candidate(base)
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)
    request = NodeCaptureRequest(
        document_prefixes=("",), include_visibility=True,
        visibility_limit=10, max_documents=10, max_bytes=8192,
    )
    before_status = owner.status(node_epoch="node", started_ns=1)
    before_capture = owner.capture(request)
    with sqlite3.connect(owner.path) as conn:
        before_dump = tuple(conn.iterdump())
    original_clear = NodeStore._clear_candidate_rows

    def fail_after_receipt(*_args, **_kwargs):
        raise OSError("injected late admission failure")

    monkeypatch.setattr(
        NodeStore, "_clear_candidate_rows", staticmethod(fail_after_receipt))
    with pytest.raises(OSError, match="injected late admission failure"):
        root.admit_node_recovery(owner, claim, sealed.recovery_id)

    reopened = NodeStore(owner.path, identity())
    assert reopened.status(node_epoch="node", started_ns=1) == before_status
    assert reopened.capture(request) == before_capture
    with sqlite3.connect(owner.path) as conn:
        assert tuple(conn.iterdump()) == before_dump

    monkeypatch.setattr(
        NodeStore, "_clear_candidate_rows", staticmethod(original_clear))
    assert root.admit_node_recovery(
        reopened, claim, sealed.recovery_id) == sealed.generation


def test_mutation_after_collection_before_seal_retires_private_work(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    executor = ClaimedRecoveryExecutor(owner, tx, root)
    result = executor.step(None, attempted_ns=10)
    for attempted in range(11, 30):
        result = InactiveRecoveryExecutor(owner, tx).step(
            result.recovery_id, attempted_ns=attempted)
        if result.action == "events":
            break
    assert result.state == "building" and result.action == "events"

    intent = root.begin((source_selectors.Selector(
        "doc_exact", "users/later.json"),))
    root.complete(intent)

    with pytest.raises(NodeGenerationChanged):
        InactiveRecoveryExecutor(owner, tx).step(
            result.recovery_id, attempted_ns=31)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (result.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries WHERE recovery_id=?",
            (result.recovery_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)


def test_mutation_after_provider_page_before_stage_rejects_stale_page(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    started = ClaimedRecoveryExecutor(owner, tx, root).step(
        None, attempted_ns=10)
    original = tx.recovery_page

    def move_after_response(*args, **kwargs):
        page = original(*args, **kwargs)
        intent = root.begin((source_selectors.Selector(
            "doc_exact", "users/later.json"),))
        root.complete(intent)
        return page

    monkeypatch.setattr(tx, "recovery_page", move_after_response)
    with pytest.raises(NodeGenerationChanged):
        InactiveRecoveryExecutor(owner, tx).step(
            started.recovery_id, attempted_ns=11)

    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (started.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM candidate_docs WHERE generation=?",
            (started.generation,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)


def test_mutation_after_close_token_before_seal_rejects_stale_close(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    result = ClaimedRecoveryExecutor(owner, tx, root).step(
        None, attempted_ns=10)
    for attempted in range(11, 30):
        result = InactiveRecoveryExecutor(owner, tx).step(
            result.recovery_id, attempted_ns=attempted)
        if result.action == "events":
            break
    assert result.state == "building" and result.action == "events"
    original = tx.recovery_fence
    calls = 0

    def move_after_close_token():
        nonlocal calls
        calls += 1
        if calls == 2:
            intent = root.begin((source_selectors.Selector(
                "doc_exact", "users/later.json"),))
            root.complete(intent)
        return original()

    monkeypatch.setattr(tx, "recovery_fence", move_after_close_token)
    with pytest.raises(NodeGenerationChanged):
        InactiveRecoveryExecutor(owner, tx).step(
            result.recovery_id, attempted_ns=31)
    assert calls == 2

    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (result.generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries WHERE recovery_id=?",
            (result.recovery_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)


def test_admission_serializes_before_later_mutation_and_stale_sqlite_writer(
        tmp_path, monkeypatch):
    owner, root, claim, sealed = sealed_claimed_recovery(tmp_path)
    original_apply = NodeStore._apply_candidate
    entered = threading.Event()
    release = threading.Event()
    mutation_attempted = threading.Event()
    mutation_done = threading.Event()
    admission_result = []
    mutation_result = []
    failures = []

    def blocked_apply(conn, *args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("admission test barrier timed out")
        return original_apply(conn, *args)

    def admit():
        try:
            admission_result.append(root.admit_node_recovery(
                owner, claim, sealed.recovery_id))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def mutate():
        try:
            mutation_attempted.set()
            intent = root.begin((source_selectors.Selector(
                "doc_exact", "users/later.json"),))
            root.complete(intent)
            mutation_result.append(intent.token)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            mutation_done.set()

    monkeypatch.setattr(
        NodeStore, "_apply_candidate", staticmethod(blocked_apply))
    admission_thread = threading.Thread(target=admit)
    admission_thread.start()
    mutation_thread = threading.Thread(target=mutate)
    try:
        assert entered.wait(5)

        # A stale SQLite writer cannot enter while the one-use permit exists
        # inside the coordinator's uncommitted node transaction.
        with sqlite3.connect(owner.path, timeout=0.05) as stale, pytest.raises(
                sqlite3.OperationalError, match="locked"):
            stale.execute("BEGIN IMMEDIATE")

        mutation_thread.start()
        assert mutation_attempted.wait(5)
        assert not mutation_done.wait(0.1)
    finally:
        release.set()
        admission_thread.join(5)
        if mutation_thread.ident is not None:
            mutation_thread.join(5)

    assert not admission_thread.is_alive() and not mutation_thread.is_alive()
    assert failures == []
    assert admission_result == [sealed.generation]
    assert len(mutation_result) == 1
    status = owner.status(node_epoch="node", started_ns=1)
    assert status.admitted_generation == sealed.generation
    assert status.pending_mutations == 1 and status.health == "pending"
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchall() == [("completed",)]
        assert conn.execute(
            "SELECT admitted_generation FROM node_recovery_receipts "
            "WHERE recovery_id=?", (sealed.recovery_id,),
        ).fetchone() == (sealed.generation,)


def test_coordinator_replacement_after_seal_cannot_publish_old_claim(tmp_path):
    owner, root, claim, sealed = sealed_claimed_recovery(
        tmp_path, with_fence=True)
    with sqlite3.connect(root.path) as conn:
        conn.execute(
            "UPDATE mutation_root SET epoch=? WHERE singleton=1",
            ("f" * 32,),
        )
    replacement = MutationCoordinator(tmp_path / "home", root.identity)

    with pytest.raises(SourceChanged, match="mutation_root_changed"):
        root.admit_node_recovery(owner, claim, sealed.recovery_id)
    with pytest.raises(SourceChanged, match="node_recovery_claim_changed"):
        replacement.admit_node_recovery(owner, claim, sealed.recovery_id)

    status = owner.status(node_epoch="node", started_ns=1)
    assert status.admitted_generation == 0
    assert status.pending_mutations == 1 and status.health == "pending"
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (sealed.generation,),
        ).fetchone() == ("sealed",)
        assert conn.execute(
            "SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_receipts").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (1,)


@pytest.mark.parametrize("phase", ("before_node_commit", "after_node_commit"))
def test_process_exit_around_node_commit_is_recoverable(tmp_path, phase):
    owner, root, claim, sealed = sealed_claimed_recovery(
        tmp_path, with_fence=True)
    claim_payload = vars(claim).copy()
    claim_payload["fence_tokens"] = list(claim.fence_tokens)
    script = r"""
import json
import os
import sys
from contextlib import contextmanager

from agentbridge.node.admission import NodeRecoveryClaim
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.store import NodeStore
from agentbridge.store.mutation_coordinator import MutationCoordinator

phase, node_path, home, root_identity, claim_json, recovery_id = sys.argv[1:]
data = json.loads(claim_json)
data["fence_tokens"] = tuple(data["fence_tokens"])
claim = NodeRecoveryClaim(**data)
node = NodeStore(node_path, ReplicaIdentity(
    claim.provider_endpoint, claim.root, "user", "mac"))
root = MutationCoordinator(home, root_identity)
if phase == "before_node_commit":
    NodeStore._clear_candidate_rows = staticmethod(
        lambda *_args, **_kwargs: os._exit(91))
else:
    original = root._transaction
    @contextmanager
    def exit_after_commit(**kwargs):
        with original(**kwargs) as conn:
            yield conn
            os._exit(92)
    root._transaction = exit_after_commit
root.admit_node_recovery(node, claim, recovery_id)
raise AssertionError("crash injection did not run")
"""
    result = subprocess.run(
        [
            sys.executable, "-c", script, phase, str(owner.path),
            str(tmp_path / "home"), root.identity,
            json.dumps(claim_payload, sort_keys=True), sealed.recovery_id,
        ],
        cwd=tmp_path, check=False, timeout=15,
    )
    assert result.returncode == (91 if phase == "before_node_commit" else 92)

    reopened = NodeStore(owner.path, identity())
    status = reopened.status(node_epoch="node", started_ns=1)
    if phase == "before_node_commit":
        assert status.admitted_generation == 0
        assert status.pending_mutations == 1
        with sqlite3.connect(owner.path) as conn:
            assert conn.execute(
                "SELECT state FROM node_generations WHERE generation=?",
                (sealed.generation,),
            ).fetchone() == ("sealed",)
            assert conn.execute(
                "SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
            assert conn.execute(
                "SELECT count(*) FROM node_recovery_receipts").fetchone() == (0,)
    else:
        assert status.admitted_generation == sealed.generation
        assert status.pending_mutations == 0

    assert root.admit_node_recovery(
        reopened, claim, sealed.recovery_id) == sealed.generation


@pytest.mark.parametrize("same_opening", (True, False))
def test_competing_claimed_recovery_starts_converge_or_reject(
        tmp_path, same_opening):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    claim = root.claim_node_recovery(owner)
    first = _provider_cut(tx.recovery_fence())
    second = first if same_opening else type(first)(
        first.schema_version, first.index_epoch, first.source_epoch,
        first.account_id, first.role, first.minimum_cursor, first.cursor + 1,
    )
    barrier = threading.Barrier(3)
    results = []
    failures = []

    def start(opening, created_ns):
        try:
            barrier.wait()
            results.append(root.begin_node_recovery(
                owner, claim, opening, created_ns=created_ns).recovery_id)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = (
        threading.Thread(target=start, args=(first, 10)),
        threading.Thread(target=start, args=(second, 11)),
    )
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    if same_opening:
        assert failures == []
        assert len(results) == 2 and len(set(results)) == 1
    else:
        assert len(results) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], NodeGenerationChanged)
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM claimed_recoveries").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries").fetchone() == (1,)


def test_competing_admissions_return_one_committed_generation(tmp_path):
    owner, root, claim, sealed = sealed_claimed_recovery(
        tmp_path, with_fence=True)
    barrier = threading.Barrier(3)
    results = []
    failures = []

    def admit():
        try:
            barrier.wait()
            results.append(root.admit_node_recovery(
                owner, claim, sealed.recovery_id))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = (threading.Thread(target=admit), threading.Thread(target=admit))
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert results == [sealed.generation, sealed.generation]
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_receipts").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM node_admission_permits").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM node_mutation_fences").fetchone() == (0,)


def test_lost_admission_response_recovers_from_retained_receipt(
        tmp_path, monkeypatch):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)
    original = root._transaction

    @contextmanager
    def lose_response(**kwargs):
        with original(**kwargs) as conn:
            yield conn
            raise OSError("root commit response lost")

    monkeypatch.setattr(root, "_transaction", lose_response)
    with pytest.raises(OSError, match="root commit response lost"):
        root.admit_node_recovery(owner, claim, sealed.recovery_id)
    monkeypatch.setattr(root, "_transaction", original)

    assert root.admit_node_recovery(
        owner, claim, sealed.recovery_id) == sealed.generation
    assert owner.capture(NodeCaptureRequest(max_bytes=1024)).generation == (
        sealed.generation)


def test_retained_receipt_survives_later_root_mutation(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)
    admitted = root.admit_node_recovery(owner, claim, sealed.recovery_id)

    later = root.begin((source_selectors.Selector(
        "doc_exact", "users/later.json"),))
    root.complete(later)
    before = owner.status(node_epoch="node", started_ns=1)

    assert root.admit_node_recovery(
        owner, claim, sealed.recovery_id) == admitted
    after = owner.status(node_epoch="node", started_ns=1)
    assert before.pending_mutations == after.pending_mutations == 1
    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchall() == [("completed",)]


def test_old_generation_pruning_cascades_retained_receipt(tmp_path):
    owner = NodeStore(tmp_path / "node.sqlite3", identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    claim = root.claim_node_recovery(owner)
    opening = _provider_cut(tx.recovery_fence())
    state = root.begin_node_recovery(owner, claim, opening, created_ns=10)
    sealed, _actions = run_to_seal(
        InactiveRecoveryExecutor(owner, tx), state.recovery_id)
    admitted = root.admit_node_recovery(owner, claim, sealed.recovery_id)
    replacement = owner.begin_candidate(created_ns=20)
    owner.seal_candidate(replacement, NodeInputBatch(), observed_ns=21)
    owner.admit_candidate(replacement)

    with sqlite3.connect(owner.path) as conn:
        assert conn.execute(
            "DELETE FROM node_generations WHERE generation=? "
            "AND state='superseded'", (admitted,),
        ).rowcount == 1
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_receipts WHERE recovery_id=?",
            (sealed.recovery_id,),
        ).fetchone() == (0,)

    assert NodeStore(owner.path, identity()).status(
        node_epoch="node", started_ns=1).admitted_generation == replacement


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
        conn.execute("DROP TRIGGER node_recovery_receipt_prune")
        conn.execute("DROP TRIGGER node_admission_permit_consumer")
        conn.execute("DROP TRIGGER node_claimed_generation_guard")
        conn.execute("DROP TRIGGER node_claim_tag_immutable_guard")
        conn.execute("DROP TRIGGER node_pending_mutation_guard")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_digest")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_id")
        conn.execute(node_store._V10_PENDING_MUTATION_GUARD)
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
        ).fetchone() == (11,)
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
        conn.execute("DROP TRIGGER node_recovery_receipt_prune")
        conn.execute("DROP TRIGGER node_admission_permit_consumer")
        conn.execute("DROP TRIGGER node_claimed_generation_guard")
        conn.execute("DROP TRIGGER node_claim_tag_immutable_guard")
        conn.execute("DROP TRIGGER node_pending_mutation_guard")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_digest")
        conn.execute("ALTER TABLE node_generations DROP COLUMN claim_id")
        conn.execute(node_store._V10_PENDING_MUTATION_GUARD)
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


def test_v10_migration_preserves_tagged_claim_and_installs_permit_guards(tmp_path):
    path = tmp_path / "node.sqlite3"
    owner = NodeStore(path, identity())
    tx, _client = transport()
    root = MutationCoordinator(tmp_path / "home", root_identity(tx))
    root.register_node(owner)
    started = ClaimedRecoveryExecutor(owner, tx, root).step(None, attempted_ns=10)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER node_recovery_receipt_prune")
        conn.execute("DROP TRIGGER node_admission_permit_consumer")
        conn.execute("DROP TRIGGER node_pending_mutation_guard")
        conn.execute("DROP TRIGGER node_claimed_generation_guard")
        conn.execute(node_store._V10_PENDING_MUTATION_GUARD)
        conn.execute(node_store._V10_CLAIMED_GENERATION_GUARD)
        conn.execute("DROP TABLE node_schema")
        conn.execute("""CREATE TABLE node_schema(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=10),
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""")
        conn.execute("INSERT INTO node_schema VALUES(1,10,1)")

    reopened = NodeStore(path, identity())

    assert reopened.claimed_recovery_id() == started.recovery_id
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version FROM node_schema WHERE singleton=1"
        ).fetchone() == (11,)
        assert {
            name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        } == {
            "node_pending_mutation_guard", "node_claimed_generation_guard",
            "node_claim_tag_immutable_guard", "node_admission_permit_consumer",
            "node_recovery_receipt_prune",
        }


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
