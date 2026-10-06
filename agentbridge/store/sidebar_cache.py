"""Bounded display-only sidebar presentations in the local SQLite cache.

Rows are last successfully finalized canonical presentations for one viewer.
They make startup paint cheap; they never authorize an action or continue a
membership verdict. Every mutation and transcript read still uses a fresh
canonical operation. The Store itself is already scoped by root, user and
machine; the explicit viewer column prevents accidental cross-session reuse.
"""
from __future__ import annotations

import json
import sqlite3

MAX_ROWS = 128
MAX_ROW_BYTES = 256 * 1024
MAX_BYTES = 4 * 1024 * 1024

_SCHEMA = """CREATE TABLE IF NOT EXISTS sidebar_presentations(
  viewer TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_ns INTEGER NOT NULL,
  PRIMARY KEY(viewer, chat_id)
)"""


class SidebarCacheUnavailable(RuntimeError):
    pass


def initialize(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(_SCHEMA)


def _identity(value: str, label: str) -> str:
    if (type(value) is not str or not value or len(value.encode()) > 256
            or "\x00" in value):
        raise ValueError(f"invalid sidebar {label}")
    return value


def _encoded(chat_id: str, row: dict) -> str:
    chat_id = _identity(chat_id, "chat")
    if type(row) is not dict or row.get("id") != chat_id:
        raise ValueError("sidebar row identity mismatch")
    payload = json.dumps(row, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":"))
    if len(payload.encode()) > MAX_ROW_BYTES:
        raise OverflowError("sidebar row exceeds cache budget")
    return payload


def publish(conn: sqlite3.Connection, viewer: str, chat_id: str,
            row: dict | None, *, updated_ns: int) -> bool:
    """Replace/delete one display row. Return whether visible bytes changed."""
    viewer, chat_id = _identity(viewer, "viewer"), _identity(chat_id, "chat")
    if type(updated_ns) is not int or not 0 <= updated_ns <= 2**63 - 1:
        raise ValueError("invalid sidebar update time")
    with conn:
        prior = conn.execute(
            "SELECT payload FROM sidebar_presentations WHERE viewer=? AND chat_id=?",
            (viewer, chat_id),
        ).fetchone()
        if row is None:
            if prior is None:
                return False
            conn.execute(
                "DELETE FROM sidebar_presentations WHERE viewer=? AND chat_id=?",
                (viewer, chat_id),
            )
            return True
        payload = _encoded(chat_id, row)
        if prior == (payload,):
            return False
        conn.execute(
            "INSERT INTO sidebar_presentations(viewer,chat_id,payload,updated_ns) "
            "VALUES(?,?,?,?) ON CONFLICT(viewer,chat_id) DO UPDATE SET "
            "payload=excluded.payload,updated_ns=excluded.updated_ns",
            (viewer, chat_id, payload, updated_ns),
        )
        return True


def capture(path, viewer: str, *, allowed_ids: frozenset[str] | None = None) -> list[dict]:
    """Read one bounded viewer cache; optional inventory filtering is display-only."""
    viewer = _identity(viewer, "viewer")
    if allowed_ids is not None:
        if (type(allowed_ids) is not frozenset or len(allowed_ids) > MAX_ROWS
                or any(_identity(chat, "chat") != chat for chat in allowed_ids)):
            raise ValueError("invalid sidebar inventory")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
    try:
        rows = conn.execute(
            "SELECT chat_id,payload FROM sidebar_presentations "
            "WHERE viewer=? ORDER BY updated_ns DESC,chat_id LIMIT ?",
            (viewer, MAX_ROWS + 1),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise SidebarCacheUnavailable("sidebar cache unavailable") from exc
    finally:
        conn.close()
    if len(rows) > MAX_ROWS:
        raise SidebarCacheUnavailable("sidebar cache row budget")
    used, out = 0, []
    for chat_id, payload in rows:
        if (type(chat_id) is not str or type(payload) is not str
                or allowed_ids is not None and chat_id not in allowed_ids):
            if allowed_ids is not None and type(chat_id) is str:
                continue
            raise SidebarCacheUnavailable("invalid sidebar cache row")
        used += len(chat_id.encode()) + len(payload.encode())
        if used > MAX_BYTES:
            raise SidebarCacheUnavailable("sidebar cache byte budget")
        try:
            value = json.loads(payload)
        except (TypeError, ValueError, RecursionError) as exc:
            raise SidebarCacheUnavailable("invalid sidebar cache payload") from exc
        if type(value) is not dict or value.get("id") != chat_id:
            raise SidebarCacheUnavailable("sidebar cache identity mismatch")
        out.append(value)
    return out


def prune(conn: sqlite3.Connection, viewer: str, allowed_ids: frozenset[str]) -> int:
    """Drop rows absent from one successfully captured local inventory."""
    viewer = _identity(viewer, "viewer")
    if (type(allowed_ids) is not frozenset or len(allowed_ids) > MAX_ROWS
            or any(_identity(chat, "chat") != chat for chat in allowed_ids)):
        raise ValueError("invalid sidebar inventory")
    with conn:
        if allowed_ids:
            placeholders = ",".join("?" for _ in allowed_ids)
            removed = conn.execute(
                "DELETE FROM sidebar_presentations WHERE viewer=? "
                f"AND chat_id NOT IN ({placeholders})",
                (viewer, *allowed_ids),
            ).rowcount
        else:
            removed = conn.execute(
                "DELETE FROM sidebar_presentations WHERE viewer=?", (viewer,)
            ).rowcount
        remaining = conn.execute(
            "SELECT chat_id FROM sidebar_presentations WHERE viewer=? LIMIT ?",
            (viewer, MAX_ROWS + 1),
        ).fetchall()
        if len(remaining) > MAX_ROWS:
            raise SidebarCacheUnavailable("sidebar cache row budget")
    return removed
