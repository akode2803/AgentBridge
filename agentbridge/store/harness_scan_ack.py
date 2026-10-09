"""Durable per-chat acknowledgement for harness runtime discovery.

The existing membership-input generation is the message/change evidence.  This
table records only which exact generation a harness scan completed; it is never
membership, visibility or trust authority.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import membership_input_position as inputs
from .membership_input_position import MembershipInputPosition

MAX_PENDING = 1024
_TABLE = "harness_scan_ack"
_SQL = (
    "CREATE TABLE harness_scan_ack("
    "agent TEXT NOT NULL,"
    "chat_id TEXT NOT NULL,"
    "incarnation TEXT NOT NULL,"
    "namespace_epoch TEXT NOT NULL,"
    "generation INTEGER NOT NULL CHECK(typeof(generation)='integer' "
    f"AND generation>=0 AND generation<={inputs.MAX_SQLITE_INTEGER}),"
    "PRIMARY KEY(agent,chat_id))"
)


class HarnessScanAckUnavailable(RuntimeError):
    """The acknowledgement namespace cannot safely be read or changed."""


@dataclass(frozen=True)
class PendingHarnessScans:
    positions: tuple[MembershipInputPosition, ...]
    has_more: bool


def initialize(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "cannot initialize harness scan acknowledgements in a transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        present = _present(conn)
        if not present:
            conn.execute(_SQL)
        _verify_schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def pending(
    path: Path, agent: str, *, limit: int = MAX_PENDING,
) -> PendingHarnessScans:
    database_path = str(Path(path).resolve())
    wanted_agent = _agent(agent)
    if type(limit) is not int or not 1 <= limit <= MAX_PENDING:
        raise ValueError("invalid harness scan pending limit")
    conn = sqlite3.connect(
        Path(database_path).as_uri() + "?mode=ro", uri=True, timeout=1.0,
    )
    try:
        conn.execute("BEGIN")
        _verify_schema(conn)
        incarnation, epoch = inputs._verify_schema(conn)
        rows = conn.execute(
            "SELECT v.chat_id,v.generation FROM membership_input_versions v "
            "LEFT JOIN harness_scan_ack a ON a.agent=? AND a.chat_id=v.chat_id "
            "WHERE a.chat_id IS NULL OR a.incarnation!=? "
            "OR a.namespace_epoch!=? OR a.generation!=v.generation "
            "ORDER BY v.chat_id LIMIT ?",
            (wanted_agent, incarnation, epoch, limit + 1),
        ).fetchall()
        positions = tuple(
            MembershipInputPosition(
                database_path, incarnation, epoch, chat_id, generation,
            )
            for chat_id, generation in rows[:limit]
        )
        return PendingHarnessScans(positions, len(rows) > limit)
    finally:
        conn.close()


def acknowledge(
    conn: sqlite3.Connection,
    path: Path,
    agent: str,
    expected: MembershipInputPosition,
) -> bool:
    """Acknowledge only if the exact message generation is still current."""
    database_path = str(Path(path).resolve())
    wanted_agent = _agent(agent)
    copied = inputs._copy_expected(expected, database_path)
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "harness scan acknowledgement requires its own transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        _verify_schema(conn)
        current = inputs._capture(conn, Path(database_path), copied.chat_id)
        if current != copied:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO harness_scan_ack"
            "(agent,chat_id,incarnation,namespace_epoch,generation) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(agent,chat_id) DO UPDATE SET "
            "incarnation=excluded.incarnation,"
            "namespace_epoch=excluded.namespace_epoch,"
            "generation=excluded.generation",
            (
                wanted_agent, copied.chat_id, copied.incarnation,
                copied.namespace_epoch, copied.generation,
            ),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _present(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone() is not None


def _verify_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    if row is None or _normalized(row[0]) != _normalized(_SQL):
        raise HarnessScanAckUnavailable(
            "incompatible harness scan acknowledgement table"
        )


def _normalized(value: str) -> str:
    return " ".join(str(value).split())


def _agent(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise ValueError("invalid harness scan acknowledgement agent")
    return value


__all__ = [
    "HarnessScanAckUnavailable", "MAX_PENDING", "PendingHarnessScans",
    "acknowledge", "initialize", "pending",
]
