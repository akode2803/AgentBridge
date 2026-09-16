"""Generation-bound pending terminal inputs. No membership authority is cached.

Python owns classification: SQLite JSON1 disagrees on duplicate keys and NaN.
Every source mutation invalidates readiness before readers can observe that write.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import document_observation
from .overlay_index import _text

SCHEMA = 1
MAX_BUILD_ROWS = 100_000
MAX_BUILD_BYTES = 64 * 1024 * 1024
_MAX_INT = 2**63 - 1
_TABLES = {
    'terminal_namespace': 'CREATE TABLE terminal_namespace(singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch TEXT NOT NULL)',
    'terminal_versions': ('CREATE TABLE terminal_versions(target TEXT PRIMARY KEY,generation INTEGER NOT NULL '
                          f"CHECK(typeof(generation)='integer' AND generation>=1 AND generation<={_MAX_INT}))"),
    'terminal_ready': 'CREATE TABLE terminal_ready(target TEXT PRIMARY KEY,generation INTEGER NOT NULL,schema INTEGER NOT NULL)',
    'terminal_classes': 'CREATE TABLE terminal_classes(target TEXT NOT NULL,seq INTEGER NOT NULL,raw INTEGER NOT NULL,wrapped INTEGER NOT NULL,PRIMARY KEY(target,seq))',
}
_INDEXES = {
    'idx_terminal_source': 'CREATE INDEX idx_terminal_source ON outbox(target,state,seq,length(CAST(payload AS BLOB)))',
    'idx_terminal_raw': 'CREATE INDEX idx_terminal_raw ON terminal_classes(target,raw,seq)',
    'idx_terminal_wrapped': 'CREATE INDEX idx_terminal_wrapped ON terminal_classes(target,wrapped,seq)',
}


def _bump(target, when='1'):
    return (
        'SELECT CASE WHEN EXISTS(SELECT 1 FROM terminal_versions WHERE target=' + target +
        f" AND (typeof(generation)!='integer' OR generation<1 OR generation>={_MAX_INT})) AND ({when}) "
        "THEN RAISE(ABORT,'terminal generation invalid or exhausted') END; "
        'INSERT INTO terminal_versions(target,generation) SELECT ' + target + ',1 WHERE ' + when + ' '
        'ON CONFLICT(target) DO UPDATE SET generation=generation+1; '
        'DELETE FROM terminal_ready WHERE target=' + target + ' AND (' + when + ');'
    )


def _triggers():
    result = {}
    for event, refs in (('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW'))):
        name = f'terminal_dirty_{event.lower()}'
        actions = ' '.join(_bump(ref + '.target', 'OLD.target IS NOT NEW.target' if event == 'UPDATE' and ref == 'NEW' else '1') for ref in refs)
        when = (" WHEN OLD.target IS NOT NEW.target OR OLD.state IS NOT NEW.state OR "
                "OLD.payload IS NOT NEW.payload OR OLD.seq IS NOT NEW.seq") if event == 'UPDATE' else ''
        result[name] = f'CREATE TRIGGER {name} AFTER {event} ON outbox{when} BEGIN {actions} END'
        name = f'terminal_classes_dirty_{event.lower()}'
        actions = ' '.join('DELETE FROM terminal_ready WHERE target=' + ref + '.target;' for ref in refs)
        result[name] = f'CREATE TRIGGER {name} AFTER {event} ON terminal_classes BEGIN {actions} END'
    # REPLACE deletes its conflicting PK row without DELETE triggers unless
    # recursive_triggers is enabled. Cover INSERT and UPDATE collisions explicitly.
    for operation, condition in (('INSERT', ''), ('UPDATE OF seq', 'NEW.seq IS NOT OLD.seq AND ')):
        name = 'terminal_replace_' + ('insert' if operation == 'INSERT' else 'update')
        result[name] = (
            f'CREATE TRIGGER {name} BEFORE {operation} ON outbox WHEN ' + condition +
            'EXISTS(SELECT 1 FROM outbox WHERE seq=NEW.seq) BEGIN ' +
            _bump('(SELECT target FROM outbox WHERE seq=NEW.seq)') + ' END'
        )
    return result


class TerminalObservationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TerminalPosition:
    database_path: str
    epoch: str
    target: str
    generation: int


@dataclass(frozen=True)
class TerminalObservation:
    position: TerminalPosition
    wrapped: bool
    pending: bool


def _outbox_schema(conn):
    columns = {r[1]: (r[2].upper(), r[5]) for r in conn.execute('PRAGMA table_info(outbox)')}
    if any(columns.get(k) != v for k, v in {
        'seq': ('INTEGER', 1), 'target': ('TEXT', 0), 'state': ('TEXT', 0), 'payload': ('TEXT', 0),
    }.items()):
        raise TerminalObservationUnavailable('outbox_schema_changed')


def _schema(conn):
    _outbox_schema(conn)
    for name, sql in (*_TABLES.items(), *_INDEXES.items()):
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
            raise TerminalObservationUnavailable('terminal_schema_pending_or_changed')
    installed = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name GLOB 'terminal_*' OR tbl_name IN ('outbox','terminal_versions','terminal_namespace','terminal_ready','terminal_classes')) LIMIT 9"))
    if installed != _triggers():
        raise TerminalObservationUnavailable('terminal_triggers_changed')
    for name, names in {
        'idx_terminal_source': ('target', 'state', 'seq', None),
        'idx_terminal_raw': ('target', 'raw', 'seq'),
        'idx_terminal_wrapped': ('target', 'wrapped', 'seq'),
    }.items():
        columns = conn.execute(f'PRAGMA index_xinfo({name})').fetchall()
        if [(r[2], r[3], r[4], r[5]) for r in columns] != [
            *((column, 0, 'BINARY', 1) for column in names), (None, 0, 'BINARY', 0),
        ]:
            raise TerminalObservationUnavailable('terminal_index_changed')
    rows = conn.execute('SELECT singleton,epoch FROM terminal_namespace LIMIT 2').fetchall()
    if (len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not str
            or len(rows[0][1]) != 32 or any(c not in '0123456789abcdef' for c in rows[0][1])):
        raise TerminalObservationUnavailable('terminal_epoch_changed')
    return rows[0][1]


def initialize(conn):
    """Explicit off-path preparation. No outbox intent is rewritten or removed."""
    if conn.in_transaction:
        raise sqlite3.OperationalError('terminal preparation requires an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        _outbox_schema(conn)
        present = conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'terminal_*' OR name GLOB 'idx_terminal_*'").fetchall()
        if not present:
            for sql in _TABLES.values():
                conn.execute(sql)
            conn.execute('INSERT INTO terminal_namespace VALUES(1,?)', (uuid.uuid4().hex,))
            conn.execute('INSERT INTO terminal_versions SELECT target,1 FROM outbox GROUP BY target')
            for sql in _INDEXES.values():
                conn.execute(sql)
            for sql in _triggers().values():
                conn.execute(sql)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _expected(value, path, target=None):
    if type(value) is not TerminalPosition:
        raise ValueError('invalid terminal position')
    database_path, epoch, captured_target, generation = (
        value.database_path, value.epoch, value.target, value.generation,
    )
    _text(captured_target, 'outbox target')
    if (database_path != str(Path(path).resolve()) or (target is not None and captured_target != target)
            or type(generation) is not int or not 0 <= generation <= _MAX_INT
            or type(epoch) is not str or len(epoch) != 32
            or any(c not in '0123456789abcdef' for c in epoch)):
        raise ValueError('invalid terminal position')
    return TerminalPosition(database_path, epoch, captured_target, generation)


def _position(conn, path, target):
    if not conn.in_transaction:
        raise sqlite3.OperationalError('terminal capture requires an active transaction')
    path = str(Path(path).resolve())
    _text(target, 'outbox target')
    if str(Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()) != path:
        raise ValueError('terminal connection belongs to another database')
    epoch = _schema(conn)
    row = conn.execute('SELECT generation FROM terminal_versions WHERE target=?', (target,)).fetchone()
    if row is None:
        if conn.execute('SELECT 1 FROM outbox INDEXED BY idx_terminal_source WHERE target=? LIMIT 1', (target,)).fetchone():
            raise TerminalObservationUnavailable('terminal_generation_missing')
        generation = 0
    else:
        generation = row[0]
        if type(generation) is not int or not 1 <= generation <= _MAX_INT:
            raise TerminalObservationUnavailable('terminal_generation_invalid')
    return TerminalPosition(path, epoch, target, generation)


def matches(conn, path, expected):
    if type(expected) is not TerminalPosition:
        raise ValueError('invalid terminal position')
    expected = _expected(expected, path)
    position = _position(conn, path, expected.target)
    if position != expected:
        return False
    return position.generation == 0 or conn.execute(
        'SELECT generation,schema FROM terminal_ready WHERE target=?',
        (position.target,),
    ).fetchone() == (position.generation, SCHEMA)


def _classify(raw, wrapped):
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return 0  # canonical outbox_payloads skips these
    except RecursionError:
        return 2
    if not isinstance(payload, dict):
        return 0
    envelope = payload.get('envelope') if wrapped and isinstance(payload.get('envelope'), dict) else payload
    if envelope.get('kind') != 'info':
        return 0
    event = envelope.get('event') or {}
    if not isinstance(event, dict):
        return 0
    try:
        return int(event.get('type') in {'chat_deleted', 'member_left'})
    except TypeError:
        return 2


def refresh(conn, path, target, *, max_rows=MAX_BUILD_ROWS, max_bytes=MAX_BUILD_BYTES):
    """Background capture/classification/CAS. Never accept external verdicts."""
    if type(max_rows) is not int or not 0 <= max_rows <= MAX_BUILD_ROWS:
        raise ValueError('invalid build row budget')
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BUILD_BYTES:
        raise ValueError('invalid build byte budget')
    if conn.in_transaction:
        raise sqlite3.OperationalError('terminal refresh requires an owned transaction')
    reader = document_observation._open_reader(path)
    try:
        reader.execute('BEGIN')
        position = _position(reader, path, target)
        metadata = reader.execute("SELECT seq,length(CAST(payload AS BLOB)) FROM outbox INDEXED BY idx_terminal_source WHERE target=? AND state='pending' ORDER BY seq LIMIT ?", (target, max_rows + 1)).fetchall()
        if len(metadata) > max_rows:
            raise TerminalObservationUnavailable('terminal_build_row_budget')
        if any(type(seq) is not int or type(size) is not int or size < 0 for seq, size in metadata):
            raise TerminalObservationUnavailable('invalid_outbox_metadata')
        if sum(size for _, size in metadata) > max_bytes:
            raise TerminalObservationUnavailable('terminal_build_byte_budget')
        captured = []
        for seq, size in metadata:
            state, payload = reader.execute('SELECT state,payload FROM outbox WHERE seq=?', (seq,)).fetchone()
            if type(state) is not str or type(payload) is not str or len(payload.encode()) != size:
                raise TerminalObservationUnavailable('invalid_outbox_payload')
            captured.append((seq, state, payload))
    finally:
        reader.close()
    classes = [(target, seq, _classify(raw, False), _classify(raw, True))
               for seq, state, raw in captured if state == 'pending']
    try:
        conn.execute('BEGIN IMMEDIATE')
        if _position(conn, path, target) != position:
            raise TerminalObservationUnavailable('terminal_inputs_changed')
        conn.execute('DELETE FROM terminal_ready WHERE target=?', (target,))
        conn.execute('DELETE FROM terminal_classes WHERE target=?', (target,))
        conn.executemany('INSERT INTO terminal_classes VALUES(?,?,?,?)', classes)
        conn.execute('INSERT INTO terminal_ready VALUES(?,?,?)', (target, position.generation, SCHEMA))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return position


def capture(path, target, *, wrapped=False, expected=None):
    conn = document_observation._open_reader(path)
    try:
        conn.execute('BEGIN')
        return _capture(conn, path, target, wrapped=wrapped, expected=expected)
    finally:
        conn.close()


def _capture(conn, path, target, *, wrapped=False, expected=None):
    if type(wrapped) is not bool:
        raise ValueError('invalid envelope mode')
    if expected is not None:
        expected = _expected(expected, path, target)
    position = _position(conn, path, target)
    if expected is not None and position != expected:
        raise TerminalObservationUnavailable('terminal_inputs_changed')
    if position.generation == 0:
        return TerminalObservation(position, wrapped, False)
    ready = conn.execute('SELECT generation,schema FROM terminal_ready WHERE target=?', (target,)).fetchone()
    if ready != (position.generation, SCHEMA):
        raise TerminalObservationUnavailable('terminal_classification_pending')
    column = 'wrapped' if wrapped else 'raw'
    # Malformed evidence and terminal events both deny. Prefer an explicit
    # unavailable result if malformed evidence exists, independent of old SQL order.
    for verdict in (2, 1):
        if conn.execute(f'SELECT 1 FROM terminal_classes INDEXED BY idx_terminal_{column} WHERE target=? AND {column}=? LIMIT 1', (target, verdict)).fetchone():
            if verdict == 2:
                raise TerminalObservationUnavailable('terminal_evidence_unavailable')
            return TerminalObservation(position, wrapped, True)
    return TerminalObservation(position, wrapped, False)
