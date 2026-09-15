"""One-SQLite-cut shadow/chat capture regressions (R176)."""

import json
import sqlite3
import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from agentbridge.store import shadow_chat_inputs, shadow_slot
from agentbridge.store.db import Store
from agentbridge.store.shadow_slot import ShadowConflict, ShadowSnapshot, ShadowSource


SOURCE = ShadowSource("røot", "cache", "mirror")


def _snapshot(revision=1, records=(("shadow/a", '{"old":true}'),)):
    return ShadowSnapshot(SOURCE, revision, 7, "bootstrap_unverified", ("chat",), records)


@pytest.fixture
def stores(tmp_path):
    reader = Store(tmp_path / "store.sqlite")
    writer = Store(reader.path)
    position = reader.acquire_shadow(reader.inspect_shadow_position(), "publisher", SOURCE)
    position = reader.publish_shadow(position, _snapshot())
    reader.upsert_messages("chat", [{"id": "old", "ns": 1, "from": "ann", "body": "old"}])
    reader.set_offset("chat", "log-old", 1)
    reader.cache_doc("sync/log_cursor", {"cursor": "old"})
    yield reader, writer, position
    writer.close()
    reader.close()


def _replace_everything(writer, position):
    next_position = writer.acquire_shadow(position, "replacement", SOURCE)
    next_position = writer.publish_shadow(
        next_position, _snapshot(2, (("shadow/new", '{"new":true}'),))
    )
    writer.forget_chat("chat")
    writer.upsert_messages("chat", [{"id": "new", "ns": 2, "from": "ann", "body": "new"}])
    writer.set_offset("chat", "log-new", 2)
    writer.cache_doc("sync/log_cursor", {"cursor": "new"})
    return next_position


def _run_writer_after(entered, writer, position):
    committed = threading.Event()
    result, errors = [], []

    def write():
        try:
            assert entered.wait(5)
            result.append(_replace_everything(writer, position))
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer.close()
            committed.set()

    thread = threading.Thread(target=write)
    thread.start()
    return thread, committed, result, errors


def _assert_old(capture, position):
    assert capture.position == position
    assert capture.snapshot == _snapshot()
    assert capture.message_json == (json.dumps(
        {"id": "old", "ns": 1, "from": "ann", "body": "old"}
    ),)
    assert capture.offsets == (("log-old", 1),)
    assert capture.document_json == (("sync/log_cursor", '{"cursor": "old"}'),)


def test_initialized_and_uninitialized_cuts_are_immutable_and_exact(stores):
    reader, _writer, position = stores
    capture = reader.capture_shadow_chat_inputs(position, "chat")
    _assert_old(capture, position)
    with pytest.raises(FrozenInstanceError):
        capture.chat_id = "other"
    with pytest.raises(AttributeError):
        capture.message_json += ("{}",)

    uninitialized = reader.acquire_shadow(position, "uninitialized", SOURCE)
    paired = reader.capture_shadow_chat_inputs(uninitialized, "chat")
    assert paired.position == uninitialized
    assert paired.snapshot is None
    assert paired.message_json == capture.message_json
    assert paired.offsets == capture.offsets
    assert paired.document_json == capture.document_json


@pytest.mark.parametrize("boundary", ["identity", "materialized"])
def test_writer_commit_after_identity_or_shadow_materialization_keeps_one_old_cut(
        stores, monkeypatch, boundary):
    reader, writer, position = stores
    entered = threading.Event()
    thread, committed, replacement, errors = _run_writer_after(entered, writer, position)
    reader_thread = threading.get_ident()

    if boundary == "identity":
        original = shadow_slot._match

        def after_identity(conn, path, expected):
            matched = original(conn, path, expected)
            if threading.get_ident() == reader_thread:
                entered.set()
                assert committed.wait(5)
            return matched

        monkeypatch.setattr(shadow_slot, "_match", after_identity)
    else:
        original = shadow_slot._capture_rows

        def after_materialization(conn, current):
            rows = original(conn, current)
            if threading.get_ident() == reader_thread:
                entered.set()
                assert committed.wait(5)
            return rows

        monkeypatch.setattr(shadow_slot, "_capture_rows", after_materialization)

    try:
        capture = reader.capture_shadow_chat_inputs(position, "chat")
        _assert_old(capture, position)
        thread.join(5)
        assert not thread.is_alive()
        assert errors == []
        fresh = reader.capture_shadow_chat_inputs(replacement[0], "chat")
        assert fresh.snapshot == _snapshot(2, (("shadow/new", '{"new":true}'),))
        assert fresh.message_json[0] == json.dumps(
            {"id": "new", "ns": 2, "from": "ann", "body": "new"}
        )
        assert fresh.offsets == (("log-new", 2),)
        assert fresh.document_json == (("sync/log_cursor", '{"cursor": "new"}'),)
    finally:
        thread.join(5)


def _aggregate_charge(capture, chat_ids_json):
    position = capture.position
    fields = [position.database_path, position.incarnation, capture.chat_id]
    if position.publisher_nonce is not None:
        fields.append(position.publisher_nonce)
    if position.source is not None:
        fields.extend((position.source.root, position.source.cache, position.source.mirror_nonce))
    if capture.snapshot is not None:
        fields.extend((capture.snapshot.provenance, chat_ids_json))
        for path, payload in capture.snapshot.records:
            fields.extend((path, payload))
    for payload in capture.message_json:
        fields.append(payload)
    fields.extend(name for name, _offset in capture.offsets)
    for path, payload in capture.document_json:
        fields.append(path)
        if payload is not None:
            fields.append(payload)
    return sum(len(value.encode("utf-8")) for value in fields)


def test_aggregate_budget_is_exact_utf8_and_charges_absent_fixed_doc(stores):
    reader, _writer, position = stores
    reader.upsert_messages("chát", [{"id": "é", "ns": 1, "from": "é", "body": "é"}])
    reader.set_offset("chát", "løg", 3)
    with reader._conn():
        reader._conn().execute("DELETE FROM docs WHERE path='sync/log_cursor'")
    raw_chat_ids = reader._conn().execute(
        "SELECT chat_ids FROM diagnostic_shadow_slot WHERE singleton=1"
    ).fetchone()[0]
    capture = reader.capture_shadow_chat_inputs(position, "chát")
    exact = _aggregate_charge(capture, raw_chat_ids)
    assert capture.document_json == (("sync/log_cursor", None),)
    assert reader.capture_shadow_chat_inputs(position, "chát", max_bytes=exact) == capture
    with pytest.raises(OverflowError):
        reader.capture_shadow_chat_inputs(position, "chát", max_bytes=exact - 1)


def test_preflight_rejects_oversize_without_payload_transfer_and_closes_reader(stores, monkeypatch):
    reader, _writer, position = stores
    statements, readers = [], []
    connect = shadow_chat_inputs.sqlite3.connect

    def traced_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        readers.append(conn)
        return conn

    monkeypatch.setattr(shadow_chat_inputs.sqlite3, "connect", traced_reader)
    identity_bytes = sum(len(value.encode("utf-8")) for value in (
        position.database_path, position.incarnation, "chat", position.publisher_nonce,
        position.source.root, position.source.cache, position.source.mirror_nonce,
        "sync/log_cursor",
    ))
    with pytest.raises(OverflowError):
        reader.capture_shadow_chat_inputs(position, "chat", max_bytes=identity_bytes + 1)
    assert not any("SELECT path,payload" in sql or "SELECT payload FROM messages" in sql
                   for sql in statements)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")


@pytest.mark.parametrize("kwargs", [
    {"max_documents": True}, {"max_chat_ids": -1}, {"max_messages": 100_001},
    {"max_logs": 10_001}, {"max_bytes": 64 * 1024 * 1024 + 1},
])
def test_invalid_supported_limits_fail_before_opening_reader(stores, monkeypatch, kwargs):
    reader, _writer, position = stores
    monkeypatch.setattr(
        shadow_chat_inputs.sqlite3, "connect",
        lambda *_args, **_kwargs: pytest.fail("invalid arguments opened SQLite"),
    )
    with pytest.raises(ValueError):
        reader.capture_shadow_chat_inputs(position, "chat", **kwargs)


def test_stale_inactive_and_tampered_tokens_conflict_before_payload_selection(stores, monkeypatch):
    reader, _writer, position = stores
    inactive = reader.retire_shadow(position)
    statements = []
    original = shadow_chat_inputs.sqlite3.connect

    def traced_reader(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(shadow_chat_inputs.sqlite3, "connect", traced_reader)
    for token in (inactive, replace(position, publisher_nonce="forged")):
        with pytest.raises(ShadowConflict):
            reader.capture_shadow_chat_inputs(token, "chat")
    assert not any("SELECT path,payload" in sql or "SELECT payload FROM messages" in sql
                   for sql in statements)


def test_corrupt_shadow_is_rejected_after_reader_close(stores, monkeypatch):
    reader, _writer, position = stores
    with reader._conn():
        reader._conn().execute("UPDATE diagnostic_shadow_records SET payload='not-json'")
    open_reader, readers = shadow_chat_inputs.sqlite3.connect, []

    def tracked_reader(*args, **kwargs):
        conn = open_reader(*args, **kwargs)
        readers.append(conn)
        return conn

    monkeypatch.setattr(shadow_chat_inputs.sqlite3, "connect", tracked_reader)
    with pytest.raises(ValueError):
        reader.capture_shadow_chat_inputs(position, "chat")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")


@pytest.mark.parametrize("serialized", ['{}', '"chat"'])
def test_wrong_shaped_stored_chat_ids_reject_in_legacy_and_paired_capture_after_close(
        stores, monkeypatch, serialized):
    reader, _writer, position = stores
    with reader._conn():
        reader._conn().execute(
            "UPDATE diagnostic_shadow_slot SET chat_ids=? WHERE singleton=1", (serialized,)
        )
    legacy_readers, paired_readers = [], []
    legacy_open, paired_open = shadow_slot._open_reader, shadow_chat_inputs.sqlite3.connect

    def legacy_reader(*args, **kwargs):
        conn = legacy_open(*args, **kwargs)
        legacy_readers.append(conn)
        return conn

    def paired_reader(*args, **kwargs):
        conn = paired_open(*args, **kwargs)
        paired_readers.append(conn)
        return conn

    monkeypatch.setattr(shadow_slot, "_open_reader", legacy_reader)
    monkeypatch.setattr(shadow_chat_inputs.sqlite3, "connect", paired_reader)
    with pytest.raises(ValueError, match="JSON list"):
        reader.capture_shadow(position)
    with pytest.raises(ValueError, match="JSON list"):
        reader.capture_shadow_chat_inputs(position, "chat")
    for connection in (*legacy_readers, *paired_readers):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_message_ties_remain_canonical(stores):
    reader, _writer, position = stores
    reader.upsert_messages("ties", [
        {"id": "z", "ns": 9, "from": "ann"},
        {"id": "a", "ns": 9, "from": "ann"},
        {"id": "m", "ns": 9, "from": "bob"},
    ])
    capture = reader.capture_shadow_chat_inputs(position, "ties")
    assert [json.loads(payload)["id"] for payload in capture.message_json] == ["a", "z", "m"]
