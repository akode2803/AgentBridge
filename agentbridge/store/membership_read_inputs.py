"""Bounded SQLite inputs for a single-use membership read."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import lifecycle_heads, membership_input_position
from .membership_input_position import MembershipInputPosition

MAX_EVENTS = 100_000
MAX_HEADS = 1_024
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class MembershipReadCut:
    position: MembershipInputPosition
    events: tuple[tuple[int, str, str, str, str], ...]
    heads: tuple[tuple[str, int, str | None], ...]
    serialized_bytes: int
    heads_bytes: int


def capture_cut(
    conn: sqlite3.Connection,
    path: Path,
    chat_id: str,
    *,
    max_events: int = MAX_EVENTS,
    max_heads: int = MAX_HEADS,
    max_bytes: int = MAX_BYTES,
) -> MembershipReadCut:
    """Capture one position, event suffix superset, and retained namespace."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError("membership read capture requires a transaction")
    for value, ceiling in ((max_events, MAX_EVENTS), (max_heads, MAX_HEADS),
                           (max_bytes, MAX_BYTES)):
        if type(value) is not int or not 0 <= value <= ceiling:
            raise ValueError("membership read budget exceeds supported range")
    position = membership_input_position._capture(conn, path, chat_id)
    fixed = sum(len(value.encode("utf-8")) for value in (
        position.database_path, position.incarnation, position.namespace_epoch,
        position.chat_id,
    )) + 32
    if fixed > max_bytes:
        raise OverflowError("membership read capture exceeds byte budget")
    metadata = conn.execute(
        "SELECT ns,length(CAST(sender AS BLOB)),length(CAST(id AS BLOB)),"
        "length(CAST(kind AS BLOB)),length(CAST(payload AS BLOB)) FROM messages "
        "WHERE chat_id=? AND kind='info' AND ns>0 ORDER BY ns,sender,id LIMIT ?",
        (chat_id, max_events + 1),
    ).fetchall()
    if len(metadata) > max_events:
        raise OverflowError("membership read events exceed row budget")
    event_bytes = 0
    for ns, *sizes in metadata:
        if type(ns) is not int or any(type(size) is not int for size in sizes):
            raise sqlite3.DatabaseError("invalid stored membership event")
        event_bytes += 16 + sum(sizes)
        if fixed + event_bytes > max_bytes:
            raise OverflowError("membership read capture exceeds byte budget")
    heads, head_bytes = lifecycle_heads._capture_all(
        conn, max_heads=max_heads, max_bytes=max_bytes - fixed - event_bytes,
    )
    rows = conn.execute(
        "SELECT ns,sender,id,kind,payload FROM messages WHERE chat_id=? "
        "AND kind='info' AND ns>0 ORDER BY ns,sender,id", (chat_id,),
    ).fetchall()
    if len(rows) != len(metadata):
        raise sqlite3.DatabaseError("membership event capture changed")
    for row in rows:
        if type(row[0]) is not int or any(type(value) is not str for value in row[1:]):
            raise sqlite3.DatabaseError("invalid stored membership event")
    return MembershipReadCut(
        position, tuple(rows), tuple(heads), fixed + event_bytes + head_bytes,
        head_bytes,
    )


def matches_cut(
    conn: sqlite3.Connection,
    path: Path,
    expected: MembershipReadCut,
    *,
    max_heads: int = MAX_HEADS,
    max_bytes: int = MAX_BYTES,
) -> bool:
    """Validate the position and complete retained namespace in one transaction."""
    if type(expected) is not MembershipReadCut:
        raise TypeError("expected MembershipReadCut")
    current = membership_input_position._capture(conn, path, expected.position.chat_id)
    if current != expected.position:
        return False
    if expected.heads_bytes > max_bytes:
        raise OverflowError("retained head comparison exceeds byte budget")
    heads, _used = lifecycle_heads._capture_all(
        conn, max_heads=max_heads, max_bytes=expected.heads_bytes,
    )
    return tuple(heads) == expected.heads


__all__ = ["MembershipReadCut", "capture_cut", "matches_cut"]
