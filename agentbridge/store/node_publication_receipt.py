"""Store-bound provenance for one atomically published local-node snapshot.

The receipt records which verified node cut produced the current local source.
It is historical publication evidence, not live node freshness or continuing
membership authority. Canonical readers still recompute every policy decision.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..node.admission import NodeFinalizationPosition, NodeScopePosition
from ..node.store import _node_position
from . import document_observation, local_source, membership_input_position

MAX_RECEIPT_BYTES = 64 * 1024

_TABLE = (
    "CREATE TABLE node_publication_receipts("
    "source TEXT PRIMARY KEY,source_epoch TEXT NOT NULL,"
    "source_revision INTEGER NOT NULL,physical TEXT NOT NULL,"
    "raw_incarnation TEXT NOT NULL,raw_generation INTEGER NOT NULL,"
    "raw_cursor INTEGER NOT NULL,message_incarnation TEXT NOT NULL,"
    "message_namespace_epoch TEXT NOT NULL,chat TEXT NOT NULL,"
    "message_generation INTEGER NOT NULL,node_position BLOB NOT NULL)"
)

_LEGACY_TABLE = (
    "CREATE TABLE node_publication_receipts("
    "source TEXT PRIMARY KEY,source_epoch TEXT NOT NULL,"
    "source_revision INTEGER NOT NULL,physical TEXT NOT NULL,"
    "raw_incarnation TEXT NOT NULL,raw_generation INTEGER NOT NULL,"
    "raw_cursor INTEGER NOT NULL,node_position BLOB NOT NULL)"
)


class NodePublicationReceiptChanged(local_source.SourceChanged):
    """The durable Store/node publication binding is malformed or changed."""


@dataclass(frozen=True)
class NodePublicationReceipt:
    source: local_source.SourcePosition
    messages: membership_input_position.MembershipInputPosition
    node_position: NodeFinalizationPosition


def initialize(store) -> None:
    """Install the additive receipt table outside publication/finalization cuts."""
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "node receipt initialization needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='node_publication_receipts'",
        ).fetchone()
        if row is None:
            conn.execute(_TABLE)
        elif row == (_LEGACY_TABLE,):
            # The earlier receipt did not bind the admitted message cut. Its
            # rows cannot be upgraded by filling defaults: doing so would turn
            # missing provenance into evidence. Drop only the exact owned
            # legacy schema and let the complete coupled publisher recreate
            # every receipt from a fresh node/Store capture.
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='node_publication_receipts' LIMIT 1",
            ).fetchone() is not None:
                raise NodePublicationReceiptChanged(
                    "node publication receipt triggers changed",
                )
            conn.execute("DROP TABLE node_publication_receipts")
            conn.execute(_TABLE)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def publish_in_transaction(
        conn: sqlite3.Connection, store,
        source: local_source.SourcePosition,
        messages: membership_input_position.MembershipInputPosition,
        node_position: NodeFinalizationPosition,
) -> NodePublicationReceipt:
    """Bind an admitted Store source to its exact node cut in the same commit."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "node receipt publication requires a Store transaction",
        )
    if store_path(conn) != store.path.resolve():
        raise ValueError("node receipt connection belongs to another Store")
    _schema(conn)
    source = local_source._expected(store, source)
    if (not source.ready or source.writes_pending
            or not source.raw.initialized
            or source.logical_source is None):
        raise NodePublicationReceiptChanged(
            "node receipt requires an admitted mapped source",
        )
    try:
        incarnation_bytes = len(source.raw.incarnation.encode("utf-8"))
    except UnicodeEncodeError:
        raise NodePublicationReceiptChanged(
            "invalid node receipt source binding",
        ) from None
    if not 1 <= incarnation_bytes <= 4096:
        raise NodePublicationReceiptChanged(
            "invalid node receipt source binding",
        )
    current = local_source.capture_in_transaction(conn, store, source.source_id)
    if current != source:
        raise NodePublicationReceiptChanged(
            "local source changed before node receipt publication",
        )
    path = str(store.path.resolve())
    messages = membership_input_position._copy_expected(messages, path)
    current_messages = membership_input_position._capture(
        conn, store.path, messages.chat_id,
    )
    if current_messages != messages:
        raise NodePublicationReceiptChanged(
            "message inputs changed before node receipt publication",
        )
    checked, encoded = _encode(node_position)
    if tuple(
            (item.scope_kind, item.scope_id)
            for item in checked.scope_positions
    ) != (("chat", messages.chat_id), ("root", "")):
        raise NodePublicationReceiptChanged(
            "node receipt message scope changed",
        )
    conn.execute(
        "INSERT INTO node_publication_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET source_epoch=excluded.source_epoch,"
        "source_revision=excluded.source_revision,physical=excluded.physical,"
        "raw_incarnation=excluded.raw_incarnation,"
        "raw_generation=excluded.raw_generation,raw_cursor=excluded.raw_cursor,"
        "message_incarnation=excluded.message_incarnation,"
        "message_namespace_epoch=excluded.message_namespace_epoch,"
        "chat=excluded.chat,message_generation=excluded.message_generation,"
        "node_position=excluded.node_position",
        (source.source_id, source.epoch, source.revision, source.raw.source_id,
         source.raw.incarnation, source.raw.generation, source.raw.cursor,
         messages.incarnation, messages.namespace_epoch, messages.chat_id,
         messages.generation, encoded),
    )
    return NodePublicationReceipt(source, messages, checked)


def capture_in_transaction(
        conn: sqlite3.Connection, store,
        source: local_source.SourcePosition,
        chat_id: str,
) -> NodePublicationReceipt | None:
    """Capture matching provenance from the caller's existing Store snapshot.

    A stale receipt left by a later legacy publication is ignored because its
    exact Store position no longer matches. A receipt claiming the current cut
    is fully validated or fails closed.
    """
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "node receipt capture requires a Store transaction",
        )
    if store_path(conn) != store.path.resolve():
        raise ValueError("node receipt connection belongs to another Store")
    source = local_source._expected(store, source)
    current_messages = membership_input_position._capture(
        conn, store.path, chat_id,
    )
    coupled = source.raw.source_id.startswith("coupled:")
    if not _installed(conn):
        if coupled:
            raise NodePublicationReceiptChanged(
                "admitted coupled source has no node publication receipt",
            )
        return None
    _schema(conn)
    key = source.source_id
    shape = conn.execute(
        "SELECT typeof(source),length(CAST(source AS BLOB)),"
        "typeof(source_epoch),length(CAST(source_epoch AS BLOB)),"
        "typeof(source_revision),typeof(physical),"
        "length(CAST(physical AS BLOB)),typeof(raw_incarnation),"
        "length(CAST(raw_incarnation AS BLOB)),typeof(raw_generation),"
        "typeof(raw_cursor),typeof(message_incarnation),"
        "length(CAST(message_incarnation AS BLOB)),"
        "typeof(message_namespace_epoch),"
        "length(CAST(message_namespace_epoch AS BLOB)),"
        "typeof(chat),length(CAST(chat AS BLOB)),"
        "typeof(message_generation),typeof(node_position),"
        "length(CAST(node_position AS BLOB)) "
        "FROM node_publication_receipts WHERE source=?",
        (key,),
    ).fetchone()
    if shape is None:
        if coupled:
            raise NodePublicationReceiptChanged(
                "admitted coupled source has no node publication receipt",
            )
        return None
    if (shape[0] != "text" or type(shape[1]) is not int
            or not 1 <= shape[1] <= document_observation.MAX_SOURCE_ID_BYTES
            or shape[2] != "text" or shape[3] != 32
            or shape[4] != "integer"
            or shape[5] != "text" or type(shape[6]) is not int
            or not 1 <= shape[6] <= document_observation.MAX_SOURCE_ID_BYTES
            or shape[7] != "text" or type(shape[8]) is not int
            or not 1 <= shape[8] <= 4096
            or shape[9] != "integer" or shape[10] != "integer"
            or shape[11] != "text" or shape[12] != 32
            or shape[13] != "text" or shape[14] != 64
            or shape[15] != "text" or type(shape[16]) is not int
            or not 1 <= shape[16] <= membership_input_position.MAX_IDENTITY_BYTES
            or shape[17] != "integer"
            or shape[18] != "blob" or type(shape[19]) is not int
            or not 1 <= shape[19] <= MAX_RECEIPT_BYTES):
        raise NodePublicationReceiptChanged("malformed node publication receipt")
    binding = conn.execute(
        "SELECT source,source_epoch,source_revision,physical,raw_incarnation,"
        "raw_generation,raw_cursor,message_incarnation,"
        "message_namespace_epoch,chat,message_generation "
        "FROM node_publication_receipts WHERE source=?",
        (key,),
    ).fetchone()
    expected = (
        key, source.epoch, source.revision, source.raw.source_id,
        source.raw.incarnation, source.raw.generation, source.raw.cursor,
        current_messages.incarnation, current_messages.namespace_epoch,
        current_messages.chat_id, current_messages.generation,
    )
    if binding != expected:
        if coupled:
            raise NodePublicationReceiptChanged(
                "coupled source node publication receipt changed",
            )
        return None
    encoded = conn.execute(
        "SELECT node_position FROM node_publication_receipts WHERE source=?",
        (key,),
    ).fetchone()[0]
    return NodePublicationReceipt(source, current_messages, _decode(encoded))


def _encode(value: NodeFinalizationPosition) -> tuple[
        NodeFinalizationPosition, bytes]:
    checked = _node_position(value)
    raw = [
        checked.database_path, checked.database_incarnation,
        checked.identity_digest, checked.provider_endpoint, checked.root,
        checked.admitted_generation,
        [[row.scope_kind, row.scope_id, row.generation, row.pending_reason]
         for row in checked.scope_positions],
    ]
    encoded = json.dumps(
        raw, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise OverflowError("node publication receipt exceeds byte budget")
    return checked, encoded


def serialized_size(value: NodeFinalizationPosition) -> int:
    """Return the validated bounded receipt size for operation accounting."""
    return len(_encode(value)[1])


def _decode(value: bytes) -> NodeFinalizationPosition:
    if type(value) is not bytes or not 1 <= len(value) <= MAX_RECEIPT_BYTES:
        raise NodePublicationReceiptChanged("malformed node publication receipt")
    try:
        raw = json.loads(value.decode("utf-8"))
        if type(raw) is not list or len(raw) != 7 or type(raw[6]) is not list:
            raise ValueError
        scopes = []
        for row in raw[6]:
            if type(row) is not list or len(row) != 4:
                raise ValueError
            scopes.append(NodeScopePosition(*row))
        result = NodeFinalizationPosition(*raw[:6], tuple(scopes))
        return _node_position(result)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
            RecursionError):
        raise NodePublicationReceiptChanged(
            "malformed node publication receipt",
        ) from None


def _installed(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='node_publication_receipts'",
    ).fetchone() is not None


def _schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='node_publication_receipts'",
    ).fetchone()
    if row != (_TABLE,):
        raise NodePublicationReceiptChanged(
            "node publication receipt schema changed",
        )
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='node_publication_receipts' LIMIT 1",
    ).fetchone() is not None:
        raise NodePublicationReceiptChanged(
            "node publication receipt triggers changed",
        )


def store_path(conn: sqlite3.Connection) -> Path:
    """Return the main database path after validating it is a file binding."""
    databases = {row[1]: row[2] for row in conn.execute("PRAGMA database_list")}
    if not databases.get("main"):
        raise NodePublicationReceiptChanged("node receipt Store is unbound")
    return Path(databases["main"]).resolve()


__all__ = [
    "MAX_RECEIPT_BYTES", "NodePublicationReceipt",
    "NodePublicationReceiptChanged", "capture_in_transaction", "initialize",
    "publish_in_transaction", "serialized_size",
]
