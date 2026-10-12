"""Atomic node/Store publication of coupled document and message inputs."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from agentbridge.mesh.local_page_source import definition
from agentbridge.node.admission import NodeDocument, NodeInputBatch, NodeLogRow
from agentbridge.node.client import LocalNodeClient
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.server import LocalNodeServer
from agentbridge.store import (
    coupled_admission, coupled_payload_stage, coupled_publication,
    coupled_stage, local_source, membership_input_position, source_selectors,
    staged_publication, staged_source,
)
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


CHAT = "room"


def _identity():
    return ReplicaIdentity(
        "https://example.test", "supabase://mesh", "alice", "box",
    )


def _definition():
    identity = _identity()
    root = json.dumps([
        "local-root-v1", "supabase", identity.provider_endpoint,
        identity.root.removeprefix("supabase://"),
    ], separators=(",", ":"))
    return definition(root, CHAT)


def _admit(server, *, message_id="new", ns=7, created=1, observed=2):
    generation = server.store.begin_candidate(created_ns=created)
    server.store.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument(
            f"chats/{CHAT}/meta.json", ns, False,
            b'{"members":{"alice":true}}',
        ),),
        log_rows=(
            NodeLogRow(
                3, CHAT, "alice@box",
                json.dumps({
                    "id": message_id, "ns": ns, "from": "alice",
                    "kind": "message", "text": message_id,
                }, separators=(",", ":")).encode(),
            ),
            NodeLogRow(5, CHAT, "alice@box", b"not-json"),
        ),
        documents_mode="replace", logs_mode="replace",
    ), observed_ns=observed)
    server.store.admit_candidate(generation)
    return generation


@pytest.fixture
def world(tmp_path):
    source = _definition()
    coordinator = MutationCoordinator(tmp_path / "home", json.loads(
        source.serialized,
    )[0])
    store = Store(tmp_path / "store.sqlite")
    coupled_stage.initialize(store)
    coupled_admission.initialize(store)
    source_selectors.initialize(store)
    coordinator.register_store(store)
    with coordinator.publication_gate(store, source):
        initial = local_source.capture(store, source.source)
        ready = local_source.publish(
            store, initial,
            {f"chats/{CHAT}/meta.json": {"members": {"old": True}}},
            observed_ns=1,
        )
    store._insert_messages(store._conn(), CHAT, ({
        "id": "old", "ns": 1, "from": "old", "kind": "message",
    },), observed_ns=1)
    store.set_offset(CHAT, "old@box", 9)
    with coordinator.publication_gate(store, source):
        claimed = local_source.claim_collection(store, ready)
    expected = coupled_admission.capture(store, claimed, CHAT)
    with LocalNodeServer(tmp_path / "node", _identity()) as server:
        coordinator.register_node(server.store)
        _admit(server)
        client = LocalNodeClient(tmp_path / "node", _identity())
        yield coordinator, store, server, client, source, expected
    store.close()


def test_publish_replaces_messages_offsets_and_documents_in_one_cut(world):
    coordinator, store, _server, client, source, expected = world
    result = coupled_publication.publish(
        coordinator, store, expected, client, source, observed_ns=99,
    )

    assert result.source.ready
    assert result.source.raw.source_id.startswith("coupled:")
    assert result.previous_physical_source == expected.source.raw.source_id
    assert result.node_position.admitted_generation == 1
    assert store.messages(CHAT) == [{
        "id": "new", "ns": 7, "from": "alice", "kind": "message",
        "text": "new",
    }]
    assert store.log_offsets(CHAT) == {"alice@box": 5}
    assert store.message_observation(CHAT, "new") == (99, 0, "local-node")
    assert store.capture_selected_documents(
        result.source.raw, (f"chats/{CHAT}/meta.json",),
    ).documents() == {
        f"chats/{CHAT}/meta.json": {"members": {"alice": True}},
    }
    conn = store._conn()
    assert conn.execute(
        "SELECT physical FROM local_input_generations WHERE source=?",
        (source.source,),
    ).fetchone() == (result.source.raw.source_id,)
    assert conn.execute("SELECT count(*) FROM coupled_payload_stages").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM coupled_payload_raw").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM coupled_stage_owners").fetchone() == (0,)
    assert conn.execute(
        "SELECT phase FROM staged_sources WHERE source=?",
        (result.source.raw.source_id,),
    ).fetchone() == ("sealed",)


def test_failure_after_pointer_swap_rolls_back_every_visible_half(
        world, monkeypatch):
    coordinator, store, _server, client, source, expected = world

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected after source swap")

    monkeypatch.setattr(
        coupled_publication.coupled_payload_stage,
        "_consume_admitted", fail,
    )
    with pytest.raises(RuntimeError, match="after source swap"):
        coupled_publication.publish(
            coordinator, store, expected, client, source, observed_ns=99,
        )

    assert store.messages(CHAT) == [{
        "id": "old", "ns": 1, "from": "old", "kind": "message",
    }]
    assert store.log_offsets(CHAT) == {"old@box": 9}
    assert local_source.capture(store, source.source) == expected.source
    conn = store._conn()
    assert conn.execute(
        "SELECT phase FROM staged_sources WHERE source LIKE 'coupled:%'",
    ).fetchall() == [("abandoned",)]
    assert conn.execute(
        "SELECT physical FROM local_input_generations WHERE source=?",
        (source.source,),
    ).fetchone() == (expected.source.raw.source_id,)


def test_message_change_between_replacement_and_admission_rolls_back(
        world, monkeypatch):
    coordinator, store, _server, client, source, expected = world
    original = coupled_publication._admit_candidate

    def tamper(conn, *args, **kwargs):
        conn.execute(
            "UPDATE messages SET payload='{}' WHERE chat_id=? AND id='new'",
            (CHAT,),
        )
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(coupled_publication, "_admit_candidate", tamper)
    with pytest.raises(
            coupled_publication.CoupledPublicationChanged,
            match="message values changed"):
        coupled_publication.publish(
            coordinator, store, expected, client, source, observed_ns=99,
        )

    assert store.messages(CHAT) == [{
        "id": "old", "ns": 1, "from": "old", "kind": "message",
    }]
    assert store.log_offsets(CHAT) == {"old@box": 9}
    assert local_source.capture(store, source.source) == expected.source


def test_store_change_after_capture_rejects_and_preserves_newer_store(world,
                                                                     monkeypatch):
    coordinator, store, _server, client, source, expected = world
    original = coupled_publication.coupled_candidate.assemble

    def change(*args, **kwargs):
        candidate = original(*args, **kwargs)
        store.set_offset(CHAT, "later@box", 11)
        return candidate

    monkeypatch.setattr(coupled_publication.coupled_candidate, "assemble", change)
    with pytest.raises(
            coupled_admission.CoupledAdmissionChanged,
            match="changed during collection"):
        coupled_publication.publish(
            coordinator, store, expected, client, source, observed_ns=99,
        )
    assert store.messages(CHAT)[0]["id"] == "old"
    assert store.log_offsets(CHAT) == {"later@box": 11, "old@box": 9}


def test_node_change_after_capture_rejects_before_store_writer(world,
                                                                monkeypatch):
    coordinator, store, server, client, source, expected = world
    original = coupled_publication.coupled_candidate.assemble

    def change(*args, **kwargs):
        candidate = original(*args, **kwargs)
        generation = server.store.begin_candidate(created_ns=3)
        server.store.seal_candidate(generation, NodeInputBatch(
            documents=(NodeDocument(
                f"chats/{CHAT}/meta.json", 8, False,
                b'{"members":{"alice":true},"later":true}',
            ),), documents_mode="replace",
        ), observed_ns=4)
        server.store.admit_candidate(generation)
        return candidate

    monkeypatch.setattr(coupled_publication.coupled_candidate, "assemble", change)
    with pytest.raises(RuntimeError, match="generation changed"):
        coupled_publication.publish(
            coordinator, store, expected, client, source, observed_ns=99,
        )
    assert store.messages(CHAT)[0]["id"] == "old"
    assert store.log_offsets(CHAT) == {"old@box": 9}


def test_copied_candidate_positions_cannot_enter_public_api(world):
    coordinator, store, _server, client, source, expected = world
    candidate = coupled_publication.coupled_candidate.assemble(
        store, expected, client, source,
    )
    forged = replace(
        candidate,
        node_position=replace(
            candidate.node_position,
            admitted_generation=candidate.node_position.admitted_generation + 1,
        ),
    )
    with pytest.raises(TypeError):
        coupled_publication.publish(
            coordinator, store, expected, forged, source, observed_ns=99,
        )
    coupled_stage.abort(store, candidate.owner)


def test_payload_consumption_is_transactional(world):
    _coordinator, store, _server, client, source, expected = world
    candidate = coupled_publication.coupled_candidate.assemble(
        store, expected, client, source,
    )
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(coupled_payload_stage.CoupledPayloadChanged):
        coupled_payload_stage._consume_admitted(conn, store, candidate.payload)
    conn.rollback()
    coupled_stage.abort(store, candidate.owner)


def test_publish_retires_prior_staged_physical_in_same_transaction(world):
    coordinator, store, _server, client, source, expected = world
    prior = staged_source.begin(
        store, expected.source.source_id, CHAT, expected=expected.source,
    )
    staged_source.append(
        store, prior,
        {f"chats/{CHAT}/meta.json": {"members": {"old": True}}},
    )
    staged_source.finish(store, prior)
    ready, _index = staged_publication.admit(
        store, expected.source, prior, observed_ns=5,
    )
    with coordinator.publication_gate(store, source):
        claimed = local_source.claim_collection(store, ready)
    current = coupled_admission.capture(store, claimed, CHAT)

    result = coupled_publication.publish(
        coordinator, store, current, client, source, observed_ns=99,
    )

    assert result.previous_physical_source == prior.source_id
    assert store._conn().execute(
        "SELECT phase FROM staged_sources WHERE source=?", (prior.source_id,),
    ).fetchone() == ("abandoned",)


def test_empty_replacement_advances_reset_and_removes_old_chat_rows(world):
    coordinator, store, server, client, source, expected = world
    generation = server.store.begin_candidate(created_ns=3)
    server.store.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument(
            f"chats/{CHAT}/meta.json", 8, False,
            b'{"members":{"alice":true}}',
        ),),
        documents_mode="replace", logs_mode="replace",
    ), observed_ns=4)
    server.store.admit_candidate(generation)

    result = coupled_publication.publish(
        coordinator, store, expected, client, source, observed_ns=99,
    )

    assert result.source.ready
    assert store.messages(CHAT) == []
    assert store.log_offsets(CHAT) == {}
    assert store._conn().execute(
        "SELECT generation FROM ingestion_chat_resets WHERE chat_id=?",
        (CHAT,),
    ).fetchone() == (1,)


def test_rejected_only_stream_still_publishes_its_terminal_offset(world):
    coordinator, store, server, client, source, expected = world
    generation = server.store.begin_candidate(created_ns=3)
    server.store.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument(
            f"chats/{CHAT}/meta.json", 8, False,
            b'{"members":{"alice":true}}',
        ),),
        log_rows=(NodeLogRow(6, CHAT, "alice@box", b"not-json"),),
        documents_mode="replace", logs_mode="replace",
    ), observed_ns=4)
    server.store.admit_candidate(generation)

    coupled_publication.publish(
        coordinator, store, expected, client, source, observed_ns=99,
    )

    assert store.messages(CHAT) == []
    assert store.log_offsets(CHAT) == {"alice@box": 6}


def test_replacement_preserves_observation_for_existing_message_id(world):
    coordinator, store, server, client, source, expected = world
    before = store.message_observation(CHAT, "old")
    generation = server.store.begin_candidate(created_ns=3)
    server.store.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument(
            f"chats/{CHAT}/meta.json", 8, False,
            b'{"members":{"alice":true}}',
        ),),
        log_rows=(NodeLogRow(
            6, CHAT, "alice@box",
            b'{"id":"old","ns":8,"from":"alice",'
            b'"kind":"message","text":"updated"}',
        ),),
        documents_mode="replace", logs_mode="replace",
    ), observed_ns=4)
    server.store.admit_candidate(generation)

    coupled_publication.publish(
        coordinator, store, expected, client, source, observed_ns=99,
    )

    assert store.messages(CHAT) == [{
        "id": "old", "ns": 8, "from": "alice", "kind": "message",
        "text": "updated",
    }]
    assert store.message_observation(CHAT, "old") == before


def test_counter_exhaustion_preflight_preserves_previous_cut(world):
    coordinator, store, _server, client, source, expected = world
    maximum = membership_input_position.MAX_SQLITE_INTEGER
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_chat_resets(chat_id,generation) "
            "VALUES(?,?)",
            (CHAT, maximum),
        )
    current = coupled_admission.capture(store, expected.source, CHAT)

    with pytest.raises(OverflowError, match="reset generation exhausted"):
        coupled_publication.publish(
            coordinator, store, current, client, source, observed_ns=99,
        )

    assert store.messages(CHAT) == [{
        "id": "old", "ns": 1, "from": "old", "kind": "message",
    }]
    assert store.log_offsets(CHAT) == {"old@box": 9}
    assert local_source.capture(store, source.source) == expected.source
    assert store._conn().execute(
        "SELECT generation FROM ingestion_chat_resets WHERE chat_id=?",
        (CHAT,),
    ).fetchone() == (maximum,)


def test_source_revision_preflight_runs_before_chat_replacement(
        world, monkeypatch):
    coordinator, store, _server, client, source, expected = world
    with store._conn() as conn:
        conn.execute(
            "UPDATE local_sources SET revision=? WHERE source=?",
            (local_source.MAX - 1, source.source),
        )
    near_limit = local_source.capture(store, source.source)
    current = coupled_admission.capture(store, near_limit, CHAT)
    replaced = False

    def unexpected_replacement(*_args, **_kwargs):
        nonlocal replaced
        replaced = True
        raise AssertionError("chat replacement ran before source preflight")

    monkeypatch.setattr(
        coupled_publication, "_replace_chat", unexpected_replacement,
    )
    with pytest.raises(OverflowError, match="source revision exhausted"):
        coupled_publication.publish(
            coordinator, store, current, client, source, observed_ns=99,
        )

    assert not replaced
    assert store.messages(CHAT) == [{
        "id": "old", "ns": 1, "from": "old", "kind": "message",
    }]
    assert store.log_offsets(CHAT) == {"old@box": 9}


def test_staged_publication_has_no_coupled_document_only_admission(world):
    assert not hasattr(staged_publication, "_admit_coupled_in_transaction")


def test_legacy_publishers_cannot_rewrite_admitted_coupled_source(world):
    coordinator, store, _server, client, source, expected = world
    result = coupled_publication.publish(
        coordinator, store, expected, client, source, observed_ns=99,
    )
    with pytest.raises(
            local_source.SourceChanged,
            match="staged_source_requires_staged_publication"):
        local_source.publish(
            store, result.source,
            {f"chats/{CHAT}/meta.json": {"members": {"mallory": True}}},
            observed_ns=100,
        )
    publisher = SourcePublisher(coordinator, store, source)
    captured = publisher.capture()
    with pytest.raises(
            local_source.SourceChanged,
            match="staged_source_requires_staged_publication"):
        publisher.publish(
            captured,
            {f"chats/{CHAT}/meta.json": {"members": {"mallory": True}}},
            observed_ns=100,
        )
    assert local_source.capture(store, source.source) == result.source
