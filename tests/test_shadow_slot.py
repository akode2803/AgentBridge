"""Diagnostic slot fencing, bounded replacement, and transactional failure tests."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from agentbridge.store import shadow_slot as slot
from agentbridge.store.db import Store
from agentbridge.store.shadow_slot import (
    MAX_SQLITE_INTEGER, ShadowConflict, ShadowSnapshot, ShadowSource,
)
from agentbridge.transport.base import Transport, Watcher
from agentbridge.transport.cache import CachingTransport

SOURCE = ShadowSource("root", "cache", "mirror-one")


def snapshot(revision=1, records=(("a.json", '{"value":1}'),)):
    return ShadowSnapshot(SOURCE, revision, 42, "bootstrap_unverified", ("chat",),
                          records)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store.sqlite")
    yield s
    s.close()


def own(store):
    return store.acquire_shadow(store.inspect_shadow_position(), "publisher", SOURCE)


class IdentityMemoryTransport(Transport):
    """Small identity-capable provider for exercising real mirror captures."""

    scheme = "memory"

    def __init__(self):
        self.root = "shadow-root"
        self.cache_key = "shadow-cache"
        self.docs = {}

    def snapshot_docs(self):
        return dict(self.docs), 7

    def get_doc(self, path, default=None):
        return self.docs.get(path, default)

    def put_doc(self, path, data):
        self.docs[path] = data

    def delete_doc(self, path):
        self.docs.pop(path, None)

    def list_docs(self, prefix):
        return [path for path in self.docs if path.startswith(prefix)]

    def list_chat_ids(self):
        return []

    def list_logs(self, chat_id):
        return []

    def append_log(self, chat_id, log_name, record):
        pass

    def read_log(self, chat_id, log_name, offset=0):
        return [], offset

    def delete_chat(self, chat_id):
        pass

    def put_blob(self, path, data):
        pass

    def put_blob_from(self, local_src, path):
        pass

    def get_blob(self, path):
        return None

    def blob_size(self, path):
        return None

    def watch(self):
        return Watcher()


def test_complete_lifecycle_idempotence_and_fencing(store):
    empty = store.inspect_shadow_position()
    first = own(store)
    assert first.epoch == 1 and not first.initialized
    assert store.capture_shadow(first).snapshot is None
    published = store.publish_shadow(first, snapshot())
    assert store.capture_shadow(published).snapshot == snapshot()
    assert store.publish_shadow(published, snapshot()) == published
    for token in (empty, first):
        with pytest.raises(ShadowConflict):
            store.capture_shadow(token)
    replacement = store.acquire_shadow(published, "new-publisher", SOURCE)
    assert replacement.epoch == 2 and not replacement.initialized
    for operation in (lambda: store.publish_shadow(published, snapshot(2)),
                      lambda: store.retire_shadow(published),
                      lambda: store.acquire_shadow(published, "old", SOURCE)):
        with pytest.raises(ShadowConflict):
            operation()
    retired = store.retire_shadow(replacement)
    assert retired.epoch == 3 and retired.publisher_nonce is None
    again = store.acquire_shadow(retired, "publisher", SOURCE)
    assert again.epoch == 4
    with pytest.raises(ShadowConflict):
        store.publish_shadow(first, snapshot())


def test_revision_digest_covers_all_metadata_and_serialized_content(store):
    current = store.publish_shadow(own(store), snapshot(5))
    for changed in (snapshot(4), replace(snapshot(5), provider_cursor=43),
                    replace(snapshot(5), provenance="provider_observed"),
                    replace(snapshot(5), chat_ids=("different",)),
                    snapshot(5, (("a.json", '{"value": 1}'),)),
                    replace(snapshot(5), source=replace(SOURCE, mirror_nonce="other"))):
        with pytest.raises(ShadowConflict):
            store.publish_shadow(current, changed)
    assert store.capture_shadow(current).snapshot == snapshot(5)
    later = store.publish_shadow(current, replace(snapshot(6), provider_cursor=1))
    assert later.generation == current.generation + 1  # Provider cursor is not ordering.


def test_competing_acquisitions_have_one_winner(tmp_path):
    path = tmp_path / "race.sqlite"
    seed = Store(path)
    expected = seed.inspect_shadow_position()
    seed.close()
    barrier = Barrier(2)

    def compete(nonce):
        s = Store(path)
        try:
            barrier.wait(timeout=5)
            try:
                return s.acquire_shadow(expected, nonce, SOURCE)
            except ShadowConflict:
                return None
        finally:
            s.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(compete, ("one", "two")))
    assert sum(r is not None for r in results) == 1


def test_replacement_churn_and_generic_namespace_are_isolated(store):
    store.upsert_messages("chat", [{"id": "keep", "ns": 1}])
    generic = store.publish_document_batch(store.capture_document_position("diagnostic-mirror/v1"),
                                           {"keep": 1}, cursor=1, full=True)
    position = own(store)
    for revision in range(1, 1001):
        position = store.publish_shadow(position, snapshot(revision, ((f"doc/{revision}", "null"),)))
    assert store._conn().execute("SELECT count(*) FROM diagnostic_shadow_records").fetchone() == (1,)
    assert store._conn().execute("SELECT count(*) FROM diagnostic_shadow_slot").fetchone() == (1,)
    assert store.capture_shadow(position).snapshot.records == (("doc/1000", "null"),)
    store.reset_document_observation(generic)
    assert store.capture_shadow(position).snapshot.records == (("doc/1000", "null"),)
    store.retire_shadow(position)
    assert store._conn().execute("SELECT count(*) FROM messages").fetchone() == (1,)
    assert store._conn().execute("SELECT count(*) FROM diagnostic_shadow_records").fetchone() == (0,)


@pytest.mark.parametrize("operation", ["publish", "acquire", "retire"])
def test_sql_failure_rolls_back_deleted_records_and_owner(store, operation):
    current = store.publish_shadow(own(store), snapshot())
    # Fail after the record deletion but before owner metadata is committed.
    trigger_event = "INSERT" if operation == "acquire" else "UPDATE"
    store._conn().execute(f"CREATE TRIGGER fail_shadow BEFORE {trigger_event} ON diagnostic_shadow_slot "
                          "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        if operation == "publish":
            store.publish_shadow(current, snapshot(2, (("different", "0"),)))
        elif operation == "acquire":
            store.acquire_shadow(current, "other", SOURCE)
        else:
            store.retire_shadow(current)
    assert not store._conn().in_transaction
    assert store.capture_shadow(current).snapshot == snapshot()


def test_pending_caller_transaction_and_reader_isolation(store):
    current = store.publish_shadow(own(store), snapshot())
    store._conn().execute("DELETE FROM diagnostic_shadow_records")
    assert store.capture_shadow(current).snapshot == snapshot()
    for action in (lambda: store.publish_shadow(current, snapshot(2)),
                   lambda: store.acquire_shadow(current, "other", SOURCE),
                   lambda: store.retire_shadow(current)):
        with pytest.raises(sqlite3.OperationalError, match="caller transaction"):
            action()
    assert store._conn().in_transaction
    store._conn().rollback()


def test_reader_keeps_complete_snapshot_during_owner_takeover(tmp_path, monkeypatch):
    path = tmp_path / "takeover.sqlite"
    reader = Store(path)
    writer = Store(path)
    first = reader.publish_shadow(own(reader), snapshot())
    original_match = slot._match
    takeover_done = False

    def take_over_after_position_read(conn, database_path, expected):
        nonlocal takeover_done
        result = original_match(conn, database_path, expected)
        if not takeover_done:
            takeover_done = True
            monkeypatch.setattr(slot, "_match", original_match)
            replacement = writer.acquire_shadow(first, "replacement", SOURCE)
            writer.publish_shadow(replacement, snapshot(2, (("replacement.json", "99"),)))
        return result

    monkeypatch.setattr(slot, "_match", take_over_after_position_read)
    try:
        observed = reader.capture_shadow(first)
        assert observed.position == first
        assert observed.snapshot == snapshot()
        with pytest.raises(ShadowConflict):
            reader.capture_shadow(first)
    finally:
        writer.close()
        reader.close()


def test_sqlite_busy_preserves_position_and_closes_failed_transaction(tmp_path):
    path = tmp_path / "busy.sqlite"
    store = Store(path)
    locker = Store(path)
    current = store.publish_shadow(own(store), snapshot())
    store._conn().execute("PRAGMA busy_timeout=1")
    locker._conn().execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            store.retire_shadow(current)
        assert raised.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert not store._conn().in_transaction
    finally:
        locker._conn().rollback()
        locker.close()
    assert store.inspect_shadow_position() == current
    assert store.capture_shadow(current).snapshot == snapshot()
    store.close()


def test_database_incarnation_and_restart_bootstrap_fencing(store, tmp_path):
    old = store.publish_shadow(own(store), snapshot())
    other = Store(tmp_path / "other.sqlite")
    try:
        with pytest.raises(ShadowConflict):
            other.acquire_shadow(old, "new", SOURCE)
        forged_path = replace(old, database_path=str(other.path))
        with pytest.raises(ShadowConflict):
            other.acquire_shadow(forged_path, "new", SOURCE)
    finally:
        other.close()
    store.close()
    reopened = Store(store.path)
    try:
        new = reopened.acquire_shadow(old, "new-session", replace(SOURCE, mirror_nonce="new-mirror"))
        with pytest.raises(ShadowConflict):
            reopened.publish_shadow(old, snapshot())
        new_snapshot = replace(snapshot(), source=new.source)
        latest = reopened.publish_shadow(new, new_snapshot)
        assert reopened.capture_shadow(latest).snapshot.provenance == "bootstrap_unverified"
    finally:
        reopened.close()


def test_same_path_database_replacement_rejects_old_position(tmp_path):
    path = tmp_path / "viewer.sqlite"
    original = Store(path)
    old = original.publish_shadow(own(original), snapshot())
    original.close()

    replacement_path = tmp_path / "replacement.sqlite"
    replacement = Store(replacement_path)
    replacement._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement.close()
    replacement_path.replace(path)

    reopened = Store(path)
    try:
        assert reopened.inspect_shadow_position().incarnation != old.incarnation
        with pytest.raises(ShadowConflict):
            reopened.acquire_shadow(old, "new-owner", SOURCE)
    finally:
        reopened.close()


def _legacy_digest(conn):
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'diagnostic_shadow_%' ORDER BY name"
        )
    ]
    content = [
        (table, conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall())
        for table in tables
    ]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def test_schema_install_and_lifecycle_preserve_seeded_legacy_tables(tmp_path):
    path = tmp_path / "migration.sqlite"
    legacy = Store(path)
    conn = legacy._conn()
    conn.execute("DROP TABLE diagnostic_shadow_records")
    conn.execute("DROP TABLE diagnostic_shadow_slot")
    conn.execute("INSERT INTO messages(chat_id,id,ns,payload) VALUES('c','m',1,'{}')")
    conn.execute("INSERT INTO docs VALUES('journal','{}',1)")
    conn.execute("INSERT INTO outbox(kind,payload,created_ns) VALUES('send','{}',1)")
    conn.commit()
    before = _legacy_digest(conn)
    legacy.close()

    migrated = Store(path)
    try:
        assert _legacy_digest(migrated._conn()) == before
        position = migrated.acquire_shadow(
            migrated.inspect_shadow_position(), "publisher", SOURCE
        )
        position = migrated.publish_shadow(position, snapshot())
        migrated.retire_shadow(position)
        assert _legacy_digest(migrated._conn()) == before
    finally:
        migrated.close()


def test_owner_acquisition_and_retirement_churn_stays_bounded(store):
    position = store.inspect_shadow_position()
    for sequence in range(1_000):
        position = store.acquire_shadow(position, f"publisher-{sequence}", SOURCE)
        position = store.retire_shadow(position)
    assert position.epoch == 2_000
    assert store._conn().execute(
        "SELECT count(*) FROM diagnostic_shadow_slot"
    ).fetchone() == (1,)
    assert store._conn().execute(
        "SELECT count(*) FROM diagnostic_shadow_records"
    ).fetchone() == (0,)


def test_caching_transport_capture_roundtrips_through_shadow_slot(store):
    provider = IdentityMemoryTransport()
    provider.put_doc("some/doc.json", {"value": [None, 1, "hello"]})
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    captured = mirror.capture_mirror()
    converted = ShadowSnapshot(
        ShadowSource(
            captured.root_identity,
            captured.cache_identity,
            captured.instance_nonce,
        ),
        captured.revision,
        captured.provider_cursor,
        captured.provenance,
        captured.chat_ids,
        tuple((record.path, record.payload_json) for record in captured.records),
    )
    position = store.acquire_shadow(
        store.inspect_shadow_position(), "publisher", converted.source
    )
    position = store.publish_shadow(position, converted)
    assert store.capture_shadow(position).snapshot == converted


@pytest.mark.parametrize("column", ["epoch", "generation"])
def test_counter_exhaustion_preserves_existing_snapshot(store, column):
    store.publish_shadow(own(store), snapshot())
    store._conn().execute(f"UPDATE diagnostic_shadow_slot SET {column}=?", (MAX_SQLITE_INTEGER,))
    store._conn().commit()
    current = store.inspect_shadow_position()
    for action in (lambda: store.acquire_shadow(current, "other", SOURCE),
                   lambda: store.retire_shadow(current)):
        with pytest.raises(OverflowError):
            action()
    with pytest.raises(OverflowError):
        store.publish_shadow(current, snapshot(2))
    assert store.capture_shadow(current).snapshot == snapshot()


def test_budgets_and_malformed_input_reject_before_writer_transaction(store):
    current = own(store)
    statements = []
    store._conn().set_trace_callback(statements.append)
    bad = [replace(snapshot(), chat_ids=("chat", "chat")),
           replace(snapshot(), records=(("a", "NaN"),)),
           replace(snapshot(), records=(("../a", "0"),)),
           replace(snapshot(), records=(("a", "0"), ("a", "1"))),
           replace(snapshot(), revision=True),
           replace(snapshot(), source=ShadowSource(None, "cache", "nonce"))]
    for data in bad:
        with pytest.raises(ValueError):
            store.publish_shadow(current, data)
    for budget in ({"max_documents": 0}, {"max_chat_ids": 0}, {"max_bytes": 0}):
        with pytest.raises(OverflowError):
            store.publish_shadow(current, snapshot(), **budget)
    assert not any("BEGIN" in statement for statement in statements)
    store._conn().set_trace_callback(None)
    published = store.publish_shadow(current, snapshot())
    for budget in ({"max_documents": 0}, {"max_chat_ids": 0}, {"max_bytes": 0}):
        with pytest.raises(OverflowError):
            store.capture_shadow(published, **budget)


@pytest.mark.parametrize("operation", ["publish", "acquire", "retire"])
def test_process_death_during_replacement_rolls_back(store, operation):
    current = store.publish_shadow(own(store), snapshot())
    script = '''
import os, sqlite3, sys
from agentbridge.store.db import Store
from agentbridge.store.shadow_slot import ShadowSnapshot, ShadowSource
class Crash(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        result = super().execute(sql, *args, **kwargs)
        if sql == "DELETE FROM diagnostic_shadow_records":
            os._exit(77)
        return result
s = Store(sys.argv[1])
s.close()
s._local.conn = sqlite3.connect(s.path, factory=Crash)
p = s.inspect_shadow_position()
if sys.argv[2] == "publish":
    s.publish_shadow(p, ShadowSnapshot(p.source, 2, 0, "provider_observed", (), ()))
elif sys.argv[2] == "acquire":
    s.acquire_shadow(p, "replacement", ShadowSource("r", "c", "n"))
else:
    s.retire_shadow(p)
'''
    result = subprocess.run([sys.executable, "-c", script, str(store.path), operation],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 77, result.stderr
    assert store.capture_shadow(current).snapshot == snapshot()
    assert store._conn().execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_token_tampering_and_subclasses_never_enter_writer_transaction(store):
    class Hook(str):
        def __eq__(self, other):
            raise AssertionError("caller equality")

    current = own(store)
    tampered = replace(current)
    object.__setattr__(tampered, "publisher_nonce", Hook("publisher"))
    statements = []
    store._conn().set_trace_callback(statements.append)
    with pytest.raises(ValueError):
        store.retire_shadow(tampered)
    assert not any("BEGIN" in statement for statement in statements)
    store._conn().set_trace_callback(None)
