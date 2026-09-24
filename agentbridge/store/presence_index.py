"""Bounded exact-member receipt floors derived from complete staged raw inputs.

Presence is unsigned legacy input, not a membership, privacy or delivery proof.
This index only preserves the existing max(last_seen_ns) across payload users.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass

from . import document_observation as docs, local_source, staged_source

MAX_MEMBERS = 64
_TABLES = {
    'presence_index_rows': 'CREATE TABLE presence_index_rows(source TEXT NOT NULL,user TEXT NOT NULL,ns NUMERIC NOT NULL CHECK(typeof(ns) IN (\'integer\',\'real\')),PRIMARY KEY(source,user))',
    'presence_index_ready': 'CREATE TABLE presence_index_ready(source TEXT PRIMARY KEY,incarnation TEXT NOT NULL,generation INTEGER NOT NULL,cursor INTEGER NOT NULL,build TEXT NOT NULL)',
    'presence_index_builds': 'CREATE TABLE presence_index_builds(source TEXT PRIMARY KEY,revision INTEGER NOT NULL CHECK(typeof(revision)=\'integer\' AND revision>=0))',
}
_TRIGGERS = {
    f'presence_index_dirty_{event.lower()}':
    f'CREATE TRIGGER presence_index_dirty_{event.lower()} AFTER {event} ON presence_index_rows BEGIN '
    + ' '.join(f'DELETE FROM presence_index_ready WHERE source={ref}.source; '
               f'UPDATE presence_index_builds SET revision=revision+1 WHERE source={ref}.source'
               + (' AND OLD.source IS NOT NEW.source;' if event == 'UPDATE' and ref == 'OLD' else ';')
               for ref in refs) + ' END'
    for event, refs in [('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW'))]
}


class PresenceIndexUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PresenceInputs:
    position: docs.DocumentPosition
    build: str
    floors: tuple[tuple[str, int | float], ...]
    observed_ns: int = 0


def initialize(store):
    with local_source._writer(store) as conn:
        for name, sql in _TABLES.items():
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
                conn.execute(sql)
        for name, sql in _TRIGGERS.items():
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
                conn.execute(sql)
        _schema(conn)


def _schema(conn):
    for name, sql in _TABLES.items():
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
            raise PresenceIndexUnavailable('presence_schema_unavailable')
    actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
                              "(name GLOB 'presence_index_dirty_*' OR tbl_name IN "
                              "('presence_index_rows','presence_index_ready','presence_index_builds')) LIMIT 4"))
    if actual != _TRIGGERS:
        raise PresenceIndexUnavailable('presence_triggers_unavailable')


def _user(user):
    if type(user) is not str or not user or len(user.encode()) > 256 or '\x00' in user:
        raise ValueError('invalid presence subject')
    return user


def _floor(value):
    # Preserve integer/float comparisons; never parse a string into a cursor
    # that the legacy PresenceRecord would fail to compare. Normal writers use
    # integer ns. Values outside SQLite's representable range fail explicitly.
    if (type(value) not in (int, float, bool) or not math.isfinite(value)
            or not -(2**63) <= value < 2**63):
        raise PresenceIndexUnavailable('invalid_presence_cursor')
    return max(0, int(value) if type(value) is bool else value)


def build(store, stage, *, expected, check_open=None):
    """Background-only; bounded raw batches, no provider work or long writer lock."""
    raw = None
    before = ''
    with local_source._writer(store) as conn:
        _schema(conn)
        raw = staged_source.verify_raw_sealed(conn, store, stage, expected=expected)
        if (conn.execute('SELECT 1 FROM presence_index_rows WHERE source=? LIMIT 1',
                         (raw.source_id,)).fetchone() or
                conn.execute('SELECT 1 FROM presence_index_ready WHERE source=?',
                             (raw.source_id,)).fetchone()):
            raise PresenceIndexUnavailable('presence_build_already_started')
        conn.execute('INSERT INTO presence_index_builds VALUES(?,0)', (raw.source_id,))
    revision = 0
    while True:
        if check_open is not None:
            check_open()
        conn = docs._open_reader(store.path)
        try:
            conn.execute('BEGIN')
            if docs._capture_position(conn, store.path, raw.source_id) != raw:
                raise PresenceIndexUnavailable('presence_raw_changed')
            metadata = conn.execute('SELECT path,length(CAST(payload AS BLOB)),deleted '
                'FROM document_observation_records WHERE source_id=? AND path>? ORDER BY path LIMIT 128',
                (raw.source_id, before)).fetchall()
            if not metadata:
                break
            if any(type(path) is not str or not path.startswith('presence/') or deleted != 0
                   or type(size) is not int or size < 0 for path, size, deleted in metadata):
                raise PresenceIndexUnavailable('presence_raw_scope')
            if sum(len(path.encode()) + size for path, size, _ in metadata) > staged_source.MAX_BATCH_BYTES:
                raise PresenceIndexUnavailable('presence_batch_byte_budget')
            captured = docs._capture_selected(conn, store.path, raw,
                tuple(path for path, _size, _deleted in metadata), max_documents=128,
                max_bytes=staged_source.MAX_BATCH_BYTES)
        finally:
            conn.close()
        batch = {}
        for record in captured.records:
            doc = json.loads(record.payload_json)
            if not isinstance(doc, dict) or type(doc.get('user')) is not str or not doc['user']:
                continue
            user = _user(doc['user'])
            batch[user] = max(batch.get(user, 0), _floor(doc.get('last_seen_ns', 0)))
        with local_source._writer(store) as conn:
            _schema(conn)
            if conn.execute('SELECT revision FROM presence_index_builds WHERE source=?',
                            (raw.source_id,)).fetchone() != (revision,):
                raise PresenceIndexUnavailable('presence_build_changed')
            if staged_source.verify_raw_sealed(conn, store, stage, expected=expected) != raw:
                raise PresenceIndexUnavailable('presence_raw_changed')
            # Derived rows participate in the same physical/global staging
            # budgets as raw bytes. Repeated devices of one user allocate one
            # floor row, not an unaccounted second history-sized collection.
            added = sum(len(raw.source_id.encode()) + len(user.encode()) + 32
                        for user in batch if conn.execute(
                            'SELECT 1 FROM presence_index_rows WHERE source=? AND user=?',
                            (raw.source_id, user)).fetchone() is None)
            stage_row = staged_source._row(conn, store, stage, 'sealed')
            used = conn.execute('SELECT coalesce(sum(total_bytes),0) FROM '
                '(SELECT total_bytes FROM staged_sources LIMIT ?)',
                (staged_source.MAX_STAGES + 1,)).fetchone()[0]
            if (stage_row[7] + added > stage_row[8]
                    or used + added > staged_source.MAX_GLOBAL_BYTES):
                raise OverflowError('presence index stage byte budget')
            conn.execute('UPDATE staged_sources SET total_bytes=total_bytes+? WHERE source=?',
                         (added, raw.source_id))
            conn.executemany('INSERT INTO presence_index_rows(source,user,ns) VALUES(?,?,?) '
                             'ON CONFLICT(source,user) DO UPDATE SET ns=max(ns,excluded.ns)',
                             ((raw.source_id, user, ns) for user, ns in batch.items()))
            revision += len(batch)
            if conn.execute('SELECT revision FROM presence_index_builds WHERE source=?',
                            (raw.source_id,)).fetchone() != (revision,):
                raise PresenceIndexUnavailable('presence_build_changed')
        before = metadata[-1][0]
    token = uuid.uuid4().hex
    with local_source._writer(store) as conn:
        _schema(conn)
        if conn.execute('SELECT revision FROM presence_index_builds WHERE source=?',
                        (raw.source_id,)).fetchone() != (revision,):
            raise PresenceIndexUnavailable('presence_build_changed')
        if staged_source.verify_raw_sealed(conn, store, stage, expected=expected) != raw:
            raise PresenceIndexUnavailable('presence_raw_changed')
        conn.execute('INSERT INTO presence_index_ready VALUES(?,?,?,?,?)',
                     (raw.source_id, raw.incarnation, raw.generation, raw.cursor, token))
    return token


def capture(conn, store, raw, members):
    """No full-prefix scan; complete raw generation plus exact subject seeks."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError('presence capture requires a snapshot')
    raw = docs._validate_expected(raw, store.path)
    if type(members) is not tuple or len(members) > MAX_MEMBERS or len(set(members)) != len(members):
        raise ValueError('invalid presence selection')
    members = tuple(_user(user) for user in members)
    _schema(conn)
    if not raw.initialized or docs._capture_position(conn, store.path, raw.source_id) != raw:
        raise PresenceIndexUnavailable('presence_raw_changed')
    ready = conn.execute('SELECT incarnation,generation,cursor,build FROM presence_index_ready '
                         "WHERE source=? AND typeof(incarnation)='text' AND length(CAST(incarnation AS BLOB))<=128 "
                         "AND typeof(generation)='integer' AND typeof(cursor)='integer' "
                         "AND typeof(build)='text' AND length(CAST(build AS BLOB))=32",
                         (raw.source_id,)).fetchone()
    if (ready is None or ready[:3] != (raw.incarnation, raw.generation, raw.cursor)
            or type(ready[3]) is not str or len(ready[3]) != 32
            or any(c not in '0123456789abcdef' for c in ready[3])):
        raise PresenceIndexUnavailable('presence_index_pending')
    values = []
    for user in members:
        row = conn.execute('SELECT ns FROM presence_index_rows WHERE source=? AND user=?',
                           (raw.source_id, user)).fetchone()
        values.append((user, 0 if row is None else _floor(row[0])))
    return PresenceInputs(raw, ready[3], tuple(values))
