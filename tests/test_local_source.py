"""Durable local raw-source admission contracts."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from agentbridge.store import local_source
from agentbridge.store.db import Store


SOURCE = "folder:local"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    local_source.initialize(opened)
    yield opened
    opened.close()


def _publish_and_admit(store, documents=None, *, observed_ns=10):
    expected = local_source.capture(store, SOURCE)
    published = store.publish_document_batch(
        expected.raw, documents or {"doc": {"value": 1}}, cursor=1, full=True,
    )
    admitted = local_source.admit(
        store, expected, published, observed_ns=observed_ns,
    )
    assert admitted.ready
    return admitted


def test_raw_publication_requires_separate_admission(store):
    expected = local_source.capture(store, SOURCE)
    assert not expected.ready and expected.revision == 0
    published = store.publish_document_batch(
        expected.raw, {"doc": 1}, cursor=1, full=True,
    )
    pending = local_source.capture(store, SOURCE)
    assert pending.raw == published
    assert not pending.ready and pending.writes_pending == 0

    ready = local_source.admit(store, expected, published, observed_ns=12)
    assert ready.ready and ready.raw == published
    assert ready.revision == 1


def test_begin_write_durably_invalidates_before_external_write_and_survives_reopen(
        store):
    ready = _publish_and_admit(store)
    intent = local_source.begin_write(store, SOURCE)
    invalid = local_source.capture(store, SOURCE)
    assert not invalid.ready
    assert invalid.raw == ready.raw
    assert invalid.writes_pending == 1
    assert invalid.revision == ready.revision + 1

    path = store.path
    store.close()
    reopened = Store(path)
    try:
        persisted = local_source.capture(reopened, SOURCE)
        assert not persisted.ready
        assert persisted.writes_pending == 1
        assert persisted.revision == invalid.revision
        assert local_source.health(reopened, SOURCE)["writes_pending"] == 1
        local_source.complete_write(reopened, intent)
        completed = local_source.capture(reopened, SOURCE)
        assert completed.writes_pending == 0
        assert not completed.ready
        assert completed.revision == invalid.revision + 1
    finally:
        reopened.close()


def test_overlapping_write_intents_remain_unavailable_until_each_completes(store):
    _publish_and_admit(store)
    first = local_source.begin_write(store, SOURCE)
    second = local_source.begin_write(store, SOURCE)
    assert local_source.capture(store, SOURCE).writes_pending == 2

    local_source.complete_write(store, second)
    one_left = local_source.capture(store, SOURCE)
    assert one_left.writes_pending == 1 and not one_left.ready
    local_source.complete_write(store, first)
    none_left = local_source.capture(store, SOURCE)
    assert none_left.writes_pending == 0 and not none_left.ready


def test_stale_publisher_cannot_cross_begin_or_complete(store):
    _publish_and_admit(store)
    expected = local_source.capture(store, SOURCE)
    intent = local_source.begin_write(store, SOURCE)
    published = store.publish_document_batch(
        expected.raw, {"doc": 2}, cursor=2, full=True,
    )
    with pytest.raises(local_source.SourceChanged, match="source_changed"):
        local_source.admit(store, expected, published, observed_ns=20)

    local_source.complete_write(store, intent)
    with pytest.raises(local_source.SourceChanged, match="source_changed"):
        local_source.admit(store, expected, published, observed_ns=21)
    assert not local_source.capture(store, SOURCE).ready


def test_complete_replay_foreign_database_and_forged_owner_are_rejected(store,
                                                                         tmp_path):
    _publish_and_admit(store)
    intent = local_source.begin_write(store, SOURCE)
    local_source.complete_write(store, intent)
    with pytest.raises(local_source.SourceChanged, match="write_intent_missing"):
        local_source.complete_write(store, intent)

    foreign = Store(tmp_path / "foreign.sqlite")
    local_source.initialize(foreign)
    try:
        with pytest.raises(ValueError, match="wrong write intent"):
            local_source.complete_write(foreign, intent)
    finally:
        foreign.close()

    forged = replace(intent, incarnation="0" * 32)
    with pytest.raises(local_source.SourceChanged, match="write_owner_changed"):
        local_source.complete_write(store, forged)


def test_health_failure_preserves_last_success_and_requires_new_admission(store):
    ready = _publish_and_admit(store, observed_ns=1234)
    local_source.record_failure(store, SOURCE, reason="io")
    health = local_source.health(store, SOURCE)
    assert health == {
        "ready": False,
        "writes_pending": 0,
        "last_success_ns": 1234,
        "failures": 1,
        "error": "io",
    }
    assert local_source.capture(store, SOURCE).raw == ready.raw

    expected = local_source.capture(store, SOURCE)
    published = store.publish_document_batch(
        expected.raw, {"doc": 2}, cursor=2, full=True,
    )
    local_source.admit(store, expected, published, observed_ns=2345)
    assert local_source.health(store, SOURCE) == {
        "ready": True,
        "writes_pending": 0,
        "last_success_ns": 2345,
        "failures": 0,
        "error": "",
    }


def test_transaction_capture_is_one_coherent_sqlite_snapshot(store):
    admitted = _publish_and_admit(store)
    reader = sqlite3.connect(store.path)
    other = Store(store.path)
    try:
        reader.execute("BEGIN")
        first = local_source.capture_in_transaction(reader, store, SOURCE)
        assert first == admitted
        local_source.invalidate(other, SOURCE)
        assert local_source.capture_in_transaction(reader, store, SOURCE) == first
        reader.rollback()
        reader.execute("BEGIN")
        current = local_source.capture_in_transaction(reader, store, SOURCE)
        assert current.revision == admitted.revision + 1
        assert not current.ready
    finally:
        reader.close()
        other.close()


@pytest.mark.parametrize(
    "damage",
    [
        "DROP INDEX local_source_writes_source",
        "DROP TABLE local_source_writes",
        "UPDATE local_source_schema SET epoch='short' WHERE singleton=1",
    ],
)
def test_partial_or_corrupt_schema_fails_closed_without_repair(store, damage):
    store._conn().execute(damage)
    store._conn().commit()
    with pytest.raises(local_source.SourceChanged, match="local_source"):
        local_source.capture(store, SOURCE)
    with pytest.raises(local_source.SourceChanged, match="local_source"):
        local_source.initialize(store)


def test_admission_rejects_wrong_publication_and_pending_expected(store):
    expected = local_source.capture(store, SOURCE)
    other = store.publish_document_batch(
        store.capture_document_position("other"), {"doc": 1}, cursor=1, full=True,
    )
    with pytest.raises(local_source.SourceChanged, match="publication_not_successor"):
        local_source.admit(store, expected, other, observed_ns=1)

    intent = local_source.begin_write(store, SOURCE)
    during = local_source.capture(store, SOURCE)
    published = store.publish_document_batch(
        during.raw, {"doc": 2}, cursor=1, full=True,
    )
    with pytest.raises(local_source.SourceChanged, match="source_changed"):
        local_source.admit(store, during, published, observed_ns=2)
    assert local_source.capture(store, SOURCE).writes_pending == 1
    local_source.complete_write(store, intent)


@pytest.mark.parametrize("failure", ["budget", "exception"])
def test_publish_failure_before_raw_commit_stays_unavailable_across_reopen(
        store, monkeypatch, failure):
    old = _publish_and_admit(store)
    if failure == "exception":
        def fail_publish(*_args, **_kwargs):
            raise RuntimeError("external publication failed")

        monkeypatch.setattr(store, "publish_document_batch", fail_publish)
        error = RuntimeError
        kwargs = {}
    else:
        error = OverflowError
        kwargs = {"max_bytes": 1}

    with pytest.raises(error):
        local_source.publish(
            store, old, {"too-large": "payload"}, observed_ns=20, **kwargs,
        )
    retired = local_source.capture(store, SOURCE)
    assert not retired.ready and retired.raw == old.raw
    assert retired.revision == old.revision + 1

    path = store.path
    store.close()
    reopened = Store(path)
    try:
        persisted = local_source.capture(reopened, SOURCE)
        assert persisted == retired
        assert not persisted.ready
    finally:
        reopened.close()


def test_raw_commit_followed_by_admission_failure_remains_unavailable(store,
                                                                      monkeypatch):
    old = _publish_and_admit(store)

    def fail_admission(*_args, **_kwargs):
        raise RuntimeError("admission commit failed")

    monkeypatch.setattr(local_source, "admit", fail_admission)
    with pytest.raises(RuntimeError, match="admission commit failed"):
        local_source.publish(store, old, {"doc": 2}, observed_ns=20)
    pending = local_source.capture(store, SOURCE)
    assert not pending.ready
    assert pending.raw.initialized
    assert pending.raw.generation == old.raw.generation + 1
    assert pending.revision == old.revision + 1
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        assert local_source.capture(reopened, SOURCE) == pending
        assert not local_source.capture(reopened, SOURCE).ready
    finally:
        reopened.close()


def test_stale_publish_cas_cannot_retire_newer_winner(store):
    expected = _publish_and_admit(store)
    stale = local_source.capture(store, SOURCE)
    assert stale == expected
    winner = local_source.publish(store, expected, {"doc": "winner"}, observed_ns=30)
    assert winner.ready

    with pytest.raises(local_source.SourceChanged,
                       match="source_changed_before_publication"):
        local_source.publish(store, stale, {"doc": "stale"}, observed_ns=31)
    assert local_source.capture(store, SOURCE) == winner
    assert store.capture_document_observation(SOURCE).document("doc") == "winner"


def test_changed_database_incarnation_never_matches_ready_tuple(store):
    ready = _publish_and_admit(store)
    store._conn().execute(
        "UPDATE ingestion_identity SET incarnation=? WHERE singleton=1",
        ("f" * 32,),
    )
    store._conn().commit()
    changed = local_source.capture(store, SOURCE)
    assert changed.raw.generation == ready.raw.generation
    assert changed.raw.cursor == ready.raw.cursor
    assert changed.raw.incarnation != ready.raw.incarnation
    assert not changed.ready


def test_pending_write_limit_rejects_sixty_fifth_intent(store):
    _publish_and_admit(store)
    intents = [local_source.begin_write(store, SOURCE) for _ in range(64)]
    position = local_source.capture(store, SOURCE)
    assert len(intents) == position.writes_pending == local_source.MAX_WRITES
    with pytest.raises(local_source.SourceChanged, match="too_many_pending_writes"):
        local_source.begin_write(store, SOURCE)
    assert local_source.capture(store, SOURCE) == position


def test_owner_writes_request_sqlite_full_synchronous(store, monkeypatch):
    _publish_and_admit(store)
    real_connect = local_source.sqlite3.connect
    pragmas = []

    class TracedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql == "PRAGMA synchronous=FULL":
                pragmas.append(sql)
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def traced_connect(*args, **kwargs):
        return TracedConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(local_source.sqlite3, "connect", traced_connect)
    local_source.begin_write(store, SOURCE)
    assert pragmas == ["PRAGMA synchronous=FULL"]


def test_missing_database_is_not_recreated_by_write_barrier(store):
    store.close()
    store.path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        local_source.begin_write(store, SOURCE)
    assert not store.path.exists()
