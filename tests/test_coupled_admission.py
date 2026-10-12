"""Fences for the inactive coupled node-input publication primitives."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentbridge.store import (
    coupled_admission, local_source, log_position, page_inputs, staged_publication,
    staged_source,
)
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "node-source"
META = "chats/room/meta.json"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    staged_source.initialize(value)
    coupled_admission.initialize(value)
    yield value
    value.close()


def _message(ident="m1", ns=1):
    return {
        "id": ident, "ns": ns, "from": "alice", "kind": "message",
        "body": ident,
    }


def _insert(conn, ident="m1", ns=1):
    record = _message(ident, ns)
    conn.execute(
        "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) "
        "VALUES(?,?,?,?,?,?)",
        (CHAT, ident, ns, "alice", "message", json.dumps(record)),
    )


def _candidate(store, expected):
    stage = staged_source.begin(
        store, expected.source_id, CHAT, expected=expected,
    )
    staged_source.append(store, stage, {META: {"value": 1}})
    staged_source.finish(store, stage)
    return stage


def _claimed(store):
    return local_source.claim_collection(
        store, local_source.capture(store, SOURCE),
    )


def test_transaction_admission_rolls_back_with_callers_other_writes(store):
    expected = local_source.claim_collection(
        store, local_source.capture(store, SOURCE),
    )
    stage = _candidate(store, expected)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        staged_publication._admit_in_transaction(
            conn, store, expected, stage, observed_ns=10,
        )
        _insert(conn)
        raise RuntimeError("injected after coupled writes")
    except RuntimeError:
        conn.rollback()

    assert local_source.capture(store, SOURCE) == expected
    assert store.messages(CHAT) == []
    admitted, _index = staged_publication.admit(
        store, expected, stage, observed_ns=11,
    )
    assert admitted.ready


def test_chat_position_rejects_message_change(store):
    expected = coupled_admission.capture(store, _claimed(store), CHAT)
    with store._conn() as conn:
        _insert(conn)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(coupled_admission.CoupledAdmissionChanged):
            coupled_admission.require_current(conn, store, expected)
    finally:
        conn.rollback()


def test_chat_position_rejects_offset_only_change_and_removed_stream(store):
    expected = coupled_admission.capture(store, _claimed(store), CHAT)
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO log_offsets(chat_id,log_name,offset) VALUES(?,?,?)",
            (CHAT, "alice.log", 7),
        )
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(coupled_admission.CoupledAdmissionChanged):
            coupled_admission.require_current(conn, store, expected)
    finally:
        conn.rollback()

    with store._conn() as conn:
        conn.execute(
            "DELETE FROM log_offsets WHERE chat_id=? AND log_name=?",
            (CHAT, "alice.log"),
        )
    removed = coupled_admission.capture(store, _claimed(store), CHAT)
    assert [(item.log_name, item.offset) for item in removed.log_positions] == [
        ("alice.log", 0),
    ]


def test_chat_position_rejects_reset_even_when_chat_is_empty(store):
    expected = coupled_admission.capture(store, _claimed(store), CHAT)
    with store._conn() as conn:
        log_position.reset_chat(conn, CHAT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(coupled_admission.CoupledAdmissionChanged):
            coupled_admission.require_current(conn, store, expected)
    finally:
        conn.rollback()


def test_chat_position_rejects_unresolved_local_send(store):
    store.cache_and_outbox_add(
        CHAT, _message(), "append_log", f"{CHAT}|alice.log",
        {"envelope": _message()},
    )
    with pytest.raises(
            coupled_admission.CoupledAdmissionChanged,
            match="unresolved local sends"):
        coupled_admission.capture(store, _claimed(store), CHAT)


def test_chat_position_rejects_pending_nonmessage_append(store):
    store.cache_and_outbox_add(
        CHAT, {"id": "i1", "ns": 1, "from": "alice", "kind": "info"},
        "append_log", f"{CHAT}|alice.log",
        {"envelope": {"id": "i1", "ns": 1, "kind": "info"}},
    )
    with pytest.raises(
            coupled_admission.CoupledAdmissionChanged,
            match="unresolved chat outbox work"):
        coupled_admission.capture(store, _claimed(store), CHAT)


def test_chat_position_bounds_existing_replacement_work(store):
    with store._conn() as conn:
        _insert(conn, "m1", 1)
        _insert(conn, "m2", 2)
    with pytest.raises(OverflowError, match="message replacement budget"):
        coupled_admission.capture(
            store, _claimed(store), CHAT, max_messages=1,
        )
    with pytest.raises(OverflowError, match="replacement byte budget"):
        coupled_admission.capture(
            store, _claimed(store), CHAT, max_existing_bytes=1,
        )


def test_chat_position_rejects_tampered_offset_revision_triggers(store):
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO log_offsets(chat_id,log_name,offset) VALUES(?,?,?)",
            (CHAT, "alice.log", 7),
        )
    expected = coupled_admission.capture(store, _claimed(store), CHAT)
    with store._conn() as conn:
        conn.execute("DROP TRIGGER ingestion_offset_update")
        conn.execute(
            "UPDATE log_offsets SET offset=9 WHERE chat_id=? AND log_name=?",
            (CHAT, "alice.log"),
        )
        conn.execute(
            "UPDATE log_offsets SET offset=7 WHERE chat_id=? AND log_name=?",
            (CHAT, "alice.log"),
        )
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="triggers changed"):
            coupled_admission.require_current(conn, store, expected)
    finally:
        conn.rollback()


def test_chat_position_is_bound_to_exact_store(store, tmp_path):
    expected = coupled_admission.capture(store, _claimed(store), CHAT)
    other = Store(tmp_path / "other.sqlite")
    try:
        conn = other._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ValueError, match="another Store"):
                coupled_admission.require_current(conn, other, expected)
        finally:
            conn.rollback()
    finally:
        other.close()


def test_chat_position_requires_prepared_covering_size_index(store):
    with store._conn() as conn:
        conn.execute("DROP INDEX idx_messages_chat_composite")
    with pytest.raises(page_inputs.PageInputsChanged, match="message_index_pending"):
        coupled_admission.capture(store, _claimed(store), CHAT)


def test_transaction_admission_rejects_wrong_connection(store, tmp_path):
    expected = local_source.claim_collection(
        store, local_source.capture(store, SOURCE),
    )
    stage = _candidate(store, expected)
    other = sqlite3.connect(tmp_path / "other.sqlite")
    try:
        other.execute("CREATE TABLE marker(value)")
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="wrong staged admission database"):
            staged_publication._admit_in_transaction(
                other, store, expected, stage, observed_ns=10,
            )
    finally:
        other.close()
