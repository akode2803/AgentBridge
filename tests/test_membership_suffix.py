"""Bounded Store capture of canonical membership-event suffixes."""
from __future__ import annotations

import sqlite3

import pytest

from agentbridge.store import document_observation, membership_suffix
from agentbridge.store.db import Store


CHAT = "room"


def _info(ident, ns, event_type="renamed", sender="alice", **event):
    return {
        "id": ident, "ns": ns, "from": sender, "kind": "info",
        "event": {"type": event_type, **event},
    }


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def test_suffix_matches_state_events_after_and_uses_strict_scalar_cutoff(store):
    records = [
        _info("old", 4, "created"),
        _info("tie-z", 5, sender="zoe"),
        _info("tie-a", 5, sender="alice"),
        {"id": "message", "ns": 6, "from": "alice", "kind": "message"},
        _info("reaction", 7, "reaction"),
        _info("new", 8, "member_added", who="bob"),
    ]
    store.upsert_messages(CHAT, records)
    store.prepare_membership_suffix_index()

    captured = store.capture_membership_suffix(CHAT, 5)
    assert [row.decoded() for row in captured.rows] == store.state_events_after(CHAT, 5)
    assert [row.key.id for row in captured.rows] == ["new"]
    assert captured.after_ns == 5 and captured.captured_bytes > 0

    from_four = store.capture_membership_suffix(CHAT, 4)
    assert [row.key.id for row in from_four.rows] == ["tie-a", "tie-z", "new"]


@pytest.mark.parametrize("limit_kind", ["rows", "bytes"])
def test_budget_preflight_never_fetches_any_payload(store, monkeypatch, limit_kind):
    records = [_info("a", 1), _info("b", 2)]
    if limit_kind == "bytes":
        records[0]["event"]["body"] = "x" * 100_000
    store.upsert_messages(CHAT, records)
    store.prepare_membership_suffix_index()
    statements = []
    original = document_observation._open_reader

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(document_observation, "_open_reader", traced)
    kwargs = {"max_events": 1} if limit_kind == "rows" else {"max_bytes": 100}
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable, match="budget_exceeded"):
        store.capture_membership_suffix(CHAT, 0, **kwargs)
    assert not any(sql.startswith("SELECT payload FROM messages") for sql in statements)


def test_reaction_and_message_noise_do_not_change_fixed_suffix_work(tmp_path, monkeypatch):
    observed = []
    for count in (1_000, 100_000):
        opened = Store(tmp_path / f"store-{count}.sqlite")
        try:
            for base in range(0, count, 10_000):
                opened.upsert_messages(CHAT, (
                    _info(f"r{x:06}", x + 1, "reaction")
                    if x % 2 else {
                        "id": f"m{x:06}", "ns": x + 1,
                        "from": "alice", "kind": "message",
                    }
                    for x in range(base, min(base + 10_000, count))
                ))
            opened.upsert_messages(CHAT, [_info("qualifying", count + 1)])
            opened.prepare_membership_suffix_index()
            steps = [0]
            original = document_observation._open_reader

            def counted(path):
                conn = original(path)

                def tick():
                    steps[0] += 1
                    return 0

                conn.set_progress_handler(tick, 1)
                return conn

            monkeypatch.setattr(document_observation, "_open_reader", counted)
            suffix = opened.capture_membership_suffix(CHAT, count)
            assert [row.key.id for row in suffix.rows] == ["qualifying"]
            plan = opened._conn().execute(
                "EXPLAIN QUERY PLAN SELECT ns,sender,id,kind,"
                "length(CAST(payload AS BLOB)) FROM messages INDEXED BY "
                f"{membership_suffix.INDEX} WHERE chat_id=? AND ns>? AND "
                f"{membership_suffix.PREDICATE} ORDER BY ns,sender,id LIMIT ?",
                (CHAT, count, 129),
            ).fetchall()
            assert any("SEARCH" in row[3] and membership_suffix.INDEX in row[3]
                       for row in plan)
            observed.append(steps[0])
        finally:
            opened.close()
            monkeypatch.setattr(document_observation, "_open_reader", original)
    assert observed[0] == observed[1]


def test_expected_position_rejects_delayed_older_insert(store):
    store.upsert_messages(CHAT, [_info("new", 10)])
    store.prepare_membership_suffix_index()
    first = store.capture_membership_suffix(CHAT, 0)
    store.upsert_messages(CHAT, [_info("delayed-old", 5)])
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                       match="message_inputs_changed"):
        store.capture_membership_suffix(CHAT, 0, expected=first.position)


def test_index_is_explicit_fail_closed_and_rebuildable(store):
    store.upsert_messages(CHAT, [_info("event", 1)])
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                       match="suffix_index_pending"):
        store.capture_membership_suffix(CHAT, 0)
    store.prepare_membership_suffix_index()
    assert len(store.capture_membership_suffix(CHAT, 0).rows) == 1

    with store._conn():
        store._conn().execute(f"DROP INDEX {membership_suffix.INDEX}")
        store._conn().execute(
            f"CREATE INDEX {membership_suffix.INDEX} ON messages(chat_id,id)"
        )
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                       match="suffix_index_changed"):
        store.capture_membership_suffix(CHAT, 0)
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                       match="suffix_index_changed"):
        store.prepare_membership_suffix_index()
    with store._conn():
        store._conn().execute(f"DROP INDEX {membership_suffix.INDEX}")
    store.prepare_membership_suffix_index()
    assert [row.key.id for row in store.capture_membership_suffix(CHAT, 0).rows] == ["event"]


def test_malformed_json_is_indexed_but_only_fails_when_suffix_qualifies(store):
    conn = store._conn()
    with conn:
        conn.execute(
            "INSERT INTO messages(chat_id,id,ns,sender,kind,payload,observed_ns,"
            "observed_mono,observed_clock) VALUES(?,?,?,?,?,?,?,?,?)",
            (CHAT, "malformed", 5, "alice", "info", "{broken", 1, 1, "clock"),
        )
    store.prepare_membership_suffix_index()
    assert store.capture_membership_suffix(CHAT, 5).rows == ()
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                       match="malformed_suffix_payload"):
        store.capture_membership_suffix(CHAT, 4)


@pytest.mark.parametrize(("ident", "payload"), [
    ("bool-ns", '{"id":"bool-ns","ns":true,"from":"alice","kind":"info",'
                '"event":{"type":"renamed"}}'),
    ("7", '{"id":7,"ns":6,"from":"alice","kind":"info",'
          '"event":{"type":"renamed"}}'),
])
def test_payload_identity_requires_exact_string_id_and_nonbool_integer_ns(
        store, ident, payload):
    ns = 5 if ident == "bool-ns" else 6
    with store._conn():
        store._conn().execute(
            "INSERT INTO messages(chat_id,id,ns,sender,kind,payload,observed_ns,"
            "observed_mono,observed_clock) VALUES(?,?,?,?,?,?,?,?,?)",
            (CHAT, ident, ns, "alice", "info", payload, 1, 1, "clock"),
        )
    store.prepare_membership_suffix_index()
    with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                       match="malformed_suffix_payload"):
        store.capture_membership_suffix(CHAT, 0)


def test_transaction_ownership_is_preserved(store):
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        conn.execute("SELECT 1")
        with pytest.raises(sqlite3.OperationalError, match="owned transaction"):
            store.prepare_membership_suffix_index()
        assert conn.in_transaction
    finally:
        conn.rollback()

    store.prepare_membership_suffix_index()
    with pytest.raises(sqlite3.OperationalError, match="active transaction"):
        membership_suffix._capture(conn, store.path, CHAT, 0)
