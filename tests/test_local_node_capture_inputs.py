"""Detached generation-bound local-node input conversion."""

from dataclasses import replace
import json

import pytest

from agentbridge.mesh.local_page_source import definition
from agentbridge.node.admission import NodeDocument, NodeInputBatch, NodeLogRow
from agentbridge.node.capture_inputs import (
    CaptureLimits, DetachedInputError, DetachedInputOverflow,
    FrozenJSONObject, capture_detached_inputs, thaw_json,
)
from agentbridge.node import capture_inputs
from agentbridge.node.client import LocalNodeClient, NodeClientGenerationChanged
from agentbridge.node.protocol import ProtocolError, ReplicaIdentity
from agentbridge.node.server import LocalNodeServer
from agentbridge.store import source_selectors


def identity():
    return ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="user", machine="desktop",
    )


def admit(server, batch, *, created=1, observed=2):
    generation = server.store.begin_candidate(created_ns=created)
    server.store.seal_candidate(generation, batch, observed_ns=observed)
    server.store.admit_candidate(generation)
    return generation


def canonical(chat="chat-a"):
    node = identity()
    root = json.dumps([
        "local-root-v1", "supabase", node.provider_endpoint,
        node.root.removeprefix("supabase://"),
    ], separators=(",", ":"))
    return definition(root, chat)


def test_capture_detaches_documents_and_replays_every_stream_from_zero(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        generation = admit(server, NodeInputBatch(
            documents=(
                NodeDocument(
                    "chats/chat-a/meta.json", 4, False,
                    b'{"members":{"user":true}}',
                ),
                NodeDocument(
                    "chats/chat-a/overlays/state/old.json", 5, True, None,
                ),
                NodeDocument("users/user.json", 2, False, b'{"name":"User"}'),
            ),
            log_rows=(
                NodeLogRow(
                    1, "chat-a", "alice@desktop",
                    b'{"id":"one","ns":1,"from":"alice","kind":"message"}',
                ),
                NodeLogRow(
                    2, "chat-a", "alice@desktop",
                    b'{"id":"spoof","ns":2,"from":"mallory"}',
                ),
                NodeLogRow(3, "chat-a", "alice@desktop", b'not-json'),
                NodeLogRow(
                    4, "chat-a", "bob",
                    b'{"id":"four","ns":4,"from":"bob"}',
                ),
            ),
            documents_mode="replace", logs_mode="replace",
        ))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(),
        )

    assert result.generation == generation
    position = result.finalization_position
    assert position.database_path == str(server.database_path.resolve())
    assert position.database_incarnation == result.binding.database_incarnation
    assert position.identity_digest == identity().digest
    assert position.admitted_generation == generation
    assert position.scope_positions == result.scope_positions
    assert result.chat_id == "chat-a"
    assert [row.path for row in result.documents] == [
        "chats/chat-a/meta.json",
        "chats/chat-a/overlays/state/old.json",
        "users/user.json",
    ]
    assert result.missing_exact_paths == ()
    assert result.documents[1].deleted and result.documents[1].value is None
    assert isinstance(result.documents[0].value, FrozenJSONObject)
    mutable = thaw_json(result.documents[0].value)
    mutable["members"]["user"] = False
    assert thaw_json(result.documents[0].value)["members"]["user"] is True
    assert [(row.log_name, row.head) for row in result.streams] == [
        ("alice@desktop", 3), ("bob", 4),
    ]
    rows = [row for stream in result.streams for row in stream.rows]
    assert [row.provider_id for row in rows] == [1, 2, 3, 4]
    assert result.raw_log_rows == 4
    assert result.accepted_log_rows == 2
    assert result.rejected_log_rows == 2
    assert [row.rejection_reason for row in rows] == [
        None, "wrong_log_owner", "invalid_UTF-8_JSON_input", None,
    ]


def test_missing_exact_document_is_explicit_and_tombstones_remain_distinct(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(
            documents=(NodeDocument(
                "chats/chat-a/overlays/state/deleted.json", 8, True, None,
            ),),
            documents_mode="replace",
        ))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(),
        )
    assert result.missing_exact_paths == ("chats/chat-a/meta.json",)
    assert len(result.documents) == 1
    assert result.documents[0].deleted


def test_documents_are_all_or_overflow_and_invalid_json_fails_selection(tmp_path):
    with LocalNodeServer(tmp_path / "many", identity()) as server:
        admit(server, NodeInputBatch(documents=(
            NodeDocument("users/a.json", 1, False, b'{}'),
            NodeDocument("users/b.json", 2, False, b'{}'),
        ), documents_mode="replace"))
        with pytest.raises(DetachedInputOverflow, match="document selection"):
            capture_detached_inputs(
                LocalNodeClient(tmp_path / "many", identity()), canonical(),
                limits=CaptureLimits(max_documents=1),
            )

    with LocalNodeServer(tmp_path / "invalid", identity()) as server:
        admit(server, NodeInputBatch(documents=(NodeDocument(
            "chats/chat-a/meta.json", 1, False, b'not-json',
        ),), documents_mode="replace"))
        with pytest.raises(DetachedInputError, match="invalid document JSON"):
            capture_detached_inputs(
                LocalNodeClient(tmp_path / "invalid", identity()), canonical(),
            )


def test_definition_must_be_exact_canonical_page_scope(tmp_path):
    extra = source_selectors.definition(json.loads(canonical().serialized)[0], (
        source_selectors.Selector("doc_prefix", "users"),
        source_selectors.Selector("log_chat", "chat-a"),
    ), build="local-canonical-page-v1")
    with LocalNodeServer(tmp_path, identity()):
        client = LocalNodeClient(tmp_path, identity())
        with pytest.raises(DetachedInputError, match="differs"):
            capture_detached_inputs(client, extra)


def test_definition_reuses_canonical_chat_identity_validation(tmp_path):
    chat = "x" * 257
    root = json.loads(canonical().serialized)[0]
    forged = source_selectors.definition(root, (
        source_selectors.Selector("doc_prefix", "users"),
        source_selectors.Selector("doc_prefix", "lifecycle"),
        source_selectors.Selector("doc_exact", f"chats/{chat}/meta.json"),
        *(source_selectors.Selector(
            "doc_prefix", f"chats/{chat}/overlays/{kind}")
          for kind in ("edits", "redactions", "reactions", "pins", "state")),
        source_selectors.Selector("doc_prefix", f"chats/{chat}/keys"),
        source_selectors.Selector("log_chat", chat),
    ), build="local-canonical-page-v1")
    with LocalNodeServer(tmp_path, identity()):
        with pytest.raises(DetachedInputError, match="differs"):
            capture_detached_inputs(
                LocalNodeClient(tmp_path, identity()), forged,
            )


def test_definition_root_must_match_authenticated_node_identity(tmp_path):
    foreign_root = json.dumps([
        "local-root-v1", "supabase", "https://other.example.test", "other",
    ], separators=(",", ":"))
    with LocalNodeServer(tmp_path, identity()):
        with pytest.raises(DetachedInputError, match="another node root"):
            capture_detached_inputs(
                LocalNodeClient(tmp_path, identity()),
                definition(foreign_root, "chat-a"),
            )


def test_never_admitted_node_is_unavailable_but_degraded_snapshot_is_readable(
        tmp_path):
    with LocalNodeServer(tmp_path / "cold", identity()):
        with pytest.raises(DetachedInputError, match="no admitted snapshot"):
            capture_detached_inputs(
                LocalNodeClient(tmp_path / "cold", identity()), canonical(),
            )

    with LocalNodeServer(tmp_path / "degraded", identity()) as server:
        generation = admit(server, NodeInputBatch())
        server.store.note_refresh_state(
            "degraded", attempted_ns=3, expected_generation=generation,
        )
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path / "degraded", identity()), canonical(),
        )
    assert result.generation == generation
    assert result.health == "degraded"
    assert result.missing_exact_paths == ("chats/chat-a/meta.json",)


def test_generation_change_during_inventory_or_replay_is_rejected(tmp_path,
                                                                  monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(NodeLogRow(
            1, "chat-a", "alice", b'{"id":"one","ns":1,"from":"alice"}',
        ),), logs_mode="replace"))
        client = LocalNodeClient(tmp_path, identity())
        original = client.log_streams

        def change(*args, **kwargs):
            page = original(*args, **kwargs)
            admit(server, NodeInputBatch(documents=(NodeDocument(
                "users/new.json", 2, False, b'{}',
            ),)), created=3, observed=4)
            return page

        monkeypatch.setattr(client, "log_streams", change)
        with pytest.raises(NodeClientGenerationChanged):
            capture_detached_inputs(client, canonical())


def test_replay_requires_terminal_captured_head(tmp_path, monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(NodeLogRow(
            1, "chat-a", "alice", b'{"id":"one","ns":1,"from":"alice"}',
        ),), logs_mode="replace"))
        client = LocalNodeClient(tmp_path, identity())
        original = client.log_streams

        def wrong_head(*args, **kwargs):
            page = original(*args, **kwargs)
            return replace(page, streams=(replace(page.streams[0], head=2),))

        monkeypatch.setattr(client, "log_streams", wrong_head)
        with pytest.raises(ProtocolError, match="captured head"):
            capture_detached_inputs(client, canonical())


def test_raw_row_and_json_node_budgets_are_explicit(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(1, "chat-a", "alice", b'{"id":"one","ns":1,"from":"alice"}'),
            NodeLogRow(2, "chat-a", "alice", b'{"id":"two","ns":2,"from":"alice"}'),
        ), logs_mode="replace"))
        client = LocalNodeClient(tmp_path, identity())
        with pytest.raises(DetachedInputOverflow, match="log rows"):
            capture_detached_inputs(
                client, canonical(), limits=CaptureLimits(max_log_rows=1),
            )
        with pytest.raises(DetachedInputOverflow, match="JSON nodes"):
            capture_detached_inputs(
                client, canonical(), limits=CaptureLimits(max_json_nodes=2),
            )


def test_json_node_budget_preflights_before_materializing(monkeypatch):
    budget = capture_inputs._JSONBudget(CaptureLimits(max_json_nodes=2))
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("json.loads allocated before budget rejection")

    monkeypatch.setattr(capture_inputs.json, "loads", forbidden)
    with pytest.raises(DetachedInputOverflow, match="JSON nodes"):
        capture_inputs._decode(b"[[],[]]", budget)
    assert not called


def test_envelope_store_key_compatibility_and_namespace_bounds(tmp_path):
    rows = (
        NodeLogRow(1, "chat-a", "alice", b'{"id":7,"ns":1,"from":"alice"}'),
        NodeLogRow(2, "chat-a", "alice", b'{"id":"zero","ns":0,"from":"alice"}'),
        NodeLogRow(
            3, "chat-a", "alice",
            b'{"id":"huge","ns":9223372036854775808,"from":"alice"}',
        ),
        NodeLogRow(
            4, "chat-a", "alice",
            ('{"id":"%s","ns":4,"from":"alice"}' % ("x" * 4097)).encode(),
        ),
        NodeLogRow(5, "chat-a", "alice", b'{"id":0,"ns":5,"from":"alice"}'),
        NodeLogRow(
            6, "chat-a", "alice",
            b'{"id":9223372036854775808,"ns":6,"from":"alice"}',
        ),
        NodeLogRow(
            7, "chat-a", "alice",
            b'{"id":-9223372036854775809,"ns":7,"from":"alice"}',
        ),
        NodeLogRow(
            8, "chat-a", "alice",
            b'{"id":"true-ns","ns":true,"from":"alice"}',
        ),
    )
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=rows, logs_mode="replace"))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(),
        )
    captured = result.streams[0].rows
    assert captured[0].accepted and captured[0].message_id == "7"
    assert [row.rejection_reason for row in captured[1:7]] == [
        "invalid_namespace", "invalid_namespace", "invalid_message_id",
        "invalid_message_id", "invalid_message_id", "invalid_message_id",
    ]
    assert captured[7].accepted and captured[7].ns is True


def test_input_byte_accounting_includes_chat_id_per_log_row(tmp_path):
    chat = "c" * 200
    payload = b'{"id":"one","ns":1,"from":"alice"}'
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(1, chat, "alice", payload),
        ), logs_mode="replace"))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(chat),
        )
    assert result.input_bytes == (
        2 * len("alice") + 64 + len(chat.encode("utf-8")) + len(payload)
    )


def test_replay_chunks_long_stream_names_to_fit_wire_request(tmp_path):
    rows = tuple(
        NodeLogRow(
            index, "chat-a", f"writer-{index:02d}-" + "x" * 1_010,
            ('{"id":"m%d","ns":%d,"from":"writer-%02d-%s"}'
             % (index, index, index, "x" * 1_010)).encode(),
        )
        for index in range(1, 65)
    )
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=rows, logs_mode="replace"))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(),
        )
    assert result.raw_log_rows == result.accepted_log_rows == 64
    assert len(result.streams) == 64


def test_stream_inventory_retries_with_smaller_wire_page(tmp_path, monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(1, "chat-a", "alice", b'{"id":"a","ns":1,"from":"alice"}'),
            NodeLogRow(2, "chat-a", "bob", b'{"id":"b","ns":2,"from":"bob"}'),
        ), logs_mode="replace"))
        client = LocalNodeClient(tmp_path, identity())
        original = client.log_streams
        limits = []

        def bounded(*args, **kwargs):
            limits.append(kwargs["limit"])
            if kwargs["limit"] > 1:
                raise OverflowError("simulated inventory response overflow")
            return original(*args, **kwargs)

        monkeypatch.setattr(client, "log_streams", bounded)
        result = capture_detached_inputs(client, canonical())
    assert [row.log_name for row in result.streams] == ["alice", "bob"]
    assert limits[:2] == [1000, 500]
    assert limits[-1] == 1


def test_large_stream_is_replayed_across_pages_through_exact_head(tmp_path):
    rows = tuple(NodeLogRow(
        value, "chat-a", "alice",
        ('{"id":"m%d","ns":%d,"from":"alice"}' % (value, value)).encode(),
    ) for value in range(1, 301))
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=rows, logs_mode="replace"))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(),
        )
    assert len(result.streams) == 1
    assert result.streams[0].head == 300
    assert [row.provider_id for row in result.streams[0].rows] == list(
        range(1, 301)
    )
    assert result.raw_log_rows == result.accepted_log_rows == 300
    assert result.rejected_log_rows == 0
    assert result.request_count == 4  # documents, inventory, two log pages


def test_oversized_json_integer_is_retained_as_rejected_raw_row(tmp_path):
    payload = ('{"id":"large","ns":%s,"from":"alice"}' % ('9' * 5_000)).encode()
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(1, "chat-a", "alice", payload),
        ), logs_mode="replace"))
        result = capture_detached_inputs(
            LocalNodeClient(tmp_path, identity()), canonical(),
        )
    row = result.streams[0].rows[0]
    assert row.payload == payload
    assert not row.accepted
    assert row.rejection_reason == "invalid_UTF-8_JSON_input"


def test_failed_wire_attempts_consume_request_budget(tmp_path, monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(1, "chat-a", "alice", b'{"id":"a","ns":1,"from":"alice"}'),
            NodeLogRow(2, "chat-a", "bob", b'{"id":"b","ns":2,"from":"bob"}'),
        ), logs_mode="replace"))
        client = LocalNodeClient(tmp_path, identity())
        original = client.capture
        failed = False

        def overflow_once(request):
            nonlocal failed
            if request.logs and not failed:
                failed = True
                raise OverflowError("simulated aggregate response overflow")
            return original(request)

        monkeypatch.setattr(client, "capture", overflow_once)
        with pytest.raises(DetachedInputOverflow, match="page count"):
            capture_detached_inputs(
                client, canonical(), limits=CaptureLimits(max_pages=4),
            )
    assert failed
