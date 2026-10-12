"""Threadless composition of provider/node and node/Store scheduling."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.local_input_runtime import LocalInputRuntime
from agentbridge.mesh.node_input_publisher import NodeInputPublishResult
from agentbridge.mesh.node_event_pump import NodeEventPumpStep
from agentbridge.mesh.node_page_runtime import (
    LocalNodePageRuntime, NodePageRuntimeStep,
)
from agentbridge.mesh.sync import SyncEngine, SyncRetirement
from agentbridge.mesh import local_input_runtime
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.server import LocalNodeServer
from agentbridge.node.sync_scheduler import NodeSyncTickResult
from agentbridge.store import local_source, staged_publication, staged_source
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
def inactive_world(tmp_path):
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


@pytest.fixture
def world(inactive_world):
    runtime, store, server, clock = inactive_world
    inputs = LocalInputRuntime(server.recovery_runtime.transport, store)
    engine = SyncEngine(server.recovery_runtime.transport, store)
    inputs.bind_sync_owner(engine)
    _bind_event_owner(runtime, store, inputs, engine)
    inputs.install_node_source(runtime, engine.retire())
    try:
        yield runtime, store, server, clock
    finally:
        inputs.stop()


def _advance(runtime, clock):
    wait = runtime.wait_s(300.0)
    clock[0] += wait
    return runtime.run_due()


def _event_mesh(store, *, inputs=None, engine=None):
    from agentbridge.mesh.eventbus import EventBus
    from agentbridge.mesh.service import Mesh

    mesh = object.__new__(Mesh)
    mesh.store = store
    mesh.bus = EventBus()
    mesh.user = "alice"
    mesh.tx = None if inputs is None else inputs.transport
    mesh.local_inputs = inputs
    mesh.sync = engine
    mesh.messaging = SimpleNamespace(
        store=store, user="alice",
        mark_delivered=lambda *_args, **_kwargs: True,
    )
    return mesh


def _bind_event_owner(runtime, store, inputs, engine):
    mesh = _event_mesh(store, inputs=inputs, engine=engine)
    inputs._page_preparation = SimpleNamespace(
        mesh=mesh, close=lambda: None, select=lambda _chat: True,
        request=lambda *_args, **_kwargs: True,
        health=lambda _chat: None, run_one=lambda: False,
    )
    inputs.read_events = SimpleNamespace(
        bus=mesh.bus, close=lambda: None, changed=lambda *_args: None,
        tick=lambda: None,
    )
    runtime.bind_event_owner(mesh, inputs)


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
        return True, None

    monkeypatch.setattr(runtime.publisher, "run_due_result", blocked_run)
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


def test_three_persistent_lanes_receive_round_robin_service(world, monkeypatch):
    runtime, _store, _server, _clock = world
    publication = NodeInputPublishResult(
        "background", "published", True, 1, None,
    )
    node = NodeSyncTickResult(
        "ready", "recovery", "delta", 1, "ready", 1, 0,
    )
    monkeypatch.setattr(
        runtime.publisher, "run_due_result",
        lambda chat_id=None: (True, publication),
    )
    runtime.node._next_due_ns = 0
    monkeypatch.setattr(runtime.node, "tick", lambda **_kwargs: node)
    monkeypatch.setattr(
        runtime._events, "run_due",
        lambda: NodeEventPumpStep("events", records=1),
    )

    operations = [runtime.run_due().operation for _ in range(6)]

    assert operations == [
        "publication", "node", "event", "publication", "node", "event",
    ]


def test_concurrent_node_tick_is_reported_as_no_operation(world, monkeypatch):
    runtime, _store, _server, _clock = world
    monkeypatch.setattr(
        runtime.publisher, "run_due_result",
        lambda chat_id=None: (False, None),
    )
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


def test_local_input_owner_requires_exact_legacy_retirement(
        inactive_world, tmp_path):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    forged = SyncRetirement(engine, object())
    with pytest.raises(ValueError, match="invalid legacy sync retirement"):
        inputs.install_node_source(runtime, forged)

    other = Store(tmp_path / "foreign.sqlite")
    try:
        foreign = SyncEngine(transport, other)
        proof = foreign.retire()
        with pytest.raises(ValueError, match="foreign legacy sync owner"):
            inputs.install_node_source(runtime, proof)

        decoy = SyncEngine(transport, store)
        decoy_proof = decoy.retire()
        with pytest.raises(ValueError, match="foreign legacy sync owner"):
            inputs.install_node_source(runtime, decoy_proof)
    finally:
        other.close()
        inputs.stop()


def test_node_transfer_requires_exact_recovery_transport(
        inactive_world, tmp_path):
    _runtime, store, server, clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    proof = engine.retire()
    other_transport = LocalMutationTransport(
        transport._transport, transport._coordinator,
    )
    other_server = LocalNodeServer(
        tmp_path / "other-node", _identity(),
        recovery_transport=other_transport,
    ).start()
    other_runtime = LocalNodePageRuntime(
        other_server, store, clock=lambda: clock[0],
        clock_ns=lambda: int(clock[0] * 1_000_000_000),
    )
    try:
        with pytest.raises(ValueError, match="foreign node source owner"):
            inputs.install_node_source(other_runtime, proof)
        assert inputs._node_source is None
    finally:
        other_server.stop()
        inputs.stop()


def test_sync_retirement_can_activate_only_its_bound_local_owner(
        inactive_world):
    runtime, store, server, clock = inactive_world
    transport = server.recovery_runtime.transport
    owner = LocalInputRuntime(transport, store)
    challenger = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    owner.bind_sync_owner(engine)
    with pytest.raises(ValueError, match="foreign legacy sync transfer owner"):
        challenger.bind_sync_owner(engine)
    proof = engine.retire()
    second_runtime = LocalNodePageRuntime(
        server, store, clock=lambda: clock[0],
        clock_ns=lambda: int(clock[0] * 1_000_000_000),
    )

    _bind_event_owner(runtime, store, owner, engine)
    owner.install_node_source(runtime, proof)

    with pytest.raises(ValueError, match="foreign legacy sync owner"):
        challenger.install_node_source(second_runtime, proof)
    with pytest.raises(RuntimeError, match="inactive"):
        second_runtime.run_due()
    owner.stop()
    challenger.stop()


def test_node_transfer_publishes_owner_only_after_route_transfer(
        inactive_world, monkeypatch):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    assert inputs.request(CHAT, selected=True)
    proof = engine.retire()

    monkeypatch.setattr(
        runtime, "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("route transfer failed")),
    )
    with pytest.raises(RuntimeError, match="route transfer failed"):
        inputs.install_node_source(runtime, proof)
    assert inputs._node_source is None
    with pytest.raises(RuntimeError, match="inactive"):
        runtime.run_due()
    with pytest.raises(RuntimeError, match="inactive"):
        runtime.publisher.publish(CHAT)
    inputs.stop()


def test_node_transfer_keeps_bounded_legacy_queue_and_wakes_recovery(
        inactive_world):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    assert inputs.request("activity-room", activity=True)
    assert inputs.request("settled-room", settled=True)
    assert inputs.schedule.discover("discovered-room", now=time.monotonic())
    before = runtime.node._wake_revision

    _bind_event_owner(runtime, store, inputs, engine)
    inputs.install_node_source(runtime, engine.retire())

    assert {
        "activity-room", "settled-room", "discovered-room",
    }.issubset(runtime.publisher.schedule._states)
    assert runtime.node._wake_revision == before + 1
    inputs.stop()


def test_local_input_owner_transfers_selection_and_scheduling_once(
        inactive_world, monkeypatch):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    assert inputs.request(CHAT, selected=True)
    proof = engine.retire()
    _bind_event_owner(runtime, store, inputs, engine)
    inputs.install_node_source(runtime, proof)

    assert runtime.selected == CHAT
    assert inputs.request(CHAT, activity=True)
    called = []
    monkeypatch.setattr(
        runtime, "run_due",
        lambda: (called.append(True), NodePageRuntimeStep("publication", 0.0))[1],
    )
    assert inputs.run_due()
    assert called == [True]
    inputs.clear_selection()
    assert runtime.selected is None
    with pytest.raises(RuntimeError, match="legacy local input owner retired"):
        inputs.ingest(CHAT)
    with pytest.raises(RuntimeError, match="already installed"):
        inputs.install_node_source(runtime, proof)
    inputs.stop()


def test_node_transfer_joins_active_legacy_document_publication(
        inactive_world, monkeypatch):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    proof = engine.retire()
    _bind_event_owner(runtime, store, inputs, engine)
    entered = threading.Event()
    release = threading.Event()
    original = local_input_runtime.collect_document_batches

    def blocked_collection(*args, **kwargs):
        entered.set()
        assert release.wait(2.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        local_input_runtime, "collect_document_batches", blocked_collection,
    )
    legacy_errors = []

    def legacy_ingest():
        try:
            inputs.ingest(CHAT)
        except Exception as exc:
            legacy_errors.append(exc)

    legacy = threading.Thread(target=legacy_ingest, daemon=True)
    legacy.start()
    assert entered.wait(1.0)
    installed = []
    transfer = threading.Thread(
        target=lambda: installed.append(
            inputs.install_node_source(runtime, proof)),
        daemon=True,
    )
    transfer.start()
    time.sleep(0.02)
    assert transfer.is_alive()

    release.set()
    legacy.join(2.0)
    transfer.join(2.0)
    assert not legacy.is_alive() and not transfer.is_alive()
    assert len(legacy_errors) == 1
    assert installed == [None]
    with pytest.raises(RuntimeError, match="legacy local input owner retired"):
        inputs.ingest(CHAT)
    inputs.stop()


def test_discovery_dispatch_is_serialized_with_source_transfer(
        inactive_world, monkeypatch):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    inputs._discovery = SimpleNamespace(
        next_batch=lambda *, max_rooms: ("race-room",),
    )
    inputs._discovery_due = 0.0
    _bind_event_owner(runtime, store, inputs, engine)
    entered = threading.Event()
    release = threading.Event()
    original = inputs.schedule.discover

    def blocked_discover(chat, *, now):
        entered.set()
        assert release.wait(2.0)
        return original(chat, now=now)

    monkeypatch.setattr(inputs.schedule, "discover", blocked_discover)
    discovery = threading.Thread(target=inputs.discover, daemon=True)
    discovery.start()
    assert entered.wait(1.0)
    installed = []
    transfer = threading.Thread(
        target=lambda: installed.append(
            inputs.install_node_source(runtime, engine.retire())),
        daemon=True,
    )
    transfer.start()
    time.sleep(0.02)
    assert transfer.is_alive()

    release.set()
    discovery.join(2.0)
    transfer.join(2.0)

    assert not discovery.is_alive() and not transfer.is_alive()
    assert installed == [None]
    assert "race-room" in runtime.publisher.schedule._states
    inputs.stop()


def test_node_publication_requires_successful_source_transfer(inactive_world):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    assert runtime.request(CHAT, selected=True)

    with pytest.raises(RuntimeError, match="inactive"):
        runtime.run_due()
    with pytest.raises(RuntimeError, match="inactive"):
        runtime.publisher.publish(CHAT)

    _bind_event_owner(runtime, store, inputs, engine)
    inputs.install_node_source(runtime, engine.retire())

    assert runtime.publisher.publish(CHAT).state == "pending"
    inputs.stop()


def test_source_transfer_requires_event_owner_before_retirement_activation(
        inactive_world):
    _runtime, store, server, clock = inactive_world
    unbound = LocalNodePageRuntime(
        server, store, clock=lambda: clock[0],
        clock_ns=lambda: int(clock[0] * 1_000_000_000),
    )
    inputs = LocalInputRuntime(server.recovery_runtime.transport, store)
    engine = SyncEngine(server.recovery_runtime.transport, store)
    inputs.bind_sync_owner(engine)

    with pytest.raises(RuntimeError, match="event owner is not bound"):
        inputs.install_node_source(unbound, engine.retire())

    assert inputs._node_source is None
    with pytest.raises(RuntimeError, match="inactive"):
        unbound.run_due()
    inputs.stop()


def test_same_fields_mesh_decoy_cannot_bind_event_owner(inactive_world):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    owner = _event_mesh(store, inputs=inputs, engine=engine)
    inputs._page_preparation = SimpleNamespace(
        mesh=owner, close=lambda: None,
    )
    inputs.read_events = SimpleNamespace(bus=owner.bus, close=lambda: None)
    decoy = _event_mesh(store, inputs=inputs, engine=engine)

    with pytest.raises(ValueError, match="foreign node event source owner"):
        runtime.bind_event_owner(decoy, inputs)

    runtime.bind_event_owner(owner, inputs)
    inputs.stop()


def test_changed_node_publication_wakes_page_and_read_model(
        inactive_world, monkeypatch):
    from types import SimpleNamespace

    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    proof = engine.retire()
    _bind_event_owner(runtime, store, inputs, engine)
    inputs.install_node_source(runtime, proof)
    result = NodeInputPublishResult(CHAT, "published", True, 1, None)
    monkeypatch.setattr(
        runtime, "run_due",
        lambda: NodePageRuntimeStep(
            "publication", 0.0, publication=result,
        ),
    )
    page_requests = []
    read_events = []
    monkeypatch.setattr(
        inputs, "request_page", lambda chat: page_requests.append(chat) or True,
    )
    inputs.read_events = SimpleNamespace(
        changed=lambda scope, chat: read_events.append((scope, chat)),
        close=lambda: None,
    )

    assert inputs.run_due()
    assert page_requests == [CHAT]
    assert read_events == [("chat", CHAT)]
    inputs.stop()


def test_bound_event_consumer_runs_one_journal_quantum(world):
    runtime, store, _server, _clock = world
    subscription = runtime._events.bus.subscribe()
    record = {
        "id": "node-message", "ns": 11, "from": "alice",
        "kind": "message", "text": "ready",
    }
    store.upsert_messages(CHAT, (record,), observed_ns=1)
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, record["id"], record["ns"], record["from"], record["kind"]),
    )
    conn.commit()
    runtime._lane_cursor = 2

    step = runtime.run_due()

    assert step.operation == "event"
    assert step.event is not None and step.event.operation == "events"
    received = list(subscription.drain())
    assert [(item.type, item.chat_id, item.data) for item in received] == [
        ("read_model", CHAT, {"scope": "chat"}),
    ]
    with pytest.raises(RuntimeError, match="already bound"):
        runtime.bind_event_owner(_event_mesh(store), None)


def test_transfer_disables_stale_exact_append_and_settlement_wakes_node(
        inactive_world):
    runtime, store, server, _clock = inactive_world
    transport = server.recovery_runtime.transport
    inputs = LocalInputRuntime(transport, store)
    inputs.request(CHAT, selected=True)
    reader = inputs.reader(CHAT)
    with inputs.coordinator.publication_gate(store, reader.definition):
        expected = local_source.retire_for_publication(
            store, local_source.capture(store, reader.definition.source),
        )
    stage = staged_source.begin(
        store, reader.definition.source, CHAT, expected=expected,
    )
    staged_source.append(
        store, stage,
        {f"chats/{CHAT}/meta.json": {"members": {"alice": True}}},
    )
    staged_source.finish(store, stage)
    with inputs.coordinator.publication_gate(store, reader.definition):
        admitted, _index = staged_publication.admit(
            store, expected, stage, observed_ns=1,
        )
    assert admitted.ready and not admitted.writes_pending
    with store._conn() as conn:
        store._insert_messages(conn, CHAT, ({
            "id": "legacy", "ns": 7, "from": "alice",
            "kind": "message", "text": "ready",
        },), observed_ns=1)
    current = local_source.capture(store, reader.definition.source)
    assert current.ready and not current.writes_pending

    record = {
        "id": "local", "ns": 9, "from": "alice",
        "kind": "message", "text": "pending",
    }
    stale = inputs.prepare_local_append(CHAT, record)
    assert stale is not None
    engine = SyncEngine(transport, store)
    inputs.bind_sync_owner(engine)
    proof = engine.retire()
    _bind_event_owner(runtime, store, inputs, engine)
    inputs.install_node_source(runtime, proof)

    assert inputs.prepare_local_append(CHAT, record) is None
    assert not inputs.admit_local_append(stale, record)
    before = runtime.node._wake_revision
    assert inputs.local_append_settled(CHAT, record)
    assert runtime.node._wake_revision == before + 1
    assert store.message_count(CHAT) == 1
    inputs.stop()


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
    publication = NodeInputPublishResult(CHAT, "published", True, 1, None)
    with pytest.raises(ValueError):
        NodePageRuntimeStep("none", 0.0, publication=publication)
