"""Shared root/node mutation and finalization fence contracts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from agentbridge.node import (
    NodeCaptureRequest, NodeDocument, NodeGenerationChanged, NodeInputBatch,
    NodeStore, ReplicaIdentity,
)
from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.mutation_reservation import FinalizationMutation
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.supabase import SupabaseTransport


S = source_selectors.Selector


def _identity():
    return ReplicaIdentity(
        "https://example.supabase.co", "supabase://mesh", "alice", "machine",
    )


def _root_identity():
    transport = SupabaseTransport(
        "mesh", env={"SUPABASE_URL": "https://example.supabase.co"},
        client=object(),
    )
    return root_identity(transport)


def _admit(node: NodeStore, value: bytes = b"one") -> int:
    generation = node.begin_candidate(created_ns=1)
    node.seal_candidate(generation, NodeInputBatch(documents=(
        NodeDocument("chats/room/meta.json", 1, False, value),
    )), observed_ns=2)
    node.admit_candidate(generation)
    return generation


@pytest.fixture
def world(tmp_path):
    root = MutationCoordinator(tmp_path / "home", _root_identity())
    store = Store(tmp_path / "client.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    root.register_store(store)
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    root.register_node(node)
    definition = source_selectors.definition(
        root.identity, (S("doc_prefix", "chats/room"),), build="node-fence-test",
    )
    with root.publication_gate(store, definition):
        expected = local_source.capture(store, definition.source)
        local_source.publish(store, expected, {"ready": True}, observed_ns=1)
    _admit(node)
    capture = node.capture(NodeCaptureRequest(
        exact_document_paths=("chats/room/meta.json",), max_bytes=4_096,
    ))
    position = node.finalization_position(capture)
    try:
        yield root, store, node, definition, position
    finally:
        store.close()


def test_begin_retires_node_before_provider_and_completion_never_restores(world):
    root, _store, node, _definition, position = world
    before = next(item for item in position.scope_positions
                  if item.scope_kind == "root")
    intent = root.begin((S("doc_exact", "chats/room/meta.json"),))
    pending = node.status(node_epoch="node", started_ns=1)
    assert pending.health == "pending" and pending.pending_mutations == 1
    capture = node.capture(NodeCaptureRequest(max_bytes=4_096))
    after_begin = next(item for item in capture.scope_positions
                       if item.scope_kind == "root")
    assert after_begin.generation == before.generation + 1
    assert after_begin.pending_reason == "local_mutation"
    with pytest.raises(NodeGenerationChanged, match="mutation pending"):
        node.begin_refresh(attempted_ns=3, expected_generation=capture.generation)
    with pytest.raises(NodeGenerationChanged, match="mutation pending"):
        node.begin_candidate(created_ns=3)

    root.complete(intent)
    completed = node.status(node_epoch="node", started_ns=1)
    assert completed.health == "pending" and completed.pending_mutations == 1
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchall() == [("completed",)]
        assert conn.execute(
            "SELECT generation,pending_reason FROM scope_versions "
            "WHERE scope_kind='root' AND scope_id=''"
        ).fetchone() == (before.generation + 2, "local_mutation")
    assert NodeStore(node.path, _identity()).status(
        node_epoch="reopened", started_ns=2).pending_mutations == 1


def test_node_position_is_rechecked_under_finalization_cut(world):
    root, store, node, definition, position = world
    with root.finalization_cut(
            store, definition, node_position=position) as (_conn, source):
        assert source.ready

    generation = node.begin_candidate(created_ns=3)
    node.seal_candidate(generation, NodeInputBatch(documents=(
        NodeDocument("chats/room/meta.json", 2, False, b"two"),
    )), observed_ns=4)
    node.admit_candidate(generation)
    with pytest.raises(NodeGenerationChanged, match="generation changed"):
        with root.finalization_cut(store, definition, node_position=position):
            pytest.fail("stale node position reached caller")


def test_finalization_preflights_keyed_node_registration(world):
    root, store, _node, definition, position = world
    with sqlite3.connect(root.path) as conn:
        conn.execute(
            "UPDATE mutation_nodes SET database_incarnation=zeroblob(4097) "
            "WHERE path=?", (position.database_path,),
        )

    with pytest.raises(local_source.SourceChanged, match="invalid_node_registration"):
        with root.finalization_cut(
                store, definition, node_position=position):
            pytest.fail("corrupt node registration reached caller")


def test_finalization_reservation_retires_node_before_external_call(world):
    root, store, node, definition, position = world
    ticket = FinalizationMutation(
        root, (S("doc_exact", "chats/room/meta.json"),),
    )
    with root.finalization_cut(
            store, definition, mutation=ticket,
            node_position=position):
        ticket.accepted = True

    assert node.status(node_epoch="node", started_ns=1).pending_mutations == 1

    def provider():
        status = node.status(node_epoch="node", started_ns=1)
        assert status.health == "pending" and status.pending_mutations == 1
        return "ok"

    assert ticket.execute(provider) == "ok"
    assert node.status(node_epoch="node", started_ns=1).pending_mutations == 1
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchall() == [("completed",)]


def test_registering_previously_admitted_node_retires_inherited_readiness(tmp_path):
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    _admit(node)
    root = MutationCoordinator(tmp_path / "home", _root_identity())
    root.register_node(node)
    status = node.status(node_epoch="node", started_ns=1)
    assert status.health == "pending" and status.pending_mutations == 1
    with pytest.raises(ValueError, match="invalid node finalization position"):
        node.finalization_position(node.capture(NodeCaptureRequest(max_bytes=4_096)))


def test_pending_root_intent_is_imported_when_node_registers(tmp_path):
    root = MutationCoordinator(tmp_path / "home", _root_identity())
    intent = root.begin((S("doc_exact", "users/alice.json"),))
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    root.register_node(node)
    assert node.status(node_epoch="node", started_ns=1).pending_mutations == 1
    root.complete(intent)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchone() == ("completed",)


def test_registration_discards_candidate_that_missed_completed_mutation(tmp_path):
    root = MutationCoordinator(tmp_path / "home", _root_identity())
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    generation = node.begin_candidate(created_ns=1)
    node.seal_candidate(generation, NodeInputBatch(documents=(
        NodeDocument("users/alice.json", 1, False, b"old"),
    )), observed_ns=2)
    intent = root.begin((S("doc_exact", "users/alice.json"),))
    root.complete(intent)

    root.register_node(node)

    with pytest.raises(NodeGenerationChanged, match="candidate is not sealed"):
        node.admit_candidate(generation)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?", (generation,)
        ).fetchone() == ("abandoned",)


def test_mutation_start_clears_rows_before_abandoning_private_candidate(world):
    root, _store, node, _definition, _position = world
    generation = node.begin_candidate(created_ns=3)
    node.seal_candidate(generation, NodeInputBatch(documents=(
        NodeDocument("users/private.json", 2, False, b"private"),
    )), observed_ns=4)

    root.begin((S("doc_exact", "users/later.json"),))

    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (generation,),
        ).fetchone() == ("abandoned",)
        assert conn.execute(
            "SELECT count(*) FROM candidate_docs WHERE generation=?",
            (generation,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM candidate_chunks WHERE generation=?",
            (generation,),
        ).fetchone() == (0,)


def test_database_guard_blocks_pre_v8_writer_while_fence_is_pending(world):
    root, _store, node, _definition, _position = world
    root.begin((S("doc_exact", "chats/room/meta.json"),))

    with sqlite3.connect(node.path) as conn, pytest.raises(
            sqlite3.IntegrityError, match="local node mutation pending"):
        conn.execute(
            "UPDATE node_meta SET health='catching_up',last_attempt_ns=3 "
            "WHERE singleton=1"
        )
    with sqlite3.connect(node.path) as conn, pytest.raises(
            sqlite3.IntegrityError, match="local node mutation pending"):
        conn.execute(
            "UPDATE node_meta SET admitted_generation=admitted_generation+1,"
            "health='ready' WHERE singleton=1"
        )


def test_begin_fanout_failure_leaves_node_pending_and_no_root_intent(
        world, monkeypatch):
    root, _store, node, _definition, _position = world

    def fail_store_fanout(*_args, **_kwargs):
        raise OSError("client store unavailable")

    monkeypatch.setattr(root, "_retire", fail_store_fanout)
    with pytest.raises(OSError, match="client store unavailable"):
        root.begin((S("doc_exact", "chats/room/meta.json"),))
    with root._transaction() as conn:
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (0,)
    assert node.status(node_epoch="node", started_ns=1).pending_mutations == 1
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchone() == ("active",)


def test_root_commit_failure_withholds_ticket_but_keeps_node_retired(
        world, monkeypatch):
    root, store, node, definition, position = world
    ticket = FinalizationMutation(
        root, (S("doc_exact", "chats/room/meta.json"),),
    )
    original = root._transaction

    @contextmanager
    def fail_root_commit(**kwargs):
        with original(**kwargs) as conn:
            yield conn
            raise OSError("root commit unavailable")

    monkeypatch.setattr(root, "_transaction", fail_root_commit)
    with pytest.raises(OSError, match="root commit unavailable"):
        with root.finalization_cut(
                store, definition, mutation=ticket,
                node_position=position):
            ticket.accepted = True
    assert node.status(node_epoch="node", started_ns=1).pending_mutations == 1
    with pytest.raises(ValueError, match="not reserved"):
        ticket.execute(lambda: pytest.fail("provider must not start"))
