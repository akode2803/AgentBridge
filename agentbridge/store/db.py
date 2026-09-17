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

from . import log_position, send_status, overlay_index, page_inputs, membership_suffix, terminal_observation
from .log_position import LogPosition
from .chat_inputs import LocalChatInputs, capture as capture_chat_inputs
from . import (
    document_observation, lifecycle_heads, membership_input_position,
    shadow_chat_inputs, shadow_slot,
)
from .shadow_chat_inputs import ShadowChatInputs
from .document_observation import (
    DocumentObservation,
    DocumentObservationConflict,
    DocumentPosition,
)
from .lifecycle_heads import LifecycleHeadPosition
from .membership_input_position import MembershipInputPosition

__all__ = [
    "Store", "OutboxItem", "LogIngestionConflict", "DocumentObservation",
    "DocumentObservationConflict", "DocumentPosition", "LifecycleHeadPosition",
    "MembershipInputPosition",
]


class LogIngestionConflict(RuntimeError):
    """The local log position changed while transport bytes were being read."""

_INIT_LOCKS: dict[Path, threading.Lock] = {}
_INIT_LOCKS_GUARD = threading.Lock()


def _store_init_lock(path: Path) -> threading.Lock:
    key = path.resolve()
    with _INIT_LOCKS_GUARD:
        return _INIT_LOCKS.setdefault(key, threading.Lock())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  chat_id TEXT NOT NULL,
  id      TEXT NOT NULL,
  ns      INTEGER NOT NULL,
  sender  TEXT NOT NULL DEFAULT '',
  kind    TEXT NOT NULL DEFAULT 'message',
  payload TEXT NOT NULL,
  observed_ns INTEGER NOT NULL DEFAULT 0,
  observed_mono INTEGER NOT NULL DEFAULT 0,
  observed_clock TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS claims(
  scope TEXT NOT NULL,
  key   TEXT NOT NULL,
  ns    INTEGER NOT NULL,
  PRIMARY KEY(scope, key)
);
CREATE INDEX IF NOT EXISTS idx_claims_ns ON claims(ns);

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
        # Pin aliases and relative paths before opening any thread's connection.
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with _store_init_lock(self.path):
            self._initialize()

    def _initialize(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            additions = {
                "observed_ns": "INTEGER NOT NULL DEFAULT 0",
                "observed_mono": "INTEGER NOT NULL DEFAULT 0",
                "observed_clock": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in additions.items():
                columns = {row[1] for row in c.execute(
                    "PRAGMA table_info(messages)")}
                if name in columns:
                    continue
                try:
                    c.execute(
                        f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
                columns = {row[1] for row in c.execute(
                    "PRAGMA table_info(messages)")}
                if name not in columns:
                    raise sqlite3.OperationalError(
                        f"message schema migration did not create {name}")
        send_status.initialize(self._conn())
        log_position.initialize(self._conn())
        membership_input_position.initialize(self._conn())
        lifecycle_heads.initialize(self._conn())
        document_observation.initialize(self._conn())
        overlay_index.initialize(self._conn())
        shadow_slot.initialize(self._conn())

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

    def claim_once(self, scope: str, key: str, ns: int) -> bool:
        """Atomically and durably claim one replay-sensitive record."""
        c = self._conn()
        with c:
            cur = c.execute(
                "INSERT OR IGNORE INTO claims(scope,key,ns) VALUES(?,?,?)",
                (scope, key, int(ns)),
            )
        return cur.rowcount == 1

    def claim_with_doc(self, scope: str, key: str, ns: int,
                       path: str, data: Any) -> bool:
        """Atomically claim replay-sensitive work and create its journal.

        A caller may safely retry work whose journal says PREPARED. Once it
        records EXECUTING, recovery must settle ambiguity instead of invoking
        the external provider again.
        """
        c = self._conn()
        with c:
            cur = c.execute(
                "INSERT OR IGNORE INTO claims(scope,key,ns) VALUES(?,?,?)",
                (scope, key, int(ns)),
            )
            if cur.rowcount:
                c.execute(
                    "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)"
                    " ON CONFLICT(path) DO NOTHING",
                    (path, json.dumps(data, ensure_ascii=False), time.time_ns()),
                )
        return cur.rowcount == 1

    def prune_claims(self, before_ns: int) -> int:
        c = self._conn()
        with c:
            cur = c.execute("DELETE FROM claims WHERE ns < ?", (int(before_ns),))
        return cur.rowcount

    # -------------------------------------------------------- message cache
    def upsert_messages(self, chat_id: str, records: Iterable[dict], *,
                        observed_ns: int | None = None,
                        observed_mono: int | None = None,
                        observed_clock: str = "") -> list[dict]:
        """Idempotent by (chat_id, id) — replayed/duplicated transport records
        (shrunk-file re-reads, at-least-once outbox, own-message echoes)
        collapse here. Returns the records that were ACTUALLY NEW — the event
        pump publishes exactly these, so nothing ever notifies twice."""
        c = self._conn()
        with c:
            return self._insert_messages(
                c, chat_id, records, observed_ns=observed_ns,
                observed_mono=observed_mono, observed_clock=observed_clock)

    @staticmethod
    def _insert_messages(c: sqlite3.Connection, chat_id: str,
                         records: Iterable[dict], *,
                         observed_ns: int | None = None,
                         observed_mono: int | None = None,
                         observed_clock: str = "") -> list[dict]:
        """Insert inside the caller's transaction, without committing it."""
        inserted: list[dict] = []
        observed = int(observed_ns if observed_ns is not None else time.time_ns())
        observed_tick = int(
            observed_mono if observed_mono is not None else time.perf_counter_ns())
        for rec in records:
            rid, ns = rec.get("id"), rec.get("ns")
            if not rid or not isinstance(ns, int):
                continue  # malformed record: transport-level tolerance
            cur = c.execute(
                "INSERT OR IGNORE INTO messages(chat_id,id,ns,sender,kind,payload,"
                "observed_ns,observed_mono,observed_clock) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (chat_id, rid, ns, rec.get("from", ""), rec.get("kind", "message"),
                 json.dumps(rec, ensure_ascii=False), observed, observed_tick,
                 str(observed_clock or "")[:80]),
            )
            if cur.rowcount:
                inserted.append(rec)
        return inserted

    def capture_log_position(self, chat_id: str, log_name: str) -> LogPosition:
        return log_position.capture(self._conn(), self.path, chat_id, log_name)

    def ingest_log(self, chat_id: str, log_name: str, expected_position: LogPosition,
                   new_offset: int, records: Iterable[dict], *,
                   observed_ns: int | None = None,
                   observed_mono: int | None = None,
                   observed_clock: str = "") -> list[dict]:
        """Commit consumed records and log position together before notifying.

        Durable position comparison rejects concurrent scans and chat resets.
        It is not a complete projection revision or an authorization token.
        """
        if type(expected_position) is not LogPosition:
            raise TypeError("ingestion requires a captured LogPosition")
        if type(new_offset) is not int or new_offset < 0:
            raise ValueError("log offsets must be nonnegative integers")
        c = self._conn()
        # BEGIN must fail outside the rollback scope if a caller already owns
        # this connection's transaction; never roll back somebody else's work.
        c.execute("BEGIN IMMEDIATE")
        with c:
            if self.capture_log_position(chat_id, log_name) != expected_position:
                raise LogIngestionConflict("log position changed during read")
            inserted = self._insert_messages(
                c, chat_id, records, observed_ns=observed_ns,
                observed_mono=observed_mono, observed_clock=observed_clock)
            c.execute(
                "INSERT INTO log_offsets(chat_id,log_name,offset) VALUES(?,?,?)"
                " ON CONFLICT(chat_id,log_name) DO UPDATE SET offset=excluded.offset",
                (chat_id, log_name, new_offset),
            )
        return inserted

    def message_observation(
        self, chat_id: str, message_id: str,
    ) -> tuple[int, int, str]:
        row = self._conn().execute(
            "SELECT observed_ns,observed_mono,observed_clock FROM messages "
            "WHERE chat_id=? AND id=?",
            (chat_id, message_id),
        ).fetchone()
        return (int(row[0]), int(row[1]), str(row[2])) if row else (0, 0, "")

    def messages(self, chat_id: str, after_ns: int = 0, limit: int | None = None) -> list[dict]:
        q = (
            "SELECT payload FROM messages WHERE chat_id=? AND ns>? "
            "ORDER BY ns,sender,id"
        )
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

    def state_events_after(self, chat_id: str, ns: int) -> list[dict]:
        """Locally ingested chat-state events newer than a materialized fold.

        Reaction info rows are notification breadcrumbs, not chat state. Keep
        them out here so one reaction cannot force every later snapshot down
        the reconciliation path.
        """
        rows = self._conn().execute(
            "SELECT payload FROM messages"
            " WHERE chat_id=? AND kind='info' AND ns>?"
            " AND coalesce(json_extract(payload, '$.event.type'), '') != 'reaction'"
            " ORDER BY ns,sender,id",
            (chat_id, int(ns)),
        )
        return [json.loads(row[0]) for row in rows]

    def prepare_membership_suffix_index(self):
        return membership_suffix.initialize(self._conn())

    def capture_membership_suffix(self, chat_id, after_ns, **limits):
        return membership_suffix.capture(self.path, chat_id, after_ns, **limits)

    def capture_membership_input_position(
        self, chat_id: str,
    ) -> MembershipInputPosition:
        return membership_input_position.observe(self.path, chat_id)

    def membership_input_position_matches(
        self, expected: MembershipInputPosition,
    ) -> bool:
        return membership_input_position.matches(self.path, expected)

    def forget_chat(self, chat_id: str) -> None:
        with self._conn() as c:
            log_position.reset_chat(c, chat_id)
            c.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            c.execute("DELETE FROM log_offsets WHERE chat_id=?", (chat_id,))

    # ----------------------------------------------------- offsets & cursors
    def get_offset(self, chat_id: str, log_name: str) -> int:
        row = self._conn().execute(
            "SELECT offset FROM log_offsets WHERE chat_id=? AND log_name=?",
            (chat_id, log_name),
        ).fetchone()
        return int(row[0]) if row else 0

    def capture_chat_inputs(self, chat_id: str, *, document_paths: tuple[str, ...] = (),
                            max_messages: int = 100_000, max_logs: int = 10_000,
                            max_bytes: int = 64 * 1024 * 1024) -> LocalChatInputs:
        return capture_chat_inputs(
            self.path, chat_id, document_paths=document_paths,
            max_messages=max_messages, max_logs=max_logs, max_bytes=max_bytes)

    def inspect_shadow_position(self) -> shadow_slot.ShadowPosition:
        return shadow_slot.inspect_position(self.path)

    def acquire_shadow(self, expected: shadow_slot.ShadowPosition,
                       publisher_nonce: str,
                       source: shadow_slot.ShadowSource) -> shadow_slot.ShadowPosition:
        return shadow_slot.acquire(self._conn(), self.path, expected, publisher_nonce, source)

    def publish_shadow(self, expected: shadow_slot.ShadowPosition,
                       snapshot: shadow_slot.ShadowSnapshot, *,
                       max_documents: int = shadow_slot.MAX_RECORDS,
                       max_chat_ids: int = shadow_slot.MAX_RECORDS,
                       max_bytes: int = shadow_slot.MAX_BYTES) -> shadow_slot.ShadowPosition:
        return shadow_slot.publish(
            self._conn(), self.path, expected, snapshot,
            max_documents=max_documents, max_chat_ids=max_chat_ids, max_bytes=max_bytes)

    def retire_shadow(self, expected: shadow_slot.ShadowPosition) -> shadow_slot.ShadowPosition:
        return shadow_slot.retire(self._conn(), self.path, expected)

    def capture_shadow(self, expected: shadow_slot.ShadowPosition, *,
                       max_documents: int = shadow_slot.MAX_RECORDS,
                       max_chat_ids: int = shadow_slot.MAX_RECORDS,
                       max_bytes: int = shadow_slot.MAX_BYTES) -> shadow_slot.ShadowObservation:
        return shadow_slot.capture(self.path, expected, max_documents=max_documents,
                                   max_chat_ids=max_chat_ids, max_bytes=max_bytes)

    def capture_shadow_chat_inputs(
        self, expected_shadow: shadow_slot.ShadowPosition, chat_id: str, *,
        max_documents: int = shadow_slot.MAX_RECORDS,
        max_chat_ids: int = shadow_slot.MAX_RECORDS,
        max_messages: int = shadow_chat_inputs.MAX_MESSAGES,
        max_logs: int = shadow_chat_inputs.MAX_LOGS,
        max_bytes: int = shadow_slot.MAX_BYTES,
    ) -> ShadowChatInputs:
        return shadow_chat_inputs.capture(
            self.path, expected_shadow, chat_id, max_documents=max_documents,
            max_chat_ids=max_chat_ids, max_messages=max_messages,
            max_logs=max_logs, max_bytes=max_bytes,
        )

    def prepare_page_input_index(self):
        """Explicit background preparation; never called by a chat read."""
        page_inputs.initialize(self._conn())

    def capture_page_inputs(self, index, **selection):
        return page_inputs.capture(self.path, index, **selection)

    def publish_overlay_index(self, prepared, *, shared_source=False):
        return overlay_index.publish(self._conn(), self.path, prepared, shared_source=shared_source)

    def capture_overlay_index(self, expected, targets, state_paths=(), *,
                              max_rows=2048, max_bytes=overlay_index.MAX_SELECT_BYTES, include_reactions=False):
        return overlay_index.capture(self.path, expected, targets, state_paths,
                                     max_rows=max_rows, max_bytes=max_bytes, include_reactions=include_reactions)

    def verify_overlay_signature(self, expected, document_path, public_key, *,
                                 max_bytes=16 * 1024 * 1024):
        return overlay_index.verify_signature(
            self._conn(), self.path, expected, document_path, public_key, max_bytes=max_bytes,
        )

    def capture_overlay_proofs(self, expected, keys):
        return overlay_index.proofs(self.path, expected, keys)

    def capture_document_position(self, source_id: str) -> DocumentPosition:
        return document_observation.capture_position(self.path, source_id)

    def observe_lifecycle_head(self, subject: str) -> LifecycleHeadPosition:
        return lifecycle_heads.observe(self.path, subject)

    def publish_lifecycle_head(
        self, expected: LifecycleHeadPosition, proposed: dict,
    ) -> bool:
        return lifecycle_heads.publish(self._conn(), self.path, expected, proposed)

    def publish_document_batch(
        self, expected_position: DocumentPosition, documents: dict, *,
        cursor: int, deleted_paths: tuple[str, ...] = (), full: bool = False,
        retain_tombstones: bool = True, skip_unchanged: bool = False,
        max_documents: int = 100_000, max_bytes: int = 64 * 1024 * 1024,
    ) -> DocumentPosition:
        return document_observation.publish(
            self._conn(), self.path, expected_position, documents,
            cursor=cursor, deleted_paths=deleted_paths, full=full,
            retain_tombstones=retain_tombstones, skip_unchanged=skip_unchanged,
            max_documents=max_documents, max_bytes=max_bytes,
        )

    def invalidate_document_observation(
        self, expected_position: DocumentPosition,
    ) -> DocumentPosition:
        return document_observation.invalidate(self._conn(), self.path, expected_position)

    def capture_selected_documents(
        self, expected_position: DocumentPosition, document_paths: tuple[str, ...],
        *, max_documents: int = 256, max_bytes: int = 1024 * 1024,
    ) -> DocumentObservation:
        return document_observation.capture_selected(
            self.path, expected_position, document_paths,
            max_documents=max_documents, max_bytes=max_bytes,
        )

    def reset_document_observation(
        self, expected_position: DocumentPosition,
    ) -> DocumentPosition:
        return document_observation.reset(
            self._conn(), self.path, expected_position,
        )

    def capture_document_observation(
        self, source_id: str, *, max_documents: int = 100_000,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> DocumentObservation:
        return document_observation.capture_observation(
            self.path, source_id, max_documents=max_documents,
            max_bytes=max_bytes,
        )

    def set_offset(self, chat_id: str, log_name: str, offset: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO log_offsets(chat_id,log_name,offset) VALUES(?,?,?)"
                " ON CONFLICT(chat_id,log_name) DO UPDATE SET offset=excluded.offset",
                (chat_id, log_name, offset),
            )

    def log_offsets(self, chat_id: str) -> dict[str, int]:
        """Content-free local read frontier for every writer log in a chat."""
        rows = self._conn().execute(
            "SELECT log_name,offset FROM log_offsets WHERE chat_id=? ORDER BY log_name",
            (chat_id,),
        )
        return {str(name): int(offset) for name, offset in rows}

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

    def replace_cached_doc_if(self, path: str, expected: dict[str, Any],
                              data: Any) -> bool:
        """Compare-and-swap one local journal under a write transaction."""
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload FROM docs WHERE path=?", (path,),
            ).fetchone()
            current = json.loads(row[0]) if row else None
            if (not isinstance(current, dict)
                    or any(current.get(key) != value
                           for key, value in expected.items())):
                c.rollback()
                return False
            c.execute(
                "UPDATE docs SET payload=?,fetched_ns=? WHERE path=?",
                (json.dumps(data, ensure_ascii=False), time.time_ns(), path),
            )
            c.commit()
            return True
        except Exception:
            c.rollback()
            raise

    # ----------------------------------------------------------------- outbox
    def prepare_terminal_observation(self):
        return terminal_observation.initialize(self._conn())

    def refresh_terminal_observation(self, target, **limits):
        return terminal_observation.refresh(self._conn(), self.path, target, **limits)

    def capture_terminal_observation(self, target, **selection):
        return terminal_observation.capture(self.path, target, **selection)

    def outbox_add(self, kind: str, target: str, payload: dict[str, Any]) -> int:
        """Enqueue BEFORE any send attempt — commit here is what makes a send
        crash-safe. Returns the queue sequence number."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                (kind, target, json.dumps(payload, ensure_ascii=False), time.time_ns()),
            )
            return int(cur.lastrowid)

    def cache_and_outbox_add(
        self, chat_id: str, record: dict[str, Any],
        kind: str, target: str, outbox_payload: dict[str, Any],
        *, observed_ns: int | None = None, observed_clock: str = "",
        observed_mono: int | None = None, client_ref: str = "",
    ) -> int:
        """Atomically publish the optimistic cache row and durable send intent."""
        rid, ns = record.get("id"), record.get("ns")
        if not rid or not isinstance(ns, int):
            raise ValueError("message record needs id and integer ns")
        with self._conn() as c:
            observed = int(
                observed_ns if observed_ns is not None else time.time_ns())
            observed_tick = int(
                observed_mono if observed_mono is not None
                else time.perf_counter_ns())
            c.execute(
                "INSERT OR IGNORE INTO messages(chat_id,id,ns,sender,kind,payload,"
                "observed_ns,observed_mono,observed_clock) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (chat_id, rid, ns, record.get("from", ""),
                 record.get("kind", "message"),
                 json.dumps(record, ensure_ascii=False), observed, observed_tick,
                 str(observed_clock or "")[:80]),
            )
            cur = c.execute(
                "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                (kind, target, json.dumps(outbox_payload, ensure_ascii=False),
                 time.time_ns()),
            )
            seq = int(cur.lastrowid)
            if kind == "append_log" and record.get("kind") == "message":
                c.execute("INSERT INTO local_send_status "
                          "(chat_id,message_id,outbox_seq,state,client_ref) "
                          "VALUES(?,?,?,'queued',?)",
                          (chat_id, rid, seq, client_ref))
            return seq

    def cache_doc_and_outbox_add(
        self, path: str, data: Any, kind: str, target: str,
        payload: dict[str, Any],
    ) -> int:
        """Atomically persist local recovery state and its remote send intent."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET payload=excluded.payload,"
                " fetched_ns=excluded.fetched_ns",
                (path, json.dumps(data, ensure_ascii=False), time.time_ns()),
            )
            cur = c.execute(
                "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                (kind, target, json.dumps(payload, ensure_ascii=False),
                 time.time_ns()),
            )
            return int(cur.lastrowid)

    def cache_docs_and_outbox_add_many(
        self, docs: dict[str, Any],
        rows: list[tuple[str, str, dict[str, Any]]],
    ) -> list[int]:
        """Atomically persist related recovery maps and remote send intents."""
        with self._conn() as c:
            fetched_ns = time.time_ns()
            for path, data in docs.items():
                c.execute(
                    "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)"
                    " ON CONFLICT(path) DO UPDATE SET payload=excluded.payload,"
                    " fetched_ns=excluded.fetched_ns",
                    (path, json.dumps(data, ensure_ascii=False), fetched_ns),
                )
            seqs = []
            for kind, target, payload in rows:
                cur = c.execute(
                    "INSERT INTO outbox(kind,target,payload,created_ns)"
                    " VALUES(?,?,?,?)",
                    (kind, target, json.dumps(payload, ensure_ascii=False),
                     time.time_ns()),
                )
                seqs.append(int(cur.lastrowid))
            return seqs

    def outbox_claim_due(self, *, lease_s: float = 120.0, limit: int = 50) -> list[OutboxItem]:
        """Claim due pending items by taking a lease. A crash mid-send simply
        lets the lease expire, after which the item is claimable again."""
        now = time.time_ns()
        lease_until = now + int(lease_s * 1e9)
        items: list[OutboxItem] = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT o.seq,o.kind,o.target,o.payload,o.attempts FROM outbox o"
                " WHERE o.state='pending' AND o.next_ns<=? AND o.lease_ns<=?"
                " AND NOT EXISTS (SELECT 1 FROM outbox earlier"
                " WHERE earlier.state='pending' AND earlier.target=o.target"
                " AND earlier.seq<o.seq) ORDER BY o.seq LIMIT ?",
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
            c.execute("UPDATE local_send_status SET state='sent',accepted_ns=? "
                      "WHERE outbox_seq=?", (time.time_ns(), seq))
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
            c.execute("UPDATE local_send_status SET state='failed' "
                      "WHERE outbox_seq=?", (seq,))
            c.execute(
                "UPDATE outbox SET state='dead', lease_ns=0, last_error=? WHERE seq=?",
                (error[:500], seq),
            )

    def send_statuses(self, chat_id: str, ids: Iterable[str]) -> dict:
        return send_status.capture(self._conn(), chat_id, ids)

    def outbox_counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT state, COUNT(*) FROM outbox GROUP BY state"
        ).fetchall()
        return {state: int(n) for state, n in rows}

    def outbox_payloads(
        self, *, target: str | None = None, state: str | None = None,
    ) -> list[dict[str, Any]]:
        """Manifest ownership scan used only by bounded local spool cleanup."""
        out = []
        query = "SELECT payload FROM outbox"
        clauses = []
        args: list[str] = []
        if target is not None:
            clauses.append("target=?")
            args.append(target)
        if state is not None:
            clauses.append("state=?")
            args.append(state)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        for (payload,) in self._conn().execute(query, args):
            try:
                value = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    def outbox_prune_dead(
        self, *, max_age_s: float = 30 * 86400.0, max_rows: int = 100
    ) -> list[OutboxItem]:
        """Bound inspectable structural failures; pending sends are untouched."""
        now = time.time_ns()
        cutoff = now - int(max_age_s * 1e9)
        removed: list[OutboxItem] = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT seq,kind,target,payload,attempts,created_ns FROM outbox"
                " WHERE state='dead' ORDER BY created_ns DESC,seq DESC"
            ).fetchall()
            kept = 0
            for seq, kind, target, payload, attempts, created_ns in rows:
                if kept < max_rows and int(created_ns) >= cutoff:
                    kept += 1
                    continue
                try:
                    parsed = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    parsed = {}
                removed.append(OutboxItem(
                    int(seq), str(kind), str(target),
                    parsed if isinstance(parsed, dict) else {}, int(attempts)))
                c.execute("DELETE FROM outbox WHERE seq=?", (seq,))
        return removed
