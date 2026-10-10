"""Dedicated shadow-node database and atomic raw-input admission owner."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from .admission import (
    MAX_ACTIVE_CANDIDATES, MAX_BATCH_DOCUMENTS, MAX_BATCH_FRONTIERS,
    MAX_BATCH_VISIBILITY,
    MAX_CHANGE_PAGE, MAX_CHANGE_ROWS, MAX_GENERATION_HISTORY,
    NodeCapture, NodeCaptureOverflow, NodeCaptureRequest, NodeChange,
    NodeChangePage, NodeDocument, NodeFrontier, NodeGenerationChanged,
    NodeInputBatch, NodeInputError, NodeLogPage, NodeLogRow, NodeScopePosition,
    detached_batch, detached_capture_request,
)
from .protocol import HEALTH_VALUES, PROTOCOL_VERSION, NodeStatus, ReplicaIdentity
from .security import private_directory, protect_path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_schema(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=2),
    protocol_version INTEGER NOT NULL CHECK(protocol_version=1)
);
CREATE TABLE IF NOT EXISTS node_identity(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    provider_endpoint TEXT NOT NULL,
    root TEXT NOT NULL,
    principal TEXT NOT NULL,
    machine TEXT NOT NULL,
    identity_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_meta(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    database_incarnation TEXT NOT NULL,
    admitted_generation INTEGER NOT NULL CHECK(admitted_generation>=0),
    health TEXT NOT NULL,
    last_attempt_ns INTEGER NOT NULL CHECK(last_attempt_ns>=0),
    last_success_ns INTEGER NOT NULL CHECK(last_success_ns>=0),
    pending_mutations INTEGER NOT NULL CHECK(pending_mutations>=0)
);
CREATE TABLE IF NOT EXISTS node_generations(
    generation INTEGER PRIMARY KEY CHECK(generation>0),
    base_generation INTEGER NOT NULL CHECK(base_generation>=0),
    state TEXT NOT NULL CHECK(state IN
        ('building','sealed','admitted','superseded','abandoned')),
    created_ns INTEGER NOT NULL CHECK(created_ns>=0),
    observed_ns INTEGER CHECK(observed_ns IS NULL OR observed_ns>=0),
    documents_mode TEXT CHECK(documents_mode IS NULL OR documents_mode IN ('delta','replace')),
    visibility_mode TEXT CHECK(visibility_mode IS NULL OR visibility_mode IN ('delta','replace')),
    frontiers_mode TEXT CHECK(frontiers_mode IS NULL OR frontiers_mode IN ('delta','replace')),
    document_count INTEGER NOT NULL DEFAULT 0 CHECK(document_count>=0),
    log_count INTEGER NOT NULL DEFAULT 0 CHECK(log_count>=0),
    visibility_count INTEGER NOT NULL DEFAULT 0 CHECK(visibility_count>=0),
    frontier_count INTEGER NOT NULL DEFAULT 0 CHECK(frontier_count>=0),
    total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_admitted_node_generation
    ON node_generations(state) WHERE state='admitted';
CREATE TABLE IF NOT EXISTS candidate_docs(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    path TEXT NOT NULL, seq INTEGER NOT NULL, deleted INTEGER NOT NULL
        CHECK(deleted IN (0,1)), payload BLOB,
    PRIMARY KEY(generation,path),
    CHECK((deleted=0 AND typeof(payload)='blob') OR (deleted=1 AND payload IS NULL))
);
CREATE TABLE IF NOT EXISTS candidate_log_rows(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    id INTEGER NOT NULL, chat_id TEXT NOT NULL, log_name TEXT NOT NULL,
    payload BLOB NOT NULL, PRIMARY KEY(generation,id)
);
CREATE TABLE IF NOT EXISTS candidate_visibility(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    chat_id TEXT NOT NULL, visible INTEGER NOT NULL CHECK(visible IN (0,1)),
    PRIMARY KEY(generation,chat_id)
);
CREATE TABLE IF NOT EXISTS candidate_frontiers(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    name TEXT NOT NULL, epoch TEXT, cursor INTEGER, minimum_cursor INTEGER,
    PRIMARY KEY(generation,name)
);
CREATE TABLE IF NOT EXISTS remote_docs(
    path TEXT PRIMARY KEY, seq INTEGER NOT NULL, deleted INTEGER NOT NULL
        CHECK(deleted IN (0,1)), payload BLOB,
    updated_generation INTEGER NOT NULL,
    CHECK((deleted=0 AND typeof(payload)='blob') OR (deleted=1 AND payload IS NULL))
);
CREATE TABLE IF NOT EXISTS remote_log_rows(
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, log_name TEXT NOT NULL,
    payload BLOB NOT NULL,
    updated_generation INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS remote_log_chat
    ON remote_log_rows(chat_id,log_name,id);
CREATE TABLE IF NOT EXISTS remote_chat_visibility(
    chat_id TEXT PRIMARY KEY,
    updated_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_frontiers(
    name TEXT PRIMARY KEY, epoch TEXT, cursor INTEGER, minimum_cursor INTEGER,
    updated_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scope_versions(
    scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation>=0), pending_reason TEXT,
    PRIMARY KEY(scope_kind,scope_id)
);
CREATE TABLE IF NOT EXISTS local_changes(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, generation INTEGER NOT NULL,
    scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_change_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    minimum_cursor INTEGER NOT NULL CHECK(minimum_cursor>=0)
);
"""

_V1_TABLES = frozenset({
    "node_schema", "node_identity", "node_meta", "node_generations",
    "remote_docs", "remote_log_rows", "remote_chat_visibility",
    "remote_frontiers", "scope_versions", "local_changes",
})
_V2_TABLES = frozenset({
    "node_schema", "node_identity", "node_meta", "node_generations",
    "candidate_docs", "candidate_log_rows", "candidate_visibility",
    "candidate_frontiers", "remote_docs", "remote_log_rows",
    "remote_chat_visibility", "remote_frontiers", "scope_versions",
    "local_changes", "local_change_state",
})
_REQUIRED_INDEXES = frozenset({"one_admitted_node_generation", "remote_log_chat"})


def _positive_ns(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise NodeInputError(f"invalid {name}")
    return value


def _chat_scope(path: str) -> str | None:
    pieces = path.split("/")
    return pieces[1] if len(pieces) >= 3 and pieces[0] == "chats" and pieces[1] else None


def _chat_selector_scope(path: str) -> str | None:
    pieces = path.split("/")
    return pieces[1] if len(pieces) >= 2 and pieces[0] == "chats" and pieces[1] else None


def _capture_size(value: str | bytes | None) -> int:
    if value is None:
        return 0
    return len(value if type(value) is bytes else value.encode("utf-8"))


class _CaptureBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, amount: int) -> None:
        if type(amount) is not int or amount < 0:
            raise sqlite3.DatabaseError("invalid capture size")
        if amount > self.limit - self.used:
            raise NodeCaptureOverflow("capture exceeds byte budget")
        self.used += amount


class NodeStore:
    """Own one raw provider replica without caching canonical authority."""

    def __init__(self, path: Path | str, identity: ReplicaIdentity) -> None:
        if type(identity) is not ReplicaIdentity:
            raise ValueError("invalid replica identity")
        supplied = Path(path)
        if supplied.is_symlink():
            raise sqlite3.DatabaseError("local node database cannot be a symlink")
        self.path = supplied.resolve()
        self.identity = identity
        private_directory(self.path.parent)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _wanted_identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.identity.provider_endpoint, self.identity.root,
            self.identity.principal, self.identity.machine, self.identity.digest,
        )

    def _check_identity(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT provider_endpoint,root,principal,machine,identity_digest "
            "FROM node_identity WHERE singleton=1").fetchone()
        if row != self._wanted_identity():
            raise sqlite3.DatabaseError("local node identity mismatch")

    @staticmethod
    def _check_meta(conn: sqlite3.Connection) -> tuple:
        rows = conn.execute(
            "SELECT database_incarnation,admitted_generation,health,"
            "last_attempt_ns,last_success_ns,pending_mutations "
            "FROM node_meta WHERE singleton=1").fetchall()
        if len(rows) != 1:
            raise sqlite3.DatabaseError("invalid local node metadata")
        return rows[0]

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
                "SELECT version,protocol_version FROM node_schema WHERE singleton=1"
        ).fetchall() != [(1, PROTOCOL_VERSION)]:
            raise sqlite3.DatabaseError("unsupported local node schema")
        self._check_identity(conn)
        meta = self._check_meta(conn)
        owned = ("node_generations", "remote_docs", "remote_log_rows",
                 "remote_chat_visibility", "remote_frontiers", "scope_versions",
                 "local_changes")
        if meta[1] != 0 or any(conn.execute(
                f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in owned):
            raise sqlite3.DatabaseError("cannot migrate nonempty inactive node schema")
        # executescript commits the validation transaction before running;
        # begin a new migration transaction inside the script itself.
        conn.executescript("""
            BEGIN IMMEDIATE;
            DROP INDEX IF EXISTS one_admitted_node_generation;
            DROP INDEX IF EXISTS remote_log_chat;
            DROP TABLE local_changes;
            DROP TABLE scope_versions;
            DROP TABLE remote_frontiers;
            DROP TABLE remote_chat_visibility;
            DROP TABLE remote_log_rows;
            DROP TABLE remote_docs;
            DROP TABLE node_generations;
            DROP TABLE node_schema;
        """ + _SCHEMA)
        conn.execute("INSERT INTO node_schema VALUES(1,?,?)", (
            SCHEMA_VERSION, PROTOCOL_VERSION))
        conn.execute("INSERT INTO local_change_state VALUES(1,0)")

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                tables = frozenset(row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"))
                fresh = not tables
                if fresh:
                    conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                    conn.execute("INSERT INTO node_schema VALUES(1,?,?)", (
                        SCHEMA_VERSION, PROTOCOL_VERSION))
                    conn.execute("INSERT INTO node_identity VALUES(1,?,?,?,?,?)",
                                 self._wanted_identity())
                    conn.execute(
                        "INSERT INTO node_meta VALUES(1,?,0,'inactive',0,0,0)",
                        (secrets.token_hex(16),),
                    )
                    conn.execute("INSERT INTO local_change_state VALUES(1,0)")
                elif tables == _V1_TABLES:
                    self._migrate_v1(conn)
                elif tables != _V2_TABLES:
                    raise sqlite3.DatabaseError("incomplete local node schema")
                if conn.execute(
                        "SELECT version,protocol_version FROM node_schema "
                        "WHERE singleton=1").fetchall() != [(
                            SCHEMA_VERSION, PROTOCOL_VERSION)]:
                    raise sqlite3.DatabaseError("unsupported local node schema")
                self._check_identity(conn)
                self._check_meta(conn)
                rows = conn.execute(
                    "SELECT minimum_cursor FROM local_change_state "
                    "WHERE singleton=1").fetchall()
                if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
                    raise sqlite3.DatabaseError("invalid local change state")
                indexes = frozenset(row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%'"))
                if not _REQUIRED_INDEXES.issubset(indexes):
                    raise sqlite3.DatabaseError("incomplete local node schema")
        protect_path(self.path, directory=False)
        if not self.path.is_file():
            raise sqlite3.DatabaseError("invalid local node database")

    def status(self, *, node_epoch: str, started_ns: int) -> NodeStatus:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT database_incarnation,admitted_generation,health,"
                "last_attempt_ns,last_success_ns,pending_mutations "
                "FROM node_meta WHERE singleton=1").fetchall()
        if len(rows) != 1:
            raise sqlite3.DatabaseError("invalid local node metadata")
        incarnation, generation, health, attempt, success, pending = rows[0]
        if (type(incarnation) is not str or not incarnation
                or type(generation) is not int or generation < 0
                or type(health) is not str or health not in HEALTH_VALUES
                or any(type(value) is not int or value < 0
                       for value in (attempt, success, pending))):
            raise sqlite3.DatabaseError("invalid local node metadata")
        return NodeStatus(
            protocol_version=PROTOCOL_VERSION, node_epoch=node_epoch,
            database_incarnation=incarnation, identity_digest=self.identity.digest,
            admitted_generation=generation, health=health, started_ns=started_ns,
            last_attempt_ns=attempt, last_success_ns=success,
            pending_mutations=pending,
        )

    def begin_refresh(self, *, attempted_ns: int,
                      expected_generation: int) -> int:
        """Reserve a unique refresh token against one admitted generation."""
        attempted = _positive_ns(attempted_ns, "refresh attempt time")
        if (type(expected_generation) is not int
                or not 0 <= expected_generation <= 2**63 - 1):
            raise NodeInputError("invalid expected node generation")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT admitted_generation,last_attempt_ns,pending_mutations "
                "FROM node_meta WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("invalid local node metadata")
            if row[0] != expected_generation:
                raise NodeGenerationChanged("admitted generation changed")
            token = max(attempted, row[1] + 1)
            if token > 2**63 - 1:
                raise NodeInputError("refresh attempt time exhausted")
            health = "pending" if row[2] else "catching_up"
            conn.execute(
                "UPDATE node_meta SET health=?,last_attempt_ns=? WHERE singleton=1",
                (health, token),
            )
        return token

    def begin_candidate(self, *, created_ns: int,
                        expected_generation: int | None = None,
                        expected_attempt_ns: int | None = None) -> int:
        """Commit one reclaimable building generation against the current cut."""
        created = _positive_ns(created_ns, "candidate creation time")
        if (expected_generation is not None
                and (type(expected_generation) is not int
                     or not 0 <= expected_generation <= 2**63 - 1)):
            raise NodeInputError("invalid expected node generation")
        if expected_attempt_ns is not None:
            _positive_ns(expected_attempt_ns, "expected refresh attempt")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            base = conn.execute(
                "SELECT admitted_generation,last_attempt_ns FROM node_meta "
                "WHERE singleton=1"
            ).fetchone()
            if base is None:
                raise sqlite3.DatabaseError("invalid local node metadata")
            if (expected_generation is not None
                    and base[0] != expected_generation):
                raise NodeGenerationChanged("admitted generation changed")
            if (expected_attempt_ns is not None
                    and base[1] != expected_attempt_ns):
                raise NodeGenerationChanged("node refresh changed")
            active = conn.execute(
                "SELECT count(*) FROM node_generations "
                "WHERE state IN ('building','sealed')").fetchone()[0]
            if active >= MAX_ACTIVE_CANDIDATES:
                raise NodeInputError("too many active node candidates")
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation),0)+1 FROM node_generations"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO node_generations(generation,base_generation,state,created_ns) "
                "VALUES(?,?,'building',?)", (generation, base[0], created))
            conn.execute(
                "UPDATE node_meta SET health='catching_up',last_attempt_ns="
                "MAX(last_attempt_ns,?) WHERE singleton=1", (created,))
        return generation

    def note_refresh_state(self, health: str, *, attempted_ns: int,
                           expected_generation: int | None = None,
                           expected_attempt_ns: int | None = None) -> str:
        """Publish source freshness without changing the admitted generation."""
        attempted = _positive_ns(attempted_ns, "refresh attempt time")
        if health not in ("ready", "catching_up", "degraded", "unavailable"):
            raise NodeInputError("invalid refresh health")
        if (expected_generation is not None
                and (type(expected_generation) is not int
                     or not 0 <= expected_generation <= 2**63 - 1)):
            raise NodeInputError("invalid expected node generation")
        if expected_attempt_ns is not None:
            _positive_ns(expected_attempt_ns, "expected refresh attempt")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT admitted_generation,pending_mutations,last_attempt_ns,health "
                "FROM node_meta "
                "WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("invalid local node metadata")
            if (expected_generation is not None
                    and row[0] != expected_generation):
                raise NodeGenerationChanged("admitted generation changed")
            if (expected_attempt_ns is not None
                    and row[2] != expected_attempt_ns):
                raise NodeGenerationChanged("node refresh changed")
            if expected_attempt_ns is None and attempted < row[2]:
                return row[3]
            effective = "pending" if row[1] else health
            changed = conn.execute(
                "UPDATE node_meta SET health=?,last_attempt_ns="
                "MAX(last_attempt_ns,?),last_success_ns=CASE WHEN ?='ready' "
                "THEN MAX(last_success_ns,?) ELSE last_success_ns END "
                "WHERE singleton=1",
                (effective, attempted, effective, attempted),
            ).rowcount
            if changed != 1:
                raise sqlite3.DatabaseError("invalid local node metadata")
        return effective

    def seal_candidate(self, generation: int, batch: NodeInputBatch, *,
                       observed_ns: int) -> None:
        if type(generation) is not int or generation <= 0:
            raise NodeInputError("invalid candidate generation")
        batch = detached_batch(batch)
        observed = _positive_ns(observed_ns, "candidate observation time")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM node_generations WHERE generation=?",
                (generation,)).fetchone()
            if row != ("building",):
                raise NodeGenerationChanged("candidate is not building")
            conn.executemany(
                "INSERT INTO candidate_docs VALUES(?,?,?,?,?)",
                ((generation, value.path, value.seq, int(value.deleted), value.payload)
                 for value in batch.documents),
            )
            conn.executemany(
                "INSERT INTO candidate_log_rows VALUES(?,?,?,?,?)",
                ((generation, value.id, value.chat_id, value.log_name, value.payload)
                 for value in batch.log_rows),
            )
            conn.executemany(
                "INSERT INTO candidate_visibility VALUES(?,?,?)",
                ((generation, value.chat_id, int(value.visible))
                 for value in batch.visibility),
            )
            conn.executemany(
                "INSERT INTO candidate_frontiers VALUES(?,?,?,?,?)",
                ((generation, value.name, value.epoch, value.cursor,
                  value.minimum_cursor) for value in batch.frontiers),
            )
            changed = conn.execute(
                "UPDATE node_generations SET state='sealed',observed_ns=?,"
                "documents_mode=?,visibility_mode=?,frontiers_mode=?,"
                "document_count=?,log_count=?,visibility_count=?,frontier_count=?,"
                "total_bytes=? WHERE generation=? AND state='building'",
                (observed, batch.documents_mode, batch.visibility_mode,
                 batch.frontiers_mode, len(batch.documents), len(batch.log_rows),
                 len(batch.visibility), len(batch.frontiers), batch.byte_size,
                 generation),
            ).rowcount
            if changed != 1:
                raise NodeGenerationChanged("candidate changed while sealing")

    @staticmethod
    def _candidate_row(conn: sqlite3.Connection, generation: int) -> tuple:
        row = conn.execute(
            "SELECT base_generation,state,observed_ns,documents_mode,"
            "visibility_mode,frontiers_mode FROM node_generations "
            "WHERE generation=?", (generation,)).fetchone()
        if row is None:
            raise NodeGenerationChanged("unknown candidate generation")
        return row

    @staticmethod
    def _changed_document_paths(conn: sqlite3.Connection, generation: int,
                                mode: str) -> tuple[str, ...]:
        if mode == "delta":
            query = """
                SELECT c.path FROM candidate_docs c
                LEFT JOIN remote_docs r ON r.path=c.path
                WHERE c.generation=? AND (r.path IS NULL OR r.seq!=c.seq
                    OR r.deleted!=c.deleted OR r.payload IS NOT c.payload)
                ORDER BY c.path
            """
            return tuple(row[0] for row in conn.execute(query, (generation,)))
        query = """
            SELECT path FROM (
                SELECT c.path AS path FROM candidate_docs c
                LEFT JOIN remote_docs r ON r.path=c.path
                WHERE c.generation=? AND (r.path IS NULL OR r.seq!=c.seq
                    OR r.deleted!=c.deleted OR r.payload IS NOT c.payload)
                UNION
                SELECT r.path AS path FROM remote_docs r
                LEFT JOIN candidate_docs c ON c.generation=? AND c.path=r.path
                WHERE c.path IS NULL
            ) ORDER BY path
        """
        return tuple(row[0] for row in conn.execute(query, (generation, generation)))

    @staticmethod
    def _changed_visibility(conn: sqlite3.Connection, generation: int,
                            mode: str) -> tuple[str, ...]:
        if mode == "delta":
            query = """
                SELECT c.chat_id FROM candidate_visibility c
                LEFT JOIN remote_chat_visibility r ON r.chat_id=c.chat_id
                WHERE c.generation=? AND ((c.visible=1 AND r.chat_id IS NULL)
                    OR (c.visible=0 AND r.chat_id IS NOT NULL))
                ORDER BY c.chat_id
            """
            return tuple(row[0] for row in conn.execute(query, (generation,)))
        query = """
            SELECT chat_id FROM (
                SELECT c.chat_id AS chat_id FROM candidate_visibility c
                LEFT JOIN remote_chat_visibility r ON r.chat_id=c.chat_id
                WHERE c.generation=? AND c.visible=1 AND r.chat_id IS NULL
                UNION
                SELECT r.chat_id AS chat_id FROM remote_chat_visibility r
                LEFT JOIN candidate_visibility c
                    ON c.generation=? AND c.chat_id=r.chat_id AND c.visible=1
                WHERE c.chat_id IS NULL
            ) ORDER BY chat_id
        """
        return tuple(row[0] for row in conn.execute(query, (generation, generation)))

    @staticmethod
    def _frontiers_changed(conn: sqlite3.Connection, generation: int,
                           mode: str) -> bool:
        changed = conn.execute("""
            SELECT 1 FROM candidate_frontiers c
            LEFT JOIN remote_frontiers r ON r.name=c.name
            WHERE c.generation=? AND (r.name IS NULL OR r.epoch IS NOT c.epoch
                OR r.cursor IS NOT c.cursor OR r.minimum_cursor IS NOT c.minimum_cursor)
            LIMIT 1
        """, (generation,)).fetchone()
        if changed is not None:
            return True
        return mode == "replace" and conn.execute("""
            SELECT 1 FROM remote_frontiers r
            LEFT JOIN candidate_frontiers c ON c.generation=? AND c.name=r.name
            WHERE c.name IS NULL LIMIT 1
        """, (generation,)).fetchone() is not None

    @staticmethod
    def _check_candidate_caps(conn: sqlite3.Connection, generation: int,
                              documents_mode: str, visibility_mode: str,
                              frontiers_mode: str) -> None:
        if documents_mode == "delta":
            total = conn.execute("""
                SELECT count(*) FROM (
                    SELECT path FROM remote_docs
                    UNION SELECT path FROM candidate_docs WHERE generation=?
                )
            """, (generation,)).fetchone()[0]
            if total > MAX_BATCH_DOCUMENTS:
                raise NodeInputError("admitted documents exceed node budget")
        if visibility_mode == "delta":
            total = conn.execute("""
                SELECT count(*) FROM (
                    SELECT r.chat_id FROM remote_chat_visibility r
                    LEFT JOIN candidate_visibility c
                        ON c.generation=? AND c.chat_id=r.chat_id
                    WHERE c.chat_id IS NULL OR c.visible=1
                    UNION SELECT chat_id FROM candidate_visibility
                        WHERE generation=? AND visible=1
                )
            """, (generation, generation)).fetchone()[0]
            if total > MAX_BATCH_VISIBILITY:
                raise NodeInputError("admitted visibility exceeds node budget")
        if frontiers_mode == "delta":
            total = conn.execute("""
                SELECT count(*) FROM (
                    SELECT name FROM remote_frontiers
                    UNION SELECT name FROM candidate_frontiers WHERE generation=?
                )
            """, (generation,)).fetchone()[0]
            if total > MAX_BATCH_FRONTIERS:
                raise NodeInputError("admitted frontiers exceed node budget")

    @staticmethod
    def _apply_candidate(conn: sqlite3.Connection, generation: int,
                         documents_mode: str, visibility_mode: str,
                         frontiers_mode: str) -> None:
        if documents_mode == "replace":
            conn.execute("DELETE FROM remote_docs")
        conn.execute("""
            INSERT INTO remote_docs(path,seq,deleted,payload,updated_generation)
            SELECT path,seq,deleted,payload,? FROM candidate_docs
            WHERE generation=?
            ON CONFLICT(path) DO UPDATE SET seq=excluded.seq,
                deleted=excluded.deleted,payload=excluded.payload,
                updated_generation=excluded.updated_generation
        """, (generation, generation))
        conflict = conn.execute("""
            SELECT c.id FROM candidate_log_rows c JOIN remote_log_rows r ON r.id=c.id
            WHERE c.generation=? AND (r.chat_id!=c.chat_id OR r.log_name!=c.log_name
                OR r.payload!=c.payload) LIMIT 1
        """, (generation,)).fetchone()
        if conflict is not None:
            raise NodeInputError("log row identity conflict")
        conn.execute("""
            INSERT OR IGNORE INTO remote_log_rows(
                id,chat_id,log_name,payload,updated_generation)
            SELECT id,chat_id,log_name,payload,? FROM candidate_log_rows
            WHERE generation=?
        """, (generation, generation))
        if visibility_mode == "replace":
            conn.execute("DELETE FROM remote_chat_visibility")
        conn.execute("""
            INSERT INTO remote_chat_visibility(chat_id,updated_generation)
            SELECT chat_id,? FROM candidate_visibility
            WHERE generation=? AND visible=1
            ON CONFLICT(chat_id) DO UPDATE SET
                updated_generation=excluded.updated_generation
        """, (generation, generation))
        if visibility_mode == "delta":
            conn.execute("""
                DELETE FROM remote_chat_visibility WHERE chat_id IN (
                    SELECT chat_id FROM candidate_visibility
                    WHERE generation=? AND visible=0)
            """, (generation,))
        if frontiers_mode == "replace":
            conn.execute("DELETE FROM remote_frontiers")
        conn.execute("""
            INSERT INTO remote_frontiers(
                name,epoch,cursor,minimum_cursor,updated_generation)
            SELECT name,epoch,cursor,minimum_cursor,? FROM candidate_frontiers
            WHERE generation=?
            ON CONFLICT(name) DO UPDATE SET epoch=excluded.epoch,
                cursor=excluded.cursor,minimum_cursor=excluded.minimum_cursor,
                updated_generation=excluded.updated_generation
        """, (generation, generation))

    @staticmethod
    def _record_changes(conn: sqlite3.Connection, generation: int,
                        scopes: set[tuple[str, str]]) -> None:
        # The root position is the conservative whole-replica fence even when
        # the replay journal can retain a narrower chat-only wake.
        if scopes:
            conn.execute("""
                INSERT INTO scope_versions(scope_kind,scope_id,generation,pending_reason)
                VALUES('root','',?,NULL) ON CONFLICT(scope_kind,scope_id) DO UPDATE SET
                    generation=excluded.generation,pending_reason=NULL
            """, (generation,))
        for kind, ident in sorted(scopes):
            conn.execute("""
                INSERT INTO scope_versions(scope_kind,scope_id,generation,pending_reason)
                VALUES(?,?,?,NULL) ON CONFLICT(scope_kind,scope_id) DO UPDATE SET
                    generation=excluded.generation,pending_reason=NULL
            """, (kind, ident, generation))
            conn.execute(
                "INSERT INTO local_changes(generation,scope_kind,scope_id,kind) "
                "VALUES(?,?,?,'inputs')", (generation, kind, ident))
        newest = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM local_changes").fetchone()[0]
        cutoff = max(0, newest - MAX_CHANGE_ROWS)
        if cutoff:
            conn.execute("DELETE FROM local_changes WHERE seq<=?", (cutoff,))
            conn.execute("""
                UPDATE local_change_state SET minimum_cursor=MAX(minimum_cursor,?)
                WHERE singleton=1
            """, (cutoff,))

    @staticmethod
    def _clear_candidate_rows(conn: sqlite3.Connection, generation: int) -> None:
        for table in ("candidate_docs", "candidate_log_rows",
                      "candidate_visibility", "candidate_frontiers"):
            conn.execute(f"DELETE FROM {table} WHERE generation=?", (generation,))

    @staticmethod
    def _prune_generation_history(conn: sqlite3.Connection) -> None:
        cutoff = conn.execute(
            "SELECT COALESCE(MAX(generation),0)-? FROM node_generations",
            (MAX_GENERATION_HISTORY,)).fetchone()[0]
        if cutoff > 0:
            conn.execute("""
                DELETE FROM node_generations WHERE generation<=?
                AND state IN ('superseded','abandoned')
            """, (cutoff,))

    def admit_candidate(self, generation: int, *, health: str = "ready",
                        expected_attempt_ns: int | None = None) -> int:
        """Atomically publish every staged family or retain the previous cut."""
        if type(generation) is not int or generation <= 0:
            raise NodeInputError("invalid candidate generation")
        if health not in ("ready", "catching_up"):
            raise NodeInputError("invalid admitted generation health")
        if expected_attempt_ns is not None:
            _positive_ns(expected_attempt_ns, "expected refresh attempt")
        stale: str | None = None
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            (base, state, observed, documents_mode, visibility_mode,
             frontiers_mode) = self._candidate_row(conn, generation)
            if state != "sealed" or observed is None:
                raise NodeGenerationChanged("candidate is not sealed")
            current = conn.execute(
                "SELECT admitted_generation,last_attempt_ns FROM node_meta "
                "WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise sqlite3.DatabaseError("invalid local node metadata")
            if current[0] != base:
                conn.execute(
                    "UPDATE node_generations SET state='abandoned' "
                    "WHERE generation=?", (generation,))
                self._clear_candidate_rows(conn, generation)
                stale = "candidate base generation changed"
            elif (expected_attempt_ns is not None
                  and current[1] != expected_attempt_ns):
                conn.execute(
                    "UPDATE node_generations SET state='abandoned' "
                    "WHERE generation=?", (generation,))
                self._clear_candidate_rows(conn, generation)
                stale = "candidate refresh changed"
            else:
                self._check_candidate_caps(
                    conn, generation, documents_mode, visibility_mode,
                    frontiers_mode)
                doc_paths = self._changed_document_paths(
                    conn, generation, documents_mode)
                visible = self._changed_visibility(
                    conn, generation, visibility_mode)
                log_chats = tuple(row[0] for row in conn.execute("""
                    SELECT DISTINCT c.chat_id FROM candidate_log_rows c
                    LEFT JOIN remote_log_rows r ON r.id=c.id
                    WHERE c.generation=? AND r.id IS NULL ORDER BY c.chat_id
                """, (generation,)))
                scopes: set[tuple[str, str]] = {
                    ("chat", chat) for chat in (*visible, *log_chats)
                }
                for path in doc_paths:
                    chat = _chat_scope(path)
                    scopes.add(("chat", chat) if chat else ("root", ""))
                if self._frontiers_changed(conn, generation, frontiers_mode):
                    scopes.add(("root", ""))
                self._apply_candidate(
                    conn, generation, documents_mode, visibility_mode,
                    frontiers_mode)
                if base:
                    changed = conn.execute(
                        "UPDATE node_generations SET state='superseded' "
                        "WHERE generation=? AND state='admitted'", (base,)
                    ).rowcount
                    if changed != 1:
                        raise sqlite3.DatabaseError("admitted generation is inconsistent")
                changed = conn.execute(
                    "UPDATE node_generations SET state='admitted' "
                    "WHERE generation=? AND state='sealed'", (generation,)
                ).rowcount
                if changed != 1:
                    raise NodeGenerationChanged("candidate changed during admission")
                conn.execute(
                    "UPDATE node_meta SET admitted_generation=?,health=?,"
                    "last_success_ns=? WHERE singleton=1",
                    (generation, health, observed),
                )
                self._record_changes(conn, generation, scopes)
                self._clear_candidate_rows(conn, generation)
                self._prune_generation_history(conn)
        if stale:
            raise NodeGenerationChanged(stale)
        return generation

    def abandon_candidate(self, generation: int, *, health: str = "degraded",
                          expected_generation: int | None = None,
                          expected_attempt_ns: int | None = None) -> bool:
        if type(generation) is not int or generation <= 0:
            raise NodeInputError("invalid candidate generation")
        if health not in ("degraded", "unavailable"):
            raise NodeInputError("invalid candidate failure health")
        if (expected_generation is not None
                and (type(expected_generation) is not int
                     or not 0 <= expected_generation <= 2**63 - 1)):
            raise NodeInputError("invalid expected node generation")
        if expected_attempt_ns is not None:
            _positive_ns(expected_attempt_ns, "expected refresh attempt")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""
                UPDATE node_generations SET state='abandoned'
                WHERE generation=? AND state IN ('building','sealed')
            """, (generation,)).rowcount
            if changed:
                self._clear_candidate_rows(conn, generation)
                if expected_generation is None and expected_attempt_ns is None:
                    conn.execute(
                        "UPDATE node_meta SET health=? WHERE singleton=1", (health,))
                else:
                    conn.execute(
                        "UPDATE node_meta SET health=? WHERE singleton=1 "
                        "AND (? IS NULL OR admitted_generation=?) "
                        "AND (? IS NULL OR last_attempt_ns=?)",
                        (health, expected_generation, expected_generation,
                         expected_attempt_ns, expected_attempt_ns),
                    )
                self._prune_generation_history(conn)
        return bool(changed)

    def reclaim_candidates(self, *, before_ns: int) -> int:
        before = _positive_ns(before_ns, "candidate reclaim time")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            generations = tuple(row[0] for row in conn.execute("""
                SELECT generation FROM node_generations
                WHERE state IN ('building','sealed') AND created_ns<?
                ORDER BY generation
            """, (before,)))
            for generation in generations:
                self._clear_candidate_rows(conn, generation)
            if generations:
                conn.executemany(
                    "UPDATE node_generations SET state='abandoned' WHERE generation=?",
                    ((value,) for value in generations),
                )
                self._prune_generation_history(conn)
        return len(generations)

    @staticmethod
    def _capture_documents(conn: sqlite3.Connection,
                           request: NodeCaptureRequest,
                           budget: _CaptureBudget) -> tuple[NodeDocument, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if request.exact_document_paths:
            clauses.append("path IN (" + ",".join(
                "?" for _ in request.exact_document_paths) + ")")
            params.extend(request.exact_document_paths)
        for prefix in request.document_prefixes:
            if prefix:
                # BINARY range selection is case-sensitive and segment exact.
                # '/' sorts immediately before '0', so this selects only
                # descendants beginning with ``prefix + '/'``.
                clauses.append("path>=? AND path<?")
                params.extend((prefix + "/", prefix + "0"))
            else:
                clauses.append("1=1")
        if not clauses:
            return ()
        where = " OR ".join(f"({clause})" for clause in clauses)
        sizes = conn.execute(
            "SELECT length(CAST(path AS BLOB)),COALESCE(length(payload),0) "
            "FROM remote_docs WHERE " + where + " ORDER BY path LIMIT ?",
            (*params, request.max_documents + 1),
        )
        count = 0
        for path_bytes, payload_bytes in sizes:
            if count >= request.max_documents:
                raise NodeCaptureOverflow("document capture exceeds row budget")
            budget.add(path_bytes + payload_bytes + 24)
            count += 1
        rows = conn.execute(
            "SELECT path,seq,deleted,payload FROM remote_docs WHERE " + where
            + " ORDER BY path LIMIT ?", (*params, request.max_documents)).fetchall()
        return tuple(NodeDocument(path, seq, bool(deleted), payload)
                     for path, seq, deleted, payload in rows)

    @staticmethod
    def _capture_log_pages(conn: sqlite3.Connection,
                           request: NodeCaptureRequest,
                           budget: _CaptureBudget) -> tuple[NodeLogPage, ...]:
        pages = []
        total = 0
        for wanted in request.log_requests:
            ids = tuple(row[0] for row in conn.execute("""
                SELECT id FROM remote_log_rows
                WHERE chat_id=? AND log_name=? AND id>? ORDER BY id LIMIT ?
            """, (wanted.chat_id, wanted.log_name, wanted.after_id,
                  wanted.limit + 1)))
            has_more = len(ids) > wanted.limit
            ids = ids[:wanted.limit]
            total += len(ids)
            if total > request.max_log_rows:
                raise NodeCaptureOverflow("log capture exceeds row budget")
            if ids:
                size = conn.execute("""
                    SELECT COALESCE(SUM(length(CAST(chat_id AS BLOB))
                        +length(CAST(log_name AS BLOB))+length(payload)+24),0)
                    FROM remote_log_rows WHERE chat_id=? AND log_name=?
                        AND id>? AND id<=?
                """, (wanted.chat_id, wanted.log_name, wanted.after_id,
                      ids[-1])).fetchone()[0]
                budget.add(size)
                rows = conn.execute("""
                    SELECT id,chat_id,log_name,payload FROM remote_log_rows
                    WHERE chat_id=? AND log_name=? AND id>? AND id<=?
                    ORDER BY id
                """, (wanted.chat_id, wanted.log_name, wanted.after_id,
                      ids[-1])).fetchall()
            else:
                rows = ()
            values = tuple(NodeLogRow(*row) for row in rows)
            pages.append(NodeLogPage(
                wanted, values, values[-1].id if values else wanted.after_id,
                has_more,
            ))
        return tuple(pages)

    @staticmethod
    def _capture_frontiers(conn: sqlite3.Connection,
                           request: NodeCaptureRequest,
                           budget: _CaptureBudget) -> tuple[NodeFrontier, ...]:
        if request.frontier_names:
            where = "WHERE name IN (" + ",".join(
                "?" for _ in request.frontier_names) + ")"
            params = request.frontier_names
        else:
            where = ""
            params = ()
        sizes = conn.execute(
            "SELECT length(CAST(name AS BLOB)),"
            "COALESCE(length(CAST(epoch AS BLOB)),0) FROM remote_frontiers "
            + where + " ORDER BY name LIMIT 129", params)
        count = 0
        for name_bytes, epoch_bytes in sizes:
            if count >= MAX_BATCH_FRONTIERS:
                raise NodeCaptureOverflow("frontier capture exceeds row budget")
            budget.add(name_bytes + epoch_bytes + 32)
            count += 1
        rows = conn.execute(
            "SELECT name,epoch,cursor,minimum_cursor FROM remote_frontiers "
            + where + " ORDER BY name LIMIT 128", params).fetchall()
        return tuple(NodeFrontier(*row) for row in rows)

    @staticmethod
    def _capture_visibility(conn: sqlite3.Connection,
                            request: NodeCaptureRequest,
                            budget: _CaptureBudget) -> tuple[tuple[str, ...], bool]:
        if not request.include_visibility:
            return (), False
        rows = []
        cursor = conn.execute("""
            SELECT chat_id FROM remote_chat_visibility WHERE chat_id>?
            ORDER BY chat_id LIMIT ?
        """, (request.visibility_after, request.visibility_limit + 1))
        for index, (chat_id,) in enumerate(cursor):
            if index >= request.visibility_limit:
                return tuple(rows), True
            budget.add(_capture_size(chat_id) + 8)
            rows.append(chat_id)
        return tuple(rows), False

    @staticmethod
    def _capture_scope_positions(
        conn: sqlite3.Connection, scope_keys: set[tuple[str, str]],
        budget: _CaptureBudget,
    ) -> tuple[NodeScopePosition, ...]:
        """Read bounded scope metadata without one query per visible chat."""
        found: dict[tuple[str, str], tuple[int, str | None]] = {}
        for kind, ident in scope_keys:
            budget.add(_capture_size(kind) + _capture_size(ident) + 24)
        pending_bytes = conn.execute("""
            SELECT COALESCE(length(CAST(pending_reason AS BLOB)),0)
            FROM scope_versions WHERE scope_kind='root' AND scope_id=''
        """).fetchone()
        if pending_bytes is not None:
            budget.add(pending_bytes[0])
        root = conn.execute("""
            SELECT generation,pending_reason FROM scope_versions
            WHERE scope_kind='root' AND scope_id=''
        """).fetchone()
        if root is not None:
            found[("root", "")] = root
        chats = sorted(ident for kind, ident in scope_keys if kind == "chat")
        # Stay well below SQLite's default host-parameter ceiling.
        for start in range(0, len(chats), 500):
            chunk = chats[start:start + 500]
            sizes = conn.execute(
                "SELECT COALESCE(length(CAST(pending_reason AS BLOB)),0) "
                "FROM scope_versions WHERE scope_kind='chat' AND scope_id IN ("
                + ",".join("?" for _ in chunk) + ")", chunk)
            for (size,) in sizes:
                budget.add(size)
            rows = conn.execute(
                "SELECT scope_id,generation,pending_reason FROM scope_versions "
                "WHERE scope_kind='chat' AND scope_id IN ("
                + ",".join("?" for _ in chunk) + ")", chunk).fetchall()
            found.update({("chat", ident): (generation, pending)
                          for ident, generation, pending in rows})
        return tuple(NodeScopePosition(
            kind, ident, found.get((kind, ident), (0, None))[0],
            found.get((kind, ident), (0, None))[1],
        ) for kind, ident in sorted(scope_keys))

    def capture(self, request: NodeCaptureRequest) -> NodeCapture:
        """Capture all requested families from one SQLite read transaction."""
        request = detached_capture_request(request)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            meta = conn.execute(
                "SELECT database_incarnation,admitted_generation,health,"
                "last_success_ns FROM node_meta WHERE singleton=1").fetchone()
            if meta is None:
                raise sqlite3.DatabaseError("invalid local node metadata")
            incarnation, generation, health, last_success = meta
            if (request.expected_generation is not None
                    and request.expected_generation != generation):
                raise NodeGenerationChanged("admitted generation changed")
            budget = _CaptureBudget(request.max_bytes)
            documents = self._capture_documents(conn, request, budget)
            log_pages = self._capture_log_pages(conn, request, budget)
            visibility, visibility_more = self._capture_visibility(
                conn, request, budget)
            frontiers = self._capture_frontiers(conn, request, budget)
            scope_keys = {("root", "")}
            scope_keys.update(("chat", value.chat_id) for value in request.log_requests)
            scope_keys.update(("chat", chat) for chat in visibility)
            for selector in request.exact_document_paths + request.document_prefixes:
                chat = _chat_selector_scope(selector)
                if chat:
                    scope_keys.add(("chat", chat))
            scope_keys.update(("chat", chat) for chat in (
                _chat_scope(value.path) for value in documents) if chat)
            scope_positions = self._capture_scope_positions(
                conn, scope_keys, budget)
            conn.commit()
        return NodeCapture(
            incarnation, generation, health, last_success, documents, log_pages,
            visibility, visibility[-1] if visibility else request.visibility_after,
            visibility_more, frontiers, scope_positions, budget.used,
        )

    def capture_changes(self, *, database_incarnation: str, after_cursor: int,
                        limit: int = MAX_CHANGE_PAGE) -> NodeChangePage:
        if type(database_incarnation) is not str or not database_incarnation:
            raise NodeInputError("invalid change database incarnation")
        if type(after_cursor) is not int or after_cursor < 0:
            raise NodeInputError("invalid change cursor")
        if type(limit) is not int or not 1 <= limit <= MAX_CHANGE_PAGE:
            raise NodeInputError("invalid change page limit")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            current_incarnation = conn.execute(
                "SELECT database_incarnation FROM node_meta WHERE singleton=1"
            ).fetchone()
            minimum = conn.execute(
                "SELECT minimum_cursor FROM local_change_state WHERE singleton=1"
            ).fetchone()
            current = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM local_changes").fetchone()[0]
            if current_incarnation is None or minimum is None:
                raise sqlite3.DatabaseError("invalid local change state")
            reset = (database_incarnation != current_incarnation[0]
                     or after_cursor < minimum[0] or after_cursor > current)
            if reset:
                rows = ()
                status = "reset_required"
                cursor = after_cursor
                has_more = False
            else:
                raw = conn.execute("""
                    SELECT seq,generation,scope_kind,scope_id,kind
                    FROM local_changes WHERE seq>? ORDER BY seq LIMIT ?
                """, (after_cursor, limit + 1)).fetchall()
                has_more = len(raw) > limit
                rows = tuple(NodeChange(*row) for row in raw[:limit])
                cursor = rows[-1].seq if rows else after_cursor
                status = "ok"
            conn.commit()
        return NodeChangePage(
            status, current_incarnation[0], minimum[0], current, cursor, rows,
            has_more,
        )
