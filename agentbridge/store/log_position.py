"""Durable local ingestion positions, not message-projection authority."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


_TABLES = {
    "log_offsets": (
        "CREATE TABLE log_offsets(chat_id TEXT NOT NULL,log_name TEXT NOT NULL,"
        "offset INTEGER NOT NULL,PRIMARY KEY(chat_id,log_name))"
    ),
    "ingestion_identity": (
        "CREATE TABLE ingestion_identity(singleton INTEGER PRIMARY KEY "
        "CHECK(singleton=1),incarnation TEXT NOT NULL)"
    ),
    "ingestion_log_revisions": (
        "CREATE TABLE ingestion_log_revisions(chat_id TEXT NOT NULL, "
        "log_name TEXT NOT NULL,revision INTEGER NOT NULL "
        "CHECK(typeof(revision)='integer' AND revision>=0),"
        "PRIMARY KEY(chat_id,log_name))"
    ),
    "ingestion_chat_resets": (
        "CREATE TABLE ingestion_chat_resets(chat_id TEXT PRIMARY KEY,"
        "generation INTEGER NOT NULL CHECK(typeof(generation)='integer' "
        "AND generation>=0))"
    ),
}


def _trigger_sql(action, source):
    return (
        f"CREATE TRIGGER ingestion_offset_{action.lower()} AFTER {action} "
        "ON log_offsets BEGIN INSERT INTO ingestion_log_revisions"
        f"(chat_id,log_name,revision) VALUES({source}.chat_id,{source}.log_name,1) "
        "ON CONFLICT(chat_id,log_name) DO UPDATE SET revision=revision+1; END"
    )


_TRIGGERS = {
    f"ingestion_offset_{action.lower()}": _trigger_sql(action, source)
    for action, source in (("INSERT", "NEW"), ("UPDATE", "NEW"),
                           ("DELETE", "OLD"))
}
_TRIGGERS["ingestion_offset_move"] = (
    "CREATE TRIGGER ingestion_offset_move AFTER UPDATE ON log_offsets "
    "WHEN OLD.chat_id != NEW.chat_id OR OLD.log_name != NEW.log_name BEGIN "
    "INSERT INTO ingestion_log_revisions(chat_id,log_name,revision) "
    "VALUES(OLD.chat_id,OLD.log_name,1) ON CONFLICT(chat_id,log_name) "
    "DO UPDATE SET revision=revision+1; END"
)


@dataclass(frozen=True)
class LogPosition:
    database_path: str
    incarnation: str
    chat_id: str
    log_name: str
    reset_generation: int
    revision: int
    offset: int

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value for value in (
            self.database_path, self.incarnation, self.chat_id, self.log_name,
        )):
            raise ValueError("log position requires nonempty string scope")
        if any(type(value) is not int or value < 0 for value in (
            self.reset_generation, self.revision, self.offset,
        )):
            raise ValueError("log position counters must be nonnegative integers")


def initialize(conn: sqlite3.Connection) -> None:
    """Install additive metadata and triggers atomically, across processes."""
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_identity("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "incarnation TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO ingestion_identity VALUES(1,lower(hex(randomblob(16))))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_log_revisions("
            "chat_id TEXT NOT NULL, log_name TEXT NOT NULL,"
            "revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),"
            "PRIMARY KEY(chat_id,log_name))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_chat_resets("
            "chat_id TEXT PRIMARY KEY,"
            "generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation>=0))"
        )
        # Retain revision rows after offsets disappear. Even a no-op overwrite
        # conservatively invalidates older scans; counter overflow aborts.
        for action, source in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS ingestion_offset_{action.lower()} "
                f"AFTER {action} ON log_offsets BEGIN "
                "INSERT INTO ingestion_log_revisions(chat_id,log_name,revision) "
                f"VALUES({source}.chat_id,{source}.log_name,1) "
                "ON CONFLICT(chat_id,log_name) DO UPDATE SET revision=revision+1; END"
            )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS ingestion_offset_move "
            "AFTER UPDATE ON log_offsets "
            "WHEN OLD.chat_id != NEW.chat_id OR OLD.log_name != NEW.log_name "
            "BEGIN INSERT INTO ingestion_log_revisions(chat_id,log_name,revision) "
            "VALUES(OLD.chat_id,OLD.log_name,1) "
            "ON CONFLICT(chat_id,log_name) DO UPDATE SET revision=revision+1; END"
        )


def _verify_schema(conn: sqlite3.Connection) -> None:
    """Verify the exact tables/triggers that make log positions ABA-safe."""
    tables = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name IN (?,?,?,?)", tuple(_TABLES),
    ).fetchall())
    if ({name: _sql(value) for name, value in tables.items()}
            != {name: _sql(value) for name, value in _TABLES.items()}):
        raise RuntimeError("ingestion position schema changed")
    triggers = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
        "(tbl_name='log_offsets' OR tbl_name IN (?,?) OR "
        "(tbl_name='ingestion_chat_resets' AND "
        "name NOT GLOB 'membership_input_resets_*') OR "
        "name GLOB 'ingestion_offset_*')",
        ("ingestion_identity", "ingestion_log_revisions"),
    ).fetchall())
    if triggers != _TRIGGERS:
        raise RuntimeError("ingestion position triggers changed")
    identity = conn.execute(
        "SELECT singleton,typeof(incarnation),length(CAST(incarnation AS BLOB)),"
        "CASE WHEN typeof(incarnation)='text' AND "
        "length(CAST(incarnation AS BLOB))=32 THEN incarnation END "
        "FROM ingestion_identity LIMIT 2",
    ).fetchall()
    if (len(identity) != 1 or identity[0][:3] != (1, "text", 32)
            or type(identity[0][3]) is not str
            or any(c not in "0123456789abcdef" for c in identity[0][3])):
        raise RuntimeError("invalid ingestion database identity")


def capture(conn: sqlite3.Connection, path: Path,
            chat_id: str, log_name: str) -> LogPosition:
    """Read one coherent position without creating metadata on a read path."""
    row = conn.execute(
        "SELECT i.incarnation,coalesce(c.generation,0),"
        "coalesce(r.revision,0),coalesce(o.offset,0) "
        "FROM ingestion_identity i "
        "LEFT JOIN ingestion_chat_resets c ON c.chat_id=? "
        "LEFT JOIN ingestion_log_revisions r ON r.chat_id=? AND r.log_name=? "
        "LEFT JOIN log_offsets o ON o.chat_id=? AND o.log_name=? "
        "WHERE i.singleton=1",
        (chat_id, chat_id, log_name, chat_id, log_name),
    ).fetchone()
    if row is None:
        raise RuntimeError("missing ingestion database identity")
    return LogPosition(str(path), row[0], chat_id, log_name,
                       row[1], row[2], row[3])


def reset_chat(conn: sqlite3.Connection, chat_id: str) -> None:
    """Advance even an empty chat's reset generation in the caller's write."""
    conn.execute(
        "INSERT INTO ingestion_chat_resets(chat_id,generation) VALUES(?,1) "
        "ON CONFLICT(chat_id) DO UPDATE SET generation=generation+1",
        (chat_id,),
    )


def _sql(value):
    return (" ".join(str(value).split()).replace(", ", ",")
            .replace("( ", "(").replace(" )", ")"))
