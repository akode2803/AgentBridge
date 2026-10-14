"""Signed membership events waiting on their meta.json compare-and-swap.

Under a bound typed writer a membership action changes ``meta.json`` (the
chat's materialized state and provider ACL) first and logs its signed info
events only once that write applied. A row here bridges the two: it holds the
exact envelopes and the operation id of the attempt that would make them true,
written before the attempt is dispatched. Settling a row commits its envelopes
to the outbox (in the same Store transaction that deletes the row) or drops
them. A row is never membership or authority on its own.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from uuid import UUID

MAX_PENDING = 256
MAX_ENVELOPES = 2048
MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
_MAX_CHAT_BYTES = 1024
_MAX_SQLITE_INTEGER = 2**63 - 1
_SQL = (
    "CREATE TABLE membership_actions("
    "id INTEGER PRIMARY KEY,"
    "chat_id TEXT NOT NULL,"
    "operation_id TEXT NOT NULL UNIQUE,"
    "envelopes TEXT NOT NULL)"
)
_ID = re.compile(r"[\x21-\x7e]{1,256}\Z", re.ASCII)


@dataclass(frozen=True)
class MembershipAction:
    id: int
    chat_id: str
    operation_id: str
    envelopes: tuple[dict, ...]


def initialize(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise sqlite3.OperationalError("cannot initialize membership actions in a transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='membership_actions'").fetchone() is None:
            conn.execute(_SQL)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _chat(chat_id: object) -> str:
    if (type(chat_id) is not str or not chat_id or "/" in chat_id
            or len(chat_id.encode("utf-8")) > _MAX_CHAT_BYTES):
        raise ValueError("invalid membership action chat")
    return chat_id


def _operation(value: object) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ValueError("invalid membership action operation") from None
    return value


def _envelopes(value: object) -> tuple[dict, ...]:
    if type(value) not in (list, tuple) or not 1 <= len(value) <= MAX_ENVELOPES:
        raise ValueError("invalid membership action envelopes")
    for env in value:
        if (type(env) is not dict or env.get("kind") != "info"
                or type(env.get("event")) is not dict
                or type(env.get("id")) is not str or not _ID.fullmatch(env["id"])
                or type(env.get("ns")) is not int
                or not 0 < env["ns"] <= _MAX_SQLITE_INTEGER
                or type(env.get("from")) is not str or not env["from"]):
            raise ValueError("invalid membership action envelopes")
    if len({env["id"] for env in value}) != len(value):
        raise ValueError("invalid membership action envelopes")
    return tuple(value)


def _encode(envelopes: tuple[dict, ...]) -> str:
    try:
        encoded = json.dumps(list(envelopes), ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("invalid membership action envelopes") from None
    if len(encoded.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise ValueError("membership action is too large")
    return encoded


def add(conn: sqlite3.Connection, chat_id: str, operation_id: str,
        envelopes) -> MembershipAction:
    chat_id, operation_id = _chat(chat_id), _operation(operation_id)
    envelopes = _envelopes(envelopes)
    encoded = _encode(envelopes)
    # Inside the caller's transaction: the row must be durable before the
    # attempt it names is prepared in the journal.
    (count,) = conn.execute("SELECT count(*) FROM membership_actions").fetchone()
    if count >= MAX_PENDING:
        raise OverflowError("too many unsettled membership actions")
    row_id = conn.execute(
        "INSERT INTO membership_actions(chat_id,operation_id,envelopes) VALUES(?,?,?)",
        (chat_id, operation_id, encoded)).lastrowid
    return MembershipAction(row_id, chat_id, operation_id, envelopes)


def _decode(row) -> MembershipAction:
    row_id, chat_id, operation_id, encoded = row
    try:
        envelopes = json.loads(encoded) if type(encoded) is str else None
        return MembershipAction(row_id, _chat(chat_id), _operation(operation_id),
                                _envelopes(envelopes))
    except (ValueError, TypeError) as exc:
        raise ValueError("corrupt membership action row") from exc


def pending(conn: sqlite3.Connection, *, chat_id: str | None = None,
            limit: int = 64, after_id: int = 0) -> tuple[MembershipAction, ...]:
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError("invalid membership action limit")
    if type(after_id) is not int or after_id < 0:
        raise ValueError("invalid membership action cursor")
    if chat_id is None:
        rows = conn.execute(
            "SELECT id,chat_id,operation_id,envelopes FROM membership_actions "
            "WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,chat_id,operation_id,envelopes FROM membership_actions "
            "WHERE chat_id=? AND id>? ORDER BY id LIMIT ?",
            (_chat(chat_id), after_id, limit)).fetchall()
    return tuple(_decode(row) for row in rows)


def discard(conn: sqlite3.Connection, row_id: int) -> bool:
    """Drop a row whose attempt is known not to have applied."""
    with conn:
        return conn.execute("DELETE FROM membership_actions WHERE id=?",
                            (row_id,)).rowcount == 1


def take(conn: sqlite3.Connection, action: MembershipAction) -> None:
    """Delete the exact row inside the caller's open transaction."""
    if conn.execute(
            "DELETE FROM membership_actions WHERE id=? AND operation_id=?",
            (action.id, action.operation_id)).rowcount != 1:
        raise ValueError("membership action changed")


__all__ = ["MAX_PENDING", "MembershipAction", "add", "discard", "initialize",
           "pending", "take"]
