"""Dedicated inactive local-node database identity and schema."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from .protocol import HEALTH_VALUES, PROTOCOL_VERSION, NodeStatus, ReplicaIdentity
from .security import private_directory, protect_path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_schema(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=1),
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
    state TEXT NOT NULL CHECK(state IN ('building','sealed','admitted','abandoned')),
    created_ns INTEGER NOT NULL CHECK(created_ns>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_admitted_node_generation
    ON node_generations(state) WHERE state='admitted';
CREATE TABLE IF NOT EXISTS remote_docs(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    path TEXT NOT NULL, seq INTEGER NOT NULL, deleted INTEGER NOT NULL
        CHECK(deleted IN (0,1)), payload BLOB,
    PRIMARY KEY(generation,path),
    CHECK((deleted=0 AND typeof(payload)='blob')
          OR (deleted=1 AND payload IS NULL))
);
CREATE TABLE IF NOT EXISTS remote_log_rows(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    id INTEGER NOT NULL, chat_id TEXT NOT NULL, log_name TEXT NOT NULL,
    payload BLOB NOT NULL, PRIMARY KEY(generation,id)
);
CREATE INDEX IF NOT EXISTS remote_log_chat
    ON remote_log_rows(generation,chat_id,log_name,id);
CREATE TABLE IF NOT EXISTS remote_chat_visibility(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    chat_id TEXT NOT NULL, PRIMARY KEY(generation,chat_id)
);
CREATE TABLE IF NOT EXISTS remote_frontiers(
    generation INTEGER NOT NULL REFERENCES node_generations(generation),
    name TEXT NOT NULL, epoch TEXT, cursor INTEGER, minimum_cursor INTEGER,
    PRIMARY KEY(generation,name)
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
"""

_REQUIRED_TABLES = frozenset({
    "node_schema", "node_identity", "node_meta", "node_generations",
    "remote_docs", "remote_log_rows", "remote_chat_visibility",
    "remote_frontiers", "scope_versions", "local_changes",
})
_REQUIRED_INDEXES = frozenset({
    "one_admitted_node_generation", "remote_log_chat",
})


class NodeStore:
    """Own the future replica database without performing provider work."""

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

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                tables = frozenset(row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"))
                fresh = not tables
                if fresh:
                    # sqlite3.executescript commits any open transaction first;
                    # include a new BEGIN so schema plus identity stay atomic.
                    conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                elif tables != _REQUIRED_TABLES:
                    raise sqlite3.DatabaseError("incomplete local node schema")
                indexes = frozenset(row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%'"))
                if not _REQUIRED_INDEXES.issubset(indexes):
                    raise sqlite3.DatabaseError("incomplete local node schema")
                rows = conn.execute(
                    "SELECT version,protocol_version FROM node_schema "
                    "WHERE singleton=1").fetchall()
                if fresh:
                    if rows:
                        raise sqlite3.DatabaseError("invalid local node schema")
                    conn.execute("INSERT INTO node_schema VALUES(1,?,?)", (
                        SCHEMA_VERSION, PROTOCOL_VERSION))
                elif rows != [(SCHEMA_VERSION, PROTOCOL_VERSION)]:
                    raise sqlite3.DatabaseError("unsupported local node schema")
                wanted = (
                    self.identity.provider_endpoint, self.identity.root,
                    self.identity.principal, self.identity.machine,
                    self.identity.digest,
                )
                row = conn.execute(
                    "SELECT provider_endpoint,root,principal,machine,identity_digest "
                    "FROM node_identity WHERE singleton=1").fetchone()
                if fresh and row is None:
                    conn.execute(
                        "INSERT INTO node_identity VALUES(1,?,?,?,?,?)", wanted)
                elif row != wanted:
                    raise sqlite3.DatabaseError("local node identity mismatch")
                rows = conn.execute(
                    "SELECT database_incarnation,admitted_generation,health,"
                    "last_attempt_ns,last_success_ns,pending_mutations "
                    "FROM node_meta WHERE singleton=1").fetchall()
                if fresh and not rows:
                    conn.execute(
                        "INSERT INTO node_meta VALUES(1,?,0,'inactive',0,0,0)",
                        (secrets.token_hex(16),),
                    )
                elif len(rows) != 1:
                    raise sqlite3.DatabaseError("invalid local node metadata")
        # The containing directory is the sidecar boundary. Protect the main
        # file explicitly as well, including a pre-existing database.
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
            protocol_version=PROTOCOL_VERSION,
            node_epoch=node_epoch,
            database_incarnation=incarnation,
            identity_digest=self.identity.digest,
            admitted_generation=generation,
            health=health,
            started_ns=started_ns,
            last_attempt_ns=attempt,
            last_success_ns=success,
            pending_mutations=pending,
        )
