"""One manually fenced diagnostic snapshot per Store; never serving authority.

This namespace deliberately does not use document_observation's tombstone API.
Every successful publication replaces the complete snapshot. Tokens describe
historical SQLite state, not remote freshness or access permission.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .document_observation import (
    MAX_SQLITE_INTEGER, _open_reader, _validate_counter, _validate_document_path,
)

MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000


class ShadowConflict(RuntimeError):
    """The expected slot, owner, generation, or database no longer matches."""


@dataclass(frozen=True)
class ShadowSource:
    root: str
    cache: str
    mirror_nonce: str


@dataclass(frozen=True)
class ShadowPosition:
    database_path: str
    incarnation: str
    epoch: int
    generation: int
    publisher_nonce: str | None
    source: ShadowSource | None
    initialized: bool


@dataclass(frozen=True)
class ShadowSnapshot:
    source: ShadowSource
    revision: int
    provider_cursor: int
    provenance: str
    chat_ids: tuple[str, ...]
    records: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ShadowObservation:
    position: ShadowPosition
    snapshot: ShadowSnapshot | None


def initialize(conn: sqlite3.Connection) -> None:
    with _transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS diagnostic_shadow_slot("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "epoch INTEGER NOT NULL CHECK(epoch>=0),"
            "generation INTEGER NOT NULL CHECK(generation>=0),"
            "publisher TEXT,root TEXT,cache TEXT,mirror_nonce TEXT,"
            "initialized INTEGER NOT NULL CHECK(initialized IN (0,1)),"
            "revision INTEGER,provider_cursor INTEGER,provenance TEXT,"
            "chat_ids TEXT,digest TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS diagnostic_shadow_records("
            "path TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )


def inspect_position(path: Path) -> ShadowPosition:
    """Metadata only, used to request explicit takeover; never returns records."""
    conn = _open_reader(path)
    try:
        conn.execute("BEGIN")
        return _position(conn, path)
    finally:
        conn.close()


def acquire(conn: sqlite3.Connection, path: Path, expected: ShadowPosition,
            publisher_nonce: str, source: ShadowSource) -> ShadowPosition:
    wanted = _expected(expected, path)
    publisher = _identity(publisher_nonce)
    source = _source(source)
    with _transaction(conn):
        current = _match(conn, path, wanted)
        epoch = _advance(current.epoch)
        generation = _advance(current.generation)
        conn.execute("DELETE FROM diagnostic_shadow_records")
        conn.execute(
            "INSERT OR REPLACE INTO diagnostic_shadow_slot VALUES"
            "(1,?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,NULL)",
            (epoch, generation, publisher, source.root, source.cache,
             source.mirror_nonce),
        )
        return replace(current, epoch=epoch, generation=generation,
                       publisher_nonce=publisher, source=source, initialized=False)


def publish(conn: sqlite3.Connection, path: Path, expected: ShadowPosition,
            snapshot: ShadowSnapshot, *, max_documents: int = MAX_RECORDS,
            max_chat_ids: int = MAX_RECORDS, max_bytes: int = MAX_BYTES,
            ) -> ShadowPosition:
    wanted = _expected(expected, path, owned=True)
    prepared, chats, digest = _prepare(snapshot, max_documents, max_chat_ids, max_bytes)
    if prepared.source != wanted.source:
        raise ShadowConflict("snapshot belongs to another mirror")
    with _transaction(conn):
        current = _match(conn, path, wanted)
        if current.epoch == MAX_SQLITE_INTEGER:
            raise OverflowError("shadow owner epoch exhausted")
        prior = conn.execute(
            "SELECT revision,digest FROM diagnostic_shadow_slot WHERE singleton=1"
        ).fetchone()
        if current.initialized:
            if prepared.revision < prior[0]:
                raise ShadowConflict("mirror revision regressed")
            if prepared.revision == prior[0]:
                if digest != prior[1]:
                    raise ShadowConflict("equal mirror revision has different content")
                return current
        generation = _advance(current.generation)
        conn.execute("DELETE FROM diagnostic_shadow_records")
        conn.executemany("INSERT INTO diagnostic_shadow_records VALUES(?,?)",
                         prepared.records)
        conn.execute(
            "UPDATE diagnostic_shadow_slot SET generation=?,initialized=1,"
            "revision=?,provider_cursor=?,provenance=?,chat_ids=?,digest=? "
            "WHERE singleton=1",
            (generation, prepared.revision, prepared.provider_cursor,
             prepared.provenance, chats, digest),
        )
        return replace(current, generation=generation, initialized=True)


def retire(conn: sqlite3.Connection, path: Path,
           expected: ShadowPosition) -> ShadowPosition:
    wanted = _expected(expected, path, owned=True)
    with _transaction(conn):
        current = _match(conn, path, wanted)
        epoch, generation = _advance(current.epoch), _advance(current.generation)
        conn.execute("DELETE FROM diagnostic_shadow_records")
        conn.execute(
            "UPDATE diagnostic_shadow_slot SET epoch=?,generation=?,publisher=NULL,"
            "root=NULL,cache=NULL,mirror_nonce=NULL,initialized=0,revision=NULL,"
            "provider_cursor=NULL,provenance=NULL,chat_ids=NULL,digest=NULL "
            "WHERE singleton=1", (epoch, generation),
        )
        return replace(current, epoch=epoch, generation=generation,
                       publisher_nonce=None, source=None, initialized=False)


def capture(path: Path, expected: ShadowPosition, *,
            max_documents: int = MAX_RECORDS, max_chat_ids: int = MAX_RECORDS,
            max_bytes: int = MAX_BYTES) -> ShadowObservation:
    wanted = _expected(expected, path, owned=True)
    _budgets(max_documents, max_chat_ids, max_bytes)
    conn = _open_reader(path)
    try:
        conn.execute("BEGIN")
        current = _match(conn, path, wanted)
        if not current.initialized:
            return ShadowObservation(current, None)
        # Preflight before any stored payload crosses into Python.
        count, size = conn.execute(
            "SELECT count(*),coalesce(sum(length(CAST(path AS BLOB))+"
            "length(CAST(payload AS BLOB))),0) FROM diagnostic_shadow_records"
        ).fetchone()
        chat_size = conn.execute(
            "SELECT length(CAST(chat_ids AS BLOB)) FROM diagnostic_shadow_slot"
        ).fetchone()[0]
        if count > max_documents or size + chat_size > max_bytes:
            raise OverflowError("shadow capture exceeds budget")
        revision, cursor, provenance, chats = conn.execute(
            "SELECT revision,provider_cursor,provenance,chat_ids "
            "FROM diagnostic_shadow_slot"
        ).fetchone()
        snapshot = ShadowSnapshot(
            current.source, revision, cursor, provenance, tuple(json.loads(chats)),
            tuple(conn.execute("SELECT path,payload FROM diagnostic_shadow_records ORDER BY path")),
        )
    finally:
        conn.close()
    # Revalidate serialized content and all returned byte charges outside SQLite.
    snapshot, _, _ = _prepare(snapshot, max_documents, max_chat_ids, max_bytes)
    return ShadowObservation(current, snapshot)


def _position(conn: sqlite3.Connection, path: Path) -> ShadowPosition:
    row = conn.execute(
        "SELECT i.incarnation,s.epoch,s.generation,s.publisher,s.root,s.cache,"
        "s.mirror_nonce,s.initialized FROM ingestion_identity i "
        "LEFT JOIN diagnostic_shadow_slot s ON s.singleton=1 WHERE i.singleton=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("missing ingestion identity")
    if row[1] is None:
        return ShadowPosition(str(path), row[0], 0, 0, None, None, False)
    source = None if row[3] is None else ShadowSource(*row[4:7])
    return ShadowPosition(str(path), row[0], row[1], row[2], row[3], source, bool(row[7]))


def _match(conn: sqlite3.Connection, path: Path, expected: ShadowPosition) -> ShadowPosition:
    current = _position(conn, path)
    if current != expected:
        raise ShadowConflict("shadow position changed")
    return current


@contextmanager
def _transaction(conn: sqlite3.Connection):
    if conn.in_transaction:
        raise sqlite3.OperationalError("shadow operation cannot use a caller transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _identity(value: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError("identity requires a nonempty exact string without NUL")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError("identity too long")
    return value


def _source(value: ShadowSource) -> ShadowSource:
    if type(value) is not ShadowSource:
        raise ValueError("expected exact ShadowSource")
    return ShadowSource(_identity(value.root), _identity(value.cache),
                        _identity(value.mirror_nonce))


def _expected(value: ShadowPosition, path: Path, owned: bool = False) -> ShadowPosition:
    if type(value) is not ShadowPosition:
        raise ValueError("expected exact ShadowPosition")
    # Copy frozen-but-tamperable caller fields before validation and SQL locking.
    p = ShadowPosition(value.database_path, value.incarnation, value.epoch,
                       value.generation, value.publisher_nonce, value.source, value.initialized)
    _identity(p.database_path)
    _identity(p.incarnation)
    _validate_counter(p.epoch, "epoch")
    _validate_counter(p.generation, "generation")
    if type(p.initialized) is not bool:
        raise ValueError("initialized requires bool")
    if p.publisher_nonce is None:
        if p.source is not None or p.initialized:
            raise ValueError("inactive position has active metadata")
    else:
        p = replace(p, publisher_nonce=_identity(p.publisher_nonce), source=_source(p.source))
    if p.database_path != str(path) or (owned and p.publisher_nonce is None):
        raise ShadowConflict("position belongs to another store or has no owner")
    return p


def _advance(counter: int) -> int:
    if counter == MAX_SQLITE_INTEGER:
        raise OverflowError("shadow counter exhausted")
    return counter + 1


def _budgets(documents: int, chats: int, size: int) -> None:
    for value, limit in ((documents, MAX_RECORDS), (chats, MAX_RECORDS), (size, MAX_BYTES)):
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError("shadow budget exceeds supported range")


def _prepare(value: ShadowSnapshot, documents: int, chats: int, size: int):
    _budgets(documents, chats, size)
    if type(value) is not ShadowSnapshot:
        raise ValueError("expected exact ShadowSnapshot")
    s = ShadowSnapshot(value.source, value.revision, value.provider_cursor,
                       value.provenance, value.chat_ids, value.records)
    source = _source(s.source)
    _validate_counter(s.revision, "revision")
    _validate_counter(s.provider_cursor, "provider_cursor")
    if type(s.provenance) is not str or s.provenance not in (
            "bootstrap_unverified", "provider_observed"):
        raise ValueError("unknown mirror provenance")
    if type(s.chat_ids) is not tuple or type(s.records) is not tuple:
        raise ValueError("snapshot collections must be exact tuples")
    if len(s.chat_ids) > chats or len(s.records) > documents:
        raise OverflowError("shadow snapshot exceeds row budget")
    for chat in s.chat_ids:
        _identity(chat)
    if tuple(sorted(set(s.chat_ids))) != s.chat_ids:
        raise ValueError("chat ids must be unique and sorted")
    chat_json = json.dumps(s.chat_ids, ensure_ascii=False, separators=(",", ":"))
    used = sum(len(x.encode('utf-8')) for x in (
        source.root, source.cache, source.mirror_nonce, s.provenance, chat_json))
    if used > size:
        raise OverflowError("shadow snapshot exceeds byte budget")
    digest = hashlib.sha256()

    def feed(text: str) -> None:
        raw = text.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)

    for field in (source.root, source.cache, source.mirror_nonce,
                  str(s.revision), str(s.provider_cursor), s.provenance, chat_json):
        feed(field)
    previous = None
    for record in s.records:
        if type(record) is not tuple or len(record) != 2:
            raise ValueError("record must be an exact path/payload pair")
        name, payload = record
        _validate_document_path(name)
        if previous is not None and name <= previous:
            raise ValueError("record paths must be unique and sorted")
        previous = name
        if type(payload) is not str:
            raise ValueError("payload must be an exact serialized string")
        used += len(name.encode("utf-8")) + len(payload.encode("utf-8"))
        if used > size:
            raise OverflowError("shadow snapshot exceeds byte budget")
        json.loads(payload, parse_constant=_invalid_constant)
        feed(name)
        feed(payload)
    return replace(s, source=source), chat_json, digest.hexdigest()


def _invalid_constant(value: str):
    raise ValueError("non-finite JSON value")
