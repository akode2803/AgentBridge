"""Durable admission state for local raw inputs, never an authority verdict.

Inactive until transport mutation/ingestion owners are wired. Raw publication and
admission are separate commits: readers must require BOTH the admitted position
and the current raw position. Any interrupted transition remains unavailable.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import document_observation as docs

MAX = docs.MAX_SQLITE_INTEGER
MAX_WRITES = 64
_TABLES = {
    'local_source_schema': 'CREATE TABLE local_source_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),epoch TEXT NOT NULL)',
    'local_sources': 'CREATE TABLE local_sources(source TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(typeof(revision)=\'integer\' AND revision>=1),ready_incarnation TEXT,ready_generation INTEGER,ready_cursor INTEGER,last_success_ns INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT \'\')',
    'local_source_writes': 'CREATE TABLE local_source_writes(token TEXT PRIMARY KEY,source TEXT NOT NULL)',
}
_INDEX = 'CREATE INDEX local_source_writes_source ON local_source_writes(source)'


class SourceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class SourcePosition:
    raw: docs.DocumentPosition
    epoch: str
    revision: int
    ready: bool
    writes_pending: int


@dataclass(frozen=True)
class WriteIntent:
    database_path: str
    incarnation: str
    epoch: str
    source: str
    token: str


def source_id(root_identity, *, exact_paths=(), prefixes=(), build='phase1-v1'):
    """Bind immutable source coverage/build to its namespace, not permission.

    All publishers/readers must use this identifier. Expanding a demanded scope
    creates a different namespace; an earlier ready selection cannot silently
    stand for the expanded one. Root identity must come from the transport owner.
    """
    if (type(root_identity) is not str or not root_identity or len(root_identity) > 4096
            or type(build) is not str or not build or len(build) > 128):
        raise ValueError('invalid source definition')
    selections = []
    for values in (exact_paths, prefixes):
        if type(values) is not tuple or len(values) > 256:
            raise ValueError('invalid source selectors')
        copied = tuple(docs._validate_document_path(value) for value in values)
        selections.append(sorted(set(copied)))
    definition = json.dumps([root_identity, build, *selections],
                            ensure_ascii=False, separators=(',', ':')).encode()
    if len(definition) > 64 * 1024:
        raise ValueError('source definition too large')
    return 'local-inputs-v1:' + hashlib.sha256(definition).hexdigest()


def _expected(store, value):
    if type(value) is not SourcePosition:
        raise ValueError('expected local source position')
    raw, epoch, revision, ready, pending = (value.raw, value.epoch, value.revision,
                                           value.ready, value.writes_pending)
    raw = docs._validate_expected(raw, store.path)
    if (type(revision) is not int or not 0 <= revision <= MAX or type(ready) is not bool
            or type(pending) is not int or not 0 <= pending <= MAX_WRITES
            or type(epoch) is not str or len(epoch) != 32
            or any(c not in '0123456789abcdef' for c in epoch)):
        raise ValueError('invalid expected source position')
    return SourcePosition(raw, epoch, revision, ready, pending)


def _schema(conn):
    for name, statement in (*_TABLES.items(), ('local_source_writes_source', _INDEX)):
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (statement,):
            raise SourceChanged('local_source_schema_changed')
    row = conn.execute('SELECT version,epoch FROM local_source_schema WHERE singleton=1').fetchone()
    if row is None or row[0] != 1 or type(row[1]) is not str or len(row[1]) != 32:
        raise SourceChanged('local_source_epoch_missing')
    return row[1]


@contextmanager
def _writer(store):
    # NORMAL is sufficient for ordinary caches, not invalidate-before-external-
    # write ordering. FULL makes the WAL invalidation commit a durability barrier.
    conn = sqlite3.connect(store.path, timeout=5)
    try:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize(store):
    """Explicit off-path installation; never repair a partially missing schema."""
    with _writer(store) as conn:
        existing = conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'local_source*'").fetchall()
        if existing:
            _schema(conn)
            return
        for statement in _TABLES.values():
            conn.execute(statement)
        conn.execute(_INDEX)
        conn.execute('INSERT INTO local_source_schema VALUES(1,1,?)', (secrets.token_hex(16),))


def capture_in_transaction(conn, store, source):
    """Capture alongside messages/overlays on the caller's SQLite snapshot."""
    source = docs._validate_source_id(source)
    if not conn.in_transaction:
        raise ValueError('local source capture requires a transaction')
    databases = {row[1]: row[2] for row in conn.execute('PRAGMA database_list')}
    if not databases.get('main') or Path(databases['main']).resolve() != store.path.resolve():
        raise ValueError('wrong local source database')
    epoch = _schema(conn)
    raw = docs._capture_position(conn, store.path, source)
    row = conn.execute('SELECT revision,ready_incarnation,ready_generation,ready_cursor,last_success_ns,failures,error FROM local_sources WHERE source=?', (source,)).fetchone()
    pending = conn.execute('SELECT count(*) FROM (SELECT 1 FROM local_source_writes WHERE source=? LIMIT ?)', (source, MAX_WRITES + 1)).fetchone()[0]
    if pending > MAX_WRITES:
        raise SourceChanged('too_many_pending_writes')
    if row and (any(type(row[i]) is not int or not 0 <= row[i] <= MAX for i in (0, 4, 5))
                or row[0] == 0 or row[6] not in ('', 'io', 'incomplete', 'budget', 'conflict', 'unavailable')
                or ((row[1], row[2], row[3]) != (None, None, None) and
                    (type(row[1]) is not str or any(type(row[i]) is not int or not 0 <= row[i] <= MAX for i in (2, 3))))):
        raise SourceChanged('invalid_source_row')
    revision = row[0] if row else 0
    ready = bool(row and not pending and raw.initialized and (row[1], row[2], row[3]) == (raw.incarnation, raw.generation, raw.cursor))
    return SourcePosition(raw, epoch, revision, ready, pending)


def capture(store, source):
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        return capture_in_transaction(conn, store, source)
    finally:
        conn.close()


def _advance(conn, source, revision):
    if type(revision) is not int or not 0 <= revision < MAX:
        raise OverflowError('local source revision exhausted')
    conn.execute('INSERT INTO local_sources(source,revision) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET revision=excluded.revision,ready_incarnation=NULL,ready_generation=NULL,ready_cursor=NULL', (source, revision + 1))


def begin_write(store, source):
    """Commit invalidation before caller attempts ANY external mutation.

    A failed/ambiguous write must retain this intent. No timeout clears it.
    Recovery requires a separate, explicit writer-quiescence/reconciliation owner.
    """
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, source)
        if current.writes_pending >= MAX_WRITES:
            raise SourceChanged('too_many_pending_writes')
        _advance(conn, source, current.revision)
        token = secrets.token_hex(32)
        conn.execute('INSERT INTO local_source_writes VALUES(?,?)', (token, source))
        result = WriteIntent(str(store.path), current.raw.incarnation, current.epoch, source, token)
    return result


def complete_write(store, intent):
    """Only after definite external success; keep pending until fresh admission.

    Replayed/stale completion is rejected. Completion does not restore readiness,
    and a publisher that scanned during the write is invalidated again.
    """
    if type(intent) is not WriteIntent or intent.database_path != str(store.path):
        raise ValueError('wrong write intent')
    intent = WriteIntent(intent.database_path, intent.incarnation, intent.epoch, intent.source, intent.token)
    docs._validate_source_id(intent.source)
    if (type(intent.token) is not str or len(intent.token) != 64
            or type(intent.incarnation) is not str or type(intent.epoch) is not str):
        raise ValueError('invalid write intent')
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, intent.source)
        if current.epoch != intent.epoch or current.raw.incarnation != intent.incarnation:
            raise SourceChanged('write_owner_changed')
        if conn.execute('DELETE FROM local_source_writes WHERE token=? AND source=?', (intent.token, intent.source)).rowcount != 1:
            raise SourceChanged('write_intent_missing')
        _advance(conn, intent.source, current.revision)


def invalidate(store, source):
    """Retire readiness for observed external changes or failed ingestion."""
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, source)
        _advance(conn, source, current.revision)


def admit(store, expected, published, *, observed_ns):
    """Admit a complete raw batch, only if no mutation crossed its scan.

    `expected` MUST be captured before source collection. `published` is the
    result of a complete raw publication from exactly expected.raw. No provider
    cursor, timestamp, or ready flag establishes membership.
    """
    if type(expected) is not SourcePosition or type(published) is not docs.DocumentPosition:
        raise ValueError('invalid source admission')
    expected = _expected(store, expected)
    published = docs._validate_expected(published, store.path)
    if type(observed_ns) is not int or not 0 <= observed_ns <= MAX:
        raise ValueError('invalid observation time')
    if (published.source_id != expected.raw.source_id or published.incarnation != expected.raw.incarnation
            or published.generation != expected.raw.generation + 1 or not published.initialized):
        raise SourceChanged('publication_not_successor')
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, published.source_id)
        if (current.epoch != expected.epoch or current.revision != expected.revision
                or current.writes_pending or expected.writes_pending or current.raw != published):
            raise SourceChanged('source_changed_during_ingestion')
        _advance(conn, published.source_id, current.revision)
        conn.execute('UPDATE local_sources SET ready_incarnation=?,ready_generation=?,ready_cursor=?,last_success_ns=?,failures=0,error=\'\' WHERE source=?', (published.incarnation, published.generation, published.cursor, observed_ns, published.source_id))
    return SourcePosition(published, current.epoch, current.revision + 1, True, 0)


def record_failure(store, source, *, reason):
    """Bounded diagnostic codes only; retire readiness, preserve last success."""
    if reason not in ('io', 'incomplete', 'budget', 'conflict', 'unavailable'):
        raise ValueError('invalid source health reason')
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, source)
        _advance(conn, source, current.revision)
        conn.execute('UPDATE local_sources SET failures=min(failures+1,?),error=? WHERE source=?', (MAX, reason, source))


def health(store, source):
    source = docs._validate_source_id(source)
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        position = capture_in_transaction(conn, store, source)
        row = conn.execute('SELECT last_success_ns,failures,error FROM local_sources WHERE source=?', (source,)).fetchone() or (0, 0, '')
        return dict(ready=position.ready, writes_pending=position.writes_pending,
                    last_success_ns=row[0], failures=row[1], error=row[2])
    finally:
        conn.close()


def publish(store, expected, documents, *, observed_ns, max_documents=20_000,
            max_bytes=16 * 1024 * 1024):
    """Publish a complete admitted selection; interrupted commits stay pending.

    Collection/readiness evidence is the transport ingestion owner's job. This
    primitive always replaces the whole declared source, never assumes a delta
    proves namespace completeness. Source selectors are fixed by that owner.
    """
    expected = _expected(store, expected)
    # Retire the old ready generation durably before any raw publication work.
    # CAS losers do not own a publication attempt and cannot retire a winner.
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, expected.raw.source_id)
        if expected.writes_pending or current != expected:
            raise SourceChanged('source_changed_before_publication')
        _advance(conn, expected.raw.source_id, current.revision)
        retired = SourcePosition(current.raw, current.epoch, current.revision + 1, False, 0)
    published = store.publish_document_batch(retired.raw, documents,
        cursor=retired.raw.cursor, full=True, retain_tombstones=False,
        max_documents=max_documents, max_bytes=max_bytes)
    return admit(store, retired, published, observed_ns=observed_ns)
