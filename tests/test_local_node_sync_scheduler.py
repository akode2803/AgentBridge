"""Explicit local-node sync control-plane scheduling."""

import threading

import pytest

from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.server import LocalNodeServer
from agentbridge.node.sync_scheduler import (
    BACKGROUND_CONTINUE_NS, FAILURE_INITIAL_NS, FOREGROUND_CONTINUE_NS,
    FOREGROUND_FALLBACK_NS, HEALTHY_SAFETY_POLL_NS, LocalNodeSyncScheduler,
)
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.local_mutations import (
    LocalMutationTransport, root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

EPOCH = "12345678-1234-5678-9234-567812345678"
ACCOUNT = "12345678-1234-5678-9234-567812345679"


def _identity():
    return ReplicaIdentity(
        "https://x.test", "supabase://team", ACCOUNT, "machine")


def _rig(tmp_path):
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_source_ledger_version": 2,
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
        "ab_logs": [],
    })
    provider = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    coordinator = MutationCoordinator(
        tmp_path / "coordinator", root_identity(provider))
    transport = LocalMutationTransport(provider, coordinator)
    server = LocalNodeServer(
        tmp_path / "node", _identity(),
        recovery_transport=transport,
    ).start()
    return server, provider, client


def _drive_ready(scheduler, *, limit=30):
    results = []
    for _ in range(limit):
        result = scheduler.tick(now_ns=scheduler.next_due_ns)
        results.append(result)
        if result.state == "ready":
            return result, results
    raise AssertionError("local node scheduler did not become ready")


def test_cold_owner_advances_one_recovery_step_per_tick_then_idles(tmp_path):
    server, _provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        assert scheduler.next_due_ns == 100
        ready, results = _drive_ready(scheduler)
        assert results[0].operation == "recovery"
        assert results[0].action == "started"
        assert ready.admitted_generation == 1
        assert ready.source_health == "ready"
        assert ready.last_success_ns > 0
        assert ready.next_due_ns == (
            results[-2].next_due_ns + HEALTHY_SAFETY_POLL_NS)

        deferred = scheduler.tick(now_ns=ready.next_due_ns - 1)
        assert (deferred.state, deferred.operation, deferred.action) == (
            "deferred", "none", "not-due")
    finally:
        server.stop()


def test_burst_wakes_are_content_free_and_one_collection_completes_them(
        tmp_path, monkeypatch):
    server, provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        ready, _results = _drive_ready(scheduler)
        calls = 0
        original = provider.source_ledger_fence

        def counted_fence():
            nonlocal calls
            calls += 1
            return original()

        monkeypatch.setattr(provider, "source_ledger_fence", counted_fence)
        assert scheduler.wake("realtime", now_ns=1_000) == 1
        assert scheduler.wake("ledger", now_ns=1_000) == 2
        assert scheduler.wake("realtime", now_ns=1_000) == 3
        assert calls == 0

        result = scheduler.tick(now_ns=1_000)
        assert (result.state, result.operation, result.action) == (
            "ready", "collector", "up_to_date")
        assert calls == 1
        assert result.next_due_ns == 1_000 + HEALTHY_SAFETY_POLL_NS
        assert result.admitted_generation == ready.admitted_generation
    finally:
        server.stop()


def test_unhealthy_realtime_shortens_fallback_without_claiming_freshness(
        tmp_path):
    server, _provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        _drive_ready(scheduler)
        due = scheduler.set_mode(
            foreground=True, realtime_healthy=False, now_ns=1_000)
        assert due == 1_000 + FOREGROUND_FALLBACK_NS
        assert scheduler.tick(now_ns=due - 1).state == "deferred"
        assert scheduler.tick(now_ns=due).operation == "collector"
    finally:
        server.stop()


def test_recovery_request_uses_foreground_and_background_step_cadence(tmp_path):
    server, _provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        _drive_ready(scheduler)
        scheduler.set_mode(
            foreground=True, realtime_healthy=True, now_ns=1_000)
        scheduler.request_recovery(now_ns=1_000)
        foreground = scheduler.tick(now_ns=1_000)
        assert foreground.operation == "recovery"
        assert foreground.next_due_ns == 1_000 + FOREGROUND_CONTINUE_NS

        scheduler.set_mode(
            foreground=False, realtime_healthy=True,
            now_ns=foreground.next_due_ns)
        background = scheduler.tick(now_ns=foreground.next_due_ns)
        assert background.operation == "recovery"
        assert background.next_due_ns == (
            foreground.next_due_ns + BACKGROUND_CONTINUE_NS)
    finally:
        server.stop()


def test_provider_failure_uses_bounded_backoff_and_keeps_last_snapshot(
        tmp_path, monkeypatch):
    server, provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        ready, _results = _drive_ready(scheduler)
        calls = 0

        def offline():
            nonlocal calls
            calls += 1
            raise ConnectionError("offline")

        monkeypatch.setattr(provider, "source_ledger_fence", offline)
        scheduler.wake("realtime", now_ns=1_000)
        failed = scheduler.tick(now_ns=1_000)
        assert (failed.state, failed.operation) == ("degraded", "collector")
        assert failed.admitted_generation == ready.admitted_generation
        assert failed.source_health == "degraded"
        assert failed.next_due_ns == 1_000 + FAILURE_INITIAL_NS
        assert scheduler.tick(
            now_ns=failed.next_due_ns - 1).state == "deferred"
        assert calls == 1

        # Content-free hints coalesce behind the provider retry floor.  They
        # cannot turn an offline Realtime burst into an unbounded request loop.
        scheduler.wake("realtime", now_ns=1_001)
        scheduler.wake("ledger", now_ns=1_002)
        assert scheduler.next_due_ns == failed.next_due_ns
        assert scheduler.tick(now_ns=1_002).state == "deferred"
        assert calls == 1

        failed_again = scheduler.tick(now_ns=failed.next_due_ns)
        assert calls == 2
        assert failed_again.next_due_ns == (
            failed.next_due_ns + 2 * FAILURE_INITIAL_NS)
    finally:
        server.stop()


def test_slow_provider_failure_starts_backoff_after_call_returns(
        tmp_path, monkeypatch):
    server, provider, _client = _rig(tmp_path)
    now = 100
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: now)
        _drive_ready(scheduler)

        def slow_offline():
            nonlocal now
            now += 10 * FAILURE_INITIAL_NS
            raise ConnectionError("slow offline")

        monkeypatch.setattr(provider, "source_ledger_fence", slow_offline)
        scheduler.wake("realtime", now_ns=1_000)
        failed = scheduler.tick(now_ns=1_000)
        assert failed.next_due_ns == now + FAILURE_INITIAL_NS
        assert scheduler.tick(now_ns=now).state == "deferred"
    finally:
        server.stop()


def test_ledger_recovery_evidence_requests_full_recovery_without_authorizing_it(
        tmp_path):
    server, _provider, client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        ready, _results = _drive_ready(scheduler)
        client.db["ab_change_events"].append({
            "root": "team", "id": 1, "stream_kind": "root",
            "stream_id": "", "domain": "visibility", "source_key": None,
            "doc_head": None, "log_head": None,
        })

        scheduler.wake("ledger", now_ns=1_000)
        evidence = scheduler.tick(now_ns=1_000)
        assert (evidence.state, evidence.operation, evidence.action) == (
            "running", "collector", "recovery-required")
        assert evidence.admitted_generation == ready.admitted_generation

        recovery = scheduler.tick(now_ns=evidence.next_due_ns)
        assert recovery.operation == "recovery"
        assert recovery.action == "started"
        assert recovery.admitted_generation == ready.admitted_generation
    finally:
        server.stop()


def test_exact_ledger_backlog_continues_collector_without_full_recovery(tmp_path):
    server, provider, client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        _drive_ready(scheduler)
        recovery_calls = 0
        original = provider.recovery_fence

        def counted_recovery_fence():
            nonlocal recovery_calls
            recovery_calls += 1
            return original()

        provider.recovery_fence = counted_recovery_fence
        for event_id in range(1, 5):
            path = f"users/u{event_id}.json"
            client.db["ab_docs"].append({
                "root": "team", "path": path, "seq": event_id,
                "deleted": False, "data": {"n": event_id},
            })
            client.db["ab_change_events"].append({
                "root": "team", "id": event_id, "stream_kind": "root",
                "stream_id": "", "domain": "docs", "source_key": path,
                "doc_head": event_id, "log_head": None,
            })

        scheduler.wake("ledger", now_ns=1_000)
        partial = scheduler.tick(now_ns=1_000)
        assert (partial.state, partial.operation, partial.action) == (
            "running", "collector", "catching_up")
        assert not server.recovery_pending()

        completed = scheduler.tick(now_ns=partial.next_due_ns)
        assert (completed.state, completed.operation, completed.action) == (
            "ready", "collector", "admitted")
        assert recovery_calls == 0
    finally:
        server.stop()


def test_wake_after_uncertain_admission_survives_receipt_confirmation(
        tmp_path, monkeypatch):
    server, _provider, client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        _drive_ready(scheduler)
        coordinator = server.recovery_runtime.coordinator
        original = coordinator.admit_node_recovery
        lost = False

        def lose_response_once(node, claim, recovery_id):
            nonlocal lost
            generation = original(node, claim, recovery_id)
            if not lost:
                lost = True
                raise TimeoutError("response lost")
            return generation

        monkeypatch.setattr(
            coordinator, "admit_node_recovery", lose_response_once)
        scheduler.request_recovery(now_ns=1_000)
        uncertain = None
        for _ in range(30):
            result = scheduler.tick(now_ns=scheduler.next_due_ns)
            if result.action == "admission-uncertain":
                uncertain = result
                break
        assert uncertain is not None

        client.db["ab_docs"].append({
            "root": "team", "path": "users/later.json", "seq": 1,
            "deleted": False, "data": {"later": True},
        })
        client.db["ab_change_events"].append({
            "root": "team", "id": 1, "stream_kind": "root",
            "stream_id": "", "domain": "docs",
            "source_key": "users/later.json", "doc_head": 1,
            "log_head": None,
        })
        scheduler.wake("ledger", now_ns=uncertain.next_due_ns)

        confirmed = scheduler.tick(now_ns=uncertain.next_due_ns)
        assert (confirmed.state, confirmed.operation, confirmed.action) == (
            "running", "recovery", "admission-confirmed")
        collected = scheduler.tick(now_ns=confirmed.next_due_ns)
        assert (collected.state, collected.operation, collected.action) == (
            "ready", "collector", "admitted")
    finally:
        server.stop()


def test_concurrent_tick_is_busy_and_does_not_start_second_provider_step(
        tmp_path, monkeypatch):
    server, provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        _drive_ready(scheduler)
        scheduler.wake("realtime", now_ns=1_000)
        original = provider.source_ledger_fence
        entered = threading.Event()
        release = threading.Event()
        results = []

        def blocked_fence():
            entered.set()
            assert release.wait(5)
            return original()

        monkeypatch.setattr(provider, "source_ledger_fence", blocked_fence)
        worker = threading.Thread(
            target=lambda: results.append(scheduler.tick(now_ns=1_000)))
        worker.start()
        assert entered.wait(2)
        concurrent = scheduler.tick(now_ns=1_000)
        assert (concurrent.state, concurrent.operation, concurrent.action) == (
            "busy", "none", "tick-in-progress")
        assert scheduler.wake("realtime", now_ns=1_001) == 2
        release.set()
        worker.join(5)
        assert not worker.is_alive() and len(results) == 1
        assert results[0].state == "running"
        followup = scheduler.tick(now_ns=results[0].next_due_ns)
        assert (followup.state, followup.operation, followup.action) == (
            "ready", "collector", "up_to_date")
    finally:
        server.stop()


@pytest.mark.parametrize("kind", ["", "source", "Realtime", 1, None])
def test_wake_rejects_unbounded_or_unknown_signal(kind, tmp_path):
    server, _provider, _client = _rig(tmp_path)
    try:
        scheduler = LocalNodeSyncScheduler(server, clock_ns=lambda: 100)
        with pytest.raises(ValueError, match="invalid local node sync wake"):
            scheduler.wake(kind, now_ns=100)
    finally:
        server.stop()
