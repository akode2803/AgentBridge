"""Invisible message/raw-position payloads for one coupled document stage."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import coupled_stage, staged_source
from .document_observation import MAX_SQLITE_INTEGER

MAX_MESSAGES = 100_000
MAX_RAW_ROWS = 100_000
MAX_STREAMS = 10_000
MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 4096
MAX_KIND_BYTES = 256
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

_STATE = (
    "CREATE TABLE coupled_payload_stages("
    "stage TEXT PRIMARY KEY,epoch TEXT NOT NULL,phase TEXT NOT NULL "
    "CHECK(phase IN ('building','sealed')),revision INTEGER NOT NULL,"
    "committed_revision INTEGER NOT NULL,message_count INTEGER NOT NULL,"
    "raw_count INTEGER NOT NULL,stream_count INTEGER NOT NULL,"
    "total_bytes INTEGER NOT NULL,max_messages INTEGER NOT NULL,"
    "max_raw_rows INTEGER NOT NULL,max_streams INTEGER NOT NULL,"
    "max_bytes INTEGER NOT NULL)"
)
_MESSAGES = (
    "CREATE TABLE coupled_payload_messages("
    "stage TEXT NOT NULL,message_id TEXT NOT NULL,ns INTEGER NOT NULL,"
    "sender TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,"
    "payload_bytes INTEGER NOT NULL,PRIMARY KEY(stage,message_id))"
)
_RAW = (
    "CREATE TABLE coupled_payload_raw("
    "stage TEXT NOT NULL,log_name TEXT NOT NULL,provider_id INTEGER NOT NULL,"
    "accepted INTEGER NOT NULL,message_id TEXT,rejection TEXT,"
    "PRIMARY KEY(stage,log_name,provider_id))"
)
_STREAMS = (
    "CREATE TABLE coupled_payload_streams("
    "stage TEXT NOT NULL,log_name TEXT NOT NULL,head INTEGER NOT NULL,"
    "row_count INTEGER NOT NULL,accepted_count INTEGER NOT NULL,"
    "PRIMARY KEY(stage,log_name))"
)
_TABLES = {
    "coupled_payload_stages": _STATE,
    "coupled_payload_messages": _MESSAGES,
    "coupled_payload_raw": _RAW,
    "coupled_payload_streams": _STREAMS,
}
_PROVIDER_INDEX = (
    "CREATE UNIQUE INDEX coupled_payload_provider_id "
    "ON coupled_payload_raw(stage,provider_id)"
)


def _triggers():
    result = {}
    for table in (
            "coupled_payload_messages", "coupled_payload_raw",
            "coupled_payload_streams"):
        short = table.removeprefix("coupled_payload_")
        for action, refs in (("INSERT", ("NEW",)), ("DELETE", ("OLD",)),
                             ("UPDATE", ("OLD", "NEW"))):
            name = f"coupled_payload_{short}_{action.lower()}"
            body = " ".join(
                "SELECT CASE WHEN EXISTS(SELECT 1 FROM coupled_payload_stages "
                f"WHERE stage={ref}.stage AND revision>={MAX_SQLITE_INTEGER}) "
                "THEN RAISE(ABORT,'coupled payload revision exhausted') END; "
                "UPDATE coupled_payload_stages SET revision=revision+1 "
                f"WHERE stage={ref}.stage;" for ref in refs
            )
            result[name] = (
                f"CREATE TRIGGER {name} AFTER {action} ON {table} BEGIN "
                f"{body} END"
            )
    return result


_TRIGGERS = _triggers()


class CoupledPayloadChanged(RuntimeError):
    """The private message payload no longer matches its stage seal."""


@dataclass(frozen=True)
class RawInput:
    provider_id: int
    accepted: bool
    message_id: str | None = None
    ns: int | None = None
    sender: str | None = None
    kind: str | None = None
    payload: bytes | None = None
    rejection: str | None = None


@dataclass(frozen=True)
class PayloadHandle:
    owner: coupled_stage.CoupledStageHandle


@dataclass(frozen=True)
class PayloadSeal:
    handle: PayloadHandle
    messages: int
    raw_rows: int
    streams: int
    total_bytes: int
    revision: int


def initialize(store) -> None:
    coupled_stage.initialize(store)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled payload initialization needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE name GLOB 'coupled_payload_*'",
        ).fetchall()
        if not existing:
            for statement in _TABLES.values():
                conn.execute(statement)
            conn.execute(_PROVIDER_INDEX)
            for statement in _TRIGGERS.values():
                conn.execute(statement)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def begin(
        store, owner: coupled_stage.CoupledStageHandle, *,
        max_messages=MAX_MESSAGES, max_raw_rows=MAX_RAW_ROWS,
        max_streams=MAX_STREAMS, max_bytes=MAX_BYTES,
) -> PayloadHandle:
    initialize(store)
    owner = coupled_stage._handle(store, owner)
    _budgets(max_messages, max_raw_rows, max_streams, max_bytes)
    conn = store._conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        row = coupled_stage._row(conn, store, owner)
        staged_source._row(conn, store, owner.document_stage, "building")
        if row[4] != "building":
            raise CoupledPayloadChanged("coupled owner is not building")
        conn.execute(
            "INSERT INTO coupled_payload_stages VALUES(?,?,"
            "'building',0,0,0,0,0,0,?,?,?,?)",
            (owner.document_stage.source_id, owner.epoch, max_messages,
             max_raw_rows, max_streams, max_bytes),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return PayloadHandle(owner)


def append_stream(
        store, handle: PayloadHandle, log_name: str, head: int,
        rows: tuple[RawInput, ...],
) -> PayloadHandle:
    handle = _handle(store, handle)
    log_name = _text(log_name, "log name")
    if (type(head) is not int or not 0 <= head <= MAX_SQLITE_INTEGER
            or type(rows) is not tuple or len(rows) > MAX_RAW_ROWS):
        raise ValueError("invalid coupled stream")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled stream append needs an owned transaction",
        )
    # Bound parsing before decoding any JSON, including repeated payloads that
    # will later collapse to one message row.
    preparation_bytes = 0
    for row in rows:
        if type(row) is not RawInput:
            raise TypeError("expected RawInput")
        if row.payload is not None:
            if type(row.payload) is not bytes:
                raise ValueError("invalid message payload")
            preparation_bytes += len(row.payload)
        if preparation_bytes > MAX_BYTES:
            raise OverflowError("coupled stream preparation budget exceeded")
    prepared = tuple(_raw(row, head, log_name) for row in rows)
    if any(prepared[i].provider_id >= prepared[i + 1].provider_id
           for i in range(len(prepared) - 1)):
        raise ValueError("provider rows must be strictly ordered")
    if (prepared[-1].provider_id if prepared else 0) != head:
        raise ValueError("coupled stream did not reach declared head")
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = _row(conn, store, handle, "building")
        if (state[6] + len(prepared) > state[10]
                or state[7] + 1 > state[11]):
            raise OverflowError("coupled payload cumulative budget exceeded")
        if conn.execute(
                "SELECT 1 FROM coupled_payload_streams WHERE stage=? "
                "AND log_name=?", (handle.owner.document_stage.source_id,
                                    log_name)).fetchone() is not None:
            raise CoupledPayloadChanged("duplicate coupled stream")
        messages, used, new_messages = _insert_rows(
            conn, handle, log_name, prepared, state,
        )
        stage = handle.owner.document_stage.source_id
        conn.execute(
            "INSERT INTO coupled_payload_streams VALUES(?,?,?,?,?)",
            (stage, log_name, head, len(prepared), messages),
        )
        new_raw = state[6] + len(prepared)
        new_streams = state[7] + 1
        new_bytes = state[8] + used
        if (new_messages > state[9] or new_raw > state[10]
                or new_streams > state[11] or new_bytes > state[12]):
            raise OverflowError("coupled payload cumulative budget exceeded")
        conn.execute(
            "UPDATE coupled_payload_stages SET message_count=?,raw_count=?,"
            "stream_count=?,total_bytes=?,committed_revision=revision WHERE stage=?",
            (new_messages, new_raw, new_streams, new_bytes, stage),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return handle


def seal(store, handle: PayloadHandle) -> PayloadSeal:
    handle = _handle(store, handle)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled payload seal needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = _row(conn, store, handle, "building")
        staged_source._row(conn, store, handle.owner.document_stage, "sealed")
        stage = handle.owner.document_stage.source_id
        actual = (
            conn.execute(
                "SELECT count(*) FROM coupled_payload_messages WHERE stage=?",
                (stage,)).fetchone()[0],
            conn.execute(
                "SELECT count(*) FROM coupled_payload_raw WHERE stage=?",
                (stage,)).fetchone()[0],
            conn.execute(
                "SELECT count(*) FROM coupled_payload_streams WHERE stage=?",
                (stage,)).fetchone()[0],
        )
        if actual != state[5:8]:
            raise CoupledPayloadChanged("coupled payload counts changed")
        mismatch = conn.execute(
            "SELECT 1 FROM coupled_payload_streams s WHERE s.stage=? AND "
            "(s.row_count<>(SELECT count(*) FROM coupled_payload_raw r "
            "WHERE r.stage=s.stage AND r.log_name=s.log_name) OR "
            "s.accepted_count<>(SELECT count(*) FROM coupled_payload_raw r "
            "WHERE r.stage=s.stage AND r.log_name=s.log_name AND r.accepted=1) "
            "OR EXISTS(SELECT 1 FROM coupled_payload_raw r WHERE "
            "r.stage=s.stage AND r.log_name=s.log_name AND "
            "r.provider_id>s.head) OR s.head<>COALESCE((SELECT MAX(r.provider_id) "
            "FROM coupled_payload_raw r WHERE r.stage=s.stage AND "
            "r.log_name=s.log_name),0)) LIMIT 1", (stage,),
        ).fetchone()
        if mismatch is not None:
            raise CoupledPayloadChanged("coupled stream evidence changed")
        conn.execute(
            "UPDATE coupled_payload_stages SET phase='sealed',"
            "committed_revision=revision WHERE stage=?", (stage,),
        )
        sealed = _row(conn, store, handle, "sealed")
        result = PayloadSeal(
            handle, sealed[5], sealed[6], sealed[7], sealed[8], sealed[3],
        )
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def verify_sealed(conn, store, seal: PayloadSeal) -> None:
    if not conn.in_transaction:
        raise sqlite3.OperationalError("payload verification requires a transaction")
    databases = {row[1]: row[2] for row in conn.execute("PRAGMA database_list")}
    if (not databases.get("main")
            or Path(databases["main"]).resolve() != store.path.resolve()):
        raise ValueError("wrong coupled payload database")
    if type(seal) is not PayloadSeal:
        raise TypeError("expected PayloadSeal")
    handle = _handle(store, seal.handle)
    state = _row(conn, store, handle, "sealed")
    if (state[3] != state[4] or state[3] != seal.revision
            or state[5:9] != (
                seal.messages, seal.raw_rows, seal.streams, seal.total_bytes)):
        raise CoupledPayloadChanged("coupled payload seal changed")


def _consume_admitted(conn, store, seal: PayloadSeal) -> None:
    """Delete copied private payload rows inside the admitting transaction."""
    verify_sealed(conn, store, seal)
    stage = seal.handle.owner.document_stage.source_id
    if not staged_source._admitted(conn, stage):
        raise CoupledPayloadChanged("coupled payload stage is not admitted")
    expected = (
        ("coupled_payload_raw", seal.raw_rows),
        ("coupled_payload_messages", seal.messages),
        ("coupled_payload_streams", seal.streams),
    )
    for table, count in expected:
        changed = conn.execute(
            f"DELETE FROM {table} WHERE stage=?", (stage,),
        ).rowcount
        if changed != count:
            raise CoupledPayloadChanged("coupled payload count changed")
    if conn.execute(
            "DELETE FROM coupled_payload_stages WHERE stage=? AND epoch=?",
            (stage, seal.handle.owner.epoch),
    ).rowcount != 1:
        raise CoupledPayloadChanged("coupled payload owner changed")


def cleanup_rows(conn, source_id: str, remaining: int) -> int:
    """Delete bounded private rows before the underlying abandoned stage."""
    if not _present(conn):
        return remaining
    _schema(conn)
    for table in (
            "coupled_payload_raw", "coupled_payload_messages",
            "coupled_payload_streams"):
        if remaining <= 0:
            return 0
        rows = conn.execute(
            f"SELECT rowid FROM {table} WHERE stage=? LIMIT ?",
            (source_id, remaining),
        ).fetchall()
        conn.executemany(f"DELETE FROM {table} WHERE rowid=?", rows)
        remaining -= len(rows)
    if remaining > 0:
        conn.execute(
            "DELETE FROM coupled_payload_stages WHERE stage=?", (source_id,),
        )
    return remaining


def owner_cleanup_ready(conn, source_id: str) -> bool:
    if not _present(conn):
        return True
    _schema(conn)
    return conn.execute(
        "SELECT 1 FROM coupled_payload_stages WHERE stage=?", (source_id,),
    ).fetchone() is None


def staged_bytes(conn) -> int:
    """Charge payloads to the shared staging cap until cleanup completes."""
    if not _present(conn):
        return 0
    _schema(conn)
    rows = conn.execute(
        "SELECT total_bytes FROM coupled_payload_stages LIMIT ?",
        (staged_source.MAX_STAGES + 1,),
    ).fetchall()
    if (len(rows) > staged_source.MAX_STAGES
            or any(type(size) is not int or not 0 <= size <= MAX_BYTES
                   for (size,) in rows)):
        raise CoupledPayloadChanged("malformed coupled payload byte accounting")
    return sum(size for (size,) in rows)


def _insert_rows(conn, handle, log_name, rows, state):
    stage = handle.owner.document_stage.source_id
    log_bytes = len(log_name.encode("utf-8"))
    used = 32 + log_bytes
    count = state[5]
    document_bytes = conn.execute(
        "SELECT COALESCE(SUM(total_bytes),0) FROM "
        "(SELECT total_bytes FROM staged_sources LIMIT ?)",
        (staged_source.MAX_STAGES + 1,),
    ).fetchone()[0]
    available = min(
        state[12] - state[8],
        staged_source.MAX_GLOBAL_BYTES - document_bytes - staged_bytes(conn),
    )
    if used > available:
        raise OverflowError("coupled payload cumulative budget exceeded")
    for row in rows:
        if conn.execute(
                "SELECT 1 FROM coupled_payload_raw WHERE stage=? AND provider_id=?",
                (stage, row.provider_id)).fetchone() is not None:
            raise CoupledPayloadChanged("duplicate provider row id")
        used += 32 + log_bytes
        if row.accepted:
            # Every raw row stores its message ID, even when the message itself
            # is an exact duplicate of a previously staged envelope.
            used += len(row.message_id.encode("utf-8"))
            payload = row.payload.decode("utf-8")
            exact = (row.ns, row.sender, row.kind, payload, len(row.payload))
            old = conn.execute(
                "SELECT ns,sender,kind,payload,payload_bytes FROM "
                "coupled_payload_messages WHERE stage=? AND message_id=?",
                (stage, row.message_id),
            ).fetchone()
            if old is None:
                used += sum(len(value.encode("utf-8")) for value in (
                    row.message_id, row.sender, row.kind,
                )) + len(row.payload)
                count += 1
            elif old != exact:
                raise CoupledPayloadChanged("conflicting duplicate message id")
            if count > state[9] or used > available:
                raise OverflowError("coupled payload cumulative budget exceeded")
            if old is None:
                conn.execute(
                    "INSERT INTO coupled_payload_messages VALUES(?,?,?,?,?,?,?)",
                    (stage, row.message_id, *exact),
                )
            conn.execute(
                "INSERT INTO coupled_payload_raw VALUES(?,?,?,?,?,NULL)",
                (stage, log_name, row.provider_id, 1, row.message_id),
            )
        else:
            used += len(row.rejection.encode("utf-8"))
            if used > available:
                raise OverflowError("coupled payload cumulative budget exceeded")
            conn.execute(
                "INSERT INTO coupled_payload_raw VALUES(?,?,?,?,NULL,?)",
                (stage, log_name, row.provider_id, 0, row.rejection),
            )
    return sum(row.accepted for row in rows), used, count


def _raw(value, head, log_name):
    if type(value) is not RawInput:
        raise TypeError("expected RawInput")
    if (type(value.provider_id) is not int
            or not 1 <= value.provider_id <= head):
        raise ValueError("invalid provider row position")
    if type(value.accepted) is not bool:
        raise ValueError("invalid provider row classification")
    if value.accepted:
        message_id = _text(value.message_id, "message id")
        sender = _text(value.sender, "sender")
        if sender != log_name.split("@", 1)[0]:
            raise ValueError("message sender differs from log owner")
        kind = _text(value.kind, "message kind", MAX_KIND_BYTES)
        if (type(value.ns) is not int
                or not 1 <= value.ns <= MAX_SQLITE_INTEGER):
            raise ValueError("invalid message namespace")
        if (type(value.payload) is not bytes
                or not 1 <= len(value.payload) <= MAX_PAYLOAD_BYTES):
            raise ValueError("invalid message payload")
        try:
            decoded = json.loads(value.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ValueError("invalid message payload JSON") from exc
        if (type(decoded) is not dict
                or type(decoded.get("id")) not in (str, int)
                or str(decoded["id"]) != message_id
                or type(decoded.get("ns")) is not int
                or decoded["ns"] != value.ns
                or decoded.get("from") != sender
                or decoded.get("kind", "message") != kind):
            raise ValueError("message payload identity mismatch")
        if value.rejection is not None:
            raise ValueError("accepted row has rejection reason")
    else:
        if any(item is not None for item in (
                value.message_id, value.ns, value.sender, value.kind,
                value.payload)):
            raise ValueError("rejected row exposes message fields")
        _text(value.rejection, "rejection reason", MAX_KIND_BYTES)
    return value


def _row(conn, store, handle, phase=None):
    handle = _handle(store, handle)
    _schema(conn)
    owner = coupled_stage._row(conn, store, handle.owner)
    document = staged_source._row(conn, store, handle.owner.document_stage)
    if owner[4] != "building" or document[4] == "abandoned":
        raise CoupledPayloadChanged("coupled owner is not building")
    stage = handle.owner.document_stage.source_id
    row = conn.execute(
        "SELECT stage,epoch,phase,revision,committed_revision,message_count,"
        "raw_count,stream_count,total_bytes,max_messages,max_raw_rows,"
        "max_streams,max_bytes FROM coupled_payload_stages WHERE stage=?",
        (stage,),
    ).fetchone()
    if row is None or row[:2] != (stage, handle.owner.epoch):
        raise CoupledPayloadChanged("coupled payload owner changed")
    if (row[2] not in ("building", "sealed")
            or any(type(value) is not int or value < 0 for value in row[3:])
            or row[4] > row[3] or row[5] > row[9] or row[6] > row[10]
            or row[7] > row[11] or row[8] > row[12]):
        raise CoupledPayloadChanged("malformed coupled payload state")
    if phase is not None and row[2] != phase:
        raise CoupledPayloadChanged("coupled payload is not " + phase)
    if row[2] == "building" and row[3] != row[4]:
        raise CoupledPayloadChanged("coupled payload revision changed")
    try:
        _budgets(*row[9:13])
    except ValueError as exc:
        raise CoupledPayloadChanged("malformed coupled payload budgets") from exc
    return row


def _handle(store, value):
    if type(value) is not PayloadHandle:
        raise TypeError("expected PayloadHandle")
    return PayloadHandle(coupled_stage._handle(store, value.owner))


def _schema(conn):
    tables = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name GLOB 'coupled_payload_*'",
    ).fetchall())
    if tables != _TABLES:
        raise CoupledPayloadChanged("coupled payload schema changed")
    indexes = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='index' AND "
        "sql IS NOT NULL AND (name GLOB 'coupled_payload_*' OR "
        "tbl_name GLOB 'coupled_payload_*')",
    ).fetchall())
    if indexes != {"coupled_payload_provider_id": _PROVIDER_INDEX}:
        raise CoupledPayloadChanged("coupled payload indexes changed")
    triggers = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
        "(name GLOB 'coupled_payload_*' OR "
        "tbl_name GLOB 'coupled_payload_*')",
    ).fetchall())
    if triggers != _TRIGGERS:
        raise CoupledPayloadChanged("coupled payload triggers changed")


def _present(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name GLOB 'coupled_payload_*' LIMIT 1",
    ).fetchone() is not None


def _text(value, label, limit=MAX_TEXT_BYTES):
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"invalid {label}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {label}") from exc
    if size > limit:
        raise OverflowError(f"{label} exceeds byte budget")
    return value


def _budgets(messages, raw_rows, streams, size):
    for value, maximum in ((messages, MAX_MESSAGES), (raw_rows, MAX_RAW_ROWS),
                           (streams, MAX_STREAMS), (size, MAX_BYTES)):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("invalid coupled payload budget")


__all__ = [
    "CoupledPayloadChanged", "PayloadHandle", "PayloadSeal", "RawInput",
    "append_stream", "begin", "initialize", "seal", "verify_sealed",
]
