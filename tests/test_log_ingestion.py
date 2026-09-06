"""R164 atomic message-log ingestion and Sync publication boundaries."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

import pytest

from agentbridge.mesh.sync import CURSOR_DOC, SyncEngine
from agentbridge.store.db import LogIngestionConflict, Store
from agentbridge.transport.base import Transport
from agentbridge.transport.folder import FolderTransport


def _record(message_id: str, ns: int, sender: str = "ann") -> dict:
    return {
        "id": message_id,
        "ns": ns,
        "from": sender,
        "kind": "message",
        "body": message_id,
    }


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "cache.sqlite")
    yield opened
    opened.close()


def test_ingest_log_commits_offset_dedup_and_first_observation(store):
    first = [_record("m1", 1), _record("m2", 2)]
    first_position = store.capture_log_position("chat", "ann@box")
    assert store.ingest_log(
        "chat", "ann@box", first_position, 20, first,
        observed_ns=101, observed_mono=202, observed_clock="clock-first",
    ) == first
    assert store.get_offset("chat", "ann@box") == 20

    replay_and_new = [first[0], _record("m3", 3)]
    second_position = store.capture_log_position("chat", "ann@box")
    assert store.ingest_log(
        "chat", "ann@box", second_position, 40, replay_and_new,
        observed_ns=303, observed_mono=404, observed_clock="clock-later",
    ) == [replay_and_new[1]]

    assert [row["id"] for row in store.messages("chat")] == ["m1", "m2", "m3"]
    assert store.get_offset("chat", "ann@box") == 40
    assert store.message_observation("chat", "m1") == (101, 202, "clock-first")
    assert store.message_observation("chat", "m3") == (303, 404, "clock-later")


@pytest.mark.parametrize(
    "new_offset",
    [-1, False, 1.5, "2"],
)
def test_ingest_log_rejects_non_integer_or_negative_offsets(store, new_offset):
    position = store.capture_log_position("chat", "ann@box")
    with pytest.raises((TypeError, ValueError)):
        store.ingest_log(
            "chat", "ann@box", position, new_offset,
            [_record("must-not-land", 1)],
        )
    assert store.message_count("chat") == 0
    assert store.get_offset("chat", "ann@box") == 0


def test_ingest_log_empty_batches_advance_and_current_shrink_is_allowed(store):
    record = _record("retained", 1)
    position = store.capture_log_position("chat", "ann@box")
    assert store.ingest_log(
        "chat", "ann@box", position, 100, [record]) == [record]

    # Re-reading a legitimately shrunken log deduplicates old rows but moves
    # its byte frontier backwards when the scan began at the current offset.
    position = store.capture_log_position("chat", "ann@box")
    assert store.ingest_log(
        "chat", "ann@box", position, 20, [record]) == []
    assert store.get_offset("chat", "ann@box") == 20
    assert store.message_count("chat") == 1

    position = store.capture_log_position("chat", "ann@box")
    assert store.ingest_log("chat", "ann@box", position, 25, []) == []
    assert store.get_offset("chat", "ann@box") == 25


def test_ingest_log_stale_separate_connection_conflicts_without_writes(tmp_path):
    path = tmp_path / "cache.sqlite"
    winner = Store(path)
    stale = Store(path)
    try:
        first_position = winner.capture_log_position("chat", "ann@box")
        winner.ingest_log("chat", "ann@box", first_position, 10, [])
        stale_position = stale.capture_log_position("chat", "ann@box")
        winner_position = winner.capture_log_position("chat", "ann@box")
        winner.ingest_log(
            "chat", "ann@box", winner_position, 20, [_record("winner", 1)])

        with pytest.raises(LogIngestionConflict):
            stale.ingest_log(
                "chat", "ann@box", stale_position, 30,
                [_record("stale-loser", 2)],
            )

        assert stale.get_offset("chat", "ann@box") == 20
        assert [row["id"] for row in stale.messages("chat")] == ["winner"]
    finally:
        stale.close()
        winner.close()


def test_overlapping_writer_rechecks_offset_after_acquiring_lock(tmp_path):
    path = tmp_path / "cache.sqlite"
    winner = Store(path)
    initial_position = winner.capture_log_position("chat", "ann@box")
    winner.ingest_log("chat", "ann@box", initial_position, 10, [])
    stale_position = winner.capture_log_position("chat", "ann@box")
    ready = threading.Event()
    start = threading.Event()
    attempting_begin = threading.Event()

    def losing_scan():
        loser = Store(path)
        try:
            conn = loser._conn()
            conn.set_trace_callback(
                lambda sql: attempting_begin.set()
                if sql == "BEGIN IMMEDIATE" else None)
            ready.set()
            assert start.wait(5)
            with pytest.raises(LogIngestionConflict):
                loser.ingest_log("chat", "ann@box", stale_position, 30,
                                 [_record("loser", 2)])
        finally:
            loser.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(losing_scan)
            assert ready.wait(5)
            try:
                # The loser can still read the old WAL snapshot, but cannot
                # acquire its write lock until this transaction commits.
                conn = winner._conn()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE log_offsets SET offset=20")
                start.set()
                assert attempting_begin.wait(5)
                conn.commit()
            finally:
                winner._conn().rollback()
                start.set()
            future.result(timeout=10)
        assert winner.get_offset("chat", "ann@box") == 20
        assert winner.message_count("chat") == 0
    finally:
        winner.close()


def test_ingest_log_offset_failure_rolls_back_inserted_messages(store):
    initial_position = store.capture_log_position("chat", "ann@box")
    store.ingest_log("chat", "ann@box", initial_position, 7, [])
    position = store.capture_log_position("chat", "ann@box")
    with store._conn() as conn:
        conn.execute(
            "CREATE TRIGGER reject_ingested_offset BEFORE UPDATE OF offset "
            "ON log_offsets WHEN NEW.offset=9 "
            "BEGIN SELECT RAISE(ABORT, 'offset failed'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="offset failed"):
        store.ingest_log(
            "chat", "ann@box", position, 9, [_record("rolled-back", 1)])

    assert store.capture_log_position("chat", "ann@box") == position
    assert store.get_offset("chat", "ann@box") == 7
    assert store.message_count("chat") == 0


def test_ingest_log_process_exit_rolls_back_record_and_offset(tmp_path):
    path = tmp_path / "cache.sqlite"
    baseline = Store(path)
    position = baseline.capture_log_position("chat", "ann@box")
    baseline.ingest_log("chat", "ann@box", position, 7, [])
    outbox_seq = baseline.outbox_add(
        "append_log", "chat|ann@box", {"id": "outbox-kept"})
    baseline.cache_doc("trust/peer.json", {"trusted": True})
    baseline.close()

    script = """
import os
import sys

from agentbridge.store.db import Store


def interrupted_records():
    yield {"id": "partial", "ns": 1, "from": "ann", "kind": "message"}
    os._exit(73)


store = Store(sys.argv[1])
position = store.capture_log_position("chat", "ann@box")
store.ingest_log("chat", "ann@box", position, 99, interrupted_records())
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 73, child.stderr

    reopened = Store(path)
    try:
        assert reopened.messages("chat") == []
        assert reopened.get_offset("chat", "ann@box") == 7
        assert reopened.cached_doc("trust/peer.json") == {"trusted": True}
        pending = reopened.outbox_claim_due()
        assert [(item.seq, item.payload) for item in pending] == [
            (outbox_seq, {"id": "outbox-kept"})
        ]
    finally:
        reopened.close()


def test_ingest_log_nested_begin_does_not_rollback_callers_transaction(store):
    conn = store._conn()
    position = store.capture_log_position("chat", "ann@box")
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
        ("caller/uncommitted.json", '{"kept":true}', 1),
    )

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        store.ingest_log(
            "chat", "ann@box", position, 1, [_record("not-ingested", 1)])

    assert conn.in_transaction
    assert conn.execute(
        "SELECT payload FROM docs WHERE path='caller/uncommitted.json'"
    ).fetchone() == ('{"kept":true}',)
    conn.commit()
    assert store.cached_doc("caller/uncommitted.json") == {"kept": True}
    assert store.message_count("chat") == 0


def test_sync_filtered_folder_batch_still_advances_offset(tmp_path, monkeypatch):
    tx = FolderTransport(tmp_path / "mesh")
    tx.append_log("chat", "ann@box", _record("spoofed", 1, sender="eve"))
    store = Store(tmp_path / "cache.sqlite")
    calls = 0
    original_read_log = tx.read_log

    def counted_read_log(chat_id, log_name, offset=0):
        nonlocal calls
        calls += 1
        return original_read_log(chat_id, log_name, offset)

    monkeypatch.setattr(tx, "read_log", counted_read_log)
    try:
        engine = SyncEngine(tx, store)
        assert engine.sync_chat("chat") == 0
        expected_size = dict(tx.list_logs("chat"))["ann@box"]
        assert store.get_offset("chat", "ann@box") == expected_size
        assert store.message_count("chat") == 0

        assert engine.sync_chat("chat") == 0
        assert calls == 1
    finally:
        store.close()


def test_sync_callbacks_and_telemetry_observe_committed_folder_ingest(
    tmp_path, monkeypatch,
):
    tx = FolderTransport(tmp_path / "mesh")
    tx.append_log("chat", "ann@box", _record("m1", 1))
    path = tmp_path / "cache.sqlite"
    store = Store(path)
    observer = Store(path)
    expected_size = dict(tx.list_logs("chat"))["ann@box"]
    seen: list[str] = []

    def assert_committed(label: str) -> None:
        assert observer.get_offset("chat", "ann@box") == expected_size
        assert [row["id"] for row in observer.messages("chat")] == ["m1"]
        seen.append(label)

    engine = SyncEngine(
        tx,
        store,
        on_chat_progress=lambda _chat, _count: assert_committed("chat-progress"),
        on_progress=lambda _count: assert_committed("progress"),
        on_records=lambda _chat, _records: assert_committed("records"),
    )

    def observe(stage, trace_ref, **_kwargs):
        assert stage == "sync_observed" and trace_ref == "m1"
        assert_committed("telemetry")

    monkeypatch.setattr(engine.latency, "observe", observe)
    try:
        assert engine.sync_chat("chat", lane="hint") == 1
        assert seen == ["chat-progress", "progress", "telemetry", "records"]
    finally:
        observer.close()
        store.close()


class _ConflictingFeed(Transport):
    """Feed whose log read races with a second Store connection."""

    scheme = "conflicting-feed"
    has_change_feed = True

    def __init__(self, competitor: Store) -> None:
        self.competitor = competitor

    def changed_logs(self, cursor):
        assert cursor == 4
        return [("chat", "ann@box")], 5

    def read_log(self, chat_id, log_name, offset=0):
        position = self.competitor.capture_log_position(chat_id, log_name)
        assert position.offset == offset
        self.competitor.ingest_log(chat_id, log_name, position, 1, [])
        return [_record("loser", 1)], 1

    def list_chat_ids(self):
        return ["chat"]

    def list_logs(self, chat_id):
        return [("ann@box", 1)]

    def get_doc(self, path, default=None):
        return default

    def put_doc(self, path, data):
        pass

    def delete_doc(self, path):
        pass

    def list_docs(self, prefix):
        return []

    def append_log(self, chat_id, log_name, record):
        raise NotImplementedError

    def delete_chat(self, chat_id):
        raise NotImplementedError

    def put_blob(self, path, data):
        raise NotImplementedError

    def put_blob_from(self, local_src: Path, path: str):
        raise NotImplementedError

    def get_blob(self, path):
        return None

    def blob_size(self, path):
        return None


def test_feed_cursor_and_callbacks_stay_put_on_ingestion_conflict(tmp_path):
    path = tmp_path / "cache.sqlite"
    store = Store(path)
    competitor = Store(path)
    callbacks: list[str] = []
    telemetry: list[str] = []
    try:
        store.cache_doc(CURSOR_DOC, {"cursor": 4})
        engine = SyncEngine(
            _ConflictingFeed(competitor),
            store,
            on_chat_progress=lambda *_args: callbacks.append("chat-progress"),
            on_progress=lambda *_args: callbacks.append("progress"),
            on_records=lambda *_args: callbacks.append("records"),
        )
        engine.latency.observe = lambda *_args, **_kwargs: telemetry.append("observe")
        engine._known = {"chat"}  # exercise the feed path, not startup catch-up

        assert engine.sync_once(lane="poll") == 0
        assert store.cached_doc(CURSOR_DOC) == {"cursor": 4}
        assert store.get_offset("chat", "ann@box") == 1
        assert store.message_count("chat") == 0
        assert callbacks == []
        assert telemetry == []
    finally:
        competitor.close()
        store.close()


def test_reset_during_transport_read_rejects_batch_then_fresh_retry_succeeds(tmp_path):
    class ResettingFeed(_ConflictingFeed):
        reset_on_read = True

        def read_log(self, chat_id, log_name, offset=0):
            assert offset == 0
            if self.reset_on_read:
                self.competitor.forget_chat(chat_id)
                self.reset_on_read = False
            return [_record("fresh", 1)], 1

    path = tmp_path / "cache.sqlite"
    store = Store(path)
    competitor = Store(path)
    callbacks = []
    try:
        store.cache_doc(CURSOR_DOC, {"cursor": 4})
        engine = SyncEngine(ResettingFeed(competitor), store,
                            on_records=lambda *_args: callbacks.append("records"))
        engine._known = {"chat"}
        assert engine.sync_once(lane="poll") == 0
        assert store.cached_doc(CURSOR_DOC) == {"cursor": 4}
        assert store.get_offset("chat", "ann@box") == 0
        assert store.message_count("chat") == 0
        assert callbacks == []

        assert engine.sync_once(lane="poll") == 1
        assert store.cached_doc(CURSOR_DOC) == {"cursor": 5}
        assert store.get_offset("chat", "ann@box") == 1
        assert [row["id"] for row in store.messages("chat")] == ["fresh"]
        assert callbacks == ["records"]
    finally:
        competitor.close()
        store.close()
