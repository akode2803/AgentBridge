"""Raw inputs for one bounded historical membership-authority cut."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import lifecycle_heads, shadow_slot

MAX_EVENTS = 100_000
MAX_HEADS = 1_024
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class RawMembershipInputs:
    position: shadow_slot.ShadowPosition
    shadow: tuple[int, int, str, str, tuple[tuple[str, str], ...]] | None
    events: tuple[tuple[int, str, str, str, str], ...]
    heads: tuple[tuple[str, int, str | None], ...]


def capture_raw(
    conn: sqlite3.Connection,
    database_path,
    expected_shadow: shadow_slot.ShadowPosition,
    chat_id: str,
    *,
    max_documents: int = shadow_slot.MAX_RECORDS,
    max_events: int = MAX_EVENTS,
    max_heads: int = MAX_HEADS,
    max_bytes: int = MAX_BYTES,
) -> RawMembershipInputs:
    """Copy raw rows from the caller's active private read transaction."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError("membership capture requires a transaction")
    if type(chat_id) is not str or not chat_id:
        raise ValueError("chat_id must be a nonempty string")
    for value, ceiling in (
        (max_documents, shadow_slot.MAX_RECORDS), (max_events, MAX_EVENTS),
        (max_heads, MAX_HEADS), (max_bytes, MAX_BYTES),
    ):
        if type(value) is not int or not 0 <= value <= ceiling:
            raise ValueError("membership capture budget exceeds supported range")

    current = shadow_slot._match(conn, database_path, expected_shadow)
    used = shadow_slot._capture_preflight(
        conn, current, max_documents=max_documents, max_bytes=max_bytes,
    )
    if current.initialized:
        shadow_count = conn.execute(
            "SELECT count(*) FROM diagnostic_shadow_records"
        ).fetchone()[0]
        used += 16 + 8 * shadow_count
        if used > max_bytes:
            raise OverflowError("membership capture exceeds byte budget")
    event_metadata = conn.execute(
        "SELECT ns,length(CAST(sender AS BLOB)),length(CAST(id AS BLOB)),"
        "length(CAST(kind AS BLOB)),length(CAST(payload AS BLOB)) FROM messages "
        "WHERE chat_id=? AND kind='info' AND ns>0 ORDER BY ns,sender,id LIMIT ?",
        (chat_id, max_events + 1),
    ).fetchall()
    if len(event_metadata) > max_events:
        raise OverflowError("membership event capture exceeds row budget")
    event_bytes = 0
    for ns, *sizes in event_metadata:
        if type(ns) is not int or any(type(size) is not int for size in sizes):
            raise sqlite3.DatabaseError("invalid stored membership event")
        event_bytes += 16 + sum(sizes)
        if used + event_bytes > max_bytes:
            raise OverflowError("membership event capture exceeds byte budget")
    head_rows, head_bytes = lifecycle_heads._capture_all(
        conn, max_heads=max_heads, max_bytes=max_bytes - used - event_bytes,
    )
    used += event_bytes + head_bytes
    if used > max_bytes:
        raise OverflowError("membership capture exceeds byte budget")
    event_rows = conn.execute(
        "SELECT ns,sender,id,kind,payload FROM messages "
        "WHERE chat_id=? AND kind='info' AND ns>0 ORDER BY ns,sender,id",
        (chat_id,),
    ).fetchall()
    for row in event_rows:
        ns, sender, event_id, kind, payload = row
        if type(ns) is not int or any(type(v) is not str for v in row[1:]):
            raise sqlite3.DatabaseError("invalid stored membership event")
    for subject, generation, payload in head_rows:
        if type(subject) is not str or type(generation) is not int \
                or payload is not None and type(payload) is not str:
            raise sqlite3.DatabaseError("invalid stored lifecycle head")
    return RawMembershipInputs(
        current, shadow_slot._capture_rows(conn, current), tuple(event_rows),
        tuple(head_rows),
    )


__all__ = ["MAX_BYTES", "MAX_EVENTS", "MAX_HEADS", "RawMembershipInputs", "capture_raw"]
