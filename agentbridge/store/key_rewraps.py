"""Durable progress of re-wrapping chat history for newcomers.

Under a bound typed writer, adding members to a chat whose history stays
visible re-wraps every historical epoch key for them, one conditional update
per epoch document. A row here is that fanout's pending-work record: the chat,
the newcomers bound to the exact membership instance the add created
(``joined_ns``), and the last epoch finished. It is never membership,
visibility or key authority: every resume re-checks a fresh snapshot first.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

MAX_PENDING = 4096
MAX_NEWCOMERS = 1024
_MAX_CHAT_BYTES = 1024
_MAX_SQLITE_INTEGER = 2**63 - 1
_NAME = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z", re.ASCII)
_SQL = (
    "CREATE TABLE key_rewraps("
    "id INTEGER PRIMARY KEY,"
    "chat_id TEXT NOT NULL,"
    "newcomers TEXT NOT NULL,"
    "after_epoch INTEGER NOT NULL CHECK(typeof(after_epoch)='integer' "
    "AND after_epoch>=-1))"
)


@dataclass(frozen=True)
class KeyRewrap:
    id: int
    chat_id: str
    newcomers: dict[str, int]  # name -> joined_ns of the membership instance
    after_epoch: int


def initialize(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise sqlite3.OperationalError("cannot initialize key rewraps in a transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='key_rewraps'").fetchone() is None:
            conn.execute(_SQL)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _chat(chat_id: object) -> str:
    if (type(chat_id) is not str or not chat_id or "/" in chat_id
            or len(chat_id.encode("utf-8")) > _MAX_CHAT_BYTES):
        raise ValueError("invalid key rewrap chat")
    return chat_id


def _newcomers(value: object) -> dict[str, int]:
    if type(value) is not dict or not 1 <= len(value) <= MAX_NEWCOMERS:
        raise ValueError("invalid key rewrap newcomers")
    for name, joined in value.items():
        if (type(name) is not str or not _NAME.fullmatch(name)
                or type(joined) is not int or not 0 <= joined <= _MAX_SQLITE_INTEGER):
            raise ValueError("invalid key rewrap newcomers")
    return dict(sorted(value.items()))


def _epoch(value: object) -> int:
    if type(value) is not int or not -1 <= value <= _MAX_SQLITE_INTEGER:
        raise ValueError("invalid key rewrap epoch")
    return value


def add(conn: sqlite3.Connection, chat_id: str, newcomers: dict[str, int]) -> KeyRewrap:
    chat_id, newcomers = _chat(chat_id), _newcomers(newcomers)
    encoded = json.dumps(newcomers, separators=(",", ":"), sort_keys=True)
    with conn:
        (count,) = conn.execute("SELECT count(*) FROM key_rewraps").fetchone()
        if count >= MAX_PENDING:
            raise OverflowError("too many unfinished key rewraps")
        row_id = conn.execute(
            "INSERT INTO key_rewraps(chat_id,newcomers,after_epoch) VALUES(?,?,-1)",
            (chat_id, encoded)).lastrowid
    return KeyRewrap(row_id, chat_id, newcomers, -1)


def _decode(row) -> KeyRewrap:
    row_id, chat_id, encoded, after = row
    try:
        newcomers = json.loads(encoded) if type(encoded) is str else None
        return KeyRewrap(row_id, _chat(chat_id), _newcomers(newcomers), _epoch(after))
    except (ValueError, TypeError) as exc:
        raise ValueError("corrupt key rewrap row") from exc


def pending(conn: sqlite3.Connection, *, limit: int = 64,
            after_id: int = 0) -> tuple[KeyRewrap, ...]:
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError("invalid key rewrap limit")
    if type(after_id) is not int or after_id < 0:
        raise ValueError("invalid key rewrap cursor")
    rows = conn.execute(
        "SELECT id,chat_id,newcomers,after_epoch FROM key_rewraps "
        "WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
    return tuple(_decode(row) for row in rows)


def advance(conn: sqlite3.Connection, row_id: int, epoch: int) -> None:
    """Record that every epoch up to ``epoch`` is finished; never moves back."""
    epoch = _epoch(epoch)
    with conn:
        changed = conn.execute(
            "UPDATE key_rewraps SET after_epoch=? WHERE id=? AND after_epoch<=?",
            (epoch, row_id, epoch)).rowcount
        if changed != 1:
            raise ValueError("key rewrap row changed")


def finish(conn: sqlite3.Connection, row_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM key_rewraps WHERE id=?", (row_id,)).rowcount == 1


__all__ = ["KeyRewrap", "MAX_PENDING", "add", "advance", "finish", "initialize", "pending"]
