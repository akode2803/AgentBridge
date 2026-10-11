"""Inactive coordinator-owned recovery runtime composition."""

import sqlite3

import pytest

from agentbridge.node.admission import NodeInputError
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.recovery_runtime import LocalNodeRecoveryRuntime
from agentbridge.node.store import NodeStore
from agentbridge.store import source_selectors
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.local_mutations import (
    LocalMutationTransport, root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

EPOCH = "12345678-1234-5678-9234-567812345678"
ACCOUNT = "12345678-1234-5678-9234-567812345679"


def _identity():
    return ReplicaIdentity("https://x.test", "supabase://team", ACCOUNT, "mac")


def _other_identity():
    return ReplicaIdentity(
        "https://x.test", "supabase://team", ACCOUNT, "other-machine")


def _owned(tmp_path):
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_scoped_source_version": 1,
        "ab_change_epochs": [{
            "root": "team", "epoch": EPOCH, "minimum_cursor": 0,
            "source_schema_version": 2,
        }],
        "ab_change_events": [],
        "ab_docs": [{
            "root": "team", "path": "chats/c1/meta.json", "seq": 1,
            "deleted": False, "data": {"members": {"user": True}},
        }],
        "ab_logs": [{
            "root": "team", "id": 7, "chat_id": "c1",
            "log_name": "main", "line": "sealed",
        }],
    })
    provider = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    coordinator = MutationCoordinator(tmp_path / "home", root_identity(provider))
    transport = LocalMutationTransport(provider, coordinator)
    store = NodeStore(tmp_path / "node.sqlite3", _identity())
    return LocalNodeRecoveryRuntime(store, transport), store, transport, coordinator


def _drive(runtime, *, start=10):
    results = []
    for attempted in range(start, start + 20):
        result = runtime.step(attempted_ns=attempted)
        results.append(result)
        if result.state == "ready":
            return result, results
    raise AssertionError("recovery runtime did not become ready")


def test_runtime_registers_resumes_admits_and_then_stays_idle(tmp_path, monkeypatch):
    runtime, store, transport, _coordinator = _owned(tmp_path)
    calls = 0
    provider = transport._transport
    original = provider.recovery_fence

    def counted_fence():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(provider, "recovery_fence", counted_fence)
    ready, results = _drive(runtime)

    assert results[0].action == "started"
    assert ready.action == "admitted" and ready.generation == 1
    assert store.status(node_epoch="n", started_ns=1).health == "ready"
    before = calls
    assert runtime.step(attempted_ns=100).state == "idle"
    assert calls == before

    # A clean process restart trusts the admitted local cut as presentation
    # state and waits for explicit delivery evidence before refreshing again.
    replacement = LocalNodeRecoveryRuntime(store, transport)
    assert replacement.step(attempted_ns=101).state == "idle"
    assert calls == before


def test_runtime_restart_discovers_durable_claimed_work(tmp_path):
    runtime, store, transport, _coordinator = _owned(tmp_path)
    started = runtime.step(attempted_ns=10)
    claimed = store.claimed_recovery()
    assert started.action == "started"
    assert claimed is not None and claimed[1] == started.recovery_id

    replacement = LocalNodeRecoveryRuntime(store, transport)
    resumed = replacement.step(attempted_ns=11)
    assert resumed.recovery_id == started.recovery_id
    assert resumed.action == "manifest:documents"
    ready, _results = _drive(replacement, start=12)
    assert ready.generation == 1


def test_completed_mutation_fence_is_consumed_only_by_full_recovery(tmp_path):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    first, _results = _drive(runtime)
    intent = coordinator.begin((source_selectors.Selector(
        "doc_exact", "users/changed.json"),))
    coordinator.complete(intent)
    # The durable fence is sufficient to wake an explicit scheduler step even
    # if its process-local notification was lost.
    assert runtime.pending()

    started = runtime.step(attempted_ns=100)
    assert started.action == "started"
    assert store.status(node_epoch="n", started_ns=1).pending_mutations == 1
    ready, _results = _drive(runtime, start=101)
    assert ready.generation > first.generation
    status = store.status(node_epoch="n", started_ns=1)
    assert status.pending_mutations == 0 and status.health == "ready"


def test_active_mutation_blocks_provider_work_until_definite_completion(
        tmp_path, monkeypatch):
    runtime, store, transport, coordinator = _owned(tmp_path)
    _ready, _results = _drive(runtime)
    intent = coordinator.begin((source_selectors.Selector(
        "doc_exact", "users/in-flight.json"),))
    provider = transport._transport
    original = provider.recovery_fence
    calls = 0

    def counted_fence():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(provider, "recovery_fence", counted_fence)
    blocked = runtime.step(attempted_ns=100)
    assert (blocked.state, blocked.action) == ("retry", "cut-changed")
    assert calls == 0
    assert store.status(node_epoch="n", started_ns=1).pending_mutations == 1

    coordinator.complete(intent)
    assert runtime.step(attempted_ns=101).action == "started"
    assert calls == 1


def test_mutation_after_admission_stays_pending_for_followup_recovery(
        tmp_path, monkeypatch):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    original = coordinator.admit_node_recovery
    inserted = False

    def mutate_after_commit(node, claim, recovery_id):
        nonlocal inserted
        generation = original(node, claim, recovery_id)
        if not inserted:
            inserted = True
            intent = coordinator.begin((source_selectors.Selector(
                "doc_exact", "users/later.json"),))
            coordinator.complete(intent)
        return generation

    monkeypatch.setattr(coordinator, "admit_node_recovery", mutate_after_commit)
    pending = None
    for attempted in range(10, 30):
        pending = runtime.step(attempted_ns=attempted)
        if pending.action == "mutation-pending":
            break
    assert pending is not None and pending.state == "retry"
    assert runtime.pending()
    assert store.status(node_epoch="n", started_ns=1).pending_mutations == 1
    assert runtime.step(attempted_ns=30).action == "started"


def test_provider_failure_is_visible_and_retry_resumes_same_claim(tmp_path, monkeypatch):
    runtime, store, transport, _coordinator = _owned(tmp_path)
    provider = transport._transport
    original = provider.recovery_fence
    monkeypatch.setattr(
        provider, "recovery_fence",
        lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )

    failed = runtime.step(attempted_ns=10)
    assert (failed.state, failed.action) == (
        "unavailable", "provider-unavailable")
    assert store.status(node_epoch="n", started_ns=1).health == "unavailable"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM claimed_recoveries").fetchone() == (0,)

    monkeypatch.setattr(provider, "recovery_fence", original)
    resumed = runtime.step(attempted_ns=11)
    assert resumed.action == "started"
    ready, _results = _drive(runtime, start=12)
    assert ready.state == "ready"


def test_lost_admission_response_retries_exact_receipt_without_new_recovery(
        tmp_path, monkeypatch):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    original = coordinator.admit_node_recovery
    raised = False

    def lose_once(node, claim, recovery_id):
        nonlocal raised
        generation = original(node, claim, recovery_id)
        if not raised:
            raised = True
            raise TimeoutError("response lost")
        return generation

    monkeypatch.setattr(coordinator, "admit_node_recovery", lose_once)
    uncertain = None
    for attempted in range(10, 30):
        uncertain = runtime.step(attempted_ns=attempted)
        if uncertain.action == "admission-uncertain":
            break
    assert uncertain is not None and uncertain.state == "retry"
    committed = store.status(node_epoch="n", started_ns=1)
    assert committed.admitted_generation == 1 and committed.health == "ready"

    # The old receipt may complete only the request that produced it.
    runtime.request()
    confirmed = runtime.step(attempted_ns=30)
    assert (confirmed.state, confirmed.action, confirmed.generation) == (
        "ready", "admission-confirmed", 1)
    assert runtime.pending()
    assert runtime.step(attempted_ns=31).action == "started"


def test_mutation_between_seal_and_admission_does_not_pin_dead_claim(
        tmp_path, monkeypatch):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    original = coordinator.admit_node_recovery
    invalidated = False

    def invalidate_once(node, claim, recovery_id):
        nonlocal invalidated
        if not invalidated:
            invalidated = True
            intent = coordinator.begin((source_selectors.Selector(
                "doc_exact", "users/between.json"),))
            coordinator.complete(intent)
        return original(node, claim, recovery_id)

    monkeypatch.setattr(coordinator, "admit_node_recovery", invalidate_once)
    changed = None
    for attempted in range(10, 30):
        changed = runtime.step(attempted_ns=attempted)
        if invalidated:
            break
    assert changed is not None
    assert (changed.state, changed.action) == ("retry", "cut-changed")
    assert store.status(node_epoch="n", started_ns=1).pending_mutations == 1

    restarted = runtime.step(attempted_ns=30)
    assert restarted.action == "started"
    ready, _results = _drive(runtime, start=31)
    assert ready.state == "ready"
    assert store.status(node_epoch="n", started_ns=1).pending_mutations == 0


def test_root_revision_movement_retires_exact_stale_claim_and_recovers(
        tmp_path):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    started = runtime.step(attempted_ns=10)
    stale_recovery = started.recovery_id
    coordinator.register_node(NodeStore(
        tmp_path / "other-node.sqlite3", _other_identity()))

    changed = None
    for attempted in range(11, 30):
        changed = runtime.step(attempted_ns=attempted)
        if changed.action == "cut-changed":
            break
    assert changed is not None and changed.recovery_id == stale_recovery
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM provider_recoveries WHERE recovery_id=?",
            (stale_recovery,),
        ).fetchone() is None

    restarted = runtime.step(attempted_ns=30)
    assert restarted.action == "started"
    assert restarted.recovery_id != stale_recovery
    ready, _results = _drive(runtime, start=31)
    assert ready.state == "ready"


def test_exact_abandonment_preserves_completed_mutation_fence(tmp_path):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    intent = coordinator.begin((source_selectors.Selector(
        "doc_exact", "users/earlier.json"),))
    coordinator.complete(intent)
    started = runtime.step(attempted_ns=10)
    claimed = store.claimed_recovery()
    assert claimed is not None and claimed[1] == started.recovery_id

    assert coordinator.abandon_node_recovery(
        store, claimed[0], claimed[1]) is True
    status = store.status(node_epoch="n", started_ns=1)
    assert status.pending_mutations == 1 and status.health == "pending"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences").fetchall() == [
                ("completed",)]
        assert conn.execute(
            "SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)


def test_exact_abandonment_cannot_undo_committed_admission(tmp_path):
    runtime, store, _transport, coordinator = _owned(tmp_path)
    started = runtime.step(attempted_ns=10)
    claimed = store.claimed_recovery()
    assert claimed is not None and claimed[1] == started.recovery_id
    ready, _results = _drive(runtime, start=11)

    assert coordinator.abandon_node_recovery(
        store, claimed[0], claimed[1]) is False
    status = store.status(node_epoch="n", started_ns=1)
    assert status.admitted_generation == ready.generation
    assert status.health == "ready" and status.pending_mutations == 0


def test_request_arriving_during_admission_remains_pending(tmp_path, monkeypatch):
    runtime, _store, _transport, coordinator = _owned(tmp_path)
    original = coordinator.admit_node_recovery

    def request_again(node, claim, recovery_id):
        runtime.request()
        return original(node, claim, recovery_id)

    monkeypatch.setattr(coordinator, "admit_node_recovery", request_again)
    ready, _results = _drive(runtime)
    assert ready.state == "ready" and runtime.pending()
    assert runtime.step(attempted_ns=100).action == "started"


@pytest.mark.parametrize("attempted", [0, -1, 2**63, True, 1.5])
def test_runtime_rejects_invalid_attempt_times(tmp_path, attempted):
    runtime, _store, _transport, _coordinator = _owned(tmp_path)
    with pytest.raises(NodeInputError, match="attempt time"):
        runtime.step(attempted_ns=attempted)
