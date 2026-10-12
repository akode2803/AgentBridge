"""Invisible coupled message/raw-position stage boundaries."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from agentbridge.store import (
    coupled_payload_stage as payload_stage, coupled_stage, local_source,
    staged_source,
)
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "node-source"
META = "chats/room/meta.json"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    payload_stage.initialize(value)
    yield value
    value.close()


def _owners(store):
    source = local_source.claim_collection(
        store, local_source.capture(store, SOURCE),
    )
    owner = coupled_stage.begin(store, source, CHAT)
    payload = payload_stage.begin(store, owner)
    return source, owner, payload


def _accepted(ident="m1", provider=1, *, body="one"):
    value = {
        "id": ident, "ns": provider, "from": "alice",
        "kind": "message", "body": body,
    }
    return payload_stage.RawInput(
        provider, True, ident, provider, "alice", "message",
        json.dumps(value, separators=(",", ":")).encode(), None,
    )


def _rejected(provider=2):
    return payload_stage.RawInput(
        provider, False, rejection="wrong_log_owner",
    )


def _finish_documents(store, owner):
    staged_source.append(store, owner.document_stage, {META: {"value": 1}})
    staged_source.finish(store, owner.document_stage)


def test_stage_seals_unique_messages_raw_positions_and_complete_heads(store):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(
        store, payload, "alice@box", 2, (_accepted(), _rejected()),
    )
    # Identical message IDs collapse across streams; each provider row remains.
    payload_stage.append_stream(
        store, payload, "alice@otherbox", 3,
        (replace(_accepted(), provider_id=3),),
    )
    payload_stage.append_stream(store, payload, "empty@box", 0, ())
    _finish_documents(store, owner)
    seal = payload_stage.seal(store, payload)
    assert (seal.messages, seal.raw_rows, seal.streams) == (1, 3, 3)

    conn = store._conn()
    conn.execute("BEGIN")
    try:
        payload_stage.verify_sealed(conn, store, seal)
        assert conn.execute(
            "SELECT log_name,head,row_count,accepted_count FROM "
            "coupled_payload_streams WHERE stage=? ORDER BY log_name",
            (owner.document_stage.source_id,),
        ).fetchall() == [
            ("alice@box", 2, 2, 1),
            ("alice@otherbox", 3, 1, 1),
            ("empty@box", 0, 0, 0),
        ]
    finally:
        conn.rollback()


def test_conflicting_duplicate_message_rolls_back_whole_stream(store):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(
        store, payload, "alice@box", 1, (_accepted(),),
    )
    with pytest.raises(
            payload_stage.CoupledPayloadChanged,
            match="conflicting duplicate"):
        payload_stage.append_stream(
            store, payload, "alice@otherbox", 2,
            (replace(_accepted(body="different"), provider_id=2),),
        )
    stage = owner.document_stage.source_id
    conn = store._conn()
    assert conn.execute(
        "SELECT count(*) FROM coupled_payload_raw WHERE stage=?", (stage,),
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT count(*) FROM coupled_payload_streams WHERE stage=?", (stage,),
    ).fetchone() == (1,)


def test_invalid_storage_shapes_are_rejected_before_stage_write(store):
    _source, owner, payload = _owners(store)
    invalid = payload_stage.RawInput(
        1, True, "m", -1, "alice", "message",
        b'{"id":"m","ns":-1,"from":"alice"}', None,
    )
    with pytest.raises(ValueError, match="namespace"):
        payload_stage.append_stream(
            store, payload, "alice@box", 1, (invalid,),
        )
    assert store._conn().execute(
        "SELECT count(*) FROM coupled_payload_raw WHERE stage=?",
        (owner.document_stage.source_id,),
    ).fetchone() == (0,)


def test_sealed_payload_detects_direct_side_row_tamper(store):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(
        store, payload, "alice@box", 1, (_accepted(),),
    )
    _finish_documents(store, owner)
    seal = payload_stage.seal(store, payload)
    with store._conn() as conn:
        conn.execute(
            "UPDATE coupled_payload_messages SET kind='info' WHERE stage=?",
            (owner.document_stage.source_id,),
        )
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        with pytest.raises(
                payload_stage.CoupledPayloadChanged,
                match="seal changed"):
            payload_stage.verify_sealed(conn, store, seal)
    finally:
        conn.rollback()


def test_payload_cleanup_precedes_owner_and_document_stage(store):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(
        store, payload, "alice@box", 2, (_accepted(), _rejected()),
    )
    staged_source.append(store, owner.document_stage, {META: {"value": 1}})
    coupled_stage.abort(store, owner)
    for _ in range(20):
        staged_source.cleanup(store, max_rows=1)
    stage = owner.document_stage.source_id
    conn = store._conn()
    for table, key in (
            ("coupled_payload_stages", "stage"),
            ("coupled_payload_messages", "stage"),
            ("coupled_payload_raw", "stage"),
            ("coupled_payload_streams", "stage"),
            ("coupled_stage_owners", "stage"),
            ("staged_sources", "source")):
        assert conn.execute(
            f"SELECT 1 FROM {table} WHERE {key}=?", (stage,),
        ).fetchone() is None


def test_payload_apis_preserve_callers_transaction(store):
    _source, _owner, payload = _owners(store)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE caller_work(value)")
    with pytest.raises(
            sqlite3.OperationalError, match="append needs an owned transaction"):
        payload_stage.append_stream(store, payload, "empty@box", 0, ())
    assert conn.in_transaction
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='caller_work'",
    ).fetchone() == ("caller_work",)
    conn.rollback()


@pytest.mark.parametrize("rows", [(), (_accepted(),)])
def test_stream_must_reach_declared_head(store, rows):
    _source, owner, payload = _owners(store)
    with pytest.raises(ValueError, match="declared head"):
        payload_stage.append_stream(store, payload, "alice@box", 9, rows)
    assert store._conn().execute(
        "SELECT count(*) FROM coupled_payload_raw WHERE stage=?",
        (owner.document_stage.source_id,),
    ).fetchone() == (0,)


def test_accepted_sender_must_own_stream(store):
    _source, _owner, payload = _owners(store)
    with pytest.raises(ValueError, match="log owner"):
        payload_stage.append_stream(store, payload, "mallory@box", 1, (_accepted(),))


def test_provider_ids_are_unique_across_streams(store):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))
    with pytest.raises(payload_stage.CoupledPayloadChanged, match="provider row id"):
        payload_stage.append_stream(
            store, payload, "alice@otherbox", 1, (_accepted(),),
        )
    assert store._conn().execute(
        "SELECT count(*) FROM coupled_payload_streams WHERE stage=?",
        (owner.document_stage.source_id,),
    ).fetchone() == (1,)


@pytest.mark.parametrize("action", ["append", "seal"])
def test_building_side_row_tamper_cannot_be_checkpointed(store, action):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))
    with store._conn() as conn:
        conn.execute("UPDATE coupled_payload_messages SET sender='mallory'")
    with pytest.raises(payload_stage.CoupledPayloadChanged, match="revision changed"):
        if action == "append":
            payload_stage.append_stream(store, payload, "empty@box", 0, ())
        else:
            _finish_documents(store, owner)
            payload_stage.seal(store, payload)


def test_aborted_payload_rejects_append_seal_and_old_seal(store):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))
    _finish_documents(store, owner)
    seal = payload_stage.seal(store, payload)
    coupled_stage.abort(store, owner)
    with pytest.raises(payload_stage.CoupledPayloadChanged, match="owner is not building"):
        payload_stage.append_stream(store, payload, "empty@box", 0, ())
    with pytest.raises(payload_stage.CoupledPayloadChanged, match="owner is not building"):
        payload_stage.seal(store, payload)
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        with pytest.raises(payload_stage.CoupledPayloadChanged, match="owner is not building"):
            payload_stage.verify_sealed(conn, store, seal)
    finally:
        conn.rollback()


def test_duplicate_raw_ids_are_charged_before_over_budget_write(store):
    source = local_source.claim_collection(store, local_source.capture(store, SOURCE))
    owner = coupled_stage.begin(store, source, CHAT)
    payload = payload_stage.begin(store, owner, max_bytes=900)
    first = _accepted(ident="x" * 200)
    payload_stage.append_stream(store, payload, "alice@box", 1, (first,))
    statements = []
    conn = store._conn()
    conn.set_trace_callback(statements.append)
    try:
        with pytest.raises(OverflowError, match="budget"):
            payload_stage.append_stream(
                store, payload, "alice@otherbox", 2,
                (replace(first, provider_id=2),),
            )
    finally:
        conn.set_trace_callback(None)
    assert not any(sql.startswith("INSERT INTO coupled_payload_") for sql in statements)


def test_numeric_legacy_message_id_keeps_original_payload(store):
    _source, owner, payload = _owners(store)
    raw = b'{"id":123,"ns":1,"from":"alice"}'
    value = payload_stage.RawInput(1, True, "123", 1, "alice", "message", raw)
    payload_stage.append_stream(store, payload, "alice@box", 1, (value,))
    _finish_documents(store, owner)
    payload_stage.seal(store, payload)
    assert store._conn().execute(
        "SELECT message_id,payload FROM coupled_payload_messages",
    ).fetchone() == ("123", raw.decode())


@pytest.mark.parametrize("raw,ident", [
    (b'{"id":true,"ns":1,"from":"alice"}', "True"),
    (b'{"id":"m1","ns":true,"from":"alice"}', "m1"),
])
def test_boolean_identity_fields_are_not_integer_legacy_values(store, raw, ident):
    _source, _owner, payload = _owners(store)
    value = payload_stage.RawInput(1, True, ident, 1, "alice", "message", raw)
    with pytest.raises(ValueError, match="identity mismatch"):
        payload_stage.append_stream(store, payload, "alice@box", 1, (value,))


def test_missing_provider_uniqueness_index_blocks_append(store):
    _source, _owner, payload = _owners(store)
    with store._conn() as conn:
        conn.execute("DROP INDEX coupled_payload_provider_id")
    with pytest.raises(payload_stage.CoupledPayloadChanged, match="indexes changed"):
        payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))


def test_large_duplicate_input_is_bounded_before_json_parsing(store, monkeypatch):
    _source, _owner, payload = _owners(store)
    first = _accepted(body="x" * 2_000)
    monkeypatch.setattr(payload_stage, "MAX_BYTES", 4_096)

    def unexpected_decode(*_args):
        pytest.fail("oversized aggregate reached JSON parsing")

    monkeypatch.setattr(payload_stage, "_raw", unexpected_decode)
    with pytest.raises(OverflowError, match="preparation budget"):
        payload_stage.append_stream(
            store, payload, "alice@box", 3,
            tuple(replace(first, provider_id=i) for i in range(1, 4)),
        )


def test_seal_verification_requires_owned_database_transaction(store, tmp_path):
    _source, owner, payload = _owners(store)
    _finish_documents(store, owner)
    seal = payload_stage.seal(store, payload)
    with pytest.raises(sqlite3.OperationalError, match="requires a transaction"):
        payload_stage.verify_sealed(store._conn(), store, seal)
    with sqlite3.connect(tmp_path / "other.sqlite") as conn:
        conn.execute("BEGIN")
        with pytest.raises(ValueError, match="wrong coupled payload database"):
            payload_stage.verify_sealed(conn, store, seal)


def test_documents_cannot_ignore_existing_payload_global_charge(store, monkeypatch):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))
    used = payload_stage.staged_bytes(store._conn())
    monkeypatch.setattr(staged_source, "MAX_GLOBAL_BYTES", used + 1)
    with pytest.raises(OverflowError, match="global stage capacity"):
        staged_source.append(store, owner.document_stage, {META: {"value": 1}})
    assert store._conn().execute(
        "SELECT count(*) FROM document_observation_records WHERE source_id=?",
        (owner.document_stage.source_id,),
    ).fetchone() == (0,)


def test_payloads_cannot_ignore_existing_document_global_charge(store, monkeypatch):
    _source, owner, payload = _owners(store)
    staged_source.append(store, owner.document_stage, {META: {"value": 1}})
    used = store._conn().execute("SELECT total_bytes FROM staged_sources").fetchone()[0]
    monkeypatch.setattr(staged_source, "MAX_GLOBAL_BYTES", used + 1)
    with pytest.raises(OverflowError, match="budget"):
        payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))
    assert payload_stage.staged_bytes(store._conn()) == 0


def test_payload_global_charge_survives_abort_until_bounded_cleanup(store, monkeypatch):
    _source, owner, payload = _owners(store)
    payload_stage.append_stream(store, payload, "alice@box", 1, (_accepted(),))
    used = payload_stage.staged_bytes(store._conn())
    coupled_stage.abort(store, owner)
    monkeypatch.setattr(staged_source, "MAX_GLOBAL_BYTES", used)
    another = local_source.claim_collection(store, local_source.capture(store, "another"))
    with pytest.raises(OverflowError, match="global stage capacity"):
        coupled_stage.begin(store, another, CHAT)
    staged_source.cleanup(store, max_rows=1)
    assert payload_stage.staged_bytes(store._conn()) == used
    for _ in range(6):
        staged_source.cleanup(store, max_rows=1)
    assert payload_stage.staged_bytes(store._conn()) == 0
    assert coupled_stage.begin(store, another, CHAT)
