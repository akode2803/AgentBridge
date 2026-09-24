"""Durable admission state for local raw inputs, never an authority verdict.

Inactive until transport mutation/ingestion owners are wired. Raw publication and
admission are separate commits: readers must require BOTH the admitted position
and the current raw position. Any interrupted transition remains unavailable.
"""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import document_observation as docs

MAX = docs.MAX_SQLITE_INTEGER
MAX_WRITES = 64
_V1_TABLES = {
    'local_source_schema': 'CREATE TABLE local_source_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),epoch TEXT NOT NULL)',
    'local_sources': 'CREATE TABLE local_sources(source TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(typeof(revision)=\'integer\' AND revision>=1),ready_incarnation TEXT,ready_generation INTEGER,ready_cursor INTEGER,last_success_ns INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT \'\')',
    'local_source_writes': 'CREATE TABLE local_source_writes(token TEXT PRIMARY KEY,source TEXT NOT NULL)',
}
_TABLES = dict(_V1_TABLES)
_TABLES['local_source_schema'] = 'CREATE TABLE local_source_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=2),epoch TEXT NOT NULL)'
_MAPPING = 'CREATE TABLE local_input_generations(source TEXT PRIMARY KEY,physical TEXT NOT NULL UNIQUE)'
_INDEX = 'CREATE INDEX local_source_writes_source ON local_source_writes(source)'


def _mapping_trigger(event, refs):
    actions = []
    for ref, condition in refs:
        actions.append(
            'SELECT CASE WHEN ' + condition + ' AND EXISTS(SELECT 1 FROM local_sources '
            'WHERE source=' + ref + '.source AND revision>=9223372036854775807) '
            "THEN RAISE(ABORT,'local source revision exhausted') END; "
            'UPDATE local_sources SET revision=revision+1,ready_incarnation=NULL,'
            'ready_generation=NULL,ready_cursor=NULL WHERE source=' + ref + '.source AND '
            + condition + ';'
        )
    return ('CREATE TRIGGER local_input_generation_dirty_' + event.lower() + ' AFTER '
            + event + ' ON local_input_generations BEGIN ' + ' '.join(actions) + ' END')


_MAPPING_TRIGGERS = {
    'INSERT': _mapping_trigger('INSERT', (('NEW', '1'),)),
    'DELETE': _mapping_trigger('DELETE', (('OLD', '1'),)),
    'UPDATE': _mapping_trigger('UPDATE', (('OLD', '1'), ('NEW', 'NEW.source<>OLD.source'))),
}


class SourceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class SourcePosition:
    raw: docs.DocumentPosition
    epoch: str
    revision: int
    ready: bool
    writes_pending: int
    logical_source: str | None = None

    @property
    def source_id(self):
        return self.logical_source or self.raw.source_id


@dataclass(frozen=True)
class WriteIntent:
    database_path: str
    incarnation: str
    epoch: str
    source: str
    token: str


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
    logical = value.logical_source
    if logical is not None:
        logical = docs._validate_source_id(logical)
    return SourcePosition(raw, epoch, revision, ready, pending, logical)


def _schema(conn, *, legacy=False):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?,?) LIMIT 1", ('local_source_schema', 'local_sources', 'local_source_writes')).fetchone():
        raise SourceChanged('unexpected_source_trigger')
    for name, statement in (*(_V1_TABLES if legacy else _TABLES).items(), ('local_source_writes_source', _INDEX)):
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (statement,):
            raise SourceChanged('local_source_schema_changed')
    actual = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
        "(name GLOB 'local_input_generation_*' OR tbl_name='local_input_generations') LIMIT 4"
    ).fetchall())
    if legacy:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_input_generations' LIMIT 1").fetchone() or actual:
            raise SourceChanged('local_generation_schema_changed')
    else:
        if conn.execute("SELECT sql FROM sqlite_master WHERE name='local_input_generations'").fetchone() != (_MAPPING,) or actual != {'local_input_generation_dirty_' + event.lower(): sql for event, sql in _MAPPING_TRIGGERS.items()}:
            raise SourceChanged('local_generation_schema_changed')
    row = conn.execute('SELECT version,epoch FROM local_source_schema WHERE singleton=1').fetchone()
    if row is None or type(row[0]) is not int or row[0] != (1 if legacy else 2) or type(row[1]) is not str or len(row[1]) != 32 or any(c not in '0123456789abcdef' for c in row[1]):
        raise SourceChanged('local_source_epoch_missing')
    return row[1]


@contextmanager
def _writer(store):
    # NORMAL is sufficient for ordinary caches, not invalidate-before-external-
    # write ordering. FULL makes the WAL invalidation commit a durability barrier.
    conn = sqlite3.connect(f"{store.path.as_uri()}?mode=rw", uri=True, timeout=5)
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
            version = conn.execute('SELECT version FROM local_source_schema WHERE singleton=1').fetchone()
            if version == (1,):
                epoch = _schema(conn, legacy=True)
                conn.execute('DROP TABLE local_source_schema')
                conn.execute(_TABLES['local_source_schema'])
                conn.execute('INSERT INTO local_source_schema VALUES(1,2,?)', (epoch,))
                conn.execute(_MAPPING)
                conn.execute('INSERT INTO local_input_generations(source,physical) SELECT source,source FROM local_sources')
                for statement in _MAPPING_TRIGGERS.values():
                    conn.execute(statement)
            _schema(conn)
            return
        for statement in _TABLES.values():
            conn.execute(statement)
        conn.execute(_INDEX)
        conn.execute(_MAPPING)
        for statement in _MAPPING_TRIGGERS.values():
            conn.execute(statement)
        conn.execute('INSERT INTO local_source_schema VALUES(1,2,?)', (secrets.token_hex(16),))


def capture_in_transaction(conn, store, source):
    """Capture alongside messages/overlays on the caller's SQLite snapshot."""
    source = docs._validate_source_id(source)
    if not conn.in_transaction:
        raise ValueError('local source capture requires a transaction')
    databases = {row[1]: row[2] for row in conn.execute('PRAGMA database_list')}
    if not databases.get('main') or Path(databases['main']).resolve() != store.path.resolve():
        raise ValueError('wrong local source database')
    epoch = _schema(conn)
    size = conn.execute('SELECT typeof(physical),length(CAST(physical AS BLOB)) FROM local_input_generations WHERE source=?', (source,)).fetchone()
    if size is not None and (size[0] != 'text' or type(size[1]) is not int or not 1 <= size[1] <= docs.MAX_SOURCE_ID_BYTES):
        raise SourceChanged('invalid_generation_mapping')
    selected = conn.execute('SELECT physical FROM local_input_generations WHERE source=?', (source,)).fetchone() if size is not None else None
    physical = docs._validate_source_id(selected[0]) if selected else source
    raw = docs._capture_position(conn, store.path, physical)
    row = conn.execute('SELECT revision,ready_incarnation,ready_generation,ready_cursor,last_success_ns,failures,error FROM local_sources WHERE source=?', (source,)).fetchone()
    if bool(row) != bool(selected):
        raise SourceChanged('missing_generation_mapping')
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
    return SourcePosition(raw, epoch, revision, ready, pending, source if physical != source else None)


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
    # New logical rows always receive an explicit self mapping. Existing rows
    # must already have one; never silently repair a deleted admission pointer.
    if revision == 0:
        conn.execute('INSERT INTO local_input_generations(source,physical) VALUES(?,?)', (source, source))
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


def admit(store, expected, published, *, observed_ns, allow_unchanged=False):
    """Admit a complete raw batch, only if no mutation crossed its scan.

    `expected` MUST be captured before source collection. `published` is the
    result of a complete raw publication from exactly expected.raw. No provider
    cursor, timestamp, or ready flag establishes membership. The unchanged
    option is reserved for SourcePublisher after exact complete raw comparison;
    it does not bypass owner revision or pending-write checks.
    """
    if type(allow_unchanged) is not bool:
        raise ValueError('allow_unchanged must be a bool')
    if type(expected) is not SourcePosition or type(published) is not docs.DocumentPosition:
        raise ValueError('invalid source admission')
    expected = _expected(store, expected)
    published = docs._validate_expected(published, store.path)
    if type(observed_ns) is not int or not 0 <= observed_ns <= MAX:
        raise ValueError('invalid observation time')
    successor = published.generation == expected.raw.generation + 1
    unchanged = allow_unchanged and published == expected.raw
    if (published.source_id != expected.raw.source_id or published.incarnation != expected.raw.incarnation
            or not (successor or unchanged) or not published.initialized):
        raise SourceChanged('publication_not_successor')
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, expected.source_id)
        if (current.epoch != expected.epoch or current.revision != expected.revision
                or current.writes_pending or expected.writes_pending or current.raw != published):
            raise SourceChanged('source_changed_during_ingestion')
        _advance(conn, expected.source_id, current.revision)
        conn.execute('UPDATE local_sources SET ready_incarnation=?,ready_generation=?,ready_cursor=?,last_success_ns=?,failures=0,error=\'\' WHERE source=?', (published.incarnation, published.generation, published.cursor, observed_ns, expected.source_id))
    return SourcePosition(published, current.epoch, current.revision + 1, True, 0, current.logical_source)


def record_failure(store, source, *, reason, expected=None):
    """Bounded diagnostic codes only; retire readiness, preserve last success."""
    if expected is not None:
        expected = _expected(store, expected)
        if expected.source_id != source:
            raise ValueError('failure source mismatch')
    if reason not in ('io', 'incomplete', 'budget', 'conflict', 'unavailable'):
        raise ValueError('invalid source health reason')
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, source)
        if expected is not None and current != expected:
            return False
        _advance(conn, source, current.revision)
        conn.execute('UPDATE local_sources SET failures=min(failures+1,?),error=? WHERE source=?', (MAX, reason, source))
        return True


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


def retire_for_publication(store, expected):
    """Durably retire this exact scan before raw encoding/publication can fail.

    The root-scoped owner must also hold its publication gate when used across
    registered Stores. No transport reads or payload work belongs in this step.
    """
    expected = _expected(store, expected)
    # Retire the old ready generation durably before any raw publication work.
    # CAS losers do not own a publication attempt and cannot retire a winner.
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, expected.source_id)
        if expected.writes_pending or current != expected:
            raise SourceChanged('source_changed_before_publication')
        _advance(conn, expected.source_id, current.revision)
        retired = SourcePosition(current.raw, current.epoch, current.revision + 1, False, 0, current.logical_source)
    return retired


def claim_collection(store, expected):
    """Fence a new staged attempt without retiring the last admitted snapshot.

    Caller holds the root publication gate. Advancing the owner revision lets
    a new attempt abandon an interrupted candidate, including one from a prior
    process, without guessing whether a PID or timeout proves it dead. A live
    superseded builder also loses its exact revision CAS and cannot publish or
    retire this claim. Readiness and its original observation time are preserved
    atomically; no pending write can be cleared or admitted here.
    """
    expected = _expected(store, expected)
    with _writer(store) as conn:
        current = capture_in_transaction(conn, store, expected.source_id)
        if current != expected or current.writes_pending:
            raise SourceChanged('source_changed_before_collection')
        _advance(conn, expected.source_id, current.revision)
        if current.ready:
            conn.execute('UPDATE local_sources SET ready_incarnation=?,ready_generation=?,'
                         'ready_cursor=? WHERE source=?',
                         (current.raw.incarnation, current.raw.generation,
                          current.raw.cursor, expected.source_id))
        claimed = capture_in_transaction(conn, store, expected.source_id)
        if claimed.raw != current.raw or claimed.ready != current.ready:
            raise SourceChanged('collection_claim_changed_inputs')
    return claimed


def publish(store, expected, documents, *, observed_ns, max_documents=20_000,
            max_bytes=16 * 1024 * 1024):
    """Publish a complete admitted selection; interrupted commits stay pending.

    Collection/readiness evidence is the transport ingestion owner's job. This
    primitive always replaces the whole declared source, never assumes a delta
    proves namespace completeness. Source selectors are fixed by that owner.
    """
    expected = _expected(store, expected)
    if expected.raw.source_id.startswith('stage:'):
        raise SourceChanged('staged_source_requires_staged_publication')
    retired = retire_for_publication(store, expected)
    published = store.publish_document_batch(retired.raw, documents,
        cursor=retired.raw.cursor, full=True, retain_tombstones=False,
        max_documents=max_documents, max_bytes=max_bytes)
    return admit(store, retired, published, observed_ns=observed_ns)
