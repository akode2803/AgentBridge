"""Bounded, at-most-once work journal for newly admitted node records.

The journal is written in the same transaction that replaces one chat's
coupled Store cut.  It contains record identities and delivery high-waters,
never an authority verdict.  Consumers claim identities before publishing
best-effort process events; current payloads are re-read from the admitted
Store and any missing/moved identity becomes an explicit resync gap.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import coupled_payload_stage, page_inputs
from .document_observation import MAX_SQLITE_INTEGER

MAX_PENDING_EVENTS = 1000
MAX_CLAIM_EVENTS = 128
MAX_PROJECT_EVENTS = 32
MAX_DELIVERIES = 32
MAX_CLAIM_BYTES = coupled_payload_stage.MAX_PAYLOAD_BYTES

_STATE = (
    "CREATE TABLE node_event_journal_state("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    "gap INTEGER NOT NULL CHECK(gap IN (0,1)))"
)
_ITEMS = (
    "CREATE TABLE node_event_journal_items("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT,chat TEXT NOT NULL,"
    "message_id TEXT NOT NULL,ns INTEGER NOT NULL,sender TEXT NOT NULL,"
    "kind TEXT NOT NULL,UNIQUE(chat,message_id),"
    "CHECK(typeof(chat)='text' AND length(CAST(chat AS BLOB)) BETWEEN 1 AND 4096),"
    "CHECK(typeof(message_id)='text' AND length(CAST(message_id AS BLOB)) BETWEEN 1 AND 4096),"
    f"CHECK(typeof(ns)='integer' AND ns BETWEEN 0 AND {MAX_SQLITE_INTEGER}),"
    "CHECK(typeof(sender)='text' AND length(CAST(sender AS BLOB))<=4096),"
    "CHECK(typeof(kind)='text' AND length(CAST(kind AS BLOB)) BETWEEN 1 AND 256))"
)
_DELIVERY = (
    "CREATE TABLE node_event_delivery("
    "chat TEXT PRIMARY KEY,max_ns INTEGER NOT NULL,"
    "CHECK(typeof(chat)='text' AND length(CAST(chat AS BLOB)) BETWEEN 1 AND 4096),"
    f"CHECK(typeof(max_ns)='integer' AND max_ns BETWEEN 1 AND {MAX_SQLITE_INTEGER}))"
)
_OWNED = {
    "node_event_journal_state": _STATE,
    "node_event_journal_items": _ITEMS,
    "node_event_delivery": _DELIVERY,
}


class NodeEventJournalChanged(RuntimeError):
    """The bounded local work journal is malformed or changed."""


@dataclass(frozen=True)
class NodeEventRecord:
    chat_id: str
    record: dict


@dataclass(frozen=True)
class NodeEventClaim:
    records: tuple[NodeEventRecord, ...]
    gap: bool


@dataclass(frozen=True)
class NodeEventIdentity:
    sequence: int
    chat_id: str
    message_id: str
    ns: int
    sender: str
    kind: str

    def __post_init__(self) -> None:
        if (type(self.sequence) is not int or self.sequence < 1
                or not _bounded_text(self.chat_id, 4096)
                or not _bounded_text(self.message_id, 4096)
                or type(self.ns) is not int
                or not 0 <= self.ns <= MAX_SQLITE_INTEGER
                or not _bounded_text(self.sender, 4096, empty=True)
                or not _bounded_text(self.kind, 256)):
            raise ValueError("invalid node event identity")


@dataclass(frozen=True)
class NodeEventConsume:
    consumed: bool
    gap: bool

    def __post_init__(self) -> None:
        if type(self.consumed) is not bool or type(self.gap) is not bool:
            raise ValueError("invalid node event consumption")
        if not self.consumed and self.gap:
            raise ValueError("unconsumed node event cannot create a gap")


@dataclass(frozen=True)
class NodeDelivery:
    chat_id: str
    max_ns: int

    def __post_init__(self) -> None:
        if (not _bounded_text(self.chat_id, 4096)
                or type(self.max_ns) is not int
                or not 1 <= self.max_ns <= MAX_SQLITE_INTEGER):
            raise ValueError("invalid node delivery")


@dataclass(frozen=True)
class NodeDeliveryPage:
    deliveries: tuple[NodeDelivery, ...]
    has_more: bool


def initialize(store) -> None:
    """Install the complete journal namespace or validate the existing one."""
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "node event journal initialization needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {
            name: sql for name, sql in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' "
                "AND name IN (?,?,?)",
                tuple(_OWNED),
            )
        }
        if existing and existing != _OWNED:
            raise NodeEventJournalChanged("node event journal schema changed")
        if not existing:
            for sql in _OWNED.values():
                conn.execute(sql)
            conn.execute(
                "INSERT INTO node_event_journal_state VALUES(1,0)",
            )
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def publish_in_transaction(
    conn: sqlite3.Connection, store, *, stage: str, chat_id: str,
) -> int:
    """Journal new record identities before the coupled replacement commits."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "node event publication requires a Store transaction",
        )
    if _store_path(conn) != Path(store.path).resolve():
        raise ValueError("node event connection belongs to another Store")
    _schema(conn)
    if not _bounded_text(stage, 4096):
        raise ValueError("invalid node event stage")
    if not _bounded_text(chat_id, 4096):
        raise ValueError("invalid node event chat")
    if conn.execute(
            "SELECT 1 FROM node_event_journal_items ORDER BY sequence "
            "LIMIT 1 OFFSET ?",
            (MAX_PENDING_EVENTS,),
    ).fetchone() is not None:
        raise NodeEventJournalChanged("node event journal capacity changed")
    row = conn.execute(
        "SELECT count(*),coalesce(max(CASE WHEN p.kind!='info' "
        "THEN p.ns ELSE 0 END),0) FROM coupled_payload_messages p "
        "LEFT JOIN messages m ON m.chat_id=? AND m.id=p.message_id "
        "WHERE p.stage=? AND m.id IS NULL",
        (chat_id, stage),
    ).fetchone()
    if (row is None or type(row[0]) is not int or type(row[1]) is not int
            or not 0 <= row[0] <= coupled_payload_stage.MAX_MESSAGES
            or not 0 <= row[1] <= MAX_SQLITE_INTEGER):
        raise NodeEventJournalChanged("invalid node event candidate")
    total, max_message_ns = row
    duplicates = conn.execute(
        "SELECT count(*) FROM coupled_payload_messages p "
        "LEFT JOIN messages m ON m.chat_id=? AND m.id=p.message_id "
        "JOIN node_event_journal_items j "
        "ON j.chat=? AND j.message_id=p.message_id "
        "WHERE p.stage=? AND m.id IS NULL",
        (chat_id, chat_id, stage),
    ).fetchone()
    if (duplicates is None or type(duplicates[0]) is not int
            or not 0 <= duplicates[0] <= total):
        raise NodeEventJournalChanged("invalid pending node event count")
    pending_duplicates = duplicates[0]
    if total:
        inserted = conn.execute(
            "INSERT INTO node_event_journal_items(chat,message_id,ns,sender,kind) "
            "SELECT ?,message_id,ns,sender,kind FROM ("
            "SELECT p.message_id,p.ns,p.sender,p.kind "
            "FROM coupled_payload_messages p LEFT JOIN messages m "
            "ON m.chat_id=? AND m.id=p.message_id "
            "WHERE p.stage=? AND m.id IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM node_event_journal_items j "
            "WHERE j.chat=? AND j.message_id=p.message_id) "
            "ORDER BY p.ns DESC,p.sender DESC,p.message_id DESC LIMIT ?) "
            "ORDER BY ns,sender,message_id",
            (chat_id, chat_id, stage, chat_id, MAX_PENDING_EVENTS),
        ).rowcount
        if (type(inserted) is not int
                or inserted != min(
                    total - pending_duplicates, MAX_PENDING_EVENTS,
                )):
            raise NodeEventJournalChanged("node event insert count changed")
        pending = conn.execute(
            "SELECT count(*) FROM node_event_journal_items",
        ).fetchone()
        if (pending is None or type(pending[0]) is not int or pending[0] < 0):
            raise NodeEventJournalChanged("invalid node event journal count")
        excess = max(0, pending[0] - MAX_PENDING_EVENTS)
        if excess:
            deleted = conn.execute(
                "DELETE FROM node_event_journal_items WHERE sequence IN ("
                "SELECT sequence FROM node_event_journal_items "
                "ORDER BY sequence LIMIT ?)",
                (excess,),
            ).rowcount
            if deleted != excess:
                raise NodeEventJournalChanged("node event pruning changed")
        if pending_duplicates or total - pending_duplicates > inserted or excess:
            changed = conn.execute(
                "UPDATE node_event_journal_state SET gap=1 WHERE singleton=1",
            ).rowcount
            if changed != 1:
                raise NodeEventJournalChanged("node event gap state changed")
    if max_message_ns:
        conn.execute(
            "INSERT INTO node_event_delivery(chat,max_ns) VALUES(?,?) "
            "ON CONFLICT(chat) DO UPDATE SET max_ns=max(max_ns,excluded.max_ns)",
            (chat_id, max_message_ns),
        )
    return total


def claim_events(store, *, limit: int = MAX_CLAIM_EVENTS) -> NodeEventClaim | None:
    """Claim one bounded at-most-once event slice before process publication."""
    if (type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_CLAIM_EVENTS):
        raise ValueError("invalid node event claim limit")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('node event claim needs an owned transaction')
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        state = conn.execute(
            "SELECT gap FROM node_event_journal_state WHERE singleton=1",
        ).fetchone()
        if state is None or state[0] not in (0, 1):
            raise NodeEventJournalChanged("invalid node event journal state")
        rows = conn.execute(
            "SELECT j.sequence,j.chat,j.message_id,j.ns,j.sender,j.kind,"
            "m.ns,m.sender,m.kind,length(CAST(m.payload AS BLOB)) "
            "FROM node_event_journal_items j "
            "LEFT JOIN messages m ON m.chat_id=j.chat AND m.id=j.message_id "
            "ORDER BY j.sequence LIMIT ?",
            (limit,),
        ).fetchall()
        gap = bool(state[0])
        if not rows and not gap:
            conn.commit()
            return None
        records = []
        sequences = []
        claimed_bytes = 0
        for row in rows:
            sequence, chat, message_id, ns, sender, kind = row[:6]
            if (type(sequence) is not int or type(chat) is not str
                    or type(message_id) is not str or type(ns) is not int
                    or type(sender) is not str or type(kind) is not str):
                raise NodeEventJournalChanged("malformed node event identity")
            sequences.append(sequence)
            payload_bytes = row[9]
            if (row[6:9] != (ns, sender, kind)
                    or type(payload_bytes) is not int
                    or not 1 <= payload_bytes <= MAX_CLAIM_BYTES):
                gap = True
                continue
            if claimed_bytes + payload_bytes > MAX_CLAIM_BYTES:
                sequences.pop()
                break
            payload_row = conn.execute(
                "SELECT payload FROM messages WHERE chat_id=? AND id=? "
                "AND ns=? AND sender=? AND kind=?",
                (chat, message_id, ns, sender, kind),
            ).fetchone()
            if payload_row is None or type(payload_row[0]) is not str:
                gap = True
                continue
            claimed_bytes += payload_bytes
            try:
                payload = json.loads(payload_row[0])
            except (json.JSONDecodeError, UnicodeError, RecursionError):
                gap = True
                continue
            payload_id = payload.get('id') if type(payload) is dict else None
            if (type(payload) is not dict
                    or type(payload_id) not in (str, int)
                    or type(payload_id) is int and (
                        payload_id == 0 or not -(2**63) <= payload_id <= 2**63 - 1)
                    or str(payload_id) != message_id
                    or payload.get("ns") != ns
                    or payload.get("from", "") != sender
                    or payload.get("kind", "message") != kind):
                gap = True
                continue
            records.append(NodeEventRecord(chat, payload))
        if sequences:
            placeholders = ",".join("?" for _ in sequences)
            deleted = conn.execute(
                f"DELETE FROM node_event_journal_items WHERE sequence IN ({placeholders})",
                tuple(sequences),
            ).rowcount
            if deleted != len(sequences):
                raise NodeEventJournalChanged("node event claim changed")
        if state[0]:
            conn.execute(
                "UPDATE node_event_journal_state SET gap=0 WHERE singleton=1",
            )
        conn.commit()
        return NodeEventClaim(tuple(records), gap)
    except BaseException:
        conn.rollback()
        raise


def peek_event_identities(
    store, *, limit: int = MAX_PROJECT_EVENTS, after_chat: str | None = None,
    kind: str | None = None,
) -> tuple[NodeEventIdentity, ...]:
    """Read one payload-free, single-chat projection batch.

    An optional fixed kind lane prevents unresolved info work from starving
    ordinary messages. It never consumes the excluded rows. The journal itself
    is admission-bounded to 1000 rows. The first pending sequence chooses the
    chat. Later identities for that chat
    are included only while their current exact payload sizes fit the canonical
    page capture ceiling. A missing, moved or individually oversize first row
    is still returned so final transactional consumption can turn it into a
    durable gap instead of starving the journal.
    """
    if (type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_PROJECT_EVENTS):
        raise ValueError("invalid node event projection limit")
    if after_chat is not None and not _bounded_text(after_chat, 4096, empty=True):
        raise ValueError('invalid node event rotation cursor')
    if kind is not None and (type(kind) is not str or kind not in ("message", "info", "unknown")):
        raise ValueError("invalid node event projection kind")
    kind_filter = ("" if kind is None else " AND kind NOT IN (?,?)"
                   if kind == 'unknown' else " AND kind=?")
    kind_args = () if kind is None else ('message', 'info') if kind == 'unknown' else (kind,)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "node event projection read needs no transaction",
        )
    _schema(conn)
    page_inputs._message_schema(conn)
    if after_chat is None:
        first = conn.execute(
            "SELECT chat FROM node_event_journal_items WHERE 1=1" + kind_filter
            + " ORDER BY sequence LIMIT 1", kind_args,
        ).fetchone()
    else:
        first = conn.execute(
            'SELECT chat FROM node_event_journal_items WHERE chat>?' + kind_filter
            + ' ORDER BY chat,sequence LIMIT 1', (after_chat, *kind_args),
        ).fetchone()
        if first is None:
            first = conn.execute(
                'SELECT chat FROM node_event_journal_items WHERE chat<=?' + kind_filter
                + ' ORDER BY chat,sequence LIMIT 1', (after_chat, *kind_args),
            ).fetchone()
    if first is None:
        return ()
    if len(first) != 1 or not _bounded_text(first[0], 4096):
        raise NodeEventJournalChanged("malformed node event chat")
    rows = conn.execute(
        "SELECT j.sequence,j.chat,j.message_id,j.ns,j.sender,j.kind,"
        "m.ns,m.sender,m.kind,length(CAST(m.payload AS BLOB)) "
        "FROM node_event_journal_items j LEFT JOIN messages m "
        "ON m.chat_id=j.chat AND m.id=j.message_id "
        "WHERE j.chat=?" + kind_filter.replace('kind', 'j.kind')
        + " ORDER BY j.sequence LIMIT ?",
        (first[0], *kind_args, limit),
    ).fetchall()
    result = []
    payload_bytes = 0
    for row in rows:
        try:
            identity = NodeEventIdentity(*row[:6])
        except (TypeError, ValueError, UnicodeError) as exc:
            raise NodeEventJournalChanged(
                "malformed node event identity",
            ) from exc
        current_size = row[9]
        current_matches = (
            row[6:9] == (identity.ns, identity.sender, identity.kind)
            and type(current_size) is int and current_size > 0
        )
        if current_matches and result \
                and payload_bytes + current_size > page_inputs.MAX_BYTES:
            break
        result.append(identity)
        if current_matches:
            payload_bytes += current_size
        if payload_bytes > page_inputs.MAX_BYTES:
            break
    return tuple(result)


def event_identity_high_water(
    store, chat_id: str, *, kind: str | None = None,
) -> int | None:
    """Return the current admitted journal sequence high-water for one chat.

    This is scheduling evidence only. It is never an authority, freshness or
    consumption verdict; final projection still exact-CAS consumes identities.
    """
    if not _bounded_text(chat_id, 4096):
        raise ValueError('invalid node event chat')
    if kind is not None and (type(kind) is not str or kind not in (
            'message', 'info', 'unknown')):
        raise ValueError('invalid node event projection kind')
    kind_filter = ("" if kind is None else " AND kind NOT IN (?,?)"
                   if kind == 'unknown' else " AND kind=?")
    kind_args = () if kind is None else ('message', 'info') if kind == 'unknown' else (kind,)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            'node event projection read needs no transaction',
        )
    _schema(conn)
    row = conn.execute(
        'SELECT max(sequence) FROM node_event_journal_items '
        'WHERE chat=?' + kind_filter,
        (chat_id, *kind_args),
    ).fetchone()
    if row is None or len(row) != 1 or row[0] is not None and (
            type(row[0]) is not int or row[0] < 1):
        raise NodeEventJournalChanged('malformed node event high-water')
    return row[0]


def peek_chat_event_identities(
    store, chat_id: str, *, after_sequence: int = 0,
    through_sequence: int, limit: int = MAX_PROJECT_EVENTS,
    kind: str | None = None,
) -> tuple[NodeEventIdentity, ...]:
    """Read a bounded identity page inside one finite scheduling sweep.

    Rows admitted after ``through_sequence`` wait for the next sweep. This
    prevents a continuous tail from indefinitely postponing retained earlier
    identities. The result remains payload-free and is only a scheduling
    hint; callers must use the normal final exact-CAS consumption fence.
    """
    if not _bounded_text(chat_id, 4096):
        raise ValueError('invalid node event chat')
    if (type(after_sequence) is not int or isinstance(after_sequence, bool)
            or after_sequence < 0):
        raise ValueError('invalid node event sequence cursor')
    if (type(through_sequence) is not int or isinstance(through_sequence, bool)
            or through_sequence < 1):
        raise ValueError('invalid node event sequence high-water')
    if after_sequence > through_sequence:
        raise ValueError('node event cursor exceeds high-water')
    if (type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_PROJECT_EVENTS):
        raise ValueError('invalid node event projection limit')
    if kind is not None and (type(kind) is not str or kind not in (
            'message', 'info', 'unknown')):
        raise ValueError('invalid node event projection kind')
    kind_filter = ("" if kind is None else " AND j.kind NOT IN (?,?)"
                   if kind == 'unknown' else " AND j.kind=?")
    kind_args = () if kind is None else ('message', 'info') if kind == 'unknown' else (kind,)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            'node event projection read needs no transaction',
        )
    _schema(conn)
    page_inputs._message_schema(conn)
    rows = conn.execute(
        "SELECT j.sequence,j.chat,j.message_id,j.ns,j.sender,j.kind,"
        "m.ns,m.sender,m.kind,length(CAST(m.payload AS BLOB)) "
        "FROM node_event_journal_items j LEFT JOIN messages m "
        "ON m.chat_id=j.chat AND m.id=j.message_id "
        "WHERE j.chat=? AND j.sequence>? AND j.sequence<=?" + kind_filter
        + " ORDER BY j.sequence LIMIT ?",
        (chat_id, after_sequence, through_sequence, *kind_args, limit),
    ).fetchall()
    result = []
    payload_bytes = 0
    for row in rows:
        try:
            identity = NodeEventIdentity(*row[:6])
        except (TypeError, ValueError, UnicodeError) as exc:
            raise NodeEventJournalChanged(
                'malformed node event identity',
            ) from exc
        current_size = row[9]
        current_matches = (
            row[6:9] == (identity.ns, identity.sender, identity.kind)
            and type(current_size) is int and current_size > 0
        )
        if current_matches and result \
                and payload_bytes + current_size > page_inputs.MAX_BYTES:
            break
        result.append(identity)
        if current_matches:
            payload_bytes += current_size
        if payload_bytes > page_inputs.MAX_BYTES:
            break
    return tuple(result)


def claim_gap(store) -> bool:
    """Atomically consume only the durable global resync bit."""
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('node event gap claim needs an owned transaction')
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        changed = conn.execute(
            "UPDATE node_event_journal_state SET gap=0 "
            "WHERE singleton=1 AND gap=1",
        ).rowcount
        if changed not in (0, 1):
            raise NodeEventJournalChanged("node event gap state changed")
        conn.commit()
        return changed == 1
    except BaseException:
        conn.rollback()
        raise


def consume_event_identities_in_transaction(
    conn: sqlite3.Connection, store,
    expected: tuple[NodeEventIdentity, ...],
) -> NodeEventConsume:
    """CAS-consume an exact projected batch under its final canonical cut.

    Caller owns the transaction and commit. Journal ABA leaves every row
    pending. A matching journal batch whose current Store identity is missing,
    moved or too large for exact canonical capture is consumed and records the
    durable resync bit.
    """
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "node event consumption requires a Store transaction",
        )
    if _store_path(conn) != Path(store.path).resolve():
        raise ValueError("node event connection belongs to another Store")
    _schema(conn)
    page_inputs._message_schema(conn)
    sequences, wanted, placeholders = _event_batch(expected)
    rows = conn.execute(
        "SELECT sequence,chat,message_id,ns,sender,kind "
        f"FROM node_event_journal_items WHERE sequence IN ({placeholders}) "
        "ORDER BY sequence",
        sequences,
    ).fetchall()
    if tuple(rows) != wanted:
        return NodeEventConsume(False, False)
    gap = False
    for item in expected:
        row = conn.execute(
            "SELECT ns,sender,kind,length(CAST(payload AS BLOB)) "
            "FROM messages WHERE chat_id=? AND id=?",
            (item.chat_id, item.message_id),
        ).fetchone()
        if (row is None
                or row[:3] != (item.ns, item.sender, item.kind)
                or type(row[3]) is not int
                or not 1 <= row[3] <= page_inputs.MAX_BYTES):
            gap = True
    deleted = conn.execute(
        f"DELETE FROM node_event_journal_items WHERE sequence IN ({placeholders})",
        sequences,
    ).rowcount
    if deleted != len(expected):
        raise NodeEventJournalChanged("node event consumption changed")
    if gap:
        changed = conn.execute(
            "UPDATE node_event_journal_state SET gap=1 WHERE singleton=1",
        ).rowcount
        if changed != 1:
            raise NodeEventJournalChanged("node event gap state changed")
    return NodeEventConsume(True, gap)


def consume_info_invalidations(
    store, expected: tuple[NodeEventIdentity, ...],
) -> NodeEventConsume:
    """CAS-consume exact info identities for content-free invalidation only.

    This boundary intentionally reads no payload and establishes no event
    type, actor, membership or visibility claim.  It is therefore suitable
    only for a canonical read-model wake.  Semantic info/reaction mapping must
    use a separately final-fenced operation with the required authority and
    overlay evidence.
    """
    if (type(expected) is not tuple or not expected
            or any(type(item) is not NodeEventIdentity or item.kind != "info"
                   for item in expected)):
        raise ValueError("info invalidation requires exact info identities")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "info invalidation consumption needs an owned transaction",
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        consumed = consume_event_identities_in_transaction(conn, store, expected)
        conn.commit()
        return consumed
    except BaseException:
        conn.rollback()
        raise


def retire_event_identities_as_gap(
    store, expected: tuple[NodeEventIdentity, ...],
    *, require_oversize: bool = False,
    state_paths: tuple[str, ...] = (), require_unknown: bool = False,
) -> bool:
    """CAS-retire one terminally unprojectable event and request resync.

    Runtime callers must first adaptively split a budget failure down to one
    identity, or establish another terminal malformed-input result. Transient
    source, proof, key and authority work must retain the journal rows instead.
    require_oversize proves the matching row's irreducible exact-capture floor
    exceeds the ceiling in this transaction, without fetching its payload.
    require_unknown independently checks the unsupported journal kind.
    """
    if (type(expected) is not tuple or len(expected) != 1
            or type(require_oversize) is not bool or type(require_unknown) is not bool
            or type(state_paths) is not tuple or len(state_paths) > 1):
        raise ValueError("terminal node event retirement requires one identity")
    sequences, wanted, placeholders = _event_batch(expected)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('node event retirement needs an owned transaction')
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        rows = conn.execute(
            "SELECT sequence,chat,message_id,ns,sender,kind "
            f"FROM node_event_journal_items WHERE sequence IN ({placeholders}) "
            "ORDER BY sequence",
            sequences,
        ).fetchall()
        if tuple(rows) != wanted:
            conn.commit()
            return False
        if require_unknown and expected[0].kind in ('message', 'info'):
            conn.commit()
            return False
        if require_oversize:
            item = expected[0]
            page_inputs._message_schema(conn)
            current = conn.execute(
                'SELECT ns,sender,kind,length(CAST(payload AS BLOB)) FROM messages '
                'WHERE chat_id=? AND id=?', (item.chat_id, item.message_id),
            ).fetchone()
            if (current is None or current[:3] != (item.ns, item.sender, item.kind)
                    or type(current[3]) is not int
                    or page_inputs.minimum_exact_capture_bytes(item.chat_id,
                        (item.ns, item.sender, item.message_id, item.kind, current[3]),
                        state_paths=state_paths) <= page_inputs.MAX_BYTES):
                conn.commit()
                return False
        deleted = conn.execute(
            f"DELETE FROM node_event_journal_items WHERE sequence IN ({placeholders})",
            sequences,
        ).rowcount
        if deleted != len(expected):
            raise NodeEventJournalChanged("node event retirement changed")
        changed = conn.execute(
            "UPDATE node_event_journal_state SET gap=1 WHERE singleton=1",
        ).rowcount
        if changed != 1:
            raise NodeEventJournalChanged("node event gap state changed")
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def pending_deliveries(
    store, *, limit: int = MAX_DELIVERIES,
) -> tuple[NodeDelivery, ...]:
    if (type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_DELIVERIES):
        raise ValueError("invalid node delivery limit")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError("node delivery read needs no transaction")
    _schema(conn)
    rows = conn.execute(
        "SELECT chat,max_ns FROM node_event_delivery ORDER BY chat LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for chat, max_ns in rows:
        if (type(chat) is not str or not chat or type(max_ns) is not int
                or not 1 <= max_ns <= MAX_SQLITE_INTEGER):
            raise NodeEventJournalChanged("malformed node delivery")
        result.append(NodeDelivery(chat, max_ns))
    return tuple(result)


def delivery_page(
    store, *, after_chat: str = "", limit: int = MAX_DELIVERIES,
) -> NodeDeliveryPage:
    """Read one bounded circular delivery slice after the prior chat key."""
    if (type(after_chat) is not str or len(after_chat.encode()) > 4096
            or type(limit) is not int or isinstance(limit, bool)
            or not 1 <= limit <= MAX_DELIVERIES):
        raise ValueError("invalid node delivery page")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError("node delivery read needs no transaction")
    _schema(conn)
    rows = conn.execute(
        "SELECT chat,max_ns FROM node_event_delivery WHERE chat>? "
        "ORDER BY chat LIMIT ?",
        (after_chat, limit + 1),
    ).fetchall()
    if after_chat and len(rows) < limit + 1:
        rows.extend(conn.execute(
            "SELECT chat,max_ns FROM node_event_delivery WHERE chat<=? "
            "ORDER BY chat LIMIT ?",
            (after_chat, limit + 1 - len(rows)),
        ).fetchall())
    has_more = len(rows) > limit
    deliveries = []
    for chat, max_ns in rows[:limit]:
        try:
            deliveries.append(NodeDelivery(chat, max_ns))
        except (TypeError, ValueError) as exc:
            raise NodeEventJournalChanged("malformed node delivery") from exc
    return NodeDeliveryPage(tuple(deliveries), has_more)


def complete_delivery(store, expected: NodeDelivery) -> bool:
    """Consume only the exact high-water handled by the external receipt write."""
    if type(expected) is not NodeDelivery:
        raise TypeError("expected node delivery")
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('node delivery completion needs an owned transaction')
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        changed = conn.execute(
            "DELETE FROM node_event_delivery WHERE chat=? AND max_ns=?",
            (expected.chat_id, expected.max_ns),
        ).rowcount
        conn.commit()
        return changed == 1
    except BaseException:
        conn.rollback()
        raise


def _schema(conn: sqlite3.Connection) -> None:
    rows = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name IN (?,?,?)",
        tuple(_OWNED),
    ))
    if rows != _OWNED:
        raise NodeEventJournalChanged("node event journal schema changed")
    owned = tuple(_OWNED)
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN (?,?,?) LIMIT 1",
        owned,
    ).fetchone()
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
        "AND tbl_name IN (?,?,?) LIMIT 1",
        owned,
    ).fetchone()
    if triggers is not None or indexes is not None:
        raise NodeEventJournalChanged("node event journal objects changed")
    if conn.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE tbl_name IN (?,?,?) "
        "OR name IN (?,?,?) LIMIT 1", owned + owned,
    ).fetchone() is not None:
        raise NodeEventJournalChanged('node event journal temporary objects changed')
    state = conn.execute(
        "SELECT singleton,gap,typeof(singleton),typeof(gap) "
        "FROM node_event_journal_state",
    ).fetchall()
    if (len(state) != 1 or state[0][0] != 1 or state[0][1] not in (0, 1)
            or state[0][2:] != ("integer", "integer")):
        raise NodeEventJournalChanged("node event journal state changed")


def _store_path(conn: sqlite3.Connection) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    main = [row for row in rows if row[1] == "main"]
    if len(main) != 1 or type(main[0][2]) is not str or not main[0][2]:
        raise NodeEventJournalChanged("node event Store path unavailable")
    return Path(main[0][2]).resolve()


def _event_batch(expected):
    if (type(expected) is not tuple or not expected
            or len(expected) > MAX_PROJECT_EVENTS
            or any(type(item) is not NodeEventIdentity for item in expected)
            or len({item.sequence for item in expected}) != len(expected)
            or len({item.message_id for item in expected}) != len(expected)
            or len({item.chat_id for item in expected}) != 1
            or tuple(item.sequence for item in expected) != tuple(sorted(
                item.sequence for item in expected
            ))):
        raise ValueError("invalid node event consumption batch")
    sequences = tuple(item.sequence for item in expected)
    wanted = tuple(
        (item.sequence, item.chat_id, item.message_id,
         item.ns, item.sender, item.kind)
        for item in expected
    )
    return sequences, wanted, ",".join("?" for _ in expected)


def _bounded_text(value, maximum: int, *, empty: bool = False) -> bool:
    if type(value) is not str or (not value and not empty):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


__all__ = [
    "MAX_CLAIM_BYTES", "MAX_CLAIM_EVENTS", "MAX_DELIVERIES",
    "MAX_PENDING_EVENTS", "MAX_PROJECT_EVENTS",
    "NodeDelivery", "NodeEventClaim", "NodeEventConsume", "NodeEventIdentity",
    "NodeEventJournalChanged", "NodeEventRecord", "claim_events", "claim_gap",
    "complete_delivery", "consume_event_identities_in_transaction",
    "consume_info_invalidations", "initialize",
    "event_identity_high_water", "peek_chat_event_identities",
    "peek_event_identities", "pending_deliveries", "publish_in_transaction",
    "retire_event_identities_as_gap",
]
