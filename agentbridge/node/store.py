"""Dedicated shadow-node database and atomic raw-input admission owner."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from .admission import (
    MAX_ACTIVE_CANDIDATES, MAX_BATCH_FRONTIERS, MAX_STAGED_BYTES,
    MAX_STAGED_CHUNKS, MAX_STAGED_DOCUMENTS, MAX_STAGED_LOG_ROWS,
    MAX_STAGED_VISIBILITY,
    MAX_CHANGE_PAGE, MAX_CHANGE_ROWS, MAX_GENERATION_HISTORY,
    NodeCapture, NodeCaptureOverflow, NodeCaptureRequest, NodeChange,
    NodeChangePage, NodeDocument, NodeFrontier, NodeGenerationChanged,
    NodeInputBatch, NodeInputError, NodeLogPage, NodeLogRow, NodeScopePosition,
    NodeVisibility,
    NodeProviderCut, NodeRecoveryPlan, NodeRecoveryScope, NodeRecoveryWork,
    NodeRecoveryEvent, NodeRecoveryEventState, NodeRecoveryManifestState,
    NodeRecoveryState, NodeRecoveryStreamHead, NodeRecoveryStreamState,
    NodeRecoveryWorkState, MAX_RECOVERY_EVENTS, MAX_RECOVERY_MANIFEST_PAGE,
    MAX_RECOVERY_METADATA_BYTES, MAX_RECOVERY_OPAQUE_BYTES, MAX_RECOVERY_STREAMS,
    detached_batch, detached_capture_request,
)
from .protocol import HEALTH_VALUES, PROTOCOL_VERSION, NodeStatus, ReplicaIdentity
from .security import private_directory, protect_path

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_schema(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=6),
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
    logs_mode TEXT CHECK(logs_mode IS NULL OR logs_mode IN ('delta','replace')),
    visibility_mode TEXT CHECK(visibility_mode IS NULL OR visibility_mode IN ('delta','replace')),
    frontiers_mode TEXT CHECK(frontiers_mode IS NULL OR frontiers_mode IN ('delta','replace')),
    document_count INTEGER NOT NULL DEFAULT 0 CHECK(document_count>=0),
    log_count INTEGER NOT NULL DEFAULT 0 CHECK(log_count>=0),
    visibility_count INTEGER NOT NULL DEFAULT 0 CHECK(visibility_count>=0),
    frontier_count INTEGER NOT NULL DEFAULT 0 CHECK(frontier_count>=0),
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK(chunk_count>=0),
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
CREATE TABLE IF NOT EXISTS candidate_chunks(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    chunk_id TEXT NOT NULL, digest TEXT NOT NULL,
    document_count INTEGER NOT NULL CHECK(document_count>=0),
    log_count INTEGER NOT NULL CHECK(log_count>=0),
    visibility_count INTEGER NOT NULL CHECK(visibility_count>=0),
    frontier_count INTEGER NOT NULL CHECK(frontier_count>=0),
    total_bytes INTEGER NOT NULL CHECK(total_bytes>=0),
    PRIMARY KEY(generation,chunk_id)
);
CREATE TABLE IF NOT EXISTS provider_recoveries(
    recovery_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL UNIQUE REFERENCES node_generations(generation),
    database_incarnation TEXT NOT NULL, identity_digest TEXT NOT NULL,
    base_generation INTEGER NOT NULL CHECK(base_generation>=0),
    health_owner_token INTEGER NOT NULL CHECK(health_owner_token>=0),
    start_cursor INTEGER NOT NULL CHECK(start_cursor>=0),
    replay_cursor INTEGER NOT NULL CHECK(replay_cursor>=0),
    schema_version INTEGER NOT NULL CHECK(schema_version>=1), index_epoch TEXT NOT NULL,
    source_epoch TEXT NOT NULL, account_id TEXT NOT NULL, role TEXT NOT NULL,
    minimum_cursor INTEGER NOT NULL CHECK(minimum_cursor>=0),
    start_target_cursor INTEGER NOT NULL CHECK(start_target_cursor>=0),
    target_cursor INTEGER NOT NULL CHECK(target_cursor>=0), state TEXT NOT NULL
        CHECK(state IN ('building','sealed')),
    proof_version INTEGER NOT NULL CHECK(proof_version=1),
    proof_revision INTEGER NOT NULL CHECK(proof_revision>=0),
    event_examined_cursor INTEGER NOT NULL CHECK(event_examined_cursor>=0),
    event_terminal_cursor INTEGER CHECK(event_terminal_cursor IS NULL OR
        event_terminal_cursor>=0),
    close_token TEXT, close_revision INTEGER CHECK(close_revision IS NULL OR
        close_revision>=0),
    CHECK(minimum_cursor<=start_cursor AND start_cursor<=replay_cursor),
    CHECK(replay_cursor<=event_examined_cursor AND event_examined_cursor<=target_cursor),
    CHECK(start_target_cursor<=target_cursor),
    CHECK((close_token IS NULL)=(close_revision IS NULL))
);
CREATE TABLE IF NOT EXISTS recovery_scopes(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('root','chat')),
    scope_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation>=0),
    CHECK((scope_kind='root' AND scope_id='') OR
          (scope_kind='chat' AND length(scope_id)>0)),
    PRIMARY KEY(recovery_id,scope_kind,scope_id)
);
CREATE TABLE IF NOT EXISTS recovery_work(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    work_id TEXT NOT NULL,
    family TEXT NOT NULL CHECK(family IN ('docs','logs','visibility','frontiers')),
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('root','chat')),
    scope_id TEXT NOT NULL,
    selection BLOB NOT NULL, initial_checkpoint BLOB NOT NULL,
    checkpoint BLOB NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN
        ('pending','complete','complete_empty','denied','failed','truncated')),
    page_count INTEGER NOT NULL CHECK(page_count>=0),
    row_count INTEGER NOT NULL CHECK(row_count>=0),
    CHECK((scope_kind='root' AND scope_id='') OR
          (scope_kind='chat' AND length(scope_id)>0)),
    PRIMARY KEY(recovery_id,work_id)
);
CREATE TABLE IF NOT EXISTS recovery_pages(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    chunk_id TEXT NOT NULL, work_id TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY(recovery_id,chunk_id),
    FOREIGN KEY(recovery_id,work_id)
        REFERENCES recovery_work(recovery_id,work_id)
);
CREATE TABLE IF NOT EXISTS recovery_manifests(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    family TEXT NOT NULL CHECK(family IN ('documents','chats','streams')),
    initial_checkpoint BLOB NOT NULL, checkpoint BLOB NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN
        ('pending','complete','complete_empty')),
    page_count INTEGER NOT NULL CHECK(page_count>=0),
    row_count INTEGER NOT NULL CHECK(row_count>=0),
    PRIMARY KEY(recovery_id,family)
);
CREATE TABLE IF NOT EXISTS recovery_streams(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    chat_id TEXT NOT NULL, log_name TEXT NOT NULL,
    head INTEGER NOT NULL CHECK(head>=0),
    cursor INTEGER NOT NULL CHECK(cursor>=0),
    outcome TEXT NOT NULL CHECK(outcome IN
        ('pending','complete','complete_empty')),
    page_count INTEGER NOT NULL CHECK(page_count>=0),
    row_count INTEGER NOT NULL CHECK(row_count>=0),
    CHECK(cursor<=head),
    PRIMARY KEY(recovery_id,chat_id,log_name)
);
CREATE TABLE IF NOT EXISTS recovery_events(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    event_id INTEGER NOT NULL CHECK(event_id>0),
    stream_kind TEXT NOT NULL CHECK(stream_kind IN ('root','chat')),
    stream_id TEXT NOT NULL, domain TEXT NOT NULL
        CHECK(domain IN ('docs','logs')),
    source_key TEXT NOT NULL, doc_head INTEGER, log_head INTEGER,
    state TEXT NOT NULL CHECK(state IN ('pending','applied')),
    CHECK((stream_kind='root' AND stream_id='') OR
          (stream_kind='chat' AND length(stream_id)>0)),
    CHECK((domain='docs' AND doc_head IS NOT NULL AND log_head IS NULL) OR
          (domain='logs' AND stream_kind='chat' AND log_head IS NOT NULL
           AND doc_head IS NULL)),
    PRIMARY KEY(recovery_id,event_id)
);
CREATE TABLE IF NOT EXISTS recovery_proof_pages(
    recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
    chunk_id TEXT NOT NULL, page_kind TEXT NOT NULL CHECK(page_kind IN
        ('documents','chats','streams','log','events','repair_doc','repair_log')),
    page_key TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY(recovery_id,chunk_id)
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
_V3_TABLES = frozenset({
    "node_schema", "node_identity", "node_meta", "node_generations",
    "candidate_docs", "candidate_log_rows", "candidate_visibility",
    "candidate_frontiers", "candidate_chunks", "remote_docs",
    "remote_log_rows", "remote_chat_visibility", "remote_frontiers",
    "scope_versions", "local_changes", "local_change_state",
})
_V4_RECOVERY_TABLES = frozenset({
    "provider_recoveries", "recovery_scopes", "recovery_work", "recovery_pages",
})
_V5_RECOVERY_TABLES = _V4_RECOVERY_TABLES | frozenset({
    "recovery_manifests", "recovery_streams", "recovery_proof_pages",
})
_RECOVERY_TABLES = _V5_RECOVERY_TABLES | frozenset({"recovery_events"})
_V4_TABLES = _V3_TABLES | _V4_RECOVERY_TABLES
_V5_TABLES = _V3_TABLES | _V5_RECOVERY_TABLES
_V6_TABLES = _V3_TABLES | _RECOVERY_TABLES
_RECOVERY_SCHEMA = _SCHEMA[_SCHEMA.index("CREATE TABLE IF NOT EXISTS provider_recoveries("):
                           _SCHEMA.index("CREATE TABLE IF NOT EXISTS remote_docs(")]
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

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
                "SELECT version,protocol_version FROM node_schema WHERE singleton=1"
        ).fetchall() != [(2, PROTOCOL_VERSION)]:
            raise sqlite3.DatabaseError("unsupported local node schema")
        self._check_identity(conn)
        self._check_meta(conn)
        # V2 never resumed building candidates after process loss. Retire any
        # such private work before adding resumable chunk receipts.
        conn.execute(
            "UPDATE node_generations SET state='abandoned' "
            "WHERE state IN ('building','sealed')"
        )
        for table in ("candidate_docs", "candidate_log_rows",
                      "candidate_visibility", "candidate_frontiers"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "ALTER TABLE node_generations ADD COLUMN logs_mode TEXT "
            "CHECK(logs_mode IS NULL OR logs_mode IN ('delta','replace'))"
        )
        conn.execute(
            "ALTER TABLE node_generations ADD COLUMN chunk_count INTEGER "
            "NOT NULL DEFAULT 0 CHECK(chunk_count>=0)"
        )
        conn.execute("""
            CREATE TABLE candidate_chunks(
                generation INTEGER NOT NULL REFERENCES node_generations(generation),
                chunk_id TEXT NOT NULL, digest TEXT NOT NULL,
                document_count INTEGER NOT NULL CHECK(document_count>=0),
                log_count INTEGER NOT NULL CHECK(log_count>=0),
                visibility_count INTEGER NOT NULL CHECK(visibility_count>=0),
                frontier_count INTEGER NOT NULL CHECK(frontier_count>=0),
                total_bytes INTEGER NOT NULL CHECK(total_bytes>=0),
                PRIMARY KEY(generation,chunk_id)
            )
        """)
        conn.execute("DROP TABLE node_schema")
        conn.execute("""
            CREATE TABLE node_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL CHECK(version=3),
                protocol_version INTEGER NOT NULL CHECK(protocol_version=1)
            )
        """)
        conn.execute(
            "INSERT INTO node_schema VALUES(1,?,?)",
            (3, PROTOCOL_VERSION),
        )

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
                "SELECT version,protocol_version FROM node_schema WHERE singleton=1"
        ).fetchall() != [(3, PROTOCOL_VERSION)]:
            raise sqlite3.DatabaseError("unsupported local node schema")
        self._check_identity(conn)
        self._check_meta(conn)
        # Execute statements in the caller's IMMEDIATE transaction; executescript
        # would commit that transaction and expose a partially migrated schema.
        for statement in _RECOVERY_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("DROP TABLE node_schema")
        conn.execute("""CREATE TABLE node_schema(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=6),
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""")
        conn.execute("INSERT INTO node_schema VALUES(1,6,?)", (PROTOCOL_VERSION,))

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
                "SELECT version,protocol_version FROM node_schema WHERE singleton=1"
        ).fetchall() != [(4, PROTOCOL_VERSION)]:
            raise sqlite3.DatabaseError("unsupported local node schema")
        self._check_identity(conn)
        self._check_meta(conn)
        # V4 recoveries recorded caller-described work but lacked a closed,
        # Store-owned inventory. They cannot be upgraded into proof. Retire
        # only those private generations; admitted and generic candidates stay.
        generations = tuple(row[0] for row in conn.execute(
            "SELECT generation FROM provider_recoveries ORDER BY generation"
        ))
        for generation in generations:
            for table in ("candidate_docs", "candidate_log_rows",
                          "candidate_visibility", "candidate_frontiers",
                          "candidate_chunks"):
                conn.execute(f"DELETE FROM {table} WHERE generation=?", (generation,))
            conn.execute(
                "UPDATE node_generations SET state='abandoned' "
                "WHERE generation=? AND state IN ('building','sealed')",
                (generation,),
            )
        for table in ("recovery_pages", "recovery_work", "recovery_scopes",
                      "provider_recoveries"):
            conn.execute(f"DROP TABLE {table}")
        for statement in _RECOVERY_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("DROP TABLE node_schema")
        conn.execute("""CREATE TABLE node_schema(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=6),
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""")
        conn.execute("INSERT INTO node_schema VALUES(1,6,?)", (PROTOCOL_VERSION,))

    def _migrate_v5(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
                "SELECT version,protocol_version FROM node_schema WHERE singleton=1"
        ).fetchall() != [(5, PROTOCOL_VERSION)]:
            raise sqlite3.DatabaseError("unsupported local node schema")
        self._check_identity(conn)
        self._check_meta(conn)
        conn.execute("""CREATE TABLE recovery_events(
            recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
            event_id INTEGER NOT NULL CHECK(event_id>0),
            stream_kind TEXT NOT NULL CHECK(stream_kind IN ('root','chat')),
            stream_id TEXT NOT NULL, domain TEXT NOT NULL
                CHECK(domain IN ('docs','logs')),
            source_key TEXT NOT NULL, doc_head INTEGER, log_head INTEGER,
            state TEXT NOT NULL CHECK(state IN ('pending','applied')),
            CHECK((stream_kind='root' AND stream_id='') OR
                  (stream_kind='chat' AND length(stream_id)>0)),
            CHECK((domain='docs' AND doc_head IS NOT NULL AND log_head IS NULL) OR
                  (domain='logs' AND stream_kind='chat' AND log_head IS NOT NULL
                   AND doc_head IS NULL)),
            PRIMARY KEY(recovery_id,event_id)
        )""")
        conn.execute("ALTER TABLE recovery_proof_pages "
                     "RENAME TO recovery_proof_pages_v5")
        conn.execute("""CREATE TABLE recovery_proof_pages(
            recovery_id TEXT NOT NULL REFERENCES provider_recoveries(recovery_id),
            chunk_id TEXT NOT NULL, page_kind TEXT NOT NULL CHECK(page_kind IN
                ('documents','chats','streams','log','events','repair_doc','repair_log')),
            page_key TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY(recovery_id,chunk_id)
        )""")
        conn.execute("INSERT INTO recovery_proof_pages "
                     "SELECT * FROM recovery_proof_pages_v5")
        conn.execute("DROP TABLE recovery_proof_pages_v5")
        conn.execute("DROP TABLE node_schema")
        conn.execute("""CREATE TABLE node_schema(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL CHECK(version=6),
            protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""")
        conn.execute("INSERT INTO node_schema VALUES(1,6,?)", (PROTOCOL_VERSION,))

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
                elif tables == _V2_TABLES:
                    self._migrate_v2(conn)
                    self._migrate_v3(conn)
                elif tables == _V3_TABLES:
                    self._migrate_v3(conn)
                elif tables == _V4_TABLES:
                    self._migrate_v4(conn)
                elif tables == _V5_TABLES:
                    self._migrate_v5(conn)
                elif tables != _V6_TABLES:
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

    @staticmethod
    def _scope_generation(conn: sqlite3.Connection, kind: str,
                          ident: str) -> tuple[int, str | None]:
        return conn.execute(
            "SELECT generation,pending_reason FROM scope_versions "
            "WHERE scope_kind=? AND scope_id=?", (kind, ident),
        ).fetchone() or (0, None)

    def begin_recovery(self, plan: NodeRecoveryPlan, *,
                       created_ns: int) -> NodeRecoveryState:
        """Bind one private, resumable provider recovery to an exact local cut."""
        if type(plan) is not NodeRecoveryPlan:
            raise NodeInputError("invalid recovery plan")
        plan = NodeRecoveryPlan(
            plan.database_incarnation, plan.identity_digest, plan.base_generation,
            plan.replay_cursor, NodeProviderCut(**vars(plan.provider_cut)),
            tuple(NodeRecoveryScope(**vars(s)) for s in plan.scopes),
            tuple(NodeRecoveryWork(**vars(w)) for w in plan.work),
        )
        created = _positive_ns(created_ns, "recovery creation time")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            meta = self._check_meta(conn)
            if (meta[0] != plan.database_incarnation
                    or self.identity.digest != plan.identity_digest
                    or meta[1] != plan.base_generation or meta[5]
                    or any(self._scope_generation(conn, s.scope_kind, s.scope_id)
                           != (s.generation, None) for s in plan.scopes)):
                raise NodeGenerationChanged("recovery binding changed")
            if conn.execute("SELECT count(*) FROM node_generations WHERE state "
                            "IN ('building','sealed')").fetchone()[0] >= MAX_ACTIVE_CANDIDATES:
                raise NodeInputError("too many active node candidates")
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation),0)+1 FROM node_generations"
            ).fetchone()[0]
            health_owner_token = max(created, meta[3] + 1)
            if health_owner_token > 2**63 - 1:
                raise NodeInputError("recovery attempt time exhausted")
            recovery_id = secrets.token_hex(24)
            cut = plan.provider_cut
            conn.execute("INSERT INTO node_generations(generation,base_generation,"
                         "state,created_ns) VALUES(?,?,'building',?)",
                         (generation, plan.base_generation, created))
            conn.execute("""INSERT INTO provider_recoveries(
                recovery_id,generation,database_incarnation,identity_digest,
                base_generation,health_owner_token,start_cursor,replay_cursor,
                schema_version,index_epoch,source_epoch,account_id,role,
                minimum_cursor,start_target_cursor,target_cursor,state,
                proof_version,proof_revision,event_examined_cursor,
                event_terminal_cursor,close_token,close_revision)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,NULL,NULL,NULL)
            """, (recovery_id, generation, plan.database_incarnation,
                   plan.identity_digest, plan.base_generation, health_owner_token,
                   plan.replay_cursor, plan.replay_cursor, cut.schema_version,
                   cut.index_epoch, cut.source_epoch, cut.account_id, cut.role,
                   cut.minimum_cursor, cut.cursor, cut.cursor, "building",
                   plan.replay_cursor))
            conn.executemany("INSERT INTO recovery_scopes VALUES(?,?,?,?)",
                             ((recovery_id, s.scope_kind, s.scope_id, s.generation)
                              for s in plan.scopes))
            conn.executemany("INSERT INTO recovery_work VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             ((recovery_id, w.work_id, w.family, w.scope_kind,
                               w.scope_id, w.selection, w.checkpoint, w.checkpoint,
                               "pending", 0, 0)
                              for w in plan.work))
            conn.executemany(
                "INSERT INTO recovery_manifests VALUES(?,?,X'',X'','pending',0,0)",
                ((recovery_id, family)
                 for family in ("documents", "chats", "streams")),
            )
            if self._proof_metadata_size(conn, recovery_id) > MAX_RECOVERY_METADATA_BYTES:
                raise NodeInputError("recovery metadata exceeds budget")
            conn.execute("UPDATE node_meta SET health='catching_up',last_attempt_ns=? "
                         "WHERE singleton=1", (health_owner_token,))
        manifests = tuple(NodeRecoveryManifestState(family, b"", "pending", 0, 0)
                          for family in ("chats", "documents", "streams"))
        return NodeRecoveryState(recovery_id, generation, plan,
                                 health_owner_token, plan.replay_cursor,
                                 plan.provider_cut.cursor,
                                 "building", tuple(
            NodeRecoveryWorkState(w.work_id, w.family, w.scope_kind, w.scope_id,
                                  w.selection, w.checkpoint, "pending", 0, 0)
            for w in plan.work), 0, manifests, 0, 0,
                                 plan.replay_cursor, None, 0, 0, False)

    def _bound_recovery(self, conn: sqlite3.Connection, recovery_id: str,
                        current: NodeProviderCut) -> tuple | None:
        row = conn.execute("SELECT generation,database_incarnation,identity_digest,"
                           "base_generation,health_owner_token,start_cursor,replay_cursor,"
                           "schema_version,index_epoch,"
                           "source_epoch,account_id,role,minimum_cursor,"
                           "start_target_cursor,target_cursor,state,proof_version,"
                           "proof_revision,event_examined_cursor,event_terminal_cursor,"
                           "close_token,close_revision "
                           "FROM provider_recoveries WHERE recovery_id=?",
                           (recovery_id,)).fetchone()
        if row is None:
            raise NodeGenerationChanged("unknown recovery")
        (generation, incarnation, digest, base, health_owner, start, replay, schema,
         index, source, account, role, _minimum, _start_target, target, state,
         proof_version, _proof_revision, examined, _terminal, _close_token,
         _close_revision) = row
        meta = self._check_meta(conn)
        stale = (meta[0] != incarnation or self.identity.digest != digest
                 or meta[1] != base or bool(meta[5])
                 or current.schema_version != schema or current.index_epoch != index
                 or current.source_epoch != source or current.minimum_cursor > replay
                 or current.account_id != account or current.role != role
                 or proof_version != 1 or examined < replay or examined > target)
        for kind, ident, scope_generation in conn.execute(
                "SELECT scope_kind,scope_id,generation FROM recovery_scopes "
                "WHERE recovery_id=?", (recovery_id,)):
            if self._scope_generation(conn, kind, ident) != (scope_generation, None):
                stale = True
        if stale:
            conn.execute("UPDATE node_generations SET state='abandoned' "
                         "WHERE generation=? AND state IN ('building','sealed')",
                         (generation,))
            self._clear_candidate_rows(conn, generation)
            self._prune_generation_history(conn)
            return None
        # A delayed page may carry an older, otherwise valid cut after another
        # request advanced the target. Reject that response without destroying
        # the newer durable recovery.
        if current.cursor < target:
            raise NodeGenerationChanged("provider cut precedes recovery target")
        return row

    def recovery_state(self, recovery_id: str,
                       current_provider_cut: NodeProviderCut) -> NodeRecoveryState:
        recovery_id = self._candidate_chunk_id(recovery_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._bound_recovery(conn, recovery_id, current)
            if row is not None:
                (generation, incarnation, digest, base, health_owner, start, replay,
                 schema, index, source, account, role, minimum, start_target,
                 target, state, _proof_version, proof_revision, examined,
                 terminal, close_token, _close_revision) = row
                scopes = tuple(NodeRecoveryScope(kind, ident, value) for kind, ident, value
                               in conn.execute("SELECT scope_kind,scope_id,generation "
                                               "FROM recovery_scopes WHERE recovery_id=? "
                                               "ORDER BY scope_kind,scope_id", (recovery_id,)))
                work = tuple(NodeRecoveryWorkState(*values) for values in conn.execute(
                    "SELECT work_id,family,scope_kind,scope_id,selection,checkpoint,"
                    "outcome,page_count,row_count FROM recovery_work "
                    "WHERE recovery_id=? ORDER BY work_id", (recovery_id,)))
                initial = dict(conn.execute(
                    "SELECT work_id,initial_checkpoint FROM recovery_work "
                    "WHERE recovery_id=?", (recovery_id,)))
                plan = NodeRecoveryPlan(incarnation, digest, base, start,
                                        NodeProviderCut(schema, index, source, account,
                                                        role, minimum, start_target), scopes,
                                        tuple(NodeRecoveryWork(w.work_id, w.family,
                                                              w.scope_kind, w.scope_id,
                                                              w.selection, initial[w.work_id])
                                              for w in work))
                manifests = tuple(NodeRecoveryManifestState(*values) for values in
                                  conn.execute(
                    "SELECT family,checkpoint,outcome,page_count,row_count "
                    "FROM recovery_manifests WHERE recovery_id=? ORDER BY family",
                    (recovery_id,),
                ))
                stream_count, pending_streams = conn.execute(
                    "SELECT count(*),COALESCE(SUM(outcome='pending'),0) "
                    "FROM recovery_streams WHERE recovery_id=?", (recovery_id,),
                ).fetchone()
                event_count, pending_events = conn.execute(
                    "SELECT count(*),COALESCE(SUM(state='pending'),0) "
                    "FROM recovery_events WHERE recovery_id=?", (recovery_id,),
                ).fetchone()
                result = NodeRecoveryState(recovery_id, generation, plan,
                                           health_owner, replay, target, state, work,
                                           proof_revision, manifests, stream_count,
                                           pending_streams, examined, terminal,
                                           event_count, pending_events,
                                           close_token is not None)
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return result

    def extend_recovery_target(self, recovery_id: str,
                               current_provider_cut: NodeProviderCut) -> int:
        """Advance a closing fence after fresh same-epoch provider observation."""
        recovery_id = self._candidate_chunk_id(recovery_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._bound_recovery(conn, recovery_id, current)
            if row is not None:
                if row[15] != "building":
                    raise NodeGenerationChanged("recovery is not building")
                target = max(row[14], current.cursor)
                changed = int(target != row[14])
                if changed:
                    self._check_recovery_close_reserve(
                        conn, recovery_id, row[0],
                        prospective_target=target,
                        prospective_minimum=current.minimum_cursor,
                    )
                conn.execute("UPDATE provider_recoveries SET target_cursor=?,"
                             "proof_revision=proof_revision+?,"
                             "event_terminal_cursor=CASE WHEN ? THEN NULL ELSE "
                             "event_terminal_cursor END,close_token=NULL,"
                             "close_revision=NULL WHERE recovery_id=?",
                             (target, changed, changed, recovery_id))
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return target

    def stage_recovery_page(self, recovery_id: str,
                            current_provider_cut: NodeProviderCut, chunk_id: str,
                            work_id: str, expected_revision: int,
                            expected_checkpoint: bytes,
                            next_checkpoint: bytes, outcome: str,
                            expected_replay_cursor: int,
                            batch: NodeInputBatch) -> bool:
        # V4 accepted arbitrary caller-described work rows. Those rows cannot
        # prove a complete v5 source inventory, even when every caller item is
        # marked complete. Keep the compatibility surface visibly fail-closed.
        raise NodeInputError("typed recovery pages are required for v5 proof")

    @staticmethod
    def _recovery_batch(*, documents: tuple[NodeDocument, ...] = (),
                        log_rows: tuple[NodeLogRow, ...] = (),
                        visibility: tuple = ()) -> NodeInputBatch:
        return detached_batch(NodeInputBatch(
            documents=documents, log_rows=log_rows, visibility=visibility,
            documents_mode="replace", logs_mode="replace",
            visibility_mode="replace", frontiers_mode="replace",
        ))

    @staticmethod
    def _stream_checkpoint(chat_id: str, log_name: str) -> bytes:
        chat = chat_id.encode("utf-8")
        log = log_name.encode("utf-8")
        return len(chat).to_bytes(2, "big") + chat + log

    @staticmethod
    def _decode_stream_checkpoint(value: bytes) -> tuple[str, str]:
        if len(value) < 3:
            raise NodeInputError("invalid recovery stream checkpoint")
        chat_size = int.from_bytes(value[:2], "big")
        if chat_size < 1 or len(value) <= 2 + chat_size:
            raise NodeInputError("invalid recovery stream checkpoint")
        try:
            chat = value[2:2 + chat_size].decode("utf-8")
            log = value[2 + chat_size:].decode("utf-8")
        except UnicodeDecodeError:
            raise NodeInputError("invalid recovery stream checkpoint") from None
        marker = NodeRecoveryStreamHead(chat, log, 1)
        return marker.chat_id, marker.log_name

    @staticmethod
    def _proof_page_digest(kind: str, key: str, revision: int,
                           cut: NodeProviderCut, checkpoint: bytes,
                           has_more: bool, batch: NodeInputBatch,
                           extra: object = None) -> str:
        cut_value = (cut.schema_version, cut.index_epoch, cut.source_epoch,
                     cut.account_id, cut.role, cut.minimum_cursor, cut.cursor)
        return hashlib.sha256(repr((
            kind, key, revision, cut_value, checkpoint, has_more, extra,
            NodeStore._batch_digest(batch),
        )).encode("utf-8")).hexdigest()

    @staticmethod
    def _check_page_request(expected_revision: int, checkpoint: bytes,
                            has_more: bool) -> bytes:
        if (type(expected_revision) is not int
                or not 0 <= expected_revision <= MAX_STAGED_CHUNKS):
            raise NodeInputError("invalid recovery proof revision")
        if type(checkpoint) is not bytes or len(checkpoint) > MAX_RECOVERY_OPAQUE_BYTES:
            raise NodeInputError("invalid recovery checkpoint")
        if type(has_more) is not bool:
            raise NodeInputError("invalid recovery continuation flag")
        return bytes(checkpoint)

    def _proof_page_start(self, conn: sqlite3.Connection, recovery_id: str,
                          current: NodeProviderCut, chunk: str, kind: str,
                          key: str, digest: str) -> tuple[tuple | None, bool]:
        row = self._bound_recovery(conn, recovery_id, current)
        if row is None:
            return None, False
        prior = conn.execute(
            "SELECT page_kind,page_key,digest FROM recovery_proof_pages "
            "WHERE recovery_id=? AND chunk_id=?", (recovery_id, chunk),
        ).fetchone()
        if prior is not None:
            if prior != (kind, key, digest):
                raise NodeInputError("recovery proof page id was reused")
            return row, True
        if row[15] != "building":
            raise NodeGenerationChanged("recovery is not building")
        return row, False

    @staticmethod
    def _proof_metadata_size(conn: sqlite3.Connection, recovery_id: str) -> int:
        work = conn.execute("SELECT COALESCE(SUM(length(selection)+"
            "length(initial_checkpoint)+length(checkpoint)+"
            "length(CAST(work_id AS BLOB))+length(CAST(scope_id AS BLOB))+128),0) "
            "FROM recovery_work WHERE recovery_id=?", (recovery_id,)).fetchone()[0]
        scopes = conn.execute("SELECT COALESCE(SUM("
            "length(CAST(scope_id AS BLOB))+64),0) FROM recovery_scopes "
            "WHERE recovery_id=?", (recovery_id,)).fetchone()[0]
        manifests = conn.execute("SELECT COALESCE(SUM(length(initial_checkpoint)+"
            "length(checkpoint)+length(CAST(family AS BLOB))+96),0) "
            "FROM recovery_manifests WHERE recovery_id=?", (recovery_id,)).fetchone()[0]
        streams = conn.execute("SELECT COALESCE(SUM(length(CAST(chat_id AS BLOB))+"
            "length(CAST(log_name AS BLOB))+112),0) FROM recovery_streams "
            "WHERE recovery_id=?", (recovery_id,)).fetchone()[0]
        events = conn.execute("SELECT COALESCE(SUM(length(CAST(stream_id AS BLOB))+"
            "length(CAST(source_key AS BLOB))+144),0) FROM recovery_events "
            "WHERE recovery_id=?", (recovery_id,)).fetchone()[0]
        pages = conn.execute("SELECT COALESCE(SUM(length(CAST(chunk_id AS BLOB))+"
            "length(CAST(page_kind AS BLOB))+length(CAST(page_key AS BLOB))+"
            "length(CAST(digest AS BLOB))+80),0) FROM recovery_proof_pages "
            "WHERE recovery_id=?", (recovery_id,)).fetchone()[0]
        return work + scopes + manifests + streams + events + pages

    def _finish_proof_page(self, conn: sqlite3.Connection, recovery_id: str,
                           generation: int, current: NodeProviderCut,
                           target: int, chunk: str, kind: str, key: str,
                           digest: str, batch: NodeInputBatch, *,
                           batch_staged: bool = False,
                           clears_advanced_event_terminal: bool = True,
                           satisfies_event_terminal: bool = False) -> None:
        if not batch_staged:
            self._stage_batch(
                conn, generation, chunk, batch, self._batch_digest(batch))
        conn.execute("INSERT INTO recovery_proof_pages VALUES(?,?,?,?,?)",
                     (recovery_id, chunk, kind, key, digest))
        self._check_recovery_close_reserve(
            conn, recovery_id, generation,
            prospective_target=max(target, current.cursor),
            prospective_minimum=current.minimum_cursor,
            satisfies_event_terminal=satisfies_event_terminal,
        )
        if self._proof_metadata_size(conn, recovery_id) > MAX_RECOVERY_METADATA_BYTES:
            raise NodeInputError("recovery metadata exceeds budget")
        advanced = (clears_advanced_event_terminal
                    and current.cursor > target)
        conn.execute("UPDATE provider_recoveries SET target_cursor=?,"
                     "proof_revision=proof_revision+1,event_terminal_cursor="
                     "CASE WHEN ? THEN NULL ELSE event_terminal_cursor END,"
                     "close_token=NULL,close_revision=NULL WHERE recovery_id=?",
                     (max(target, current.cursor), int(advanced), recovery_id))

    @staticmethod
    def _recovery_close_batch(source_epoch: str, target: int,
                              minimum_cursor: int) -> NodeInputBatch:
        return NodeInputBatch(
            frontiers=(
                NodeFrontier("provider-source-ledger", source_epoch,
                             target, minimum_cursor),
                NodeFrontier("provider-source-replay", source_epoch,
                             target, minimum_cursor),
            ),
            documents_mode="replace", logs_mode="replace",
            visibility_mode="replace", frontiers_mode="replace",
        )

    @classmethod
    def _check_recovery_close_reserve(cls, conn: sqlite3.Connection,
                                      recovery_id: str,
                                      generation: int, *,
                                      prospective_target: int | None = None,
                                      prospective_minimum: int | None = None,
                                      satisfies_event_terminal: bool = False) -> None:
        pending_manifests = conn.execute(
            "SELECT count(*) FROM recovery_manifests WHERE recovery_id=? "
            "AND outcome='pending'", (recovery_id,),
        ).fetchone()[0]
        pending_streams = conn.execute(
            "SELECT count(*) FROM recovery_streams WHERE recovery_id=? "
            "AND outcome='pending'", (recovery_id,),
        ).fetchone()[0]
        pending_events = conn.execute(
            "SELECT count(*) FROM recovery_events WHERE recovery_id=? "
            "AND state='pending'", (recovery_id,),
        ).fetchone()[0]
        chunks, total_bytes = conn.execute(
            "SELECT chunk_count,total_bytes FROM node_generations "
            "WHERE generation=?", (generation,),
        ).fetchone()
        source_epoch, target, minimum_cursor, terminal = conn.execute(
            "SELECT source_epoch,target_cursor,minimum_cursor,event_terminal_cursor "
            "FROM provider_recoveries WHERE recovery_id=?", (recovery_id,),
        ).fetchone()
        if prospective_target is not None:
            target = max(target, prospective_target)
        if prospective_minimum is not None:
            minimum_cursor = prospective_minimum
        event_terminal_receipt = int(
            not satisfies_event_terminal and terminal != target)
        close_bytes = cls._recovery_close_batch(
            source_epoch, target, minimum_cursor).byte_size
        if (chunks + pending_manifests + pending_streams + pending_events
                + event_terminal_receipt + 1 > MAX_STAGED_CHUNKS
                or total_bytes + close_bytes > MAX_STAGED_BYTES):
            raise NodeInputError("recovery obligations exceed closing budget")

    def _stage_recovery_manifest(self, recovery_id: str,
                                 current_provider_cut: NodeProviderCut,
                                 chunk_id: str, family: str,
                                 expected_revision: int,
                                 expected_checkpoint: bytes,
                                 values: tuple, has_more: bool) -> bool:
        recovery_id = self._candidate_chunk_id(recovery_id)
        chunk = self._candidate_chunk_id(chunk_id)
        if family not in ("documents", "chats", "streams"):
            raise NodeInputError("invalid recovery manifest")
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        checkpoint = self._check_page_request(
            expected_revision, expected_checkpoint, has_more)
        if type(values) is not tuple or len(values) > MAX_RECOVERY_MANIFEST_PAGE:
            raise NodeInputError("invalid recovery manifest page")
        if has_more and not values:
            raise NodeInputError("continued recovery page is empty")

        if family == "documents":
            if any(type(value) is not NodeDocument for value in values):
                raise NodeInputError("invalid recovery documents")
            batch = self._recovery_batch(documents=values)
            keys = tuple(value.path.encode("utf-8") for value in batch.documents)
            extra: object = keys
        elif family == "chats":
            if any(type(value) is not str for value in values):
                raise NodeInputError("invalid recovery chats")
            visibility = tuple(NodeVisibility(value, True) for value in values)
            batch = self._recovery_batch(visibility=visibility)
            keys = tuple(value.chat_id.encode("utf-8") for value in batch.visibility)
            extra = keys
        else:
            if any(type(value) is not NodeRecoveryStreamHead for value in values):
                raise NodeInputError("invalid recovery streams")
            values = tuple(NodeRecoveryStreamHead(**vars(value)) for value in values)
            batch = self._recovery_batch()
            pairs = tuple((value.chat_id, value.log_name) for value in values)
            keys = tuple(self._stream_checkpoint(*pair) for pair in pairs)
            extra = tuple((value.chat_id, value.log_name, value.head)
                          for value in values)
        if family == "streams":
            if pairs != tuple(sorted(pairs)) or len(set(pairs)) != len(pairs):
                raise NodeInputError("recovery manifest page is not strictly ordered")
            if checkpoint:
                marker = self._decode_stream_checkpoint(checkpoint)
                if pairs and pairs[0] <= marker:
                    raise NodeInputError("recovery manifest page precedes checkpoint")
        else:
            if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
                raise NodeInputError("recovery manifest page is not strictly ordered")
            if keys and keys[0] <= checkpoint:
                raise NodeInputError("recovery manifest page precedes checkpoint")
        next_checkpoint = keys[-1] if keys else checkpoint
        digest = self._proof_page_digest(
            family, family, expected_revision, current, checkpoint,
            has_more, batch, extra,
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row, duplicate = self._proof_page_start(
                conn, recovery_id, current, chunk, family, family, digest)
            if duplicate:
                return False
            if row is not None:
                generation, target = row[0], row[14]
                manifest = conn.execute(
                    "SELECT checkpoint,outcome,page_count,row_count "
                    "FROM recovery_manifests WHERE recovery_id=? AND family=?",
                    (recovery_id, family),
                ).fetchone()
                if manifest is None:
                    raise sqlite3.DatabaseError("missing recovery manifest")
                stored, outcome, page_count, row_count = manifest
                if (outcome != "pending" or page_count != expected_revision
                        or stored != checkpoint):
                    raise NodeInputError("recovery manifest checkpoint changed")
                if family == "streams":
                    stream_count = conn.execute(
                        "SELECT count(*) FROM recovery_streams WHERE recovery_id=?",
                        (recovery_id,),
                    ).fetchone()[0]
                    if stream_count + len(values) > MAX_RECOVERY_STREAMS:
                        raise NodeInputError("recovery stream inventory exceeds budget")
                    try:
                        conn.executemany(
                            "INSERT INTO recovery_streams VALUES(?,?,?,?,0,'pending',0,0)",
                            ((recovery_id, value.chat_id, value.log_name, value.head)
                             for value in values),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise NodeInputError("recovery stream pages overlap") from exc
                final = "pending" if has_more else (
                    "complete_empty" if row_count + len(values) == 0 else "complete")
                conn.execute("UPDATE recovery_manifests SET checkpoint=?,outcome=?,"
                             "page_count=?,row_count=? WHERE recovery_id=? AND family=?",
                             (next_checkpoint, final, page_count + 1,
                              row_count + len(values), recovery_id, family))
                self._finish_proof_page(
                    conn, recovery_id, generation, current, target, chunk,
                    family, family, digest, batch)
                result = True
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return result

    @staticmethod
    def _advance_recovery_replay(conn: sqlite3.Connection,
                                 recovery_id: str) -> None:
        replay, examined = conn.execute(
            "SELECT replay_cursor,event_examined_cursor FROM provider_recoveries "
            "WHERE recovery_id=?", (recovery_id,),
        ).fetchone()
        pending = conn.execute(
            "SELECT MIN(event_id) FROM recovery_events WHERE recovery_id=? "
            "AND state='pending' AND event_id<=?", (recovery_id, examined),
        ).fetchone()[0]
        if pending is None:
            advanced = examined
        else:
            advanced = conn.execute(
                "SELECT COALESCE(MAX(event_id),?) FROM recovery_events "
                "WHERE recovery_id=? AND state='applied' AND event_id<?",
                (replay, recovery_id, pending),
            ).fetchone()[0]
        advanced = max(replay, advanced)
        conn.execute("UPDATE provider_recoveries SET replay_cursor=? "
                     "WHERE recovery_id=?", (advanced, recovery_id))

    def stage_recovery_events(self, recovery_id: str,
                              current_provider_cut: NodeProviderCut,
                              chunk_id: str, expected_examined_cursor: int,
                              events: tuple[NodeRecoveryEvent, ...], *,
                              has_more: bool) -> bool:
        recovery_id = self._candidate_chunk_id(recovery_id)
        chunk = self._candidate_chunk_id(chunk_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        if (type(expected_examined_cursor) is not int
                or not 0 <= expected_examined_cursor <= 2**63 - 1
                or type(events) is not tuple
                or len(events) > MAX_RECOVERY_MANIFEST_PAGE
                or any(type(event) is not NodeRecoveryEvent for event in events)
                or type(has_more) is not bool):
            raise NodeInputError("invalid recovery event page")
        events = tuple(NodeRecoveryEvent(**vars(event)) for event in events)
        ids = tuple(event.event_id for event in events)
        if (ids != tuple(sorted(ids)) or len(set(ids)) != len(ids)
                or any(event_id <= expected_examined_cursor
                       or event_id > current.cursor for event_id in ids)):
            raise NodeInputError("recovery events are outside their keyset")
        if has_more and not events:
            raise NodeInputError("continued recovery page is empty")
        if has_more and ids[-1] >= current.cursor:
            raise NodeInputError("recovery event continuation exceeds cut")
        batch = self._recovery_batch()
        extra = tuple((event.event_id, event.stream_kind, event.stream_id,
                       event.domain, event.source_key, event.doc_head,
                       event.log_head) for event in events)
        digest = self._proof_page_digest(
            "events", "events", expected_examined_cursor, current,
            str(expected_examined_cursor).encode("ascii"), has_more, batch, extra)
        invalidating = False
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row, duplicate = self._proof_page_start(
                conn, recovery_id, current, chunk, "events", "events", digest)
            if duplicate:
                return False
            if row is not None:
                generation, target = row[0], row[14]
                stored_examined = row[18]
                if stored_examined != expected_examined_cursor:
                    raise NodeInputError("recovery event checkpoint changed")
                if any(event.requires_restart for event in events):
                    conn.execute("UPDATE node_generations SET state='abandoned' "
                                 "WHERE generation=? AND state='building'",
                                 (generation,))
                    self._clear_candidate_rows(conn, generation)
                    self._prune_generation_history(conn)
                    invalidating = True
                else:
                    if conn.execute(
                            "SELECT count(*) FROM recovery_events "
                            "WHERE recovery_id=?", (recovery_id,),
                    ).fetchone()[0] + len(events) > MAX_RECOVERY_EVENTS:
                        raise NodeInputError("recovery event inventory exceeds budget")
                    conn.executemany(
                        "INSERT INTO recovery_events VALUES(?,?,?,?,?,?,?,?,?)",
                        ((recovery_id, event.event_id, event.stream_kind,
                          event.stream_id, event.domain, event.source_key,
                          event.doc_head, event.log_head, "pending")
                         for event in events),
                    )
                    next_examined = ids[-1] if has_more else current.cursor
                    terminal = None if has_more else current.cursor
                    self._finish_proof_page(
                        conn, recovery_id, generation, current, target, chunk,
                        "events", "events", digest, batch,
                        clears_advanced_event_terminal=False,
                        satisfies_event_terminal=not has_more)
                    conn.execute(
                        "UPDATE provider_recoveries SET event_examined_cursor=?,"
                        "event_terminal_cursor=? WHERE recovery_id=?",
                        (next_examined, terminal, recovery_id),
                    )
                    self._advance_recovery_replay(conn, recovery_id)
                    result = True
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        if invalidating:
            raise NodeGenerationChanged("recovery event requires root restart")
        return result

    @staticmethod
    def _stage_recovery_repair_candidate(
            conn: sqlite3.Connection, generation: int, chunk: str,
            batch: NodeInputBatch, digest: str) -> None:
        row = conn.execute(
            "SELECT state,documents_mode,logs_mode,visibility_mode,frontiers_mode,"
            "document_count,log_count,visibility_count,frontier_count,chunk_count,"
            "total_bytes FROM node_generations WHERE generation=?", (generation,),
        ).fetchone()
        if row is None or row[0] != "building":
            raise NodeGenerationChanged("candidate is not building")
        if row[1:5] != ("replace", "replace", "replace", "replace"):
            raise NodeInputError("recovery inventories are not staged")
        if len(batch.documents) + len(batch.log_rows) != 1:
            raise NodeInputError("recovery repair requires one row")
        document_count, log_count = row[5], row[6]
        if batch.documents:
            value = batch.documents[0]
            prior = conn.execute(
                "SELECT seq,deleted,payload FROM candidate_docs "
                "WHERE generation=? AND path=?", (generation, value.path),
            ).fetchone()
            if prior is None:
                conn.execute("INSERT INTO candidate_docs VALUES(?,?,?,?,?)",
                             (generation, value.path, value.seq,
                              int(value.deleted), value.payload))
                document_count += 1
            elif value.seq < prior[0]:
                raise NodeInputError("recovery document repair regressed")
            elif value.seq == prior[0] and prior[1:] != (
                    int(value.deleted), value.payload):
                raise NodeInputError("recovery document repair conflicts")
            elif value.seq > prior[0]:
                conn.execute("UPDATE candidate_docs SET seq=?,deleted=?,payload=? "
                             "WHERE generation=? AND path=?",
                             (value.seq, int(value.deleted), value.payload,
                              generation, value.path))
        else:
            value = batch.log_rows[0]
            prior = conn.execute(
                "SELECT chat_id,log_name,payload FROM candidate_log_rows "
                "WHERE generation=? AND id=?", (generation, value.id),
            ).fetchone()
            expected = (value.chat_id, value.log_name, value.payload)
            if prior is None:
                conn.execute("INSERT INTO candidate_log_rows VALUES(?,?,?,?,?)",
                             (generation, value.id, *expected))
                log_count += 1
            elif prior != expected:
                raise NodeInputError("recovery log repair conflicts")
        chunk_count = row[9] + 1
        total_bytes = row[10] + batch.byte_size
        if (document_count > MAX_STAGED_DOCUMENTS
                or log_count > MAX_STAGED_LOG_ROWS
                or chunk_count > MAX_STAGED_CHUNKS
                or total_bytes > MAX_STAGED_BYTES):
            raise NodeInputError("staged candidate exceeds node budget")
        conn.execute("INSERT INTO candidate_chunks VALUES(?,?,?,?,?,?,?,?)",
                     (generation, chunk, digest, len(batch.documents),
                      len(batch.log_rows), 0, 0, batch.byte_size))
        conn.execute("UPDATE node_generations SET document_count=?,log_count=?,"
                     "chunk_count=?,total_bytes=? WHERE generation=?",
                     (document_count, log_count, chunk_count, total_bytes,
                      generation))

    def _apply_recovery_event(self, recovery_id: str,
                              current_provider_cut: NodeProviderCut,
                              chunk_id: str, event_id: int,
                              batch: NodeInputBatch, kind: str) -> bool:
        recovery_id = self._candidate_chunk_id(recovery_id)
        chunk = self._candidate_chunk_id(chunk_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        if type(event_id) is not int or not 1 <= event_id <= 2**63 - 1:
            raise NodeInputError("invalid recovery event id")
        batch = detached_batch(batch)
        if any(mode != "replace" for mode in (
                batch.documents_mode, batch.logs_mode, batch.visibility_mode,
                batch.frontiers_mode)):
            raise NodeInputError("whole-root recovery requires replace batches")
        key = str(event_id)
        digest = self._proof_page_digest(
            kind, key, event_id, current, key.encode("ascii"), False, batch)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row, duplicate = self._proof_page_start(
                conn, recovery_id, current, chunk, kind, key, digest)
            if duplicate:
                return False
            if row is not None:
                generation, target = row[0], row[14]
                event = conn.execute(
                    "SELECT stream_kind,stream_id,domain,source_key,doc_head,"
                    "log_head,state FROM recovery_events WHERE recovery_id=? "
                    "AND event_id=?", (recovery_id, event_id),
                ).fetchone()
                if event is None:
                    raise NodeInputError("unknown recovery event")
                stream_kind, stream_id, domain, source_key, doc_head, log_head, state = event
                if state != "pending":
                    raise NodeInputError("recovery event is already applied")
                if kind == "repair_doc":
                    if (domain != "docs" or len(batch.documents) != 1
                            or batch.log_rows or batch.visibility or batch.frontiers):
                        raise NodeInputError("invalid recovery document repair")
                    value = batch.documents[0]
                    if value.path != source_key or value.seq < doc_head:
                        raise NodeInputError("recovery document does not cover event")
                else:
                    if (domain != "logs" or len(batch.log_rows) != 1
                            or batch.documents or batch.visibility or batch.frontiers):
                        raise NodeInputError("invalid recovery log repair")
                    value = batch.log_rows[0]
                    if (value.id != log_head or value.chat_id != stream_id
                            or value.log_name != source_key):
                        raise NodeInputError("recovery log does not cover event")
                self._stage_recovery_repair_candidate(
                    conn, generation, chunk, batch, self._batch_digest(batch))
                conn.execute("UPDATE recovery_events SET state='applied' "
                             "WHERE recovery_id=? AND event_id=?",
                             (recovery_id, event_id))
                self._advance_recovery_replay(conn, recovery_id)
                self._finish_proof_page(
                    conn, recovery_id, generation, current, target, chunk,
                    kind, key, digest, batch, batch_staged=True)
                result = True
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return result

    def apply_recovery_document_event(
            self, recovery_id: str, current_provider_cut: NodeProviderCut,
            chunk_id: str, event_id: int, document: NodeDocument) -> bool:
        if type(document) is not NodeDocument:
            raise NodeInputError("invalid recovery document repair")
        return self._apply_recovery_event(
            recovery_id, current_provider_cut, chunk_id, event_id,
            self._recovery_batch(documents=(document,)), "repair_doc")

    def apply_recovery_log_event(
            self, recovery_id: str, current_provider_cut: NodeProviderCut,
            chunk_id: str, event_id: int, log_row: NodeLogRow) -> bool:
        if type(log_row) is not NodeLogRow:
            raise NodeInputError("invalid recovery log repair")
        return self._apply_recovery_event(
            recovery_id, current_provider_cut, chunk_id, event_id,
            self._recovery_batch(log_rows=(log_row,)), "repair_log")

    def recovery_events(self, recovery_id: str,
                        current_provider_cut: NodeProviderCut, *,
                        after_event_id: int = 0,
                        limit: int = MAX_RECOVERY_MANIFEST_PAGE
                        ) -> tuple[NodeRecoveryEventState, ...]:
        recovery_id = self._candidate_chunk_id(recovery_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        if (type(after_event_id) is not int
                or not 0 <= after_event_id <= 2**63 - 1
                or type(limit) is not int
                or not 1 <= limit <= MAX_RECOVERY_MANIFEST_PAGE):
            raise NodeInputError("invalid recovery event continuation")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._bound_recovery(conn, recovery_id, current)
            if row is not None:
                values = conn.execute(
                    "SELECT event_id,stream_kind,stream_id,domain,source_key,"
                    "doc_head,log_head,state FROM recovery_events "
                    "WHERE recovery_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                    (recovery_id, after_event_id, limit),
                )
                result = tuple(NodeRecoveryEventState(
                    NodeRecoveryEvent(*value[:7]), value[7]) for value in values)
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return result

    def stage_recovery_documents(self, recovery_id: str,
                                 current_provider_cut: NodeProviderCut,
                                 chunk_id: str, expected_revision: int,
                                 expected_checkpoint: bytes,
                                 documents: tuple[NodeDocument, ...], *,
                                 has_more: bool) -> bool:
        return self._stage_recovery_manifest(
            recovery_id, current_provider_cut, chunk_id, "documents",
            expected_revision, expected_checkpoint, documents, has_more)

    def stage_recovery_chats(self, recovery_id: str,
                             current_provider_cut: NodeProviderCut,
                             chunk_id: str, expected_revision: int,
                             expected_checkpoint: bytes, chat_ids: tuple[str, ...], *,
                             has_more: bool) -> bool:
        return self._stage_recovery_manifest(
            recovery_id, current_provider_cut, chunk_id, "chats",
            expected_revision, expected_checkpoint, chat_ids, has_more)

    def stage_recovery_streams(self, recovery_id: str,
                               current_provider_cut: NodeProviderCut,
                               chunk_id: str, expected_revision: int,
                               expected_checkpoint: bytes,
                               streams: tuple[NodeRecoveryStreamHead, ...], *,
                               has_more: bool) -> bool:
        return self._stage_recovery_manifest(
            recovery_id, current_provider_cut, chunk_id, "streams",
            expected_revision, expected_checkpoint, streams, has_more)

    def stage_recovery_log_page(self, recovery_id: str,
                                current_provider_cut: NodeProviderCut,
                                chunk_id: str, chat_id: str, log_name: str,
                                expected_revision: int, expected_cursor: int,
                                rows: tuple[NodeLogRow, ...], *,
                                has_more: bool) -> bool:
        recovery_id = self._candidate_chunk_id(recovery_id)
        chunk = self._candidate_chunk_id(chunk_id)
        stream = NodeRecoveryStreamHead(chat_id, log_name, 1)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        if (type(expected_revision) is not int
                or not 0 <= expected_revision <= MAX_STAGED_CHUNKS):
            raise NodeInputError("invalid recovery stream revision")
        if type(expected_cursor) is not int or not 0 <= expected_cursor <= 2**63 - 1:
            raise NodeInputError("invalid recovery stream cursor")
        if type(has_more) is not bool or type(rows) is not tuple or len(
                rows) > MAX_RECOVERY_MANIFEST_PAGE:
            raise NodeInputError("invalid recovery log page")
        if any(type(value) is not NodeLogRow for value in rows):
            raise NodeInputError("invalid recovery log rows")
        batch = self._recovery_batch(log_rows=rows)
        if any((value.chat_id, value.log_name) != (stream.chat_id, stream.log_name)
               for value in batch.log_rows):
            raise NodeInputError("recovery log row crosses stream")
        ids = tuple(value.id for value in batch.log_rows)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise NodeInputError("recovery log page is not strictly ordered")
        if ids and ids[0] <= expected_cursor:
            raise NodeInputError("recovery log page precedes cursor")
        if has_more and not ids:
            raise NodeInputError("continued recovery page is empty")
        key = hashlib.sha256(self._stream_checkpoint(
            stream.chat_id, stream.log_name)).hexdigest()
        digest = self._proof_page_digest(
            "log", key, expected_revision, current,
            str(expected_cursor).encode("ascii"), has_more, batch,
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row, duplicate = self._proof_page_start(
                conn, recovery_id, current, chunk, "log", key, digest)
            if duplicate:
                return False
            if row is not None:
                generation, target = row[0], row[14]
                state = conn.execute(
                    "SELECT head,cursor,outcome,page_count,row_count "
                    "FROM recovery_streams WHERE recovery_id=? AND chat_id=? "
                    "AND log_name=?", (recovery_id, stream.chat_id, stream.log_name),
                ).fetchone()
                if state is None:
                    raise NodeInputError("unknown recovery stream")
                head, cursor, outcome, page_count, row_count = state
                if (outcome != "pending" or page_count != expected_revision
                        or cursor != expected_cursor):
                    raise NodeInputError("recovery stream checkpoint changed")
                if ids and ids[-1] > head:
                    raise NodeInputError("recovery log row exceeds captured head")
                if has_more and ids[-1] >= head:
                    raise NodeInputError("recovery log continuation exceeds head")
                # A stream head is a captured upper fence, not a promise that
                # its historical row still exists. Physical deletion can leave
                # a positive head with an empty exact range. A terminal source
                # page therefore proves exhaustion through the head; the later
                # event-replay proof must still account for the root delete.
                next_cursor = ids[-1] if has_more else head
                final = "pending" if has_more else (
                    "complete_empty" if row_count + len(ids) == 0 else "complete")
                conn.execute("UPDATE recovery_streams SET cursor=?,outcome=?,"
                             "page_count=?,row_count=? WHERE recovery_id=? "
                             "AND chat_id=? AND log_name=?",
                             (next_cursor, final, page_count + 1,
                              row_count + len(ids), recovery_id,
                              stream.chat_id, stream.log_name))
                self._finish_proof_page(
                    conn, recovery_id, generation, current, target, chunk,
                    "log", key, digest, batch)
                result = True
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return result

    def recovery_streams(self, recovery_id: str,
                         current_provider_cut: NodeProviderCut, *,
                         after: tuple[str, str] | None = None,
                         limit: int = MAX_RECOVERY_MANIFEST_PAGE
                         ) -> tuple[NodeRecoveryStreamState, ...]:
        recovery_id = self._candidate_chunk_id(recovery_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        if (type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_MANIFEST_PAGE):
            raise NodeInputError("invalid recovery stream limit")
        if after is not None:
            if (type(after) is not tuple or len(after) != 2
                    or any(type(value) is not str for value in after)):
                raise NodeInputError("invalid recovery stream continuation")
            marker = NodeRecoveryStreamHead(after[0], after[1], 1)
            after = (marker.chat_id, marker.log_name)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._bound_recovery(conn, recovery_id, current)
            if row is not None:
                if after is None:
                    values = conn.execute(
                        "SELECT chat_id,log_name,head,cursor,outcome,page_count,row_count "
                        "FROM recovery_streams WHERE recovery_id=? "
                        "ORDER BY chat_id,log_name LIMIT ?", (recovery_id, limit),
                    )
                else:
                    values = conn.execute(
                        "SELECT chat_id,log_name,head,cursor,outcome,page_count,row_count "
                        "FROM recovery_streams WHERE recovery_id=? AND "
                        "(chat_id>? OR (chat_id=? AND log_name>?)) "
                        "ORDER BY chat_id,log_name LIMIT ?",
                        (recovery_id, after[0], after[0], after[1], limit),
                    )
                result = tuple(NodeRecoveryStreamState(*value) for value in values)
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return result

    def seal_recovery(self, recovery_id: str,
                      current_provider_cut: NodeProviderCut, *,
                      observed_ns: int, close_token: str | None = None) -> None:
        recovery_id = self._candidate_chunk_id(recovery_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        observed = _positive_ns(observed_ns, "recovery observation time")
        if close_token is None:
            raise NodeInputError("recovery close token is required")
        token = self._candidate_chunk_id(close_token)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._bound_recovery(conn, recovery_id, current)
            if row is not None:
                (generation, _, _, _, _, _, _, _, _, source_epoch, _, _, _minimum,
                 _, target, state, _, proof_revision, _, _, stored_token,
                 close_revision) = row
                if state != "building":
                    raise NodeGenerationChanged("recovery is not building")
                if current.cursor != target:
                    raise NodeGenerationChanged("recovery closing fence advanced")
                if stored_token != token or close_revision != proof_revision:
                    raise NodeGenerationChanged("recovery close token changed")
                if not self._recovery_proof_complete(conn, recovery_id, target):
                    raise NodeInputError("recovery proof is incomplete")
                frontiers = self._recovery_close_batch(
                    source_epoch, target, current.minimum_cursor)
                self._stage_batch(
                    conn, generation, "recovery-close-" + token, frontiers,
                    self._batch_digest(frontiers))
                conn.execute("UPDATE provider_recoveries SET state='sealed' "
                             "WHERE recovery_id=?", (recovery_id,))
                self._seal_staged(conn, generation, observed)
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")

    @staticmethod
    def _recovery_proof_complete(conn: sqlite3.Connection,
                                 recovery_id: str, target: int) -> bool:
        manifests = conn.execute(
            "SELECT count(*),COALESCE(SUM(outcome IN ('complete','complete_empty')),0) "
            "FROM recovery_manifests WHERE recovery_id=?", (recovery_id,),
        ).fetchone()
        pending_streams = conn.execute(
            "SELECT count(*) FROM recovery_streams WHERE recovery_id=? "
            "AND outcome='pending'", (recovery_id,),
        ).fetchone()[0]
        pending_events = conn.execute(
            "SELECT count(*) FROM recovery_events WHERE recovery_id=? "
            "AND state='pending'", (recovery_id,),
        ).fetchone()[0]
        cursors = conn.execute(
            "SELECT replay_cursor,event_examined_cursor,event_terminal_cursor "
            "FROM provider_recoveries WHERE recovery_id=?", (recovery_id,),
        ).fetchone()
        return (manifests == (3, 3) and pending_streams == 0
                and pending_events == 0 and cursors == (target, target, target))

    def prepare_recovery_close(self, recovery_id: str,
                               current_provider_cut: NodeProviderCut) -> str:
        recovery_id = self._candidate_chunk_id(recovery_id)
        if type(current_provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid current provider cut")
        current = NodeProviderCut(**vars(current_provider_cut))
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._bound_recovery(conn, recovery_id, current)
            if row is not None:
                target, state, proof_revision = row[14], row[15], row[17]
                if state != "building":
                    raise NodeGenerationChanged("recovery is not building")
                if current.cursor != target:
                    raise NodeGenerationChanged("recovery closing fence advanced")
                if not self._recovery_proof_complete(conn, recovery_id, target):
                    raise NodeInputError("recovery proof is incomplete")
                self._check_recovery_close_reserve(conn, recovery_id, row[0])
                if row[20] is not None and row[21] == proof_revision:
                    token = row[20]
                else:
                    token = secrets.token_hex(24)
                    conn.execute("UPDATE provider_recoveries SET close_token=?,"
                                 "close_revision=? WHERE recovery_id=?",
                                 (token, proof_revision, recovery_id))
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        return token

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

    @staticmethod
    def _candidate_chunk_id(value: object) -> str:
        if type(value) is not str or not value or "\x00" in value:
            raise NodeInputError("invalid candidate chunk id")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise NodeInputError("invalid candidate chunk id") from None
        if len(encoded) > 256:
            raise NodeInputError("invalid candidate chunk id")
        return value

    @staticmethod
    def _batch_digest(batch: NodeInputBatch) -> str:
        digest = hashlib.sha256()

        def add(value: str | bytes | int | bool | None) -> None:
            if value is None:
                raw = b"n"
            elif type(value) is bytes:
                raw = b"b" + value
            elif type(value) is str:
                raw = b"s" + value.encode("utf-8")
            elif type(value) is bool:
                raw = b"t" if value else b"f"
            else:
                raw = b"i" + str(value).encode("ascii")
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)

        add("modes")
        for mode in (
            batch.documents_mode, batch.logs_mode, batch.visibility_mode,
            batch.frontiers_mode,
        ):
            add(mode)
        add("documents")
        add(len(batch.documents))
        for row in batch.documents:
            for value in (row.path, row.seq, row.deleted, row.payload):
                add(value)
        add("log_rows")
        add(len(batch.log_rows))
        for row in batch.log_rows:
            for value in (row.id, row.chat_id, row.log_name, row.payload):
                add(value)
        add("visibility")
        add(len(batch.visibility))
        for row in batch.visibility:
            add(row.chat_id)
            add(row.visible)
        add("frontiers")
        add(len(batch.frontiers))
        for row in batch.frontiers:
            for value in (
                row.name, row.epoch, row.cursor, row.minimum_cursor,
            ):
                add(value)
        return digest.hexdigest()

    def stage_candidate_batch(self, generation: int, chunk_id: str,
                              batch: NodeInputBatch) -> bool:
        """Durably append one bounded, idempotent recovery chunk.

        The candidate remains private until a separate seal and admission. A
        retry with the same id and bytes is a no-op; reusing an id for different
        bytes fails closed.
        """
        if type(generation) is not int or generation <= 0:
            raise NodeInputError("invalid candidate generation")
        chunk = self._candidate_chunk_id(chunk_id)
        batch = detached_batch(batch)
        digest = self._batch_digest(batch)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                    "SELECT 1 FROM provider_recoveries WHERE generation=?",
                    (generation,)).fetchone():
                raise NodeInputError("recovery candidate requires recovery pages")
            return self._stage_batch(conn, generation, chunk, batch, digest)

    @staticmethod
    def _stage_batch(conn: sqlite3.Connection, generation: int, chunk: str,
                     batch: NodeInputBatch, digest: str) -> bool:
            row = conn.execute(
                "SELECT state,documents_mode,logs_mode,visibility_mode,"
                "frontiers_mode,document_count,log_count,visibility_count,"
                "frontier_count,chunk_count,total_bytes FROM node_generations "
                "WHERE generation=?",
                (generation,)).fetchone()
            if row is None or row[0] != "building":
                raise NodeGenerationChanged("candidate is not building")
            modes = (
                batch.documents_mode, batch.logs_mode, batch.visibility_mode,
                batch.frontiers_mode,
            )
            prior_modes = row[1:5]
            if all(value is None for value in prior_modes):
                conn.execute(
                    "UPDATE node_generations SET documents_mode=?,logs_mode=?,"
                    "visibility_mode=?,frontiers_mode=? WHERE generation=?",
                    (*modes, generation),
                )
            elif prior_modes != modes:
                raise NodeInputError("candidate batch modes changed")
            prior = conn.execute(
                "SELECT digest FROM candidate_chunks "
                "WHERE generation=? AND chunk_id=?", (generation, chunk),
            ).fetchone()
            if prior is not None:
                if prior != (digest,):
                    raise NodeInputError("candidate chunk id was reused")
                return False
            try:
                conn.executemany(
                    "INSERT INTO candidate_docs VALUES(?,?,?,?,?)",
                    ((generation, value.path, value.seq, int(value.deleted),
                      value.payload) for value in batch.documents),
                )
                conn.executemany(
                    "INSERT INTO candidate_log_rows VALUES(?,?,?,?,?)",
                    ((generation, value.id, value.chat_id, value.log_name,
                      value.payload) for value in batch.log_rows),
                )
                conn.executemany(
                    "INSERT INTO candidate_visibility VALUES(?,?,?)",
                    ((generation, value.chat_id, int(value.visible))
                     for value in batch.visibility),
                )
            except sqlite3.IntegrityError as exc:
                raise NodeInputError("candidate chunks overlap") from exc
            conn.executemany(
                "INSERT INTO candidate_frontiers VALUES(?,?,?,?,?) "
                "ON CONFLICT(generation,name) DO UPDATE SET epoch=excluded.epoch,"
                "cursor=excluded.cursor,minimum_cursor=excluded.minimum_cursor",
                ((generation, value.name, value.epoch, value.cursor,
                  value.minimum_cursor) for value in batch.frontiers),
            )
            conn.execute(
                "INSERT INTO candidate_chunks VALUES(?,?,?,?,?,?,?,?)",
                (generation, chunk, digest, len(batch.documents),
                 len(batch.log_rows), len(batch.visibility),
                 len(batch.frontiers), batch.byte_size),
            )
            counts = (
                row[5] + len(batch.documents),
                row[6] + len(batch.log_rows),
                row[7] + len(batch.visibility),
                conn.execute("SELECT count(*) FROM candidate_frontiers WHERE generation=?",
                             (generation,)).fetchone()[0],
                row[9] + 1,
                row[10] + batch.byte_size,
            )
            if (counts[0] > MAX_STAGED_DOCUMENTS
                    or counts[1] > MAX_STAGED_LOG_ROWS
                    or counts[2] > MAX_STAGED_VISIBILITY
                    or counts[3] > MAX_BATCH_FRONTIERS
                    or counts[4] > MAX_STAGED_CHUNKS
                    or counts[5] > MAX_STAGED_BYTES):
                raise NodeInputError("staged candidate exceeds node budget")
            conn.execute(
                "UPDATE node_generations SET document_count=?,log_count=?,"
                "visibility_count=?,frontier_count=?,chunk_count=?,total_bytes=? "
                "WHERE generation=?", (*counts, generation),
            )
            return True

    def seal_staged_candidate(self, generation: int, *, observed_ns: int) -> None:
        if type(generation) is not int or generation <= 0:
            raise NodeInputError("invalid candidate generation")
        observed = _positive_ns(observed_ns, "candidate observation time")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                    "SELECT 1 FROM provider_recoveries WHERE generation=?",
                    (generation,)).fetchone():
                raise NodeInputError("recovery candidate requires recovery seal")
            self._seal_staged(conn, generation, observed)

    @staticmethod
    def _seal_staged(conn: sqlite3.Connection, generation: int,
                     observed: int) -> None:
            row = conn.execute(
                "SELECT state,documents_mode,logs_mode,visibility_mode,"
                "frontiers_mode FROM node_generations WHERE generation=?",
                (generation,),
            ).fetchone()
            if (row is None or row[0] != "building"
                    or any(value is None for value in row[1:])):
                raise NodeGenerationChanged("candidate is not staged")
            changed = conn.execute(
                "UPDATE node_generations SET state='sealed',observed_ns=? "
                "WHERE generation=? AND state='building'",
                (observed, generation),
            ).rowcount
            if changed != 1:
                raise NodeGenerationChanged("candidate changed while sealing")

    def seal_candidate(self, generation: int, batch: NodeInputBatch, *,
                       observed_ns: int) -> None:
        self.stage_candidate_batch(generation, "one-shot", batch)
        self.seal_staged_candidate(generation, observed_ns=observed_ns)

    @staticmethod
    def _candidate_row(conn: sqlite3.Connection, generation: int) -> tuple:
        row = conn.execute(
            "SELECT base_generation,state,observed_ns,documents_mode,logs_mode,"
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
                              documents_mode: str, logs_mode: str,
                              visibility_mode: str,
                              frontiers_mode: str) -> None:
        if documents_mode == "delta":
            total = conn.execute("""
                SELECT count(*) FROM (
                    SELECT path FROM remote_docs
                    UNION SELECT path FROM candidate_docs WHERE generation=?
                )
            """, (generation,)).fetchone()[0]
            if total > MAX_STAGED_DOCUMENTS:
                raise NodeInputError("admitted documents exceed node budget")
        if logs_mode == "delta":
            total = conn.execute("""
                SELECT count(*) FROM (
                    SELECT id FROM remote_log_rows
                    UNION SELECT id FROM candidate_log_rows WHERE generation=?
                )
            """, (generation,)).fetchone()[0]
            if total > MAX_STAGED_LOG_ROWS:
                raise NodeInputError("admitted log rows exceed node budget")
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
            if total > MAX_STAGED_VISIBILITY:
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
                         documents_mode: str, logs_mode: str,
                         visibility_mode: str,
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
        if logs_mode == "replace":
            conn.execute("DELETE FROM remote_log_rows")
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
        recovery = conn.execute(
            "SELECT recovery_id FROM provider_recoveries WHERE generation=?",
            (generation,)).fetchone()
        if recovery:
            for table in ("recovery_proof_pages", "recovery_events", "recovery_streams",
                          "recovery_manifests", "recovery_pages", "recovery_work",
                          "recovery_scopes"):
                conn.execute(f"DELETE FROM {table} WHERE recovery_id=?", recovery)
            conn.execute("DELETE FROM provider_recoveries WHERE recovery_id=?", recovery)
        for table in ("candidate_docs", "candidate_log_rows",
                      "candidate_visibility", "candidate_frontiers",
                      "candidate_chunks"):
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
            (base, state, observed, documents_mode, logs_mode, visibility_mode,
             frontiers_mode) = self._candidate_row(conn, generation)
            if state != "sealed" or observed is None:
                raise NodeGenerationChanged("candidate is not sealed")
            recovery = conn.execute(
                "SELECT recovery_id,state,database_incarnation,identity_digest,"
                "base_generation,health_owner_token FROM provider_recoveries "
                "WHERE generation=?",
                (generation,)).fetchone()
            if recovery and recovery[1] != "sealed":
                raise NodeGenerationChanged("recovery is not sealed")
            current = conn.execute(
                "SELECT admitted_generation,last_attempt_ns,database_incarnation,"
                "pending_mutations,health,last_success_ns FROM node_meta "
                "WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise sqlite3.DatabaseError("invalid local node metadata")
            recovery_stale = False
            if recovery:
                recovery_stale = (current[2] != recovery[2]
                                  or self.identity.digest != recovery[3]
                                  or current[0] != recovery[4]
                                  or bool(current[3]))
                for kind, ident, scope_generation in conn.execute(
                        "SELECT scope_kind,scope_id,generation FROM recovery_scopes "
                        "WHERE recovery_id=?", (recovery[0],)):
                    if self._scope_generation(conn, kind, ident) != (
                            scope_generation, None):
                        recovery_stale = True
            if current[0] != base or recovery_stale:
                conn.execute(
                    "UPDATE node_generations SET state='abandoned' "
                    "WHERE generation=?", (generation,))
                self._clear_candidate_rows(conn, generation)
                stale = ("recovery binding changed" if recovery_stale
                         else "candidate base generation changed")
            elif (expected_attempt_ns is not None
                  and current[1] != expected_attempt_ns):
                conn.execute(
                    "UPDATE node_generations SET state='abandoned' "
                    "WHERE generation=?", (generation,))
                self._clear_candidate_rows(conn, generation)
                stale = "candidate refresh changed"
            else:
                self._check_candidate_caps(
                    conn, generation, documents_mode, logs_mode, visibility_mode,
                    frontiers_mode)
                doc_paths = self._changed_document_paths(
                    conn, generation, documents_mode)
                visible = self._changed_visibility(
                    conn, generation, visibility_mode)
                if logs_mode == "replace":
                    log_chats = tuple(row[0] for row in conn.execute("""
                        SELECT chat_id FROM (
                            SELECT chat_id FROM remote_log_rows
                            UNION SELECT chat_id FROM candidate_log_rows
                                WHERE generation=?
                        ) ORDER BY chat_id
                    """, (generation,)))
                else:
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
                    conn, generation, documents_mode, logs_mode, visibility_mode,
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
                owns_health = recovery is None or current[1] == recovery[5]
                conn.execute(
                    "UPDATE node_meta SET admitted_generation=?,"
                    "health=CASE WHEN ? THEN ? ELSE health END,"
                    "last_success_ns=MAX(last_success_ns,?) WHERE singleton=1",
                    (generation, int(owns_health), health, observed),
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
            recovery_owner = conn.execute(
                "SELECT base_generation,health_owner_token "
                "FROM provider_recoveries WHERE generation=?", (generation,),
            ).fetchone()
            changed = conn.execute("""
                UPDATE node_generations SET state='abandoned'
                WHERE generation=? AND state IN ('building','sealed')
            """, (generation,)).rowcount
            if changed:
                self._clear_candidate_rows(conn, generation)
                if recovery_owner is not None:
                    owned_generation, owned_attempt = recovery_owner
                    if ((expected_generation is None
                         or expected_generation == owned_generation)
                            and (expected_attempt_ns is None
                                 or expected_attempt_ns == owned_attempt)):
                        conn.execute(
                            "UPDATE node_meta SET health=? WHERE singleton=1 "
                            "AND admitted_generation=? AND last_attempt_ns=?",
                            (health, owned_generation, owned_attempt),
                        )
                elif expected_generation is None and expected_attempt_ns is None:
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
