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
_DISPLAY_TABLES = {
    'presence_display_rows': 'CREATE TABLE presence_display_rows(source TEXT NOT NULL,user TEXT NOT NULL,seen_ns NUMERIC NOT NULL CHECK(typeof(seen_ns) IN (\'integer\',\'real\')),last_seen TEXT NOT NULL,online_ns NUMERIC NOT NULL CHECK(typeof(online_ns) IN (\'integer\',\'real\')),PRIMARY KEY(source,user))',
    'presence_display_ready': 'CREATE TABLE presence_display_ready(source TEXT PRIMARY KEY,incarnation TEXT NOT NULL,generation INTEGER NOT NULL,cursor INTEGER NOT NULL,build TEXT NOT NULL)',
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
_DISPLAY_TRIGGERS = {
    f'presence_display_dirty_{event.lower()}':
    f'CREATE TRIGGER presence_display_dirty_{event.lower()} AFTER {event} ON presence_display_rows BEGIN '
    + ' '.join(f'DELETE FROM presence_display_ready WHERE source={ref}.source; '
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


@dataclass(frozen=True)
class PresenceDisplayInputs:
    position: docs.DocumentPosition
    build: str
    subjects: tuple[tuple[str, int | float, str, int | float], ...]
    observed_ns: int = 0


def initialize(store):
    with local_source._writer(store) as conn:
        for name, sql in {**_TABLES, **_DISPLAY_TABLES}.items():
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
                conn.execute(sql)
        for name, sql in {**_TRIGGERS, **_DISPLAY_TRIGGERS}.items():
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
                conn.execute(sql)
        _schema(conn)


def _schema(conn):
    for name, sql in {**_TABLES, **_DISPLAY_TABLES}.items():
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
            raise PresenceIndexUnavailable('presence_schema_unavailable')
    actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
                              "(name GLOB 'presence_index_dirty_*' OR name GLOB 'presence_display_dirty_*' "
                              "OR tbl_name IN ('presence_index_rows','presence_index_ready',"
                              "'presence_index_builds','presence_display_rows','presence_display_ready')) LIMIT 7"))
    if actual != {**_TRIGGERS, **_DISPLAY_TRIGGERS}:
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


def _display_record(doc):
    online = doc.get('online', False)
    last_seen = doc.get('last_seen', '')
    if type(online) is not bool or type(last_seen) is not str:
        raise PresenceIndexUnavailable('invalid_presence_display')
    try:
        if len(last_seen.encode()) > 256:
            raise PresenceIndexUnavailable('presence_display_byte_budget')
    except UnicodeError as exc:
        raise PresenceIndexUnavailable('invalid_presence_display') from exc
    return last_seen, online


def build(store, stage, *, expected, check_open=None):
    """Background-only; bounded raw batches, no provider work or long writer lock."""
    raw = None
    before = ''
    with local_source._writer(store) as conn:
        _schema(conn)
        raw = staged_source.verify_raw_sealed(conn, store, stage, expected=expected)
        if (conn.execute('SELECT 1 FROM presence_index_rows WHERE source=? LIMIT 1',
                         (raw.source_id,)).fetchone() or
                conn.execute('SELECT 1 FROM presence_display_rows WHERE source=? LIMIT 1',
                             (raw.source_id,)).fetchone() or
                conn.execute('SELECT 1 FROM presence_index_ready WHERE source=?',
                             (raw.source_id,)).fetchone() or
                conn.execute('SELECT 1 FROM presence_display_ready WHERE source=?',
                             (raw.source_id,)).fetchone()):
            raise PresenceIndexUnavailable('presence_build_already_started')
        conn.execute('INSERT INTO presence_index_builds VALUES(?,0)', (raw.source_id,))
    revision = 0
    display_valid = True
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
        display = {}
        for record in captured.records:
            doc = json.loads(record.payload_json)
            if not isinstance(doc, dict) or type(doc.get('user')) is not str or not doc['user']:
                continue
            user = _user(doc['user'])
            ns = _floor(doc.get('last_seen_ns', 0))
            batch[user] = max(batch.get(user, 0), ns)
            if display_valid:
                try:
                    shown, online = _display_record(doc)
                except PresenceIndexUnavailable:
                    # A bad display-only field cannot erase a valid delivery
                    # floor. This generation simply has no display readiness.
                    display_valid = False
                    display.clear()
                else:
                    prior_ns, prior_shown, prior_online = display.get(user, (0, '', 0))
                    display[user] = (ns if ns > prior_ns else prior_ns,
                                     shown if ns > prior_ns else prior_shown,
                                     max(prior_online, ns if online else 0))
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
            # Reserve the maximum normalized display field size per new user;
            # repeated devices update the same indexed row within that charge.
            added += sum(len(raw.source_id.encode()) + len(user.encode()) + 256 + 48
                         for user in display if conn.execute(
                             'SELECT 1 FROM presence_display_rows WHERE source=? AND user=?',
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
            conn.executemany('INSERT INTO presence_display_rows(source,user,seen_ns,last_seen,online_ns) '
                             'VALUES(?,?,?,?,?) ON CONFLICT(source,user) DO UPDATE SET '
                             'last_seen=CASE WHEN excluded.seen_ns>seen_ns THEN excluded.last_seen '
                             'ELSE last_seen END, seen_ns=max(seen_ns,excluded.seen_ns), '
                             'online_ns=max(online_ns,excluded.online_ns)',
                             ((raw.source_id, user, ns, shown, online_ns)
                              for user, (ns, shown, online_ns) in display.items()))
            revision += len(batch) + len(display)
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
        if display_valid:
            conn.execute('INSERT INTO presence_display_ready VALUES(?,?,?,?,?)',
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


def capture_display(conn, store, raw, users):
    """Exact selected presentation inputs; old floor-only builds stay pending."""
    floor = capture(conn, store, raw, ())
    if type(users) is not tuple or len(users) > MAX_MEMBERS or len(set(users)) != len(users):
        raise ValueError('invalid presence display selection')
    users = tuple(_user(user) for user in users)
    ready = conn.execute('SELECT incarnation,generation,cursor,build FROM presence_display_ready '
                         "WHERE source=? AND typeof(incarnation)='text' AND "
                         "length(CAST(incarnation AS BLOB))<=128 AND "
                         "typeof(generation)='integer' AND typeof(cursor)='integer' AND "
                         "typeof(build)='text' AND length(CAST(build AS BLOB))=32",
                         (raw.source_id,)).fetchone()
    if ready != (raw.incarnation, raw.generation, raw.cursor, floor.build):
        raise PresenceIndexUnavailable('presence_display_pending')
    subjects = []
    for user in users:
        safe = conn.execute('SELECT typeof(seen_ns),typeof(last_seen),'
                            'length(CAST(last_seen AS BLOB)),typeof(online_ns) '
                            'FROM presence_display_rows WHERE source=? AND user=?',
                            (raw.source_id, user)).fetchone()
        if safe is None:
            subjects.append((user, 0, '', 0))
            continue
        if (safe[0] not in ('integer', 'real') or safe[1] != 'text'
                or type(safe[2]) is not int or safe[2] > 256
                or safe[3] not in ('integer', 'real')):
            raise PresenceIndexUnavailable('invalid_presence_display')
        seen, shown, online = conn.execute(
            'SELECT seen_ns,last_seen,online_ns FROM presence_display_rows '
            'WHERE source=? AND user=?', (raw.source_id, user)).fetchone()
        try:
            if len(shown.encode()) > 256:
                raise PresenceIndexUnavailable('presence_display_byte_budget')
        except UnicodeError as exc:
            raise PresenceIndexUnavailable('invalid_presence_display') from exc
        subjects.append((user, _floor(seen), shown, _floor(online)))
    return PresenceDisplayInputs(raw, floor.build, tuple(subjects))
