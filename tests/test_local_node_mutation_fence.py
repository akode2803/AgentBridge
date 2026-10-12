"""Shared root/node mutation and finalization fence contracts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agentbridge.node import (
    NodeCaptureRequest, NodeDocument, NodeGenerationChanged, NodeInputBatch,
    NodeStore, ReplicaIdentity,
)
from agentbridge.node.admission import NodeProviderCut
from agentbridge.store import local_source, source_selectors
from agentbridge.store.local_source import SourceChanged
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


def _cut(cursor: int = 5) -> NodeProviderCut:
    return NodeProviderCut(
        1, "index:source-v1", "source-epoch", "alice", "member", 0, cursor)


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


def test_node_position_is_rechecked_and_held_under_publication_gate(world):
    root, store, node, definition, position = world
    with root.publication_gate(
            store, definition, node_position=position):
        contender = sqlite3.connect(node.path, timeout=0.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()

    generation = node.begin_candidate(created_ns=3)
    node.seal_candidate(generation, NodeInputBatch(documents=(
        NodeDocument("chats/room/meta.json", 2, False, b"two"),
    )), observed_ns=4)
    node.admit_candidate(generation)
    with pytest.raises(NodeGenerationChanged, match="generation changed"):
        with root.publication_gate(
                store, definition, node_position=position):
            pytest.fail("stale node position reached publisher")


def test_publication_node_position_must_cover_log_scope(world):
    root, store, _node, _definition, position = world
    selected = source_selectors.definition(
        root.identity, (S("log_chat", "room"),), build="node-log-test",
    )
    root_only = replace(position, scope_positions=tuple(
        item for item in position.scope_positions if item.scope_kind == "root"
    ))
    with pytest.raises(ValueError, match="does not cover"):
        with root.publication_gate(
                store, selected, node_position=root_only):
            pytest.fail("incomplete node scope reached publisher")


def test_publication_validates_node_position_before_scope_enumeration(world):
    root, store, _node, definition, position = world
    malformed = replace(
        position,
        scope_positions=(item for item in position.scope_positions),
    )
    with pytest.raises(ValueError, match="invalid node finalization position"):
        with root.publication_gate(
                store, definition, node_position=malformed):
            pytest.fail("malformed node position reached publisher")


@pytest.mark.parametrize("scope_id", ([], "x" * 1_025))
def test_publication_preflights_nested_node_scope_fields(world, scope_id):
    root, store, _node, definition, position = world
    malformed = replace(position, scope_positions=tuple(
        replace(item, scope_id=scope_id)
        if item.scope_kind == "chat" else item
        for item in position.scope_positions
    ))
    with pytest.raises(ValueError, match="invalid node finalization position"):
        with root.publication_gate(
                store, definition, node_position=malformed):
            pytest.fail("malformed nested node scope reached publisher")


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


def test_completed_fence_set_is_claimed_once_under_root_node_cut(world):
    root, _store, node, _definition, _position = world
    intent = root.begin((S("doc_exact", "chats/room/meta.json"),))
    root.complete(intent)

    claim = root.claim_node_recovery(node)
    repeated = root.claim_node_recovery(node)

    assert repeated == claim
    assert claim.coordinator_path == str(root.path)
    assert claim.coordinator_identity == root.identity
    assert claim.coordinator_epoch == root.epoch
    assert claim.fence_tokens == (intent.token,)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT coordinator_path,coordinator_identity,coordinator_epoch "
            "FROM node_coordinator_owner"
        ).fetchone() == (str(root.path), root.identity, root.epoch)
        assert conn.execute(
            "SELECT claim_id,claim_digest,root_revision FROM node_recovery_claims"
        ).fetchone() == (claim.claim_id, claim.claim_digest, claim.root_revision)


def test_claimed_recovery_begins_idempotently_and_pages_while_pending(world):
    root, _store, node, _definition, _position = world
    intent = root.begin((S("doc_exact", "users/a.json"),))
    root.complete(intent)
    claim = root.claim_node_recovery(node)
    current = _cut()

    state = root.begin_node_recovery(node, claim, current, created_ns=10)
    repeated = root.begin_node_recovery(node, claim, current, created_ns=11)

    assert repeated.recovery_id == state.recovery_id
    assert repeated.generation == state.generation
    assert state.health_owner_token == 0
    assert node.status(node_epoch="node", started_ns=1).health == "pending"
    with pytest.raises(NodeGenerationChanged, match="claimed local recovery changed"):
        root.begin_node_recovery(node, claim, _cut(6), created_ns=12)
    node.stage_recovery_documents(
        state.recovery_id, current, "documents-empty", 0, b"", (),
        has_more=False,
    )
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT recovery_id,claim_id,claim_digest FROM claimed_recoveries"
        ).fetchone() == (state.recovery_id, claim.claim_id, claim.claim_digest)


def test_root_revision_movement_rejects_claimed_recovery_start(world, tmp_path):
    root, _store, node, _definition, _position = world
    claim = root.claim_node_recovery(node)
    other = NodeStore(
        tmp_path / "other-node" / "replica.sqlite",
        ReplicaIdentity(
            "https://example.supabase.co", "supabase://mesh", "alice", "other",
        ),
    )
    root.register_node(other)

    with pytest.raises(SourceChanged, match="node_recovery_claim_changed"):
        root.begin_node_recovery(node, claim, _cut(), created_ns=10)
    replacement = root.claim_node_recovery(node)
    assert replacement.root_revision > claim.root_revision
    assert replacement.claim_id != claim.claim_id


def test_claimed_recovery_rejects_provider_principal_rebind(world):
    root, _store, node, _definition, _position = world
    claim = root.claim_node_recovery(node)
    wrong = NodeProviderCut(
        1, "index:source-v1", "source-epoch", "bob", "member", 0, 5)

    with pytest.raises(NodeGenerationChanged, match="provider principal changed"):
        root.begin_node_recovery(node, claim, wrong, created_ns=10)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM provider_recoveries").fetchone() == (0,)


def test_later_mutation_invalidates_claimed_recovery_before_provider_page(world):
    root, _store, node, _definition, _position = world
    claim = root.claim_node_recovery(node)
    state = root.begin_node_recovery(node, claim, _cut(), created_ns=10)

    root.begin((S("doc_exact", "users/later.json"),))

    with pytest.raises(NodeGenerationChanged, match="unknown recovery"):
        node.recovery_state(state.recovery_id, _cut())
    with sqlite3.connect(node.path) as conn:
        assert conn.execute("SELECT count(*) FROM node_recovery_claims").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM claimed_recoveries").fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=?",
            (state.generation,),
        ).fetchone() == ("abandoned",)


def test_ordinary_recovery_cannot_start_behind_unconsumed_claim(world):
    root, _store, node, _definition, _position = world
    root.claim_node_recovery(node)
    plan = node.root_recovery_plan(_cut())

    with pytest.raises(NodeGenerationChanged, match="recovery claim is active"):
        node.begin_recovery(plan, created_ns=10)


def test_active_or_later_mutation_cannot_join_recovery_claim(world):
    root, _store, node, _definition, _position = world
    first = root.begin((S("doc_exact", "users/a.json"),))
    with pytest.raises(SourceChanged, match="root_mutation_pending"):
        root.claim_node_recovery(node)
    root.complete(first)
    claim = root.claim_node_recovery(node)

    second = root.begin((S("doc_exact", "users/b.json"),))
    with pytest.raises(SourceChanged, match="root_mutation_pending"):
        root.claim_node_recovery(node)
    root.complete(second)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute("SELECT claim_id FROM node_recovery_claims").fetchone() is None
    replacement = root.claim_node_recovery(node)
    assert replacement.claim_id != claim.claim_id
    assert replacement.fence_tokens == tuple(sorted((first.token, second.token)))


def test_foreign_coordinator_cannot_replace_bound_node_owner(tmp_path):
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    first = MutationCoordinator(tmp_path / "first", _root_identity())
    first.register_node(node)
    second = MutationCoordinator(tmp_path / "second", _root_identity())

    with pytest.raises(NodeGenerationChanged, match="coordinator owner changed"):
        second.register_node(node)


def test_corrupt_owner_singleton_cannot_be_rebound(tmp_path):
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    first = MutationCoordinator(tmp_path / "first", _root_identity())
    first.register_node(node)
    with sqlite3.connect(node.path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE node_coordinator_owner SET singleton=2")

    with pytest.raises(sqlite3.DatabaseError, match="invalid local node coordinator owner"):
        NodeStore(node.path, _identity())
    second = MutationCoordinator(tmp_path / "second", _root_identity())
    with pytest.raises(sqlite3.DatabaseError, match="invalid local node coordinator owner"):
        second.register_node(node)


def test_migrated_unowned_fence_refuses_coordinator_binding(tmp_path):
    node = NodeStore(tmp_path / "node" / "replica.sqlite", _identity())
    registration = node.coordinator_registration()
    with NodeStore.coordinator_cut(registration) as conn:
        NodeStore.coordinator_add_fence(
            conn, coordinator_epoch="1" * 32, token="2" * 64,
            state="completed",
        )
    root = MutationCoordinator(tmp_path / "home", _root_identity())

    with pytest.raises(NodeGenerationChanged, match="unowned local node mutation fences"):
        root.register_node(node)


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


@pytest.mark.parametrize("corruption", ("fence", "scope"))
def test_mutation_fence_audit_preflights_bounded_fields(world, corruption):
    root, _store, node, _definition, _position = world
    root.begin((S("doc_exact", "users/later.json"),))
    with sqlite3.connect(node.path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        if corruption == "fence":
            conn.execute(
                "UPDATE node_mutation_fences SET token=zeroblob(65)")
        else:
            conn.execute(
                "UPDATE node_mutation_scopes SET scope_id=zeroblob(1025)")

    with pytest.raises(sqlite3.DatabaseError, match="invalid local node mutation fence"):
        NodeStore(node.path, _identity())


def test_reserved_permit_row_cannot_weaken_foundation_guard(world):
    root, _store, node, _definition, _position = world
    root.begin((S("doc_exact", "chats/room/meta.json"),))
    with sqlite3.connect(node.path) as conn:
        incarnation, base, pending = conn.execute(
            "SELECT database_incarnation,admitted_generation,pending_mutations "
            "FROM node_meta WHERE singleton=1"
        ).fetchone()
        conn.execute(
            "INSERT INTO node_admission_permits VALUES(1,?,?,?,?,?,?,?,0)",
            ("1" * 48, incarnation, base, base + 1, "2" * 64, 0, pending),
        )
        with pytest.raises(sqlite3.IntegrityError, match="local node mutation pending"):
            conn.execute(
                "UPDATE node_meta SET admitted_generation=?,pending_mutations=0,"
                "health='ready' WHERE singleton=1", (base + 1,),
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
    with pytest.raises(NodeGenerationChanged, match="fences are not claimable"):
        root.claim_node_recovery(node)
    with root.node_activation_gate(node) as ready:
        assert ready is False


def test_activation_gate_allows_completed_recovery_fence(world):
    root, _store, node, _definition, _position = world
    registration = node.coordinator_registration()
    with NodeStore.coordinator_cut(registration) as conn:
        NodeStore.coordinator_add_fence(
            conn, coordinator_epoch=root.epoch, token="f" * 64,
            state="completed",
        )

    with root.node_activation_gate(node) as ready:
        assert ready is True


def test_completion_node_commit_then_root_rollback_cannot_be_claimed(
        world, monkeypatch):
    root, _store, node, _definition, _position = world
    intent = root.begin((S("doc_exact", "chats/room/meta.json"),))
    original = root._transaction

    @contextmanager
    def fail_root_commit(**kwargs):
        with original(**kwargs) as conn:
            yield conn
            raise OSError("root commit unavailable")

    monkeypatch.setattr(root, "_transaction", fail_root_commit)
    with pytest.raises(OSError, match="root commit unavailable"):
        root.complete(intent)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute(
            "SELECT state FROM node_mutation_fences"
        ).fetchone() == ("completed",)
    monkeypatch.setattr(root, "_transaction", original)
    with pytest.raises(SourceChanged, match="root_mutation_pending"):
        root.claim_node_recovery(node)


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
