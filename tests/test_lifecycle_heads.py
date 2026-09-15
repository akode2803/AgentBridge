"""Regression contracts for generation-fenced retained lifecycle heads."""

from __future__ import annotations

import sqlite3

import pytest

from agentbridge.store.db import Store
from agentbridge.store.lifecycle_heads import MAX_BYTES, MAX_SQLITE_INTEGER, PREFIX


def _head(label: str) -> dict:
    return {"id": label, "v": 1}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    yield value
    value.close()


def _payload(store, subject="claude"):
    row = store._conn().execute(
        "SELECT payload FROM docs WHERE path=?", (PREFIX + subject,),
    ).fetchone()
    return row[0] if row else None


def test_absent_insert_update_and_nochange_require_exact_position(store):
    absent = store.observe_lifecycle_head("claude")
    assert absent.payload_json is None and absent.generation == 0

    assert store.publish_lifecycle_head(absent, _head("one"))
    first = store.observe_lifecycle_head("claude")
    assert first.payload_json == '{"id":"one","v":1}'
    assert first.generation > absent.generation

    fetched = store._conn().execute(
        "SELECT fetched_ns FROM docs WHERE path=?", (PREFIX + "claude",),
    ).fetchone()[0]
    assert store.publish_lifecycle_head(first, _head("one"))
    assert store.observe_lifecycle_head("claude") == first
    assert store._conn().execute(
        "SELECT fetched_ns FROM docs WHERE path=?", (PREFIX + "claude",),
    ).fetchone()[0] == fetched

    assert store.publish_lifecycle_head(first, _head("two"))
    winner = store.observe_lifecycle_head("claude")
    assert winner.generation > first.generation
    assert not store.publish_lifecycle_head(first, _head("one"))
    assert store.observe_lifecycle_head("claude") == winner


def test_delete_reinsert_same_payload_is_generation_fenced_aba(store):
    initial = store.observe_lifecycle_head("claude")
    assert store.publish_lifecycle_head(initial, _head("stable"))
    stale = store.observe_lifecycle_head("claude")

    with store._conn() as conn:
        conn.execute("DELETE FROM docs WHERE path=?", (PREFIX + "claude",))
        conn.execute(
            "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
            (PREFIX + "claude", stale.payload_json, 17),
        )
    current = store.observe_lifecycle_head("claude")
    assert current.payload_json == stale.payload_json
    assert current.generation > stale.generation
    assert not store.publish_lifecycle_head(stale, _head("loser"))
    assert _payload(store) == stale.payload_json


def test_generic_docs_writes_and_renames_invalidate_only_affected_subjects(store):
    for name in ("claude", "fable"):
        expected = store.observe_lifecycle_head(name)
        assert store.publish_lifecycle_head(expected, _head(name))
    claude = store.observe_lifecycle_head("claude")
    fable = store.observe_lifecycle_head("fable")

    store.cache_doc("unrelated/doc", {"keep": True})
    assert store.observe_lifecycle_head("claude") == claude
    assert store.observe_lifecycle_head("fable") == fable

    with store._conn() as conn:
        conn.execute("DELETE FROM docs WHERE path=?", (PREFIX + "fable",))
        conn.execute("UPDATE docs SET path=? WHERE path=?",
                     (PREFIX + "fable", PREFIX + "claude"))
    after_claude = store.observe_lifecycle_head("claude")
    after_fable = store.observe_lifecycle_head("fable")
    assert after_claude.generation > claude.generation
    assert after_claude.payload_json is None
    assert after_fable.generation > fable.generation
    assert after_fable.payload_json == claude.payload_json
    assert not store.publish_lifecycle_head(claude, _head("stale"))
    assert not store.publish_lifecycle_head(fable, _head("stale"))


@pytest.mark.parametrize("recursive", (0, 1))
def test_replace_invalidates_position_with_both_trigger_settings(store, recursive):
    expected = store.observe_lifecycle_head("claude")
    assert store.publish_lifecycle_head(expected, _head("first"))
    stale = store.observe_lifecycle_head("claude")
    conn = store._conn()
    conn.execute(f"PRAGMA recursive_triggers={recursive}")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
            (PREFIX + "claude", '{"id":"replace","v":1}', 9),
        )
    fresh = store.observe_lifecycle_head("claude")
    assert fresh.generation > stale.generation
    assert fresh.payload_json == '{"id":"replace","v":1}'
    assert not store.publish_lifecycle_head(stale, _head("loser"))


def test_present_malformed_payload_is_observable_not_absent(store):
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
            (PREFIX + "claude", "{not-json", 1),
        )
    observed = store.observe_lifecycle_head("claude")
    assert observed.payload_json == "{not-json"
    assert observed.generation >= 1
    assert store.publish_lifecycle_head(observed, _head("replacement"))


def test_sql_failure_rolls_back_head_and_preserves_unrelated_data(store):
    store.cache_doc("unrelated/doc", {"keep": True})
    store.upsert_messages("chat", [{"id": "m", "ns": 1}])
    outbox = store.outbox_add("append_log", "chat|writer", {"id": "o"})
    expected = store.observe_lifecycle_head("claude")
    store._conn().execute(
        "CREATE TRIGGER fail_lifecycle_insert BEFORE INSERT ON docs "
        "WHEN NEW.path='lifecycle/head/claude' "
        "BEGIN SELECT RAISE(ABORT,'injected'); END",
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.publish_lifecycle_head(expected, _head("blocked"))
    assert not store._conn().in_transaction
    assert store.observe_lifecycle_head("claude") == expected
    assert store.cached_doc("unrelated/doc") == {"keep": True}
    assert [row["id"] for row in store.messages("chat")] == ["m"]
    assert [item.seq for item in store.outbox_claim_due()] == [outbox]


def test_database_replacement_rejects_old_observation(tmp_path):
    path = tmp_path / "store.sqlite"
    original = Store(path)
    try:
        expected = original.observe_lifecycle_head("claude")
    finally:
        original.close()
    path.unlink()
    replacement = Store(path)
    try:
        assert replacement.observe_lifecycle_head("claude").incarnation != expected.incarnation
        assert not replacement.publish_lifecycle_head(expected, _head("stale"))
        assert replacement.observe_lifecycle_head("claude").payload_json is None
    finally:
        replacement.close()


def test_reopen_seeds_legacy_head_and_rejects_missing_generation(tmp_path):
    path = tmp_path / "store.sqlite"
    legacy = Store(path)
    try:
        legacy.cache_doc(PREFIX + "claude", _head("legacy"))
    finally:
        legacy.close()
    conn = sqlite3.connect(path)
    try:
        for name in (
            "lifecycle_head_docs_insert", "lifecycle_head_docs_delete",
            "lifecycle_head_docs_update_old", "lifecycle_head_docs_update_new",
        ):
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE lifecycle_head_versions")
        conn.commit()
    finally:
        conn.close()

    migrated = Store(path)
    try:
        seeded = migrated.observe_lifecycle_head("claude")
        assert seeded.payload_json == '{"id": "legacy", "v": 1}'
        assert seeded.generation == 1
    finally:
        migrated.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM lifecycle_head_versions WHERE path=?",
                     (PREFIX + "claude",))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.DatabaseError, match="missing its durable generation"):
        Store(path)


def test_generation_exhaustion_and_oversized_values_roll_back(store):
    expected = store.observe_lifecycle_head("claude")
    assert store.publish_lifecycle_head(expected, _head("before"))
    before = store.observe_lifecycle_head("claude")
    with store._conn() as conn:
        conn.execute(
            "UPDATE lifecycle_head_versions SET generation=? WHERE path=?",
            (MAX_SQLITE_INTEGER, PREFIX + "claude"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.cache_doc(PREFIX + "claude", _head("overflow"))
    assert _payload(store) == before.payload_json
    assert store._conn().execute(
        "SELECT generation FROM lifecycle_head_versions WHERE path=?",
        (PREFIX + "claude",),
    ).fetchone()[0] == MAX_SQLITE_INTEGER

    oversized = store.observe_lifecycle_head("fable")
    with pytest.raises(OverflowError, match="proposal exceeds byte budget"):
        store.publish_lifecycle_head(oversized, {"blob": "x" * MAX_BYTES})
    assert store.observe_lifecycle_head("fable") == oversized

    with store._conn() as conn:
        conn.execute(
            "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
            (PREFIX + "large", "x" * (MAX_BYTES + 1), 1),
        )
    with pytest.raises(OverflowError, match="payload exceeds byte budget"):
        store.observe_lifecycle_head("large")
