"""Bounded canonical state-event suffix inputs; never membership authority.

The materialized boundary must come from the caller's captured canonical meta.
An oversized suffix is unavailable, not a partial snapshot or an empty suffix.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import document_observation, membership_input_position, page_inputs
from .membership_input_position import MembershipInputPosition
from .page_inputs import SerializedMessage

INDEX = 'idx_messages_membership_suffix'
# Keep malformed JSON in the candidate set without changing ingestion into an
# eager JSON-validation boundary. Captures that encounter it fail on decoding.
PREDICATE = ("kind='info' AND CASE WHEN json_valid(payload) THEN "
             "coalesce(json_extract(payload, '$.event.type'), '') != 'reaction' ELSE 1 END")
INDEX_SQL = (f'CREATE INDEX {INDEX} ON messages'
             f'(chat_id,ns,sender,id,length(CAST(payload AS BLOB)),kind) WHERE {PREDICATE}')
MAX_EVENTS = 256
MAX_BYTES = 4 * 1024 * 1024


class MembershipSuffixUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MembershipSuffix:
    position: MembershipInputPosition
    after_ns: int
    rows: tuple[SerializedMessage, ...]
    captured_bytes: int


def _schema(conn):
    page_inputs._message_schema(conn)
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()
    if row is None:
        raise MembershipSuffixUnavailable('suffix_index_pending')
    if row != (INDEX_SQL,):
        raise MembershipSuffixUnavailable('suffix_index_changed')
    columns = conn.execute(f'PRAGMA index_xinfo({INDEX})').fetchall()
    if [(r[2], r[3], r[4], r[5]) for r in columns] != [
        ('chat_id', 0, 'BINARY', 1), ('ns', 0, 'BINARY', 1),
        ('sender', 0, 'BINARY', 1), ('id', 0, 'BINARY', 1),
        (None, 0, 'BINARY', 1), ('kind', 0, 'BINARY', 1),
        (None, 0, 'BINARY', 0),
    ]:
        raise MembershipSuffixUnavailable('suffix_index_changed')


def initialize(conn):
    """Explicit background index preparation, with no foreground fallback."""
    if conn.in_transaction:
        raise sqlite3.OperationalError('suffix preparation requires an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        page_inputs._message_schema(conn)
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (INDEX,)).fetchone() is None:
            conn.execute(INDEX_SQL)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def capture(path, chat_id, after_ns, *, expected=None, max_events=128, max_bytes=MAX_BYTES):
    """Capture ALL qualifying newer events or raise before any payload read.

    Mirrors `Store.state_events_after`: exclude reaction breadcrumbs in the
    partial index, use the strict scalar materialized_ns boundary, and restore
    deterministic `(ns,sender,id)` order. A matching position is freshness
    evidence only; the caller still owns source/authority validation.
    """
    conn = document_observation._open_reader(path)
    try:
        conn.execute('BEGIN')
        result = _capture(conn, path, chat_id, after_ns, expected=expected,
                          max_events=max_events, max_bytes=max_bytes)
    finally:
        conn.close()
    return result


def _capture(conn, path, chat_id, after_ns, *, expected=None, max_events=128, max_bytes=MAX_BYTES):
    if not conn.in_transaction:
        raise sqlite3.OperationalError('suffix capture requires an active transaction')
    if type(after_ns) is not int or not 0 <= after_ns <= 2**63 - 1:
        raise ValueError('invalid materialized boundary')
    if type(max_events) is not int or not 0 <= max_events <= MAX_EVENTS:
        raise ValueError('invalid suffix event budget')
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES:
        raise ValueError('invalid suffix byte budget')
    if expected is not None:
        expected = membership_input_position._copy_expected(expected, str(Path(path).resolve()))
        if expected.chat_id != chat_id:
            raise ValueError('suffix position belongs to another chat')
    _schema(conn)
    position = membership_input_position._capture(conn, path, chat_id)
    if expected is not None and expected != position:
        raise MembershipSuffixUnavailable('message_inputs_changed')
    meta = conn.execute(
        f'SELECT ns,sender,id,kind,length(CAST(payload AS BLOB)) FROM messages INDEXED BY {INDEX} '
        f'WHERE chat_id=? AND ns>? AND {PREDICATE} ORDER BY ns,sender,id LIMIT ?',
        (position.chat_id, after_ns, max_events + 1),
    ).fetchall()
    if len(meta) > max_events:
        raise MembershipSuffixUnavailable('suffix_event_budget_exceeded')
    try:
        selected = [page_inputs._metadata(row) for row in meta]
    except page_inputs.PageInputsChanged as exc:
        raise MembershipSuffixUnavailable('malformed_suffix_key') from exc
    used = sum(len(s.encode()) for s in (position.database_path, position.chat_id,
                                        position.incarnation, position.namespace_epoch)) + 40
    used += sum(size + len(key.sender.encode()) + len(key.id.encode()) + len(kind.encode())
                for key, kind, size in selected)
    if used > max_bytes:
        raise MembershipSuffixUnavailable('suffix_byte_budget_exceeded')
    rows = []
    for key, kind, size in selected:
        row = conn.execute('SELECT payload FROM messages WHERE chat_id=? AND id=?',
                           (position.chat_id, key.id)).fetchone()
        if row is None or type(row[0]) is not str or len(row[0].encode()) != size:
            raise MembershipSuffixUnavailable('malformed_suffix_payload')
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MembershipSuffixUnavailable('malformed_suffix_payload') from exc
        if (type(payload) is not dict or type(payload.get('ns')) is not int
                or any(type(payload.get(name)) is not str for name in ('id', 'from', 'kind'))
                or (payload['ns'], payload['from'], payload['id'], payload['kind'])
                != (key.ns, key.sender, key.id, kind)):
            raise MembershipSuffixUnavailable('malformed_suffix_payload')
        rows.append(SerializedMessage(key, kind, row[0]))
    return MembershipSuffix(position, after_ns, tuple(rows), used)


def matches_position(conn, path, expected):
    """Payload-free final suffix fence inside the caller's active transaction."""
    copied = membership_input_position._copy_expected(expected, str(Path(path).resolve()))
    if not conn.in_transaction:
        raise sqlite3.OperationalError('suffix matching requires an active transaction')
    _schema(conn)
    return membership_input_position._capture(conn, path, copied.chat_id) == copied
