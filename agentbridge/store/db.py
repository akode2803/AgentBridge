"""Local SQLite store: message cache, incremental-read offsets, cursors, doc
cache, and the durable outbox table (backlog item "local caching", R3).

One database per (machine, mesh root) at ``~/.agentbridge/cache/``. WAL mode;
connections are per-thread. The cache is exactly that — a cache: it can be
deleted and rebuilt from the transport at any time. The OUTBOX rows are the
one exception: they hold unsent user data and are deleted only after the
transport confirms the send (the "no message ever lost" guarantee).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = ["Store", "OutboxItem"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  chat_id TEXT NOT NULL,
  id      TEXT NOT NULL,
  ns      INTEGER NOT NULL,
  sender  TEXT NOT NULL DEFAULT '',
  kind    TEXT NOT NULL DEFAULT 'message',
  payload TEXT NOT NULL,
  PRIMARY KEY(chat_id, id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ns ON messages(chat_id, ns);

CREATE TABLE IF NOT EXISTS log_offsets(
  chat_id  TEXT NOT NULL,
  log_name TEXT NOT NULL,
  offset   INTEGER NOT NULL,
  PRIMARY KEY(chat_id, log_name)
);

CREATE TABLE IF NOT EXISTS cursors(
  scope TEXT NOT NULL,
  key   TEXT NOT NULL,
  ns    INTEGER NOT NULL,
  PRIMARY KEY(scope, key)
);

CREATE TABLE IF NOT EXISTS docs(
  path       TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  fetched_ns INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox(
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  target     TEXT NOT NULL DEFAULT '',
  payload    TEXT NOT NULL,
  created_ns INTEGER NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  next_ns    INTEGER NOT NULL DEFAULT 0,
  lease_ns   INTEGER NOT NULL DEFAULT 0,
  state      TEXT NOT NULL DEFAULT 'pending',
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(state, next_ns, lease_ns);
"""


@dataclass
class OutboxItem:
    seq: int
    kind: str
    target: str
    payload: dict[str, Any]
    attempts: int


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -------------------------------------------------------- message cache
    def upsert_messages(self, chat_id: str, records: Iterable[dict]) -> list[dict]:
        """Idempotent by (chat_id, id) — replayed/duplicated transport records
        (shrunk-file re-reads, at-least-once outbox, own-message echoes)
        collapse here. Returns the records that were ACTUALLY NEW — the event
        pump publishes exactly these, so nothing ever notifies twice."""
        c = self._conn()
        inserted: list[dict] = []
        with c:
            for rec in records:
                rid, ns = rec.get("id"), rec.get("ns")
                if not rid or not isinstance(ns, int):
                    continue  # malformed record: transport-level tolerance
                cur = c.execute(
                    "INSERT OR IGNORE INTO messages(chat_id,id,ns,sender,kind,payload)"
                    " VALUES(?,?,?,?,?,?)",
                    (chat_id, rid, ns, rec.get("from", ""), rec.get("kind", "message"),
                     json.dumps(rec, ensure_ascii=False)),
                )
                if cur.rowcount:
                    inserted.append(rec)
        return inserted

    def messages(self, chat_id: str, after_ns: int = 0, limit: int | None = None) -> list[dict]:
        q = "SELECT payload FROM messages WHERE chat_id=? AND ns>? ORDER BY ns"
        args: list[Any] = [chat_id, after_ns]
        if limit is not None:
            q += " LIMIT ?"
            args.append(limit)
        return [json.loads(r[0]) for r in self._conn().execute(q, args)]

    def message_count(self, chat_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return int(row[0])

    def forget_chat(self, chat_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            c.execute("DELETE FROM log_offsets WHERE chat_id=?", (chat_id,))

    # ----------------------------------------------------- offsets & cursors
    def get_offset(self, chat_id: str, log_name: str) -> int:
        row = self._conn().execute(
            "SELECT offset FROM log_offsets WHERE chat_id=? AND log_name=?",
            (chat_id, log_name),
        ).fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, chat_id: str, log_name: str, offset: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO log_offsets(chat_id,log_name,offset) VALUES(?,?,?)"
                " ON CONFLICT(chat_id,log_name) DO UPDATE SET offset=excluded.offset",
                (chat_id, log_name, offset),
            )

    def get_cursor(self, scope: str, key: str) -> int:
        row = self._conn().execute(
            "SELECT ns FROM cursors WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, scope: str, key: str, ns: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO cursors(scope,key,ns) VALUES(?,?,?)"
                " ON CONFLICT(scope,key) DO UPDATE SET ns=excluded.ns",
                (scope, key, ns),
            )

    # -------------------------------------------------------------- doc cache
    def cache_doc(self, path: str, data: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET payload=excluded.payload,"
                " fetched_ns=excluded.fetched_ns",
                (path, json.dumps(data, ensure_ascii=False), time.time_ns()),
            )

    def cached_doc(self, path: str, default: Any = None) -> Any:
        row = self._conn().execute(
            "SELECT payload FROM docs WHERE path=?", (path,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    # ----------------------------------------------------------------- outbox
    def outbox_add(self, kind: str, target: str, payload: dict[str, Any]) -> int:
        """Enqueue BEFORE any send attempt — commit here is what makes a send
        crash-safe. Returns the queue sequence number."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                (kind, target, json.dumps(payload, ensure_ascii=False), time.time_ns()),
            )
            return int(cur.lastrowid)

    def outbox_claim_due(self, *, lease_s: float = 120.0, limit: int = 50) -> list[OutboxItem]:
        """Claim due pending items by taking a lease. A crash mid-send simply
        lets the lease expire, after which the item is claimable again."""
        now = time.time_ns()
        lease_until = now + int(lease_s * 1e9)
        items: list[OutboxItem] = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT seq,kind,target,payload,attempts FROM outbox"
                " WHERE state='pending' AND next_ns<=? AND lease_ns<=?"
                " ORDER BY seq LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for seq, kind, target, payload, attempts in rows:
                claimed = c.execute(
                    "UPDATE outbox SET lease_ns=? WHERE seq=? AND lease_ns<=?",
                    (lease_until, seq, now),
                ).rowcount
                if claimed:
                    items.append(OutboxItem(seq, kind, target, json.loads(payload), attempts))
        return items

    def outbox_done(self, seq: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM outbox WHERE seq=?", (seq,))

    def outbox_retry(self, seq: int, error: str, delay_s: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE outbox SET attempts=attempts+1, next_ns=?, lease_ns=0,"
                " last_error=? WHERE seq=?",
                (time.time_ns() + int(delay_s * 1e9), error[:500], seq),
            )

    def outbox_dead(self, seq: int, error: str) -> None:
        """Only for structurally unprocessable items (unknown kind, malformed
        payload). Transient failures NEVER go dead — they retry forever."""
        with self._conn() as c:
            c.execute(
                "UPDATE outbox SET state='dead', lease_ns=0, last_error=? WHERE seq=?",
                (error[:500], seq),
            )

    def outbox_counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT state, COUNT(*) FROM outbox GROUP BY state"
        ).fetchall()
        return {state: int(n) for state, n in rows}
