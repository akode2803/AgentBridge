"""Bounded Store evidence for a future coupled node-input publication.

This module does not stage or publish payloads.  It captures the local inputs
whose movement would make a complete-chat replacement unsafe, then rechecks the
same evidence inside the caller's eventual Store writer transaction.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import (
    local_source, log_position, membership_input_position, page_inputs,
    send_status,
)

MAX_MESSAGES = 100_000
MAX_LOGS = 10_000
MAX_EXISTING_BYTES = 64 * 1024 * 1024
_OUTBOX_INDEX = (
    "CREATE INDEX idx_outbox_append_chat ON outbox(CASE WHEN "
    "instr(target,'|')>0 THEN substr(target,1,instr(target,'|')-1) "
    "ELSE target END) WHERE kind='append_log'"
)


class CoupledAdmissionChanged(RuntimeError):
    """The Store no longer matches the cut used to prepare a candidate."""


@dataclass(frozen=True)
class StoreChatPosition:
    database_path: str
    source: local_source.SourcePosition
    membership: membership_input_position.MembershipInputPosition
    log_positions: tuple[log_position.LogPosition, ...]
    message_count: int


def initialize(store) -> None:
    """Prepare the covering message-size index outside any publication cut."""
    page_inputs.initialize(store._conn())


def capture(
        store, expected_source: local_source.SourcePosition, chat_id: str, *,
        max_messages: int = MAX_MESSAGES,
        max_logs: int = MAX_LOGS,
        max_existing_bytes: int = MAX_EXISTING_BYTES) -> StoreChatPosition:
    """Capture evidence for an exact, already-claimed source owner revision."""
    _limits(max_messages, max_logs, max_existing_bytes)
    expected_source = local_source._expected(store, expected_source)
    conn = sqlite3.connect(
        store.path.as_uri() + "?mode=ro", uri=True, timeout=1.0,
    )
    try:
        conn.execute("BEGIN")
        result = _capture(
            conn, store, expected_source.source_id, chat_id,
            max_messages=max_messages, max_logs=max_logs,
            max_existing_bytes=max_existing_bytes,
        )
        if result.source != expected_source:
            raise CoupledAdmissionChanged("source changed before collection")
        return result
    finally:
        conn.close()


def require_current(
        conn: sqlite3.Connection, store, expected: StoreChatPosition, *,
        max_messages: int = MAX_MESSAGES, max_logs: int = MAX_LOGS,
        max_existing_bytes: int = MAX_EXISTING_BYTES,
) -> None:
    """Reject a stale or unsafe replacement cut inside a Store transaction."""
    if type(expected) is not StoreChatPosition:
        raise TypeError("expected StoreChatPosition")
    _limits(max_messages, max_logs, max_existing_bytes)
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled admission check requires a Store transaction",
        )
    if expected.database_path != str(store.path.resolve()):
        raise ValueError("coupled admission position belongs to another Store")
    current = _capture(
        conn, store, expected.source.source_id, expected.membership.chat_id,
        max_messages=max_messages, max_logs=max_logs,
        max_existing_bytes=max_existing_bytes,
    )
    if current != expected:
        raise CoupledAdmissionChanged("chat inputs changed during collection")


def _capture(
        conn, store, source, chat_id, *, max_messages, max_logs,
        max_existing_bytes):
    path = store.path.resolve()
    databases = {row[1]: row[2] for row in conn.execute("PRAGMA database_list")}
    if (not databases.get("main")
            or Path(databases["main"]).resolve() != path):
        raise ValueError("coupled admission connection belongs to another Store")
    log_position._verify_schema(conn)
    page_inputs._schema(conn)
    send_status._verify_unresolved_index(conn)
    _verify_outbox_index(conn)
    source_position = local_source.capture_in_transaction(conn, store, source)
    membership = membership_input_position._capture(conn, path, chat_id)
    names = _log_names(conn, chat_id, max_logs)
    positions = tuple(
        log_position.capture(conn, path, chat_id, name) for name in names
    )
    message_count = _bounded_messages(
        conn, chat_id, max_messages, max_existing_bytes,
    )
    if conn.execute(
            "SELECT 1 FROM local_send_status WHERE chat_id=? "
            "AND state<>'sent' LIMIT 1", (chat_id,)).fetchone() is not None:
        raise CoupledAdmissionChanged("unresolved local sends")
    if conn.execute(
            "SELECT 1 FROM outbox WHERE kind='append_log' AND "
            "CASE WHEN instr(target,'|')>0 THEN "
            "substr(target,1,instr(target,'|')-1) ELSE target END=? LIMIT 1",
            (chat_id,)).fetchone() is not None:
        raise CoupledAdmissionChanged("unresolved chat outbox work")
    return StoreChatPosition(
        str(path), source_position, membership, positions, message_count,
    )


def _log_names(conn, chat_id, limit):
    for table in ("log_offsets", "ingestion_log_revisions"):
        shapes = conn.execute(
            f"SELECT typeof(log_name),length(CAST(log_name AS BLOB)) FROM {table} "
            "WHERE chat_id=? LIMIT ?", (chat_id, limit + 1),
        ).fetchall()
        if len(shapes) > limit:
            raise OverflowError("existing chat exceeds log-position budget")
        if any(kind != "text" or type(size) is not int or not 1 <= size <= 4096
               for kind, size in shapes):
            raise CoupledAdmissionChanged("invalid log-position identity")
    for table, column in (("log_offsets", "offset"),
                          ("ingestion_log_revisions", "revision"),
                          ("ingestion_chat_resets", "generation")):
        if conn.execute(
                f"SELECT 1 FROM {table} WHERE chat_id=? AND "
                f"(typeof({column})!='integer' OR {column}<0) LIMIT 1",
                (chat_id,)).fetchone() is not None:
            raise CoupledAdmissionChanged("invalid log-position counter")
    rows = conn.execute(
        "SELECT log_name FROM (SELECT log_name FROM log_offsets WHERE chat_id=? "
        "UNION SELECT log_name FROM ingestion_log_revisions WHERE chat_id=?) "
        "ORDER BY log_name LIMIT ?",
        (chat_id, chat_id, limit + 1),
    ).fetchall()
    if len(rows) > limit:
        raise OverflowError("existing chat exceeds log-position budget")
    names = []
    for (name,) in rows:
        if (type(name) is not str or not name or "\x00" in name
                or len(name.encode("utf-8")) > 4096):
            raise CoupledAdmissionChanged("invalid log-position identity")
        names.append(name)
    return tuple(names)


def _bounded_messages(conn, chat_id, limit, max_bytes):
    used = 0
    count = 0
    for count, (size,) in enumerate(conn.execute(
            f"SELECT length(CAST(payload AS BLOB)) FROM messages INDEXED BY "
            f"{page_inputs.INDEX} WHERE chat_id=? LIMIT ?",
            (chat_id, limit + 1)), 1):
        if count > limit:
            raise OverflowError("existing chat exceeds message replacement budget")
        if type(size) is not int or size < 0:
            raise CoupledAdmissionChanged("invalid existing message payload")
        used += size
        if used > max_bytes:
            raise OverflowError("existing chat exceeds replacement byte budget")
    return count


def _verify_outbox_index(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_outbox_append_chat'",
    ).fetchone()
    if row is None or " ".join(row[0].split()) != _OUTBOX_INDEX:
        raise RuntimeError("append-log outbox index changed")


def _limits(max_messages, max_logs, max_existing_bytes):
    if (type(max_messages) is not int or not 0 <= max_messages <= MAX_MESSAGES
            or type(max_logs) is not int or not 0 <= max_logs <= MAX_LOGS
            or type(max_existing_bytes) is not int
            or not 0 <= max_existing_bytes <= MAX_EXISTING_BYTES):
        raise ValueError("coupled admission budget exceeds supported range")


__all__ = [
    "CoupledAdmissionChanged", "MAX_EXISTING_BYTES", "MAX_LOGS", "MAX_MESSAGES",
    "StoreChatPosition", "capture", "initialize", "require_current",
]
