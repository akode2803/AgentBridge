"""Generation-fenced positions for local membership input mutations."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MAX_SQLITE_INTEGER = 2**63 - 1
MAX_IDENTITY_BYTES = 4096

_NAMESPACE_TABLE = "membership_input_namespace"
_VERSIONS_TABLE = "membership_input_versions"
_NAMESPACE_SQL = (
    "CREATE TABLE membership_input_namespace("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    "epoch TEXT NOT NULL)"
)
_VERSIONS_SQL = (
    "CREATE TABLE membership_input_versions("
    "chat_id TEXT PRIMARY KEY,"
    "generation INTEGER NOT NULL CHECK(typeof(generation)='integer' "
    f"AND generation>=1 AND generation<={MAX_SQLITE_INTEGER}))"
)


def _advance(new: str) -> str:
    return (
        f"SELECT CASE WHEN EXISTS(SELECT 1 FROM {_VERSIONS_TABLE} "
        f"WHERE chat_id={new} AND (typeof(generation)!='integer' "
        f"OR generation<1 OR generation>={MAX_SQLITE_INTEGER})) "
        "THEN RAISE(ABORT,'membership input generation unavailable') END; "
        f"INSERT INTO {_VERSIONS_TABLE}(chat_id,generation) VALUES({new},1) "
        "ON CONFLICT(chat_id) DO UPDATE SET generation=generation+1;"
    )


def _advance_moved() -> str:
    return (
        f"SELECT CASE WHEN NEW.chat_id!=OLD.chat_id AND EXISTS(SELECT 1 FROM "
        f"{_VERSIONS_TABLE} WHERE chat_id=NEW.chat_id AND "
        f"(typeof(generation)!='integer' OR generation<1 OR generation>="
        f"{MAX_SQLITE_INTEGER})) THEN RAISE(ABORT,'membership input generation "
        "unavailable') END; "
        f"INSERT INTO {_VERSIONS_TABLE}(chat_id,generation) "
        "SELECT NEW.chat_id,1 WHERE NEW.chat_id!=OLD.chat_id "
        "ON CONFLICT(chat_id) DO UPDATE SET generation=generation+1;"
    )


_TRIGGERS = {
    "membership_input_messages_insert": (
        "CREATE TRIGGER membership_input_messages_insert AFTER INSERT ON messages "
        f"BEGIN {_advance('NEW.chat_id')} END"
    ),
    "membership_input_messages_delete": (
        "CREATE TRIGGER membership_input_messages_delete AFTER DELETE ON messages "
        f"BEGIN {_advance('OLD.chat_id')} END"
    ),
    "membership_input_messages_update": (
        "CREATE TRIGGER membership_input_messages_update AFTER UPDATE ON messages "
        f"BEGIN {_advance('OLD.chat_id')} {_advance_moved()} END"
    ),
    "membership_input_resets_insert": (
        "CREATE TRIGGER membership_input_resets_insert AFTER INSERT "
        f"ON ingestion_chat_resets BEGIN {_advance('NEW.chat_id')} END"
    ),
    "membership_input_resets_delete": (
        "CREATE TRIGGER membership_input_resets_delete AFTER DELETE "
        f"ON ingestion_chat_resets BEGIN {_advance('OLD.chat_id')} END"
    ),
    "membership_input_resets_update": (
        "CREATE TRIGGER membership_input_resets_update AFTER UPDATE "
        f"ON ingestion_chat_resets BEGIN {_advance('OLD.chat_id')} "
        f"{_advance_moved()} END"
    ),
}
_OWNED = {_NAMESPACE_TABLE, _VERSIONS_TABLE, *_TRIGGERS}


class MembershipInputUnavailable(RuntimeError):
    """The owned membership-input position namespace is unavailable."""


@dataclass(frozen=True)
class MembershipInputPosition:
    database_path: str
    incarnation: str
    namespace_epoch: str
    chat_id: str
    generation: int

    def __post_init__(self) -> None:
        _validate_position(self)


def initialize(conn: sqlite3.Connection) -> None:
    """Atomically install a wholly absent namespace or verify an existing one."""
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "cannot initialize membership input positions in a transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _owned_names(conn)
        if existing and existing != _OWNED:
            raise MembershipInputUnavailable("partial membership input namespace")
        if not existing:
            conn.execute(_NAMESPACE_SQL)
            conn.execute(_VERSIONS_SQL)
            conn.execute(
                f"INSERT INTO {_NAMESPACE_TABLE}(singleton,epoch) VALUES(1,?)",
                (secrets.token_hex(32),),
            )
            for sql in _TRIGGERS.values():
                conn.execute(sql)
            conn.execute(
                f"INSERT INTO {_VERSIONS_TABLE}(chat_id,generation) "
                "SELECT chat_id,1 FROM (SELECT DISTINCT chat_id FROM messages "
                "UNION SELECT DISTINCT chat_id FROM ingestion_chat_resets)"
            )
        _verify_schema(conn, check_missing=True)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def observe(path: Path, chat_id: str) -> MembershipInputPosition:
    database_path, wanted_chat = _scope(path, chat_id)
    conn = _open_reader(Path(database_path))
    try:
        conn.execute("BEGIN")
        return _capture(conn, Path(database_path), wanted_chat)
    finally:
        conn.close()


def matches(path: Path, expected: MembershipInputPosition) -> bool:
    database_path = str(Path(path).resolve())
    wanted = _copy_expected(expected, database_path)
    current = observe(Path(database_path), wanted.chat_id)
    return current == wanted


def _capture(
    conn: sqlite3.Connection, path: Path, chat_id: str,
) -> MembershipInputPosition:
    """Capture inside the caller's active private read transaction."""
    database_path, wanted_chat = _scope(path, chat_id)
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "membership input capture requires an active transaction"
        )
    connection_path = conn.execute("PRAGMA database_list").fetchone()[2]
    if str(Path(connection_path).resolve()) != database_path:
        raise ValueError("membership input connection belongs to another database")
    incarnation, epoch = _verify_schema(conn)
    row = conn.execute(
        "SELECT typeof(generation),CASE WHEN typeof(generation)='integer' "
        f"THEN generation END FROM {_VERSIONS_TABLE} WHERE chat_id=?",
        (wanted_chat,),
    ).fetchone()
    present = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=? LIMIT 1),"
        "EXISTS(SELECT 1 FROM ingestion_chat_resets WHERE chat_id=? LIMIT 1)",
        (wanted_chat, wanted_chat),
    ).fetchone()
    if row is None:
        if present != (0, 0):
            raise MembershipInputUnavailable("membership input generation is missing")
        generation = 0
    else:
        generation = row[1]
        if row[0] != "integer" or type(generation) is not int \
                or not 1 <= generation <= MAX_SQLITE_INTEGER:
            raise MembershipInputUnavailable("invalid membership input generation")
    return MembershipInputPosition(
        database_path, incarnation, epoch, wanted_chat, generation,
    )


def _verify_schema(
    conn: sqlite3.Connection, *, check_missing: bool = False,
) -> tuple[str, str]:
    if _owned_names(conn) != _OWNED:
        raise MembershipInputUnavailable("partial membership input namespace")
    expected = {_NAMESPACE_TABLE: _NAMESPACE_SQL, _VERSIONS_TABLE: _VERSIONS_SQL}
    rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN (?,?)",
        (_NAMESPACE_TABLE, _VERSIONS_TABLE),
    ).fetchall()
    installed = {name: _sql(sql) for name, sql in rows}
    if installed != {name: _sql(sql) for name, sql in expected.items()}:
        raise MembershipInputUnavailable("incompatible membership input tables")
    trigger_rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND name LIKE 'membership_input_%'"
    ).fetchall()
    triggers = {name: _sql(sql) for name, sql in trigger_rows}
    if triggers != {name: _sql(sql) for name, sql in _TRIGGERS.items()}:
        raise MembershipInputUnavailable("incompatible membership input triggers")
    incarnation = _incarnation(_read_identity(
        conn, "ingestion_identity", "incarnation", 32,
        "invalid ingestion identity",
    ))
    epoch = _epoch(_read_identity(
        conn, _NAMESPACE_TABLE, "epoch", 64,
        "invalid membership input namespace identity",
    ))
    if check_missing:
        missing = conn.execute(
            f"SELECT 1 FROM (SELECT chat_id FROM messages UNION "
            "SELECT chat_id FROM ingestion_chat_resets) c LEFT JOIN "
            f"{_VERSIONS_TABLE} v ON v.chat_id=c.chat_id "
            "WHERE v.chat_id IS NULL LIMIT 1"
        ).fetchone()
        if missing is not None:
            raise MembershipInputUnavailable("membership input generation is missing")
    return incarnation, epoch


def _read_identity(conn, table: str, column: str, size: int, reason: str):
    metadata = conn.execute(
        f"SELECT singleton,typeof({column}),length(CAST({column} AS BLOB)) "
        f"FROM {table} LIMIT 2"
    ).fetchall()
    if metadata != [(1, "text", size)]:
        raise MembershipInputUnavailable(reason)
    return conn.execute(
        f"SELECT {column} FROM {table} WHERE singleton=1"
    ).fetchone()[0]


def _owned_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE (type='table' AND name IN (?,?)) "
        "OR (type='trigger' AND name LIKE 'membership_input_%')",
        (_NAMESPACE_TABLE, _VERSIONS_TABLE),
    )
    return {row[0] for row in rows}


def _copy_expected(value: MembershipInputPosition, database_path: str):
    if type(value) is not MembershipInputPosition:
        raise TypeError("expected MembershipInputPosition")
    copied = MembershipInputPosition(
        value.database_path, value.incarnation, value.namespace_epoch,
        value.chat_id, value.generation,
    )
    if copied.database_path != database_path:
        raise ValueError("membership input position belongs to another database")
    return copied


def _validate_position(value: MembershipInputPosition) -> None:
    for item, label in (
        (value.database_path, "database path"),
        (value.chat_id, "chat id"),
    ):
        _identity(item, label)
    _incarnation(value.incarnation)
    _epoch(value.namespace_epoch)
    if type(value.generation) is not int \
            or not 0 <= value.generation <= MAX_SQLITE_INTEGER:
        raise ValueError("invalid membership input generation")


def _scope(path: Path, chat_id: str) -> tuple[str, str]:
    database_path = str(Path(path).resolve())
    _identity(database_path, "database path")
    return database_path, _identity(chat_id, "chat id")


def _identity(value, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"membership input {label} must be a nonempty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"membership input {label} must be valid UTF-8") from exc
    if size > MAX_IDENTITY_BYTES:
        raise OverflowError(f"membership input {label} exceeds byte budget")
    return value


def _epoch(value) -> str:
    _identity(value, "namespace epoch")
    if len(value) != 64 or any(character not in "0123456789abcdef"
                               for character in value):
        raise MembershipInputUnavailable(
            "invalid membership input namespace epoch"
        )
    return value


def _incarnation(value) -> str:
    _identity(value, "incarnation")
    if len(value) != 32 or any(character not in "0123456789abcdef"
                               for character in value):
        raise MembershipInputUnavailable("invalid ingestion incarnation")
    return value


def _open_reader(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)


def _sql(value: str) -> str:
    return " ".join(str(value).split())


__all__ = [
    "MAX_IDENTITY_BYTES", "MAX_SQLITE_INTEGER", "MembershipInputPosition",
    "MembershipInputUnavailable", "initialize", "matches", "observe",
]
