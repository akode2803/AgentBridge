"""Caller-driven scheduling and publication from the coherent local node."""
from __future__ import annotations

import json

import pytest

from agentbridge.mesh import node_input_publisher as publisher_module
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.node_input_publisher import (
    NodeInputPublishResult, NodeInputPublisher,
)
from agentbridge.node.admission import NodeDocument, NodeInputBatch, NodeLogRow
from agentbridge.node.client import (
    LocalNodeClient, NodeClientGenerationChanged, NodeClientUnavailable,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.server import LocalNodeServer
from agentbridge.store import local_source
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator


CHAT = "room"


def _identity():
    return ReplicaIdentity(
        "https://example.test", "supabase://mesh", "alice", "box",
    )


def _root(identity):
    return json.dumps([
        "local-root-v1", "supabase", identity.provider_endpoint,
        identity.root.removeprefix("supabase://"),
    ], separators=(",", ":"))


def _admit(server, message_id, ns):
    generation = server.store.begin_candidate(created_ns=ns)
    server.store.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument(
            f"chats/{CHAT}/meta.json", ns, False,
            b'{"members":{"alice":true}}',
        ),),
        log_rows=(NodeLogRow(
            ns, CHAT, "alice@box",
            json.dumps({
                "id": message_id, "ns": ns, "from": "alice",
                "kind": "message", "text": message_id,
            }, separators=(",", ":")).encode(),
        ),),
        documents_mode="replace", logs_mode="replace",
    ), observed_ns=ns)
    server.store.admit_candidate(generation)
    return generation


@pytest.fixture
def world(tmp_path):
    identity = _identity()
    coordinator = MutationCoordinator(tmp_path / "home", _root(identity))
    store = Store(tmp_path / "store.sqlite")
    server = LocalNodeServer(tmp_path / "node", identity).start()
    assert server.store is not None
    coordinator.register_node(server.store)
    _admit(server, "first", 1)
    client = LocalNodeClient(tmp_path / "node", identity)
    publisher = NodeInputPublisher(coordinator, store, client)
    try:
        yield coordinator, store, server, publisher
    finally:
        server.stop()
        store.close()


def test_initial_publish_and_same_generation_skip(world):
    coordinator, store, _server, publisher = world
    first = publisher.publish(CHAT)

    assert first.state == "published" and first.changed
    assert first.node_generation == 1
    assert store.messages(CHAT) == [{
        "id": "first", "ns": 1, "from": "alice", "kind": "message",
        "text": "first",
    }]
    receipt = LocalPageSource(coordinator, store, CHAT).capture()

    unchanged = publisher.publish(CHAT)

    assert unchanged == NodeInputPublishResult(
        CHAT, "unchanged", False, 1, first.source,
    )
    assert LocalPageSource(coordinator, store, CHAT).capture() == receipt


def test_new_node_generation_atomically_replaces_page_inputs(world):
    coordinator, store, server, publisher = world
    first = publisher.publish(CHAT)
    _admit(server, "second", 2)

    second = publisher.publish(CHAT)

    assert second.state == "published" and second.changed
    assert second.node_generation == 2
    assert second.source != first.source
    assert store.messages(CHAT) == [{
        "id": "second", "ns": 2, "from": "alice", "kind": "message",
        "text": "second",
    }]
    assert LocalPageSource(
        coordinator, store, CHAT,
    ).capture().node_position.admitted_generation == 2


def test_legacy_message_writer_fails_closed_until_complete_republication(world):
    coordinator, store, _server, publisher = world
    first = publisher.publish(CHAT)
    original = LocalPageSource(coordinator, store, CHAT).capture()
    with store._conn() as conn:
        store._insert_messages(conn, CHAT, ({
            "id": "hybrid", "ns": 3, "from": "legacy",
            "kind": "message", "text": "must not become readable",
        },), observed_ns=3)

    with pytest.raises(
            local_source.SourceChanged,
            match="node publication receipt changed"):
        LocalPageSource(coordinator, store, CHAT).capture()

    repaired = publisher.publish(CHAT)

    assert repaired.state == "published" and repaired.changed
    assert repaired.node_generation == first.node_generation
    assert store.messages(CHAT) == [{
        "id": "first", "ns": 1, "from": "alice", "kind": "message",
        "text": "first",
    }]
    current = LocalPageSource(coordinator, store, CHAT).capture()
    assert current.node_messages != original.node_messages
    assert current.node_messages == store.capture_membership_input_position(CHAT)
    assert publisher.publish(CHAT).state == "unchanged"


def test_unavailable_refresh_preserves_last_readable_snapshot(world):
    coordinator, store, server, publisher = world
    first = publisher.publish(CHAT)
    receipt = LocalPageSource(coordinator, store, CHAT).capture()
    server.stop()

    failed = publisher.publish(CHAT)

    assert failed.state == "unavailable" and not failed.changed
    assert local_source.capture(
        store, first.source.source_id,
    ) == first.source
    assert LocalPageSource(coordinator, store, CHAT).capture() == receipt
    health = local_source.health(store, first.source.source_id)
    assert health["ready"] and health["failures"] == 1
    assert health["error"] == "unavailable"


def test_degraded_same_generation_preserves_receipt_and_records_health(world):
    coordinator, store, server, publisher = world
    first = publisher.publish(CHAT)
    receipt = LocalPageSource(coordinator, store, CHAT).capture()
    server.store.note_refresh_state(
        "degraded", attempted_ns=5,
        expected_generation=first.node_generation,
    )

    degraded = publisher.publish(CHAT)

    assert degraded.state == "degraded" and not degraded.changed
    assert LocalPageSource(coordinator, store, CHAT).capture() == receipt
    health = local_source.health(store, first.source.source_id)
    assert health["ready"] and health["error"] == "unavailable"


def test_ready_same_generation_clears_prior_refresh_failure(world):
    _coordinator, store, server, publisher = world
    first = publisher.publish(CHAT)
    server.store.note_refresh_state(
        "degraded", attempted_ns=5,
        expected_generation=first.node_generation,
    )
    assert publisher.publish(CHAT).state == "degraded"
    server.store.note_refresh_state(
        "ready", attempted_ns=6,
        expected_generation=first.node_generation,
    )

    recovered = publisher.publish(CHAT)

    assert recovered.state == "unchanged"
    health = local_source.health(store, first.source.source_id)
    assert health["ready"] and health["failures"] == 0
    assert health["error"] == ""


def test_message_race_during_same_generation_observation_is_superseded(
        world, monkeypatch):
    coordinator, store, _server, publisher = world
    first = publisher.publish(CHAT)
    original = LocalPageSource.record_refresh_observation

    def race(reader, receipt, **kwargs):
        with store._conn() as conn:
            store._insert_messages(conn, CHAT, ({
                "id": "raced", "ns": 3, "from": "legacy",
                "kind": "message", "text": "raced",
            },), observed_ns=3)
        return original(reader, receipt, **kwargs)

    monkeypatch.setattr(LocalPageSource, "record_refresh_observation", race)
    raced = publisher.publish(CHAT)

    assert raced.state == "superseded" and not raced.changed
    with pytest.raises(local_source.SourceChanged):
        LocalPageSource(coordinator, store, CHAT).capture()

    monkeypatch.setattr(
        LocalPageSource, "record_refresh_observation", original,
    )
    repaired = publisher.publish(CHAT)
    assert repaired.state == "published" and repaired.changed
    assert repaired.node_generation == first.node_generation


def test_first_publication_preserves_degraded_node_health(world):
    _coordinator, store, server, publisher = world
    server.store.note_refresh_state(
        "degraded", attempted_ns=5, expected_generation=1,
    )
    status = server.store.status(node_epoch="test", started_ns=1)

    published = publisher.publish(CHAT)

    assert published.state == "published"
    health = local_source.health(store, published.source.source_id)
    assert health["ready"] and health["failures"] == 1
    assert health["error"] == "unavailable"
    assert health["last_success_ns"] == status.last_success_ns


def test_refresh_failure_does_not_restore_invalidated_source(world):
    _coordinator, store, server, publisher = world
    first = publisher.publish(CHAT)
    local_source.invalidate(store, first.source.source_id)
    pending = local_source.capture(store, first.source.source_id)
    server.stop()

    failed = publisher.publish(CHAT)

    assert failed.state == "unavailable"
    assert local_source.capture(store, first.source.source_id) == pending
    health = local_source.health(store, first.source.source_id)
    assert not health["ready"] and health["failures"] == 1


def test_source_movement_during_failed_status_request_wins_health_cas(
        world, monkeypatch):
    _coordinator, store, _server, publisher = world
    first = publisher.publish(CHAT)

    def fail_after_invalidation():
        local_source.invalidate(store, first.source.source_id)
        raise NodeClientUnavailable("unavailable")

    monkeypatch.setattr(publisher.client, "status", fail_after_invalidation)
    failed = publisher.publish(CHAT)

    assert failed.state == "unavailable"
    current = local_source.capture(store, first.source.source_id)
    assert not current.ready and current.revision == first.source.revision + 1
    health = local_source.health(store, first.source.source_id)
    assert health["failures"] == 0 and health["error"] == ""


def test_selected_request_precedes_background_without_foreground_io(
        world, monkeypatch):
    _coordinator, _store, _server, publisher = world
    clock = [0.0]
    publisher.clock = lambda: clock[0]
    calls = []

    def publish(chat):
        calls.append(chat)
        return NodeInputPublishResult(chat, "unchanged", False, 1, None)

    monkeypatch.setattr(publisher, "publish", publish)
    assert publisher.request("background")
    assert publisher.request("selected", selected=True)
    assert publisher.run_due() is False
    clock[0] = 0.05
    assert publisher.run_due() is True
    assert calls == ["selected"]


def test_scheduler_releases_job_after_unexpected_publisher_error(
        world, monkeypatch):
    _coordinator, _store, _server, publisher = world
    clock = [0.0]
    publisher.clock = lambda: clock[0]
    publisher.request(CHAT)
    monkeypatch.setattr(
        publisher, "publish", lambda _chat: (_ for _ in ()).throw(
            RuntimeError("unexpected"),
        ),
    )

    clock[0] = 0.05
    assert publisher.run_due() is True
    assert publisher.schedule._running is None
    clock[0] = 4.05
    assert publisher.run_due() is True


@pytest.mark.parametrize(
    ("error", "state", "blocked", "failures"),
    [
        (NodeClientGenerationChanged("race"), "superseded", 0, 1),
        (local_source.SourceChanged("source_mutation_pending"),
         "source_mutation_pending", 1, 0),
    ],
)
def test_scheduler_distinguishes_transient_race_from_durable_mutation(
        world, monkeypatch, error, state, blocked, failures):
    _coordinator, _store, _server, publisher = world
    clock = [0.0]
    publisher.clock = lambda: clock[0]
    monkeypatch.setattr(
        publisher_module.coupled_publication,
        "publish", lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    result = publisher.publish(CHAT)
    assert result.state == state
    monkeypatch.setattr(publisher, "publish", lambda _chat: result)
    assert publisher.request(CHAT)
    clock[0] = 0.05
    assert publisher.run_due()
    scheduled = publisher.schedule._states[CHAT]
    assert scheduled.blocked == blocked
    assert scheduled.failures == failures
    assert publisher.schedule._running is None
    clock[0] = 4.05
    assert publisher.run_due() is True


def test_invalid_result_is_rejected():
    with pytest.raises(ValueError, match="state"):
        NodeInputPublishResult(CHAT, "wrong", False, 0, None)
