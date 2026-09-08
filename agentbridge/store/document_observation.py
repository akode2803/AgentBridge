"""Isolated, bounded local observation of serialized transport documents.

These rows are concurrency evidence for later projection work.  They are not
transport freshness, authorization, or cache-admission authority.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_SOURCE_ID_BYTES = 512
MAX_DOCUMENT_PATH_BYTES = 4096
MAX_SQLITE_INTEGER = 2**63 - 1


class DocumentObservationConflict(RuntimeError):
    """The observed source position changed before publication or reset."""


@dataclass(frozen=True)
class DocumentPosition:
    database_path: str
    incarnation: str
    source_id: str
    generation: int
    cursor: int
    initialized: bool

    def __post_init__(self) -> None:
        _validate_position(self)


@dataclass(frozen=True)
class SerializedDocumentRecord:
    path: str
    payload_json: str | None
    deleted: bool

    def decoded(self) -> Any:
        """Return a detached JSON value; tombstones have no decodable value."""
        if self.deleted:
            raise ValueError("a tombstone has no document payload")
        if self.payload_json is None:
            raise RuntimeError("live observation row is missing serialized payload")
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class DocumentObservation:
    position: DocumentPosition
    records: tuple[SerializedDocumentRecord, ...]

    def documents(self) -> dict[str, Any]:
        """Decode live rows into a new mapping on every call."""
        return {
            record.path: record.decoded()
            for record in self.records
            if not record.deleted
        }

    def decoded_records(self) -> list[tuple[str, Any | None, bool]]:
        """Decode all rows while retaining an explicit tombstone discriminator."""
        return [
            (record.path, None if record.deleted else record.decoded(), record.deleted)
            for record in self.records
        ]

    def document(self, path: str, default: Any = None) -> Any:
        """Decode one live row; return default for missing rows or tombstones."""
        for record in self.records:
            if record.path == path:
                return default if record.deleted else record.decoded()
        return default


def initialize(conn: sqlite3.Connection) -> None:
    """Install the additive observation namespace atomically."""
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS document_observation_sources("
            "source_id TEXT PRIMARY KEY,"
            "generation INTEGER NOT NULL "
            "CHECK(typeof(generation)='integer' AND generation>=0),"
            "cursor INTEGER NOT NULL "
            "CHECK(typeof(cursor)='integer' AND cursor>=0),"
            "initialized INTEGER NOT NULL "
            "CHECK(typeof(initialized)='integer' AND initialized IN (0,1)))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS document_observation_records("
            "source_id TEXT NOT NULL, path TEXT NOT NULL, payload TEXT,"
            "deleted INTEGER NOT NULL "
            "CHECK(typeof(deleted)='integer' AND deleted IN (0,1)),"
            "PRIMARY KEY(source_id,path),"
            "CHECK((deleted=0 AND payload IS NOT NULL) OR "
            "(deleted=1 AND payload IS NULL)))"
        )


def capture_position(path: Path, source_id: str) -> DocumentPosition:
    """Capture committed source state without creating a source row."""
    source = _validate_source_id(source_id)
    conn = _open_reader(path)
    try:
        conn.execute("BEGIN")
        return _capture_position(conn, path, source)
    finally:
        conn.close()


def publish(
    conn: sqlite3.Connection,
    path: Path,
    expected_position: DocumentPosition,
    documents: dict,
    *,
    cursor: int,
    deleted_paths: tuple[str, ...] = (),
    full: bool = False,
    max_documents: int = 100_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> DocumentPosition:
    """Publish one preprocessed full or delta batch in a single write."""
    expected = _validate_expected(expected_position, path)
    new_cursor = _validate_counter(cursor, "cursor")
    document_budget = _validate_budget(max_documents, "max_documents")
    byte_budget = _validate_budget(max_bytes, "max_bytes")
    if type(full) is not bool:
        raise ValueError("full must be a bool")
    if type(documents) is not dict:
        raise ValueError("documents must be a dict")
    if type(deleted_paths) is not tuple:
        raise ValueError("deleted_paths must be a tuple")
    if full and deleted_paths:
        raise ValueError("full publication cannot include deleted_paths")
    if len(documents) + len(deleted_paths) > document_budget:
        raise OverflowError("document batch exceeds row budget")

    normalized: dict[str, str] = {}
    used = 0
    for raw_path, value in documents.items():
        name = _validate_document_path(raw_path)
        _validate_json_keys(value)
        payload = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        used += len(name.encode("utf-8")) + len(payload.encode("utf-8"))
        if used > byte_budget:
            raise OverflowError("document batch exceeds byte budget")
        normalized[name] = payload

    deleted: list[str] = []
    seen_deleted: set[str] = set()
    for raw_path in deleted_paths:
        name = _validate_document_path(raw_path)
        if name in seen_deleted:
            raise ValueError("deleted_paths contains duplicate paths")
        if name in normalized:
            raise ValueError("a path cannot be changed and deleted together")
        seen_deleted.add(name)
        deleted.append(name)
        used += len(name.encode("utf-8"))
        if used > byte_budget:
            raise OverflowError("document batch exceeds byte budget")

    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "cannot publish document observations within a caller transaction"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _capture_position(conn, path, expected.source_id)
        if current != expected:
            raise DocumentObservationConflict(
                "document observation position changed during publication"
            )
        if new_cursor < current.cursor:
            raise ValueError("document cursor cannot regress without reset")
        if not full and not current.initialized:
            raise ValueError("document deltas require an initialized source")
        if current.generation == MAX_SQLITE_INTEGER:
            raise OverflowError("document generation is exhausted")

        if full:
            conn.execute(
                "UPDATE document_observation_records SET payload=NULL,deleted=1 "
                "WHERE source_id=?",
                (expected.source_id,),
            )
        for name, payload in normalized.items():
            conn.execute(
                "INSERT INTO document_observation_records"
                "(source_id,path,payload,deleted) VALUES(?,?,?,0) "
                "ON CONFLICT(source_id,path) DO UPDATE SET "
                "payload=excluded.payload,deleted=0",
                (expected.source_id, name, payload),
            )
        for name in deleted:
            conn.execute(
                "INSERT INTO document_observation_records"
                "(source_id,path,payload,deleted) VALUES(?,?,NULL,1) "
                "ON CONFLICT(source_id,path) DO UPDATE SET payload=NULL,deleted=1",
                (expected.source_id, name),
            )
        count = conn.execute(
            "SELECT count(*) FROM document_observation_records WHERE source_id=?",
            (expected.source_id,),
        ).fetchone()[0]
        if count > document_budget:
            raise OverflowError("document observation exceeds row budget")

        generation = current.generation + 1
        conn.execute(
            "INSERT INTO document_observation_sources"
            "(source_id,generation,cursor,initialized) VALUES(?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET generation=excluded.generation,"
            "cursor=excluded.cursor,initialized=excluded.initialized",
            (expected.source_id, generation, new_cursor,
             int(full or current.initialized)),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return DocumentPosition(
        str(path), current.incarnation, expected.source_id,
        generation, new_cursor, full or current.initialized,
    )


def reset(
    conn: sqlite3.Connection,
    path: Path,
    expected_position: DocumentPosition,
) -> DocumentPosition:
    """Invalidate one source and remove only its observed rows."""
    expected = _validate_expected(expected_position, path)
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "cannot reset document observations within a caller transaction"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _capture_position(conn, path, expected.source_id)
        if current != expected:
            raise DocumentObservationConflict(
                "document observation position changed during reset"
            )
        if current.generation == MAX_SQLITE_INTEGER:
            raise OverflowError("document generation is exhausted")
        generation = current.generation + 1
        conn.execute(
            "DELETE FROM document_observation_records WHERE source_id=?",
            (expected.source_id,),
        )
        conn.execute(
            "INSERT INTO document_observation_sources"
            "(source_id,generation,cursor,initialized) VALUES(?,?,0,0) "
            "ON CONFLICT(source_id) DO UPDATE SET generation=excluded.generation,"
            "cursor=0,initialized=0",
            (expected.source_id, generation),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return DocumentPosition(
        str(path), current.incarnation, expected.source_id,
        generation, 0, False,
    )


def capture_observation(
    path: Path,
    source_id: str,
    *,
    max_documents: int = 100_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> DocumentObservation:
    """Capture one bounded committed source snapshot on a private reader."""
    source = _validate_source_id(source_id)
    document_budget = _validate_budget(max_documents, "max_documents")
    byte_budget = _validate_budget(max_bytes, "max_bytes")
    conn = _open_reader(path)
    try:
        conn.execute("BEGIN")
        position = _capture_position(conn, path, source)
        count, size = conn.execute(
            "SELECT count(*),coalesce(sum(length(CAST(path AS BLOB)) + "
            "coalesce(length(CAST(payload AS BLOB)),0)),0) "
            "FROM document_observation_records WHERE source_id=?",
            (source,),
        ).fetchone()
        if count > document_budget:
            raise OverflowError("document observation exceeds row budget")
        if size > byte_budget:
            raise OverflowError("document observation exceeds byte budget")
        records = tuple(
            SerializedDocumentRecord(str(name), payload, bool(deleted))
            for name, payload, deleted in conn.execute(
                "SELECT path,payload,deleted FROM document_observation_records "
                "WHERE source_id=? ORDER BY path",
                (source,),
            )
        )
        return DocumentObservation(position, records)
    finally:
        conn.close()


def _open_reader(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0
    )
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except BaseException:
        conn.close()
        raise


def _capture_position(
    conn: sqlite3.Connection, path: Path, source_id: str
) -> DocumentPosition:
    row = conn.execute(
        "SELECT i.incarnation,coalesce(s.generation,0),"
        "coalesce(s.cursor,0),coalesce(s.initialized,0) "
        "FROM ingestion_identity i LEFT JOIN document_observation_sources s "
        "ON s.source_id=? WHERE i.singleton=1",
        (source_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("missing ingestion database identity")
    return DocumentPosition(
        str(path), str(row[0]), source_id, int(row[1]), int(row[2]), bool(row[3])
    )


def _validate_expected(position: DocumentPosition, path: Path) -> DocumentPosition:
    if type(position) is not DocumentPosition:
        raise TypeError("publication requires a captured DocumentPosition")
    _validate_position(position)
    if position.database_path != str(path):
        raise DocumentObservationConflict("document position belongs to another store")
    return position


def _validate_position(position: DocumentPosition) -> None:
    if type(position.database_path) is not str or not position.database_path:
        raise ValueError("document position requires a database path")
    if type(position.incarnation) is not str or not position.incarnation:
        raise ValueError("document position requires an incarnation")
    _validate_source_id(position.source_id)
    _validate_counter(position.generation, "generation")
    _validate_counter(position.cursor, "cursor")
    if type(position.initialized) is not bool:
        raise ValueError("initialized must be a bool")


def _validate_source_id(source_id: str) -> str:
    if type(source_id) is not str or not source_id or "\x00" in source_id:
        raise ValueError("source_id must be a nonempty string without NUL")
    if len(source_id.encode("utf-8")) > MAX_SOURCE_ID_BYTES:
        raise ValueError("source_id exceeds its UTF-8 byte limit")
    return source_id


def _validate_document_path(path: str) -> str:
    if type(path) is not str or not path or "\x00" in path:
        raise ValueError("document path must be a nonempty string without NUL")
    if path.startswith("/") or "\\" in path or ":" in path:
        raise ValueError("document path must be canonical and relative")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("document path must not contain empty or dot segments")
    if len(path.encode("utf-8")) > MAX_DOCUMENT_PATH_BYTES:
        raise ValueError("document path exceeds its UTF-8 byte limit")
    return path


def _validate_counter(value: int, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_SQLITE_INTEGER:
        raise ValueError(f"{label} must be a nonnegative SQLite integer")
    return value


def _validate_budget(value: int, label: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_SQLITE_INTEGER:
        raise ValueError(f"{label} must be a nonnegative SQLite integer")
    return value


def _validate_json_keys(value: Any, active: set[int] | None = None) -> None:
    """Reject object-key coercion before serialization; tolerate shared subtrees."""
    if active is None:
        active = set()
    if isinstance(value, dict):
        marker = id(value)
        if marker in active:
            return  # json.dumps reports the circular reference before publication.
        active.add(marker)
        try:
            if any(type(key) is not str for key in value):
                raise ValueError("JSON object keys must be strings")
            for nested in value.values():
                _validate_json_keys(nested, active)
        finally:
            active.remove(marker)
    elif isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            return
        active.add(marker)
        try:
            for nested in value:
                _validate_json_keys(nested, active)
        finally:
            active.remove(marker)
