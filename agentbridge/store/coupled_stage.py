"""Private ownership marker for coupled document/message candidates.

The marker prevents a sealed document stage from reaching the ordinary
document-only publisher.  Payload side tables and coupled admission are added
separately; this module grants no readiness or authority.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from . import local_source, overlay_index, staged_source

_TABLE = (
    "CREATE TABLE coupled_stage_owners("
    "stage TEXT PRIMARY KEY,logical_source TEXT NOT NULL,chat TEXT NOT NULL,"
    "epoch TEXT NOT NULL,phase TEXT NOT NULL "
    "CHECK(phase IN ('building','abandoned')),"
    "store_incarnation TEXT NOT NULL,owner_epoch TEXT NOT NULL,"
    "owner_revision INTEGER NOT NULL)"
)


class CoupledStageChanged(RuntimeError):
    """The private stage owner no longer matches its durable marker."""


@dataclass(frozen=True)
class CoupledStageHandle:
    document_stage: staged_source.StageHandle
    epoch: str


def initialize(store) -> None:
    """Install the ownership marker before any coupled stage is constructed."""
    staged_source.initialize(store)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled stage initialization needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='coupled_stage_owners'",
        ).fetchone() is None:
            conn.execute(_TABLE)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def begin(
        store, expected, chat_id: str, *,
        max_total_bytes=staged_source.MAX_TOTAL_BYTES,
        max_documents=staged_source.MAX_DOCUMENTS,
) -> CoupledStageHandle:
    """Open a document stage and durably reserve it for coupled admission."""
    initialize(store)
    expected = local_source._expected(store, expected)
    chat_id = overlay_index._chat(chat_id)
    stage = staged_source._begin(
        store, expected.source_id, chat_id, "chat", expected=expected,
        max_total_bytes=max_total_bytes, max_documents=max_documents,
        source_prefix="coupled:",
    )
    epoch = uuid.uuid4().hex
    conn = store._conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        staged_source._row(conn, store, stage, "building")
        current = local_source.capture_in_transaction(
            conn, store, expected.source_id,
        )
        if current != expected:
            raise CoupledStageChanged("coupled stage source changed")
        conn.execute(
            "INSERT INTO coupled_stage_owners VALUES(?,?,?,?,"
            "'building',?,?,?)",
            (stage.source_id, stage.logical_source, stage.chat_id, epoch,
             stage.incarnation, expected.epoch, expected.revision),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        staged_source.abort(store, stage)
        raise
    return CoupledStageHandle(stage, epoch)


def abort(store, handle: CoupledStageHandle) -> None:
    """Make an unfinished coupled stage eligible for bounded reclamation."""
    handle = _handle(store, handle)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled stage abort needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, store, handle)
        stage = handle.document_stage
        staged_source._row(conn, store, stage)
        if staged_source._admitted(conn, stage.source_id):
            raise CoupledStageChanged("cannot abandon admitted coupled stage")
        conn.execute(
            "UPDATE staged_sources SET phase='abandoned' WHERE source=?",
            (stage.source_id,),
        )
        if row[4] != "abandoned":
            conn.execute(
                "UPDATE coupled_stage_owners SET phase='abandoned' "
                "WHERE stage=?", (stage.source_id,),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def reject_generic_admission(conn, store, stage) -> None:
    """Fail closed when an ordinary publisher receives a coupled stage."""
    if stage.source_id.startswith("coupled:"):
        raise CoupledStageChanged(
            "coupled_stage_requires_coupled_publication",
        )
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='coupled_stage_owners'",
    ).fetchone()
    if existing is None:
        return
    _schema(conn)
    if conn.execute(
            "SELECT 1 FROM coupled_stage_owners WHERE stage=? LIMIT 1",
            (stage.source_id,)).fetchone() is not None:
        raise CoupledStageChanged(
            "coupled_stage_requires_coupled_publication",
        )


def _consume_admitted(conn, store, handle: CoupledStageHandle) -> None:
    """Remove private ownership only after this stage is the admitted source."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "coupled owner consumption requires a transaction",
        )
    handle = _handle(store, handle)
    row = _row(conn, store, handle)
    stage = handle.document_stage
    staged_source._row(conn, store, stage, "sealed")
    if row[4] != "building" or not staged_source._admitted(
            conn, stage.source_id):
        raise CoupledStageChanged("coupled stage is not admitted")
    from . import coupled_payload_stage
    if not coupled_payload_stage.owner_cleanup_ready(conn, stage.source_id):
        raise CoupledStageChanged("coupled payload remains attached")
    if conn.execute(
            "DELETE FROM coupled_stage_owners WHERE stage=? AND epoch=?",
            (stage.source_id, handle.epoch),
    ).rowcount != 1:
        raise CoupledStageChanged("coupled stage owner changed")


def cleanup_owner(conn, source_id: str) -> None:
    """Delete one marker only while its underlying stage is abandoned."""
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='coupled_stage_owners'",
    ).fetchone()
    if existing is None:
        return
    _schema(conn)
    from . import coupled_payload_stage
    if not coupled_payload_stage.owner_cleanup_ready(conn, source_id):
        return
    conn.execute(
        "DELETE FROM coupled_stage_owners WHERE stage=? AND EXISTS "
        "(SELECT 1 FROM staged_sources WHERE source=? AND phase='abandoned')",
        (source_id, source_id),
    )


def _handle(store, handle):
    if type(handle) is not CoupledStageHandle:
        raise TypeError("expected CoupledStageHandle")
    stage = staged_source._handle(store, handle.document_stage)
    if (type(handle.epoch) is not str or len(handle.epoch) != 32
            or any(c not in "0123456789abcdef" for c in handle.epoch)):
        raise ValueError("invalid coupled stage epoch")
    return CoupledStageHandle(stage, handle.epoch)


def _row(conn, store, handle):
    handle = _handle(store, handle)
    _schema(conn)
    stage = handle.document_stage
    shape = conn.execute(
        "SELECT typeof(stage),length(CAST(stage AS BLOB)),"
        "typeof(logical_source),length(CAST(logical_source AS BLOB)),"
        "typeof(chat),length(CAST(chat AS BLOB)),"
        "typeof(epoch),length(CAST(epoch AS BLOB)),"
        "typeof(phase),length(CAST(phase AS BLOB)),"
        "typeof(store_incarnation),length(CAST(store_incarnation AS BLOB)),"
        "typeof(owner_epoch),length(CAST(owner_epoch AS BLOB)),"
        "typeof(owner_revision) FROM coupled_stage_owners WHERE stage=?",
        (stage.source_id,),
    ).fetchone()
    if (shape is None or any(
            kind != "text" or type(size) is not int or size > limit
            for kind, size, limit in zip(
                shape[:-1:2], shape[1:-1:2],
                (64, 4096, 4096, 32, 16, 4096, 32), strict=True,
            )) or shape[-1] != "integer"):
        raise CoupledStageChanged("malformed coupled stage owner")
    row = conn.execute(
        "SELECT stage,logical_source,chat,epoch,phase,store_incarnation,"
        "owner_epoch,owner_revision FROM coupled_stage_owners WHERE stage=?",
        (stage.source_id,),
    ).fetchone()
    if row is None or row[:4] != (
            stage.source_id, stage.logical_source, stage.chat_id, handle.epoch):
        raise CoupledStageChanged("coupled stage owner changed")
    if (row[4] not in ("building", "abandoned")
            or row[5] != stage.incarnation
            or type(row[6]) is not str or len(row[6]) != 32
            or type(row[7]) is not int or row[7] < 0):
        raise CoupledStageChanged("malformed coupled stage owner")
    return row


def _schema(conn) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='coupled_stage_owners'",
    ).fetchone()
    if row != (_TABLE,):
        raise CoupledStageChanged("coupled stage schema changed")
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='coupled_stage_owners' LIMIT 1",
    ).fetchone() is not None:
        raise CoupledStageChanged("coupled stage triggers changed")


__all__ = [
    "CoupledStageChanged", "CoupledStageHandle", "abort", "begin",
    "initialize",
]
