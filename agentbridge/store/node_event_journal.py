"""Bounded, at-most-once work journal for newly admitted node records.

The journal is written in the same transaction that replaces one chat's
coupled Store cut.  It contains record identities and delivery high-waters,
never an authority verdict.  Consumers claim identities before publishing
best-effort process events; current payloads are re-read from the admitted
Store and any missing/moved identity becomes an explicit resync gap.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import coupled_payload_stage
from .document_observation import MAX_SQLITE_INTEGER

MAX_PENDING_EVENTS = 1000
MAX_CLAIM_EVENTS = 128
MAX_DELIVERIES = 32
MAX_CLAIM_BYTES = coupled_payload_stage.MAX_PAYLOAD_BYTES

_STATE = (
    "CREATE TABLE node_event_journal_state("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    "gap INTEGER NOT NULL CHECK(gap IN (0,1)))"
)
_ITEMS = (
    "CREATE TABLE node_event_journal_items("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT,chat TEXT NOT NULL,"
    "message_id TEXT NOT NULL,ns INTEGER NOT NULL,sender TEXT NOT NULL,"
    "kind TEXT NOT NULL,UNIQUE(chat,message_id),"
    "CHECK(typeof(chat)='text' AND length(CAST(chat AS BLOB)) BETWEEN 1 AND 4096),"
    "CHECK(typeof(message_id)='text' AND length(CAST(message_id AS BLOB)) BETWEEN 1 AND 4096),"
    f"CHECK(typeof(ns)='integer' AND ns BETWEEN 0 AND {MAX_SQLITE_INTEGER}),"
    "CHECK(typeof(sender)='text' AND length(CAST(sender AS BLOB))<=4096),"
    "CHECK(typeof(kind)='text' AND length(CAST(kind AS BLOB)) BETWEEN 1 AND 256))"
)
_DELIVERY = (
    "CREATE TABLE node_event_delivery("
    "chat TEXT PRIMARY KEY,max_ns INTEGER NOT NULL,"
    "CHECK(typeof(chat)='text' AND length(CAST(chat AS BLOB)) BETWEEN 1 AND 4096),"
    f"CHECK(typeof(max_ns)='integer' AND max_ns BETWEEN 1 AND {MAX_SQLITE_INTEGER}))"
)
_OWNED = {
    "node_event_journal_state": _STATE,
    "node_event_journal_items": _ITEMS,
    "node_event_delivery": _DELIVERY,
}


class NodeEventJournalChanged(RuntimeError):
    """The bounded local work journal is malformed or changed."""


@dataclass(frozen=True)
class NodeEventRecord:
    chat_id: str
    record: dict


@dataclass(frozen=True)
class NodeEventClaim:
    records: tuple[NodeEventRecord, ...]
    gap: bool


@dataclass(frozen=True)
class NodeDelivery:
    chat_id: str
    max_ns: int

    def __post_init__(self) -> None:
        if (not _bounded_text(self.chat_id, 4096)
                or type(self.max_ns) is not int
                or not 1 <= self.max_ns <= MAX_SQLITE_INTEGER):
            raise ValueError("invalid node delivery")


@dataclass(frozen=True)
class NodeDeliveryPage:
    deliveries: tuple[NodeDelivery, ...]
    has_more: bool


def initialize(store) -> None:
    """Install the complete journal namespace or validate the existing one."""
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "node event journal initialization needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {
            name: sql for name, sql in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' "
                "AND name IN (?,?,?)",
                tuple(_OWNED),
            )
        }
        if existing and existing != _OWNED:
            raise NodeEventJournalChanged("node event journal schema changed")
        if not existing:
            for sql in _OWNED.values():
                conn.execute(sql)
            conn.execute(
                "INSERT INTO node_event_journal_state VALUES(1,0)",
            )
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def publish_in_transaction(
    conn: sqlite3.Connection, store, *, stage: str, chat_id: str,
) -> int:
    """Journal new record identities before the coupled replacement commits."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "node event publication requires a Store transaction",
        )
    if _store_path(conn) != Path(store.path).resolve():
        raise ValueError("node event connection belongs to another Store")
    _schema(conn)
    if not _bounded_text(stage, 4096):
        raise ValueError("invalid node event stage")
    if not _bounded_text(chat_id, 4096):
        raise ValueError("invalid node event chat")
    if conn.execute(
            "SELECT 1 FROM node_event_journal_items ORDER BY sequence "
            "LIMIT 1 OFFSET ?",
            (MAX_PENDING_EVENTS,),
    ).fetchone() is not None:
        raise NodeEventJournalChanged("node event journal capacity changed")
    row = conn.execute(
        "SELECT count(*),coalesce(max(CASE WHEN p.kind!='info' "
        "THEN p.ns ELSE 0 END),0) FROM coupled_payload_messages p "
        "LEFT JOIN messages m ON m.chat_id=? AND m.id=p.message_id "
        "WHERE p.stage=? AND m.id IS NULL",
        (chat_id, stage),
    ).fetchone()
    if (row is None or type(row[0]) is not int or type(row[1]) is not int
            or not 0 <= row[0] <= coupled_payload_stage.MAX_MESSAGES
            or not 0 <= row[1] <= MAX_SQLITE_INTEGER):
        raise NodeEventJournalChanged("invalid node event candidate")
    total, max_message_ns = row
    duplicates = conn.execute(
        "SELECT count(*) FROM coupled_payload_messages p "
        "LEFT JOIN messages m ON m.chat_id=? AND m.id=p.message_id "
        "JOIN node_event_journal_items j "
        "ON j.chat=? AND j.message_id=p.message_id "
        "WHERE p.stage=? AND m.id IS NULL",
        (chat_id, chat_id, stage),
    ).fetchone()
    if (duplicates is None or type(duplicates[0]) is not int
            or not 0 <= duplicates[0] <= total):
        raise NodeEventJournalChanged("invalid pending node event count")
    pending_duplicates = duplicates[0]
    if total:
        inserted = conn.execute(
            "INSERT INTO node_event_journal_items(chat,message_id,ns,sender,kind) "
            "SELECT ?,message_id,ns,sender,kind FROM ("
            "SELECT p.message_id,p.ns,p.sender,p.kind "
            "FROM coupled_payload_messages p LEFT JOIN messages m "
            "ON m.chat_id=? AND m.id=p.message_id "
            "WHERE p.stage=? AND m.id IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM node_event_journal_items j "
            "WHERE j.chat=? AND j.message_id=p.message_id) "
            "ORDER BY p.ns DESC,p.sender DESC,p.message_id DESC LIMIT ?) "
            "ORDER BY ns,sender,message_id",
            (chat_id, chat_id, stage, chat_id, MAX_PENDING_EVENTS),
        ).rowcount
        if (type(inserted) is not int
                or inserted != min(
                    total - pending_duplicates, MAX_PENDING_EVENTS,
                )):
            raise NodeEventJournalChanged("node event insert count changed")
        pending = conn.execute(
            "SELECT count(*) FROM node_event_journal_items",
        ).fetchone()
        if (pending is None or type(pending[0]) is not int or pending[0] < 0):
            raise NodeEventJournalChanged("invalid node event journal count")
        excess = max(0, pending[0] - MAX_PENDING_EVENTS)
        if excess:
            deleted = conn.execute(
                "DELETE FROM node_event_journal_items WHERE sequence IN ("
                "SELECT sequence FROM node_event_journal_items "
                "ORDER BY sequence LIMIT ?)",
                (excess,),
            ).rowcount
            if deleted != excess:
                raise NodeEventJournalChanged("node event pruning changed")
        if pending_duplicates or total - pending_duplicates > inserted or excess:
            changed = conn.execute(
                "UPDATE node_event_journal_state SET gap=1 WHERE singleton=1",
            ).rowcount
            if changed != 1:
                raise NodeEventJournalChanged("node event gap state changed")
    if max_message_ns:
        conn.execute(
            "INSERT INTO node_event_delivery(chat,max_ns) VALUES(?,?) "
            "ON CONFLICT(chat) DO UPDATE SET max_ns=max(max_ns,excluded.max_ns)",
            (chat_id, max_message_ns),
        )
    return total


def claim_events(store, *, limit: int = MAX_CLAIM_EVENTS) -> NodeEventClaim | None:
    """Claim one bounded at-most-once event slice before process publication."""
    if (type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_CLAIM_EVENTS):
        raise ValueError("invalid node event claim limit")
    conn = store._conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        state = conn.execute(
            "SELECT gap FROM node_event_journal_state WHERE singleton=1",
        ).fetchone()
        if state is None or state[0] not in (0, 1):
            raise NodeEventJournalChanged("invalid node event journal state")
        rows = conn.execute(
            "SELECT j.sequence,j.chat,j.message_id,j.ns,j.sender,j.kind,"
            "m.ns,m.sender,m.kind,length(CAST(m.payload AS BLOB)) "
            "FROM node_event_journal_items j "
            "LEFT JOIN messages m ON m.chat_id=j.chat AND m.id=j.message_id "
            "ORDER BY j.sequence LIMIT ?",
            (limit,),
        ).fetchall()
        gap = bool(state[0])
        if not rows and not gap:
            conn.commit()
            return None
        records = []
        sequences = []
        claimed_bytes = 0
        for row in rows:
            sequence, chat, message_id, ns, sender, kind = row[:6]
            if (type(sequence) is not int or type(chat) is not str
                    or type(message_id) is not str or type(ns) is not int
                    or type(sender) is not str or type(kind) is not str):
                raise NodeEventJournalChanged("malformed node event identity")
            sequences.append(sequence)
            payload_bytes = row[9]
            if (row[6:9] != (ns, sender, kind)
                    or type(payload_bytes) is not int
                    or not 1 <= payload_bytes <= MAX_CLAIM_BYTES):
                gap = True
                continue
            if claimed_bytes + payload_bytes > MAX_CLAIM_BYTES:
                sequences.pop()
                break
            payload_row = conn.execute(
                "SELECT payload FROM messages WHERE chat_id=? AND id=? "
                "AND ns=? AND sender=? AND kind=?",
                (chat, message_id, ns, sender, kind),
            ).fetchone()
            if payload_row is None or type(payload_row[0]) is not str:
                gap = True
                continue
            claimed_bytes += payload_bytes
            try:
                payload = json.loads(payload_row[0])
            except (json.JSONDecodeError, UnicodeError, RecursionError):
                gap = True
                continue
            if (type(payload) is not dict or payload.get("id") != message_id
                    or payload.get("ns") != ns
                    or payload.get("from", "") != sender
                    or payload.get("kind", "message") != kind):
                gap = True
                continue
            records.append(NodeEventRecord(chat, payload))
        if sequences:
            placeholders = ",".join("?" for _ in sequences)
            deleted = conn.execute(
                f"DELETE FROM node_event_journal_items WHERE sequence IN ({placeholders})",
                tuple(sequences),
            ).rowcount
            if deleted != len(sequences):
                raise NodeEventJournalChanged("node event claim changed")
        if state[0]:
            conn.execute(
                "UPDATE node_event_journal_state SET gap=0 WHERE singleton=1",
            )
        conn.commit()
        return NodeEventClaim(tuple(records), gap)
    except BaseException:
        conn.rollback()
        raise


def pending_deliveries(
    store, *, limit: int = MAX_DELIVERIES,
) -> tuple[NodeDelivery, ...]:
    if (type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_DELIVERIES):
        raise ValueError("invalid node delivery limit")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError("node delivery read needs no transaction")
    _schema(conn)
    rows = conn.execute(
        "SELECT chat,max_ns FROM node_event_delivery ORDER BY chat LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for chat, max_ns in rows:
        if (type(chat) is not str or not chat or type(max_ns) is not int
                or not 1 <= max_ns <= MAX_SQLITE_INTEGER):
            raise NodeEventJournalChanged("malformed node delivery")
        result.append(NodeDelivery(chat, max_ns))
    return tuple(result)


def delivery_page(
    store, *, after_chat: str = "", limit: int = MAX_DELIVERIES,
) -> NodeDeliveryPage:
    """Read one bounded circular delivery slice after the prior chat key."""
    if (type(after_chat) is not str or len(after_chat.encode()) > 4096
            or type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_DELIVERIES):
        raise ValueError("invalid node delivery page")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError("node delivery read needs no transaction")
    _schema(conn)
    rows = conn.execute(
        "SELECT chat,max_ns FROM node_event_delivery WHERE chat>? "
        "ORDER BY chat LIMIT ?",
        (after_chat, limit + 1),
    ).fetchall()
    if after_chat and len(rows) < limit + 1:
        rows.extend(conn.execute(
            "SELECT chat,max_ns FROM node_event_delivery WHERE chat<=? "
            "ORDER BY chat LIMIT ?",
            (after_chat, limit + 1 - len(rows)),
        ).fetchall())
    has_more = len(rows) > limit
    deliveries = []
    for chat, max_ns in rows[:limit]:
        try:
            deliveries.append(NodeDelivery(chat, max_ns))
        except (TypeError, ValueError) as exc:
            raise NodeEventJournalChanged("malformed node delivery") from exc
    return NodeDeliveryPage(tuple(deliveries), has_more)


def complete_delivery(store, expected: NodeDelivery) -> bool:
    """Consume only the exact high-water handled by the external receipt write."""
    if type(expected) is not NodeDelivery:
        raise TypeError("expected node delivery")
    conn = store._conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        changed = conn.execute(
            "DELETE FROM node_event_delivery WHERE chat=? AND max_ns=?",
            (expected.chat_id, expected.max_ns),
        ).rowcount
        conn.commit()
        return changed == 1
    except BaseException:
        conn.rollback()
        raise


def _schema(conn: sqlite3.Connection) -> None:
    rows = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name IN (?,?,?)",
        tuple(_OWNED),
    ))
    if rows != _OWNED:
        raise NodeEventJournalChanged("node event journal schema changed")
    owned = tuple(_OWNED)
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN (?,?,?) LIMIT 1",
        owned,
    ).fetchone()
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
        "AND tbl_name IN (?,?,?) LIMIT 1",
        owned,
    ).fetchone()
    if triggers is not None or indexes is not None:
        raise NodeEventJournalChanged("node event journal objects changed")
    state = conn.execute(
        "SELECT singleton,gap,typeof(singleton),typeof(gap) "
        "FROM node_event_journal_state",
    ).fetchall()
    if (len(state) != 1 or state[0][0] != 1 or state[0][1] not in (0, 1)
            or state[0][2:] != ("integer", "integer")):
        raise NodeEventJournalChanged("node event journal state changed")


def _store_path(conn: sqlite3.Connection) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    main = [row for row in rows if row[1] == "main"]
    if len(main) != 1 or type(main[0][2]) is not str or not main[0][2]:
        raise NodeEventJournalChanged("node event Store path unavailable")
    return Path(main[0][2]).resolve()


def _bounded_text(value, maximum: int) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


__all__ = [
    "MAX_CLAIM_BYTES", "MAX_CLAIM_EVENTS", "MAX_DELIVERIES",
    "MAX_PENDING_EVENTS",
    "NodeDelivery", "NodeEventClaim", "NodeEventJournalChanged",
    "NodeEventRecord", "claim_events", "complete_delivery", "initialize",
    "pending_deliveries", "publish_in_transaction",
]
