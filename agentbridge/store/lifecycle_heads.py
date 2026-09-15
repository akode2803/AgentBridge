"""Generation-fenced retained lifecycle heads in the generic docs table."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


MAX_BYTES = 1024 * 1024
MAX_IDENTITY_BYTES = 4096
MAX_SQLITE_INTEGER = 2**63 - 1
PREFIX = "lifecycle/head/"

_TABLE_SQL = (
    "CREATE TABLE lifecycle_head_versions("
    "path TEXT PRIMARY KEY,"
    "generation INTEGER NOT NULL CHECK(typeof(generation)='integer' "
    f"AND generation>=1 AND generation<={MAX_SQLITE_INTEGER}))"
)

_TRIGGERS = {
    "lifecycle_head_docs_insert": (
        "CREATE TRIGGER lifecycle_head_docs_insert AFTER INSERT ON docs "
        f"WHEN substr(NEW.path,1,{len(PREFIX)})='{PREFIX}' "
        f"AND length(NEW.path)>{len(PREFIX)} BEGIN "
        "INSERT INTO lifecycle_head_versions(path,generation) VALUES(NEW.path,1) "
        "ON CONFLICT(path) DO UPDATE SET generation=generation+1; END"
    ),
    "lifecycle_head_docs_delete": (
        "CREATE TRIGGER lifecycle_head_docs_delete AFTER DELETE ON docs "
        f"WHEN substr(OLD.path,1,{len(PREFIX)})='{PREFIX}' "
        f"AND length(OLD.path)>{len(PREFIX)} BEGIN "
        "INSERT INTO lifecycle_head_versions(path,generation) VALUES(OLD.path,1) "
        "ON CONFLICT(path) DO UPDATE SET generation=generation+1; END"
    ),
    "lifecycle_head_docs_update_old": (
        "CREATE TRIGGER lifecycle_head_docs_update_old AFTER UPDATE ON docs "
        f"WHEN OLD.path!=NEW.path AND substr(OLD.path,1,{len(PREFIX)})='{PREFIX}' "
        f"AND length(OLD.path)>{len(PREFIX)} BEGIN "
        "INSERT INTO lifecycle_head_versions(path,generation) VALUES(OLD.path,1) "
        "ON CONFLICT(path) DO UPDATE SET generation=generation+1; END"
    ),
    "lifecycle_head_docs_update_new": (
        "CREATE TRIGGER lifecycle_head_docs_update_new AFTER UPDATE ON docs "
        f"WHEN substr(NEW.path,1,{len(PREFIX)})='{PREFIX}' "
        f"AND length(NEW.path)>{len(PREFIX)} "
        "AND (OLD.path!=NEW.path OR OLD.payload IS NOT NEW.payload) BEGIN "
        "INSERT INTO lifecycle_head_versions(path,generation) VALUES(NEW.path,1) "
        "ON CONFLICT(path) DO UPDATE SET generation=generation+1; END"
    ),
}


@dataclass(frozen=True)
class LifecycleHeadPosition:
    database_path: str
    incarnation: str
    subject: str
    generation: int
    payload_json: str | None

    def __post_init__(self) -> None:
        _validate_position(self)


def initialize(conn: sqlite3.Connection) -> None:
    """Install once, then verify the complete owned schema on every reopen."""
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "cannot initialize lifecycle heads within a caller transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='lifecycle_head_versions'"
        ).fetchone() is not None
        if not exists:
            conn.execute(_TABLE_SQL)
            conn.execute(
                "INSERT INTO lifecycle_head_versions(path,generation) "
                "SELECT path,1 FROM docs WHERE substr(path,1,?)=? AND length(path)>?",
                (len(PREFIX), PREFIX, len(PREFIX)),
            )
            for sql in _TRIGGERS.values():
                conn.execute(sql)
        _verify_schema(conn)
        missing = conn.execute(
            "SELECT d.path FROM docs d LEFT JOIN lifecycle_head_versions v "
            "ON v.path=d.path WHERE substr(d.path,1,?)=? AND length(d.path)>? "
            "AND v.path IS NULL LIMIT 1",
            (len(PREFIX), PREFIX, len(PREFIX)),
        ).fetchone()
        if missing is not None:
            raise sqlite3.DatabaseError(
                "lifecycle head is missing its durable generation"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def observe(path: Path, subject: str) -> LifecycleHeadPosition:
    subject, doc_path = _subject_path(subject)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT i.incarnation,v.generation,length(CAST(d.payload AS BLOB)) "
            "FROM ingestion_identity i "
            "LEFT JOIN lifecycle_head_versions v ON v.path=? "
            "LEFT JOIN docs d ON d.path=? WHERE i.singleton=1",
            (doc_path, doc_path),
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("missing ingestion database identity")
        incarnation, generation, payload_bytes = row
        _stored_identity(incarnation)
        if generation is None:
            present = conn.execute(
                "SELECT 1 FROM docs WHERE path=?", (doc_path,)
            ).fetchone()
            if present is not None:
                raise sqlite3.DatabaseError(
                    "lifecycle head is missing its durable generation"
                )
            generation = 0
        elif type(generation) is not int or not 1 <= generation <= MAX_SQLITE_INTEGER:
            raise sqlite3.DatabaseError("invalid lifecycle head generation")
        if payload_bytes is not None and type(payload_bytes) is not int:
            raise sqlite3.DatabaseError("invalid lifecycle head payload length")
        if payload_bytes is not None and payload_bytes > MAX_BYTES:
            raise OverflowError("lifecycle head payload exceeds byte budget")
        payload_row = conn.execute(
            "SELECT payload FROM docs WHERE path=?", (doc_path,)
        ).fetchone()
        payload = payload_row[0] if payload_row is not None else None
        if payload_row is not None and type(payload) is not str:
            raise sqlite3.DatabaseError("invalid lifecycle head payload")
        return LifecycleHeadPosition(
            str(path), incarnation, subject, generation, payload,
        )
    finally:
        conn.close()


def publish(
    conn: sqlite3.Connection,
    path: Path,
    expected: LifecycleHeadPosition,
    proposed: dict,
) -> bool:
    wanted = _copy_expected(expected, path)
    if type(proposed) is not dict:
        raise ValueError("lifecycle head proposal must be an exact dict")
    payload = json.dumps(
        proposed, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise OverflowError("lifecycle head proposal exceeds byte budget")
    doc_path = PREFIX + wanted.subject
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "cannot publish lifecycle head within a caller transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _capture_current(conn, path, wanted.subject)
        if current != wanted:
            conn.rollback()
            return False
        if current.payload_json == payload:
            conn.rollback()
            return True
        now = time.time_ns()
        if current.payload_json is None:
            conn.execute(
                "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
                (doc_path, payload, now),
            )
        else:
            changed = conn.execute(
                "UPDATE docs SET payload=?,fetched_ns=? WHERE path=?",
                (payload, now, doc_path),
            )
            if changed.rowcount != 1:
                raise sqlite3.DatabaseError("observed lifecycle head disappeared")
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def _capture_current(
    conn: sqlite3.Connection, path: Path, subject: str,
) -> LifecycleHeadPosition:
    doc_path = PREFIX + subject
    row = conn.execute(
        "SELECT i.incarnation,v.generation,length(CAST(d.payload AS BLOB)) "
        "FROM ingestion_identity i "
        "LEFT JOIN lifecycle_head_versions v ON v.path=? "
        "LEFT JOIN docs d ON d.path=? WHERE i.singleton=1",
        (doc_path, doc_path),
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("missing ingestion database identity")
    incarnation, generation, payload_bytes = row
    _stored_identity(incarnation)
    present = conn.execute("SELECT 1 FROM docs WHERE path=?", (doc_path,)).fetchone()
    if present is not None and generation is None:
        raise sqlite3.DatabaseError("lifecycle head is missing its durable generation")
    if generation is not None and (
        type(generation) is not int or not 1 <= generation <= MAX_SQLITE_INTEGER
    ):
        raise sqlite3.DatabaseError("invalid lifecycle head generation")
    if payload_bytes is not None and type(payload_bytes) is not int:
        raise sqlite3.DatabaseError("invalid lifecycle head payload length")
    if payload_bytes is not None and payload_bytes > MAX_BYTES:
        raise OverflowError("lifecycle head payload exceeds byte budget")
    payload_row = conn.execute(
        "SELECT payload FROM docs WHERE path=?", (doc_path,)
    ).fetchone()
    raw = payload_row[0] if payload_row is not None else None
    if payload_row is not None and type(raw) is not str:
        raise sqlite3.DatabaseError("invalid lifecycle head payload")
    return LifecycleHeadPosition(
        str(path), incarnation, subject, generation or 0, raw,
    )


def _copy_expected(
    value: LifecycleHeadPosition, path: Path,
) -> LifecycleHeadPosition:
    if type(value) is not LifecycleHeadPosition:
        raise TypeError("expected lifecycle head position token")
    copied = LifecycleHeadPosition(
        value.database_path, value.incarnation, value.subject,
        value.generation, value.payload_json,
    )
    if copied.database_path != str(path):
        raise ValueError("lifecycle head belongs to another database path")
    return copied


def _validate_position(value: LifecycleHeadPosition) -> None:
    subject, _path = _subject_path(value.subject)
    if type(value.database_path) is not str or not value.database_path:
        raise ValueError("lifecycle head requires a database path")
    if len(value.database_path.encode("utf-8")) > MAX_IDENTITY_BYTES:
        raise OverflowError("lifecycle head database path exceeds byte budget")
    if type(value.incarnation) is not str or not value.incarnation:
        raise ValueError("lifecycle head requires a database incarnation")
    if len(value.incarnation.encode("utf-8")) > MAX_IDENTITY_BYTES:
        raise OverflowError("lifecycle head incarnation exceeds byte budget")
    if type(value.generation) is not int or not 0 <= value.generation <= MAX_SQLITE_INTEGER:
        raise ValueError("invalid lifecycle head generation")
    if value.payload_json is not None:
        if type(value.payload_json) is not str:
            raise ValueError("lifecycle head payload must be serialized text")
        if len(value.payload_json.encode("utf-8")) > MAX_BYTES:
            raise OverflowError("lifecycle head payload exceeds byte budget")
    if len((PREFIX + subject).encode("utf-8")) > MAX_BYTES:
        raise OverflowError("lifecycle head token exceeds byte budget")


def _subject_path(value: str) -> tuple[str, str]:
    if type(value) is not str or not value:
        raise ValueError("lifecycle head subject must be a nonempty string")
    if any(char in value for char in ("/", "\\", "\x00")):
        raise ValueError("invalid lifecycle head subject")
    if len(value.encode("utf-8")) > 4096:
        raise OverflowError("lifecycle head subject exceeds byte budget")
    return value, PREFIX + value


def _verify_schema(conn: sqlite3.Connection) -> None:
    columns = conn.execute("PRAGMA table_info(lifecycle_head_versions)").fetchall()
    shape = [(row[1], row[2], row[3], row[5]) for row in columns]
    if shape != [("path", "TEXT", 0, 1), ("generation", "INTEGER", 1, 0)]:
        raise sqlite3.DatabaseError("incompatible lifecycle head version table")
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='lifecycle_head_versions'"
    ).fetchone()
    if table is None or _sql(table[0]) != _sql(_TABLE_SQL):
        raise sqlite3.DatabaseError("incompatible lifecycle head version table")
    rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND name LIKE 'lifecycle_head_docs_%'"
    ).fetchall()
    installed = {str(name): _sql(sql) for name, sql in rows}
    expected = {name: _sql(sql) for name, sql in _TRIGGERS.items()}
    if installed != expected:
        raise sqlite3.DatabaseError("incompatible lifecycle head triggers")


def _sql(value: str) -> str:
    # Preserve literal case: lifecycle paths are case-sensitive SQL values.
    return " ".join(str(value).split())


def _stored_identity(value: object) -> None:
    if type(value) is not str or not value:
        raise sqlite3.DatabaseError("invalid ingestion database identity")
    if len(value.encode("utf-8")) > MAX_IDENTITY_BYTES:
        raise OverflowError("ingestion database identity exceeds byte budget")


__all__ = ["LifecycleHeadPosition", "initialize", "observe", "publish"]
