"""Exact detached-node to coupled Store candidate assembly."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from agentbridge.mesh.local_page_source import definition
from agentbridge.node.admission import NodeFinalizationPosition, NodeScopePosition
from agentbridge.node.capture_inputs import (
    DetachedDocument, DetachedLogRow, DetachedLogStream, DetachedNodeInputs,
    _JSONBudget, _decode, CaptureLimits,
)
from agentbridge.node.client import NodeBinding
from agentbridge.store import (
    coupled_admission, coupled_candidate, coupled_stage, local_source,
)
from agentbridge.store.db import Store


CHAT = "room"


def _definition():
    root = json.dumps([
        "local-root-v1", "supabase", "https://example.test", "mesh",
    ], separators=(",", ":"))
    return definition(root, CHAT)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    coupled_stage.initialize(value)
    coupled_admission.initialize(value)
    source = local_source.claim_collection(
        value, local_source.capture(value, _definition().source),
    )
    yield value, coupled_admission.capture(value, source, CHAT)
    value.close()


def _frozen(payload):
    return _decode(payload, _JSONBudget(CaptureLimits()))


def _inputs(position):
    meta = b'{"members":{"alice":true}}'
    message = b'{"id":"m1","ns":7,"from":"alice","kind":"message"}'
    scopes = (
        NodeScopePosition("chat", CHAT, 4, None),
        NodeScopePosition("root", "", 3, None),
    )
    node = NodeFinalizationPosition(
        str(Path(position.database_path).with_name("node.sqlite")), "d" * 32, "e" * 64,
        "https://example.test", "supabase://mesh", 9, scopes,
    )
    documents = (
        DetachedDocument(
            f"chats/{CHAT}/meta.json", 1, False, meta, _frozen(meta),
        ),
        DetachedDocument(
            f"chats/{CHAT}/overlays/state/old.json", 2, True, None, None,
        ),
    )
    rows = (
        DetachedLogRow(
            3, CHAT, "alice@box", message, _frozen(message), True, None,
            "m1", 7, "alice", "message",
        ),
        DetachedLogRow(
            5, CHAT, "alice@box", b"not-json", None, False,
            "invalid_UTF-8_JSON_input", None, None, None, None,
        ),
    )
    streams = (DetachedLogStream("alice@box", 5, rows),)
    charged = sum(len(row.path.encode()) + 32 + (
        0 if row.payload is None else len(row.payload)
    ) for row in documents)
    charged += len("alice@box".encode()) + 32
    charged += sum(len("alice@box".encode()) + 32 + len(row.payload)
                   for row in rows)
    return DetachedNodeInputs(
        _definition(), CHAT, NodeBinding("node", "d" * 32, "e" * 64, 1),
        "capture", 9, "ready", 8, scopes, node, documents, (), streams,
        2, 1, 1, charged, 3,
    )


def test_assembly_seals_documents_messages_and_raw_evidence(store):
    value, position = store
    candidate = coupled_candidate._assemble_detached(value, position, _inputs(position))
    assert candidate.store_position == position
    assert candidate.payload.messages == 1
    assert candidate.payload.raw_rows == 2
    assert candidate.payload.streams == 1
    stage = candidate.owner.document_stage.source_id
    conn = value._conn()
    assert conn.execute(
        "SELECT phase FROM staged_sources WHERE source=?", (stage,),
    ).fetchone() == ("sealed",)
    assert conn.execute(
        "SELECT id FROM messages WHERE chat_id=?", (CHAT,),
    ).fetchall() == []
    assert conn.execute(
        "SELECT message_id,ns,sender FROM coupled_payload_messages WHERE stage=?",
        (stage,),
    ).fetchall() == [("m1", 7, "alice")]
    # Tombstones are known absences in a complete replacement, not live docs.
    assert conn.execute(
        "SELECT path FROM document_observation_records WHERE source_id=?",
        (stage,),
    ).fetchall() == [(f"chats/{CHAT}/meta.json",)]


def test_forged_rejection_cannot_hide_valid_message(store):
    value, position = store
    inputs = _inputs(position)
    valid = inputs.streams[0].rows[0]
    forged = replace(
        valid, accepted=False, value=None, rejection_reason="not_json",
        message_id=None, ns=None, sender=None, kind=None,
    )
    changed = replace(
        inputs, streams=(replace(inputs.streams[0], rows=(forged,) +
                                inputs.streams[0].rows[1:]),),
        accepted_log_rows=0, rejected_log_rows=2,
    )
    with pytest.raises(
            coupled_candidate.CoupledCandidateError,
            match="classification changed"):
        coupled_candidate._assemble_detached(value, position, changed)
    assert value._conn().execute(
        "SELECT count(*) FROM staged_sources WHERE source LIKE 'coupled:%'",
    ).fetchone() == (0,)


def test_accounting_or_terminal_head_change_rejects_before_stage(store):
    value, position = store
    inputs = _inputs(position)
    with pytest.raises(
            coupled_candidate.CoupledCandidateError, match="accounting changed"):
        coupled_candidate._assemble_detached(
            value, position, replace(inputs, input_bytes=inputs.input_bytes + 1),
        )
    changed_stream = replace(inputs.streams[0], head=6)
    with pytest.raises(
            coupled_candidate.CoupledCandidateError, match="stream head"):
        coupled_candidate._assemble_detached(
            value, position, replace(inputs, streams=(changed_stream,)),
        )


def test_node_or_store_binding_change_rejects_before_stage(store):
    value, position = store
    inputs = _inputs(position)
    wrong_node = replace(
        inputs.finalization_position, admitted_generation=10,
    )
    with pytest.raises(
            coupled_candidate.CoupledCandidateError,
            match="node position changed"):
        coupled_candidate._assemble_detached(
            value, position, replace(inputs, finalization_position=wrong_node),
        )
    with pytest.raises(
            coupled_candidate.CoupledCandidateError, match="Store scope"):
        coupled_candidate._assemble_detached(
            value, replace(position, database_path="/tmp/other.sqlite"), inputs,
        )


def test_write_failure_abandons_partial_candidate(store, monkeypatch):
    value, position = store
    original = coupled_candidate.coupled_payload_stage.append_stream

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected")

    monkeypatch.setattr(
        coupled_candidate.coupled_payload_stage, "append_stream", fail,
    )
    with pytest.raises(RuntimeError, match="injected"):
        coupled_candidate._assemble_detached(value, position, _inputs(position))
    assert value._conn().execute(
        "SELECT phase FROM staged_sources WHERE source LIKE 'coupled:%'",
    ).fetchall() == [("abandoned",)]


def test_public_assembly_owns_capture_and_rejects_detached_substitution(store, tmp_path):
    from agentbridge.node.admission import NodeDocument, NodeInputBatch, NodeLogRow
    from agentbridge.node.client import LocalNodeClient
    from agentbridge.node.protocol import ReplicaIdentity
    from agentbridge.node.server import LocalNodeServer

    value, position = store
    identity = ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="alice", machine="box",
    )
    with LocalNodeServer(tmp_path / "node", identity) as server:
        generation = server.store.begin_candidate(created_ns=1)
        server.store.seal_candidate(generation, NodeInputBatch(
            documents=(NodeDocument(f"chats/{CHAT}/meta.json", 1, False, b"null"),),
            log_rows=(
                NodeLogRow(3, CHAT, "alice@box",
                           b'{"id":"m1","ns":7,"from":"alice","kind":"message"}'),
                NodeLogRow(5, CHAT, "alice@box", b"not-json"),
            ), documents_mode="replace", logs_mode="replace",
        ), observed_ns=2)
        server.store.admit_candidate(generation)
        client = LocalNodeClient(tmp_path / "node", identity)
        inputs = coupled_candidate.capture_inputs.capture_detached_inputs(client, _definition())
        # Retaining the terminal rejected row and rebasing every scalar cannot
        # turn caller-controlled subsets into a supported assembly input.
        hidden = inputs.streams[0].rows[0]
        forged = replace(inputs, streams=(replace(inputs.streams[0], rows=inputs.streams[0].rows[1:]),),
                         raw_log_rows=1, accepted_log_rows=0,
                         input_bytes=inputs.input_bytes-len(hidden.log_name.encode())-32-len(hidden.payload))
        with pytest.raises(TypeError, match="local node client"):
            coupled_candidate.assemble(value, position, forged, _definition())
        candidate = coupled_candidate.assemble(value, position, client, _definition())
    assert candidate.payload.messages == 1
    assert candidate.payload.raw_rows == 2
    stage = candidate.owner.document_stage.source_id
    assert value._conn().execute(
        "SELECT path FROM document_observation_records WHERE source_id=?", (stage,),
    ).fetchall() == [(f"chats/{CHAT}/meta.json",)]
    assert value._conn().execute("SELECT count(*) FROM messages").fetchone() == (0,)


def test_live_json_null_is_not_a_tombstone(store):
    value, position = store
    inputs = _inputs(position)
    old = inputs.documents[0]
    changed = replace(inputs, documents=(replace(old, payload=b"null", value=None),) + inputs.documents[1:],
                      input_bytes=inputs.input_bytes-len(old.payload)+4)
    candidate = coupled_candidate._assemble_detached(value, position, changed)
    assert value._conn().execute(
        "SELECT path,payload,deleted FROM document_observation_records WHERE source_id=?",
        (candidate.owner.document_stage.source_id,),
    ).fetchall() == [(old.path, "null", 0)]


@pytest.mark.parametrize("field", ["raw_log_rows", "accepted_log_rows", "rejected_log_rows"])
def test_bool_counts_rejected_before_decoding(store, monkeypatch, field):
    value, position = store
    inputs = replace(_inputs(position), **{field: True})
    def fail(*args):
        pytest.fail("decoded malformed scalar inputs")
    monkeypatch.setattr(coupled_candidate.capture_inputs, "_decode", fail)
    with pytest.raises(coupled_candidate.CoupledCandidateError, match="accounting"):
        coupled_candidate._assemble_detached(value, position, inputs)


def test_document_aggregate_cap_checked_before_next_decode(store, monkeypatch):
    value, position = store
    inputs = _inputs(position)
    cost = coupled_candidate._charge(inputs.documents[0].path, inputs.documents[0].payload)
    limits = CaptureLimits()
    monkeypatch.setattr(coupled_candidate.capture_inputs, "CaptureLimits", lambda: limits)
    monkeypatch.setattr(coupled_candidate.capture_inputs, "MAX_INPUT_DOCUMENT_BYTES", cost - 1)
    def fail(*args):
        pytest.fail("decoded document beyond byte budget")
    monkeypatch.setattr(coupled_candidate.capture_inputs, "_decode", fail)
    with pytest.raises(coupled_candidate.CoupledCandidateError, match="byte budget"):
        coupled_candidate._assemble_detached(value, position, inputs)
    assert value._conn().execute("SELECT count(*) FROM staged_sources").fetchone() == (0,)


def test_log_cumulative_cap_checked_before_classification(store, monkeypatch):
    value, position = store
    inputs = _inputs(position)
    limits = CaptureLimits()
    monkeypatch.setattr(coupled_candidate.capture_inputs, "CaptureLimits", lambda: limits)
    monkeypatch.setattr(coupled_candidate.capture_inputs, "MAX_INPUT_BYTES", inputs.input_bytes - 1)
    inputs = replace(inputs, input_bytes=inputs.input_bytes - 1)
    original = coupled_candidate.capture_inputs._envelope
    seen = []
    def classify(row, budget):
        seen.append(row.id)
        return original(row, budget)
    monkeypatch.setattr(coupled_candidate.capture_inputs, "_envelope", classify)
    with pytest.raises(coupled_candidate.CoupledCandidateError, match="byte budget"):
        coupled_candidate._assemble_detached(value, position, inputs)
    assert seen == [3]


@pytest.mark.parametrize("bad", [
    lambda x: replace(x, scope_positions=(object(), object())),
    lambda x: replace(x, finalization_position=replace(x.finalization_position, root=42)),
    lambda x: replace(x, finalization_position=replace(x.finalization_position, admitted_generation=True)),
    lambda x: replace(x, binding=replace(x.binding, node_epoch="\ud800")),
    lambda x: replace(x, binding=replace(x.binding, started_ns=True)),
    lambda x: replace(x, documents=(replace(x.documents[0], path="\ud800"),)),
    lambda x: replace(x, definition=replace(x.definition, serialized="\ud800")),
    lambda x: replace(x, streams=(replace(x.streams[0], head=0, rows=()),), raw_log_rows=0, accepted_log_rows=0, rejected_log_rows=0),
])
def test_malformed_shapes_fail_before_staging(store, bad):
    value, position = store
    with pytest.raises(coupled_candidate.CoupledCandidateError):
        coupled_candidate._assemble_detached(value, position, bad(_inputs(position)))
    assert value._conn().execute("SELECT count(*) FROM staged_sources").fetchone() == (0,)


def test_raw_tuple_count_preflight_precedes_decoding(store, monkeypatch):
    value, position = store
    inputs = replace(_inputs(position), raw_log_rows=1, accepted_log_rows=1, rejected_log_rows=0)
    monkeypatch.setattr(coupled_candidate.capture_inputs, "MAX_INPUT_LOG_ROWS", 1)
    def fail(*args):
        pytest.fail("decoded input beyond raw row bound")
    monkeypatch.setattr(coupled_candidate.capture_inputs, "_decode", fail)
    with pytest.raises(coupled_candidate.CoupledCandidateError, match="row budget"):
        coupled_candidate._assemble_detached(value, position, inputs)
