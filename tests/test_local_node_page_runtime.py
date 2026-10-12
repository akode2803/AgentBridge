"""Threadless composition of provider/node and node/Store scheduling."""
from __future__ import annotations

import json
import threading

import pytest

from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.node_page_runtime import (
    LocalNodePageRuntime, NodePageRuntimeStep,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.server import LocalNodeServer
from agentbridge.node.sync_scheduler import NodeSyncTickResult
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.local_mutations import (
    LocalMutationTransport, root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient


CHAT = "c1"
EPOCH = "12345678-1234-5678-9234-567812345678"
ACCOUNT = "12345678-1234-5678-9234-567812345679"


def _identity():
    return ReplicaIdentity(
        "https://x.test", "supabase://team", ACCOUNT, "machine",
    )


@pytest.fixture
def world(tmp_path):
    payload = json.dumps({
        "id": "remote", "ns": 7, "from": "alice",
        "kind": "message", "text": "ready",
    }, separators=(",", ":"))
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
            "root": "team", "path": f"chats/{CHAT}/meta.json", "seq": 1,
            "deleted": False, "data": {"members": {"alice": True}},
        }],
        "ab_logs": [{
            "root": "team", "id": 7, "chat_id": CHAT,
            "log_name": "alice@machine", "line": payload,
        }],
    })
    provider = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    coordinator = MutationCoordinator(tmp_path / "home", root_identity(provider))
    transport = LocalMutationTransport(provider, coordinator)
    server = LocalNodeServer(
        tmp_path / "node-home", _identity(), recovery_transport=transport,
    ).start()
    store = Store(tmp_path / "store.sqlite")
    clock = [0.0]
    runtime = LocalNodePageRuntime(
        server, store, clock=lambda: clock[0],
        clock_ns=lambda: int(clock[0] * 1_000_000_000),
    )
    try:
        yield runtime, store, server, clock
    finally:
        server.stop()
        store.close()


def _advance(runtime, clock):
    wait = runtime.wait_s(300.0)
    clock[0] += wait
    return runtime.run_due()


def test_selected_chat_gets_first_local_publication_opportunity(world):
    runtime, _store, _server, clock = world
    assert runtime.request(CHAT, selected=True)

    deferred = runtime.run_due()
    assert deferred.operation == "none"
    assert deferred.next_due_s == pytest.approx(0.05)
    page = _advance(runtime, clock)
    assert page.operation == "publication"
    assert runtime.node.next_due_ns == 0
    node = runtime.run_due()
    assert node.operation == "node"


def test_selected_page_first_cannot_be_consumed_by_due_background(world,
                                                                  monkeypatch):
    runtime, _store, _server, clock = world
    assert runtime.discover("background")
    clock[0] = 0.05
    assert runtime.request(CHAT, selected=True)
    published = []
    original = runtime.publisher.publish

    def record(chat):
        published.append(chat)
        return original(chat)

    monkeypatch.setattr(runtime.publisher, "publish", record)
    assert runtime.run_due().operation == "none"
    clock[0] = 0.1
    assert runtime.run_due().operation == "publication"
    assert published == [CHAT]


def test_same_route_lease_renewal_does_not_restore_page_first(world):
    runtime, _store, _server, clock = world
    assert runtime.request(CHAT, selected=True)
    clock[0] = 0.05
    assert runtime.run_due().operation == "publication"
    assert runtime._page_first_chat is None

    assert runtime.request(CHAT, selected=True)
    assert runtime._page_first_chat is None
    assert runtime.run_due().operation == "node"


def test_completed_page_first_cannot_clear_newer_selected_route(
        world, monkeypatch):
    runtime, _store, _server, clock = world
    assert runtime.request(CHAT, selected=True)
    clock[0] = 0.05
    entered = threading.Event()
    release = threading.Event()

    def blocked_run(chat_id=None):
        assert chat_id == CHAT
        entered.set()
        assert release.wait(2.0)
        return True

    monkeypatch.setattr(runtime.publisher, "run_due", blocked_run)
    thread = threading.Thread(target=runtime.run_due, daemon=True)
    thread.start()
    assert entered.wait(1.0)
    assert runtime.request("new-route", selected=True)
    release.set()
    thread.join(2.0)

    assert not thread.is_alive()
    assert runtime.selected == "new-route"
    assert runtime._page_first_chat == "new-route"


def test_cold_node_recovery_eventually_publishes_one_coherent_store_cut(world):
    runtime, store, _server, clock = world
    runtime.request(CHAT, selected=True)

    operations = []
    for _ in range(40):
        step = runtime.run_due()
        operations.append(step.operation)
        try:
            receipt = LocalPageSource(
                runtime.publisher.coordinator, store, CHAT,
            ).capture()
        except Exception:
            receipt = None
        if receipt is not None and store.messages(CHAT):
            break
        wait = runtime.wait_s(300.0)
        clock[0] += max(wait, 0.001)
    else:
        raise AssertionError("coherent Store cut was not published")

    assert "node" in operations and "publication" in operations
    assert receipt.node_position.admitted_generation == 1
    assert receipt.node_messages == store.capture_membership_input_position(CHAT)
    assert store.messages(CHAT) == [{
        "id": "remote", "ns": 7, "from": "alice", "kind": "message",
        "text": "ready",
    }]


def test_one_run_never_combines_provider_and_store_work(world, monkeypatch):
    runtime, _store, _server, clock = world
    runtime.request(CHAT, selected=True)
    calls = []
    original_page = runtime.publisher.publish
    original_node = runtime.node.tick
    monkeypatch.setattr(
        runtime.publisher, "publish",
        lambda chat: (calls.append("page"), original_page(chat))[1],
    )
    monkeypatch.setattr(
        runtime.node, "tick",
        lambda **kwargs: (calls.append("node"), original_node(**kwargs))[1],
    )

    for _ in range(12):
        calls.clear()
        runtime.run_due()
        assert len(calls) <= 1
        clock[0] += max(runtime.wait_s(300.0), 0.001)


def test_due_room_backlog_cannot_starve_due_node_work(world):
    runtime, _store, _server, clock = world
    runtime.discover("background-a")
    runtime.discover("background-b")
    clock[0] = 0.05

    first = runtime.run_due()
    second = runtime.run_due()

    assert first.operation == "publication"
    assert second.operation == "node"


def test_concurrent_node_tick_is_reported_as_no_operation(world, monkeypatch):
    runtime, _store, _server, _clock = world
    monkeypatch.setattr(runtime.publisher, "run_due", lambda chat_id=None: False)
    monkeypatch.setattr(
        runtime.node, "tick",
        lambda **_kwargs: NodeSyncTickResult(
            "deferred", "none", "in-progress", 1,
            "pending", 0, 0,
        ),
    )

    step = runtime.run_due()

    assert step.operation == "none"
    assert step.node is None


def test_realtime_wake_is_content_free_until_due_step(world, monkeypatch):
    runtime, _store, server, clock = world
    runtime.request(CHAT, selected=True)
    calls = 0
    provider = server.recovery_runtime.transport._transport
    original = provider.recovery_fence

    def counted():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(provider, "recovery_fence", counted)
    runtime.wake("realtime")
    assert calls == 0
    runtime.run_due()
    assert calls == 0  # selected Store opportunity remains first
    clock[0] = 0.05
    runtime.run_due()
    assert calls == 0
    runtime.run_due()
    assert calls == 1


def test_settled_mutation_wakes_node_recovery_without_claiming_success(world):
    runtime, _store, _server, clock = world
    runtime.request(CHAT, selected=True)
    clock[0] = 10.0
    before = runtime.node._wake_revision
    assert runtime.request(CHAT, settled=True)
    assert runtime.node._wake_revision == before + 1
    assert runtime.node.next_due_ns <= int(clock[0] * 1_000_000_000)


def test_settled_mutation_wakes_node_even_when_page_queue_is_full(world):
    runtime, _store, _server, _clock = world
    runtime.publisher.schedule.capacity = 1
    assert runtime.discover("only-slot")
    before = runtime.node._wake_revision

    assert not runtime.request("rejected-room", settled=True)
    assert runtime.node._wake_revision == before + 1


def test_selection_and_realtime_mode_are_explicit(world):
    runtime, _store, _server, _clock = world
    runtime.request(CHAT, selected=True)
    assert runtime.selected == CHAT
    runtime.set_realtime_healthy(False)
    assert not runtime.realtime_healthy
    runtime.clear_selection()
    assert runtime.selected is None


def test_selection_lease_expiry_demotes_both_schedulers(world):
    runtime, _store, _server, clock = world
    runtime.request(CHAT, selected=True)
    assert runtime.node._foreground
    clock[0] = 15.01

    runtime.wait_s(300.0)

    assert runtime.selected is None
    assert runtime._page_first_chat is None
    assert not runtime.node._foreground
    assert runtime.publisher.schedule._selected is None


def test_invalid_result_and_wait_are_rejected():
    with pytest.raises(ValueError):
        NodePageRuntimeStep("wrong", 0.0)
    with pytest.raises(ValueError):
        NodePageRuntimeStep("none", float("nan"))
