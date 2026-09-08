"""Committed local capture boundaries, budgets and detached decoding."""

import json
from dataclasses import FrozenInstanceError

import pytest

from agentbridge.store import chat_inputs
from agentbridge.store.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store.sqlite")
    s.upsert_messages("chat", [{"id": "a", "ns": 1, "body": {"text": "old"}}])
    s.set_offset("chat", "ann@box", 1)
    s.cache_doc("cursor", {"cursor": 1})
    yield s
    s.close()


def test_capture_is_immutable_and_decodes_detached_values(store):
    snap = store.capture_chat_inputs("chat", document_paths=("cursor", "missing"))
    with pytest.raises(FrozenInstanceError):
        snap.chat_id = "other"
    snap.messages()[0]["body"]["text"] = "modified"
    snap.document("cursor")["cursor"] = 2
    assert snap.messages()[0]["body"]["text"] == "old"
    assert snap.document("cursor") == {"cursor": 1}
    assert snap.document("missing", "absent") == "absent"
    with pytest.raises(KeyError):
        snap.document("not-requested")
    assert snap.offsets == (("ann@box", 1),)


@pytest.mark.parametrize("reset", [False, True])
def test_writer_between_capture_queries_cannot_mix_commits(store, monkeypatch, reset):
    writer = Store(store.path)
    connect = chat_inputs.sqlite3.connect
    fired = []

    def open_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)

        def race(sql):
            if not fired and sql.startswith("SELECT payload FROM messages"):
                fired.append(True)
                if reset:
                    writer.forget_chat("chat")
                with writer._conn() as c:
                    c.execute("UPDATE messages SET payload=? WHERE chat_id='chat'",
                              (json.dumps({"id": "a", "ns": 1, "body": "new"}),))
                    c.execute("UPDATE log_offsets SET offset=2 WHERE chat_id='chat'")
                    c.execute("UPDATE docs SET payload=? WHERE path='cursor'",
                              ('{"cursor":2}',))

        conn.set_trace_callback(race)
        return conn

    monkeypatch.setattr(chat_inputs.sqlite3, "connect", open_reader)
    try:
        snap = store.capture_chat_inputs("chat", document_paths=("cursor",))
        assert fired
        assert snap.messages()[0]["body"] == {"text": "old"}
        assert snap.offsets == (("ann@box", 1),)
        assert snap.document("cursor") == {"cursor": 1}
        fresh = store.capture_chat_inputs("chat", document_paths=("cursor",))
        assert fresh.document("cursor") == {"cursor": 2}
        assert fresh.messages() == ([] if reset else [{"id": "a", "ns": 1, "body": "new"}])
        assert fresh.offsets == (() if reset else (("ann@box", 2),))
    finally:
        writer.close()


def test_caller_write_transaction_is_neither_observed_nor_completed(store):
    c = store._conn()
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE docs SET payload='null' WHERE path='cursor'")
    try:
        snap = store.capture_chat_inputs("chat", document_paths=("cursor",))
        assert snap.document("cursor") == {"cursor": 1}
        assert c.in_transaction
        assert store.cached_doc("cursor") is None
    finally:
        c.rollback()


@pytest.mark.parametrize("budget", [{"max_messages": 0}, {"max_logs": 0}, {"max_bytes": 1}])
def test_budget_failure_does_not_poison_next_capture(store, budget):
    with pytest.raises(OverflowError):
        store.capture_chat_inputs("chat", **budget)
    assert len(store.capture_chat_inputs("chat").messages()) == 1


def test_utf8_byte_budget_is_exact_and_includes_docs_and_log_names(store):
    store.cache_doc("unicode", "\u00e9")
    c = store._conn()
    payload = c.execute("SELECT payload FROM messages").fetchone()[0]
    doc = c.execute("SELECT payload FROM docs WHERE path='unicode'").fetchone()[0]
    total = len(payload.encode()) + len(doc.encode()) + len("ann@box") + len("unicode")
    with pytest.raises(OverflowError):
        store.capture_chat_inputs("chat", document_paths=("unicode",), max_bytes=total - 1)
    assert store.capture_chat_inputs("chat", document_paths=("unicode",), max_bytes=total).document("unicode") == "\u00e9"


def test_only_requested_chat_and_documents_are_captured(store):
    store.upsert_messages("other", [{"id": "private", "ns": 2}])
    store.cache_doc("null", None)
    snap = store.capture_chat_inputs("chat", document_paths=("null", "missing"))
    assert [m["id"] for m in snap.messages()] == ["a"]
    assert snap.document("null", "fallback") is None
    assert snap.document("missing", "fallback") == "fallback"


@pytest.mark.parametrize("options", [
    {"max_messages": True}, {"max_logs": -1}, {"max_bytes": 1.5},
    {"document_paths": ["cursor"]}, {"document_paths": ("cursor", "cursor")},
    {"document_paths": ("",)}, {"document_paths": tuple(str(n) for n in range(129))},
])
def test_invalid_capture_arguments_are_rejected(store, options):
    with pytest.raises(ValueError):
        store.capture_chat_inputs("chat", **options)


def test_oversized_payload_is_rejected_before_materialization(store, monkeypatch):
    connect = chat_inputs.sqlite3.connect
    statements = []
    readers = []

    def open_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        readers.append(conn)
        return conn

    monkeypatch.setattr(chat_inputs.sqlite3, "connect", open_reader)
    with pytest.raises(OverflowError):
        store.capture_chat_inputs("chat", max_bytes=1)
    assert not any(sql.startswith("SELECT payload") for sql in statements)
    with pytest.raises(chat_inputs.sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")


def test_successful_capture_releases_connection_before_decoding(store, monkeypatch):
    connect = chat_inputs.sqlite3.connect
    readers = []

    def open_reader(*args, **kwargs):
        conn = connect(*args, **kwargs)
        readers.append(conn)
        return conn

    monkeypatch.setattr(chat_inputs.sqlite3, "connect", open_reader)
    snap = store.capture_chat_inputs("chat")
    with pytest.raises(chat_inputs.sqlite3.ProgrammingError, match="closed"):
        readers[0].execute("SELECT 1")
    assert snap.messages()[0]["id"] == "a"
