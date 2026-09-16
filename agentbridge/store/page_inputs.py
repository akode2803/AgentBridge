"""Bounded local message/overlay snapshots; no canonical policy or authority."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import document_observation as source
from . import membership_input_position as messages
from . import overlay_index as overlays

INDEX = 'idx_messages_chat_composite'
INDEX_SQL = f'CREATE INDEX {INDEX} ON messages(chat_id,ns,sender,id,length(CAST(payload AS BLOB)),kind)'
MAX_RAW_ROWS = 200
MAX_EXACT_IDS = 64
MAX_BYTES = 4 * 1024 * 1024


class PageInputsChanged(RuntimeError):
    """Input positions no longer match the requested local snapshot."""


@dataclass(frozen=True, order=True)
class MessageKey:
    ns: int
    sender: str
    id: str


@dataclass(frozen=True)
class SerializedMessage:
    key: MessageKey
    kind: str
    payload_json: str

    def decoded(self):
        try:
            doc = json.loads(self.payload_json)
        except (TypeError, ValueError, RecursionError) as exc:
            raise PageInputsChanged('malformed_message_payload') from exc
        if (type(doc) is not dict or type(doc.get('id')) not in (str, int)
                or str(doc['id']) != self.key.id
                or not isinstance(doc.get('ns'), int) or int(doc['ns']) != self.key.ns
                or type(doc.get('from', '')) is not str or doc.get('from', '') != self.key.sender
                or type(doc.get('kind', 'message')) is not str or doc.get('kind', 'message') != self.kind):
            raise PageInputsChanged('message_payload_identity_mismatch')
        return doc


@dataclass(frozen=True)
class PageInputPosition:
    messages: messages.MembershipInputPosition
    overlays: overlays.OverlayIndexPosition


@dataclass(frozen=True)
class RawPageInputs:
    position: PageInputPosition
    rows: tuple[SerializedMessage, ...]
    lookahead: MessageKey | None
    exact_rows: tuple[SerializedMessage, ...]
    absent_ids: tuple[str, ...]
    documents: source.DocumentObservation
    indexed: overlays.IndexedOverlayInputs
    proofs: tuple[tuple[str, str, bool | None], ...]
    captured_bytes: int
    raw_window_captured: bool = True


def initialize(conn):
    """Explicit off-path index build; never commit somebody else's transaction."""
    if conn.in_transaction:
        raise sqlite3.OperationalError('page index preparation requires an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (INDEX,)).fetchone() is None:
            conn.execute(INDEX_SQL)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _schema(conn):
    columns = {r[1]: (r[2].upper(), r[5]) for r in conn.execute('PRAGMA table_info(messages)')}
    required = {'chat_id': ('TEXT', 1), 'id': ('TEXT', 2), 'ns': ('INTEGER', 0),
                'sender': ('TEXT', 0), 'kind': ('TEXT', 0), 'payload': ('TEXT', 0)}
    if any(columns.get(k) != v for k, v in required.items()):
        raise PageInputsChanged('message_schema_changed')
    primary = conn.execute('PRAGMA index_xinfo(sqlite_autoindex_messages_1)').fetchall()
    if [(r[2], r[3], r[4], r[5]) for r in primary] != [
        ('chat_id', 0, 'BINARY', 1), ('id', 0, 'BINARY', 1), (None, 0, 'BINARY', 0),
    ]:
        raise PageInputsChanged('message_primary_key_changed')
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()
    if row is None:
        raise PageInputsChanged('message_index_pending')
    if row != (INDEX_SQL,):
        raise PageInputsChanged('message_index_changed')
    cols = conn.execute(f'PRAGMA index_xinfo({INDEX})').fetchall()
    if [(r[2], r[3], r[4], r[5]) for r in cols] != [
        ('chat_id', 0, 'BINARY', 1), ('ns', 0, 'BINARY', 1),
        ('sender', 0, 'BINARY', 1), ('id', 0, 'BINARY', 1),
        (None, 0, 'BINARY', 1), ('kind', 0, 'BINARY', 1),
        (None, 0, 'BINARY', 0),
    ]:
        raise PageInputsChanged('message_index_changed')


def _key(key):
    if type(key) is not MessageKey:
        raise ValueError('invalid message key')
    key = MessageKey(key.ns, key.sender, key.id)
    if type(key.ns) is not int or not 1 <= key.ns <= 2**63 - 1:
        raise ValueError('invalid message key')
    overlays._text(key.sender, 'sender', empty=True)
    overlays._text(key.id, 'message id')
    return key


def _metadata(row):
    ns, sender, ident, kind, size = row
    try:
        key = _key(MessageKey(ns, sender, ident))
        overlays._text(kind, 'message kind', 256)
    except (TypeError, ValueError) as exc:
        raise PageInputsChanged('malformed_message_key') from exc
    if type(size) is not int or size < 0:
        raise PageInputsChanged('malformed_message_size')
    return key, kind, size


def capture(
    path: Path, index: overlays.OverlayIndexPosition, *,
    expected: PageInputPosition | None = None,
    before: MessageKey | None = None, raw_limit: int = 128,
    exact_ids: tuple[str, ...] = (), state_paths: tuple[str, ...] = (),
    proof_keys: tuple[tuple[str, str], ...] = (), max_bytes: int = MAX_BYTES,
) -> RawPageInputs:
    """One SQLite read snapshot for bounded raw rows and their dependencies.

    ``raw_limit`` is NOT a visible-message limit. The canonical caller determines
    the oldest row actually folded; the lookahead is never a continuation cursor.
    This function does not count total history or infer remote completeness.
    A zero raw limit supports bounded exact-target/revalidation captures.
    """
    index = overlays._wanted(index, path)
    if expected is not None:
        if type(expected) is not PageInputPosition:
            raise ValueError('inconsistent page position')
        expected_messages, expected_overlays = expected.messages, expected.overlays
        expected = PageInputPosition(messages._copy_expected(expected_messages, str(path)),
                                     overlays._wanted(expected_overlays, path))
        if expected.overlays != index:
            raise ValueError('inconsistent page position')
        if expected.messages.chat_id != index.chat_id:
            raise ValueError('page position belongs to another chat')
    if before is not None:
        before = _key(before)
    if type(raw_limit) is not int or not 0 <= raw_limit <= MAX_RAW_ROWS:
        raise ValueError('invalid raw row limit')
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES:
        raise ValueError('invalid page byte budget')
    if type(exact_ids) is not tuple or len(exact_ids) > MAX_EXACT_IDS:
        raise ValueError('invalid exact message selectors')
    for ident in exact_ids:
        overlays._text(ident, 'message id')
    if len(set(exact_ids)) != len(exact_ids):
        raise ValueError('duplicate exact message selector')
    conn = source._open_reader(path)
    try:
        conn.execute('BEGIN')
        _schema(conn)
        position = PageInputPosition(messages._capture(conn, path, index.chat_id), index)
        if expected is not None and position != expected:
            raise PageInputsChanged('message_position_changed')
        overlays._ready(conn, path, index)
        params = [index.chat_id]
        sql = (f'SELECT ns,sender,id,kind,length(CAST(payload AS BLOB)) FROM messages INDEXED BY {INDEX} '
               'WHERE chat_id=? AND ns>0')
        if before is not None:
            sql += ' AND (ns,sender,id)<(?,?,?)'
            params.extend((before.ns, before.sender, before.id))
        sql += ' ORDER BY ns DESC,sender DESC,id DESC LIMIT ?'
        meta = [_metadata(r) for r in conn.execute(sql, (*params, raw_limit + 1))] if raw_limit else []
        lookahead = meta[raw_limit][0] if len(meta) > raw_limit else None
        selected = meta[:raw_limit]
        exact_meta, absent = [], []
        for ident in exact_ids:
            found = conn.execute(
                'SELECT ns,sender,id FROM messages WHERE chat_id=? AND id=? AND ns>0',
                (index.chat_id, ident),
            ).fetchone()
            if found is None:
                absent.append(ident)
            else:
                key = _key(MessageKey(*found))
                # Seek the covering expression index using the exact raw key.
                # Computing length(payload) through the ID primary key would
                # materialize an oversized payload before checking its budget.
                row = conn.execute(
                    f'SELECT ns,sender,id,kind,length(CAST(payload AS BLOB)) '
                    f'FROM messages INDEXED BY {INDEX} '
                    'WHERE chat_id=? AND ns=? AND sender=? AND id=?',
                    (index.chat_id, key.ns, key.sender, key.id),
                ).fetchone()
                if row is None:
                    raise PageInputsChanged('message_key_changed')
                exact_meta.append(_metadata(row))
        unique = {key.id: (key, kind, size) for key, kind, size in selected + exact_meta}
        if len(unique) > overlays.MAX_TARGETS:
            raise OverflowError('page target budget exceeded')
        used = sum(size + len(key.sender.encode()) + len(key.id.encode()) + len(kind.encode())
                   for key, kind, size in unique.values())
        used += sum(len(i.encode()) for i in exact_ids)
        if lookahead is not None:
            used += len(lookahead.sender.encode()) + len(lookahead.id.encode())
        if used > max_bytes:
            raise OverflowError('message page exceeds byte budget')
        targets = tuple(sorted(unique))
        indexed = overlays._capture(conn, path, index, targets, state_paths,
                                    max_bytes=min(overlays.MAX_SELECT_BYTES, max_bytes - used))
        # R204 budgets count UTF-8 field content, not Python/JSON framing. Charge
        # returned fields here too so combined capture cannot multiply the cap.
        used += sum(sum(len(v.encode()) for v in (d.path, d.kind, d.actor, d.shape_error, d.scalars_json))
                    for d in indexed.documents)
        used += sum(sum(len(v.encode()) for v in (c.path, c.kind, c.target, c.value))
                    for c in indexed.candidates)
        used += sum(len(v.encode()) for v in indexed.absent_states)
        doc_paths = tuple(f'chats/{index.chat_id}/overlays/{kind}/{ident}.json'
                          for ident in targets for kind in ('edits', 'redactions'))
        # Message IDs used as document basenames must obey the existing path
        # validator. Unsupported IDs fail explicitly, never broaden a prefix.
        for name in doc_paths:
            source._validate_document_path(name)
            if len(name.split('/')) != 5:
                raise ValueError('message id is not an overlay basename')
        documents = source._capture_selected(conn, path, index.source, doc_paths,
                                             max_documents=2 * overlays.MAX_TARGETS,
                                             max_bytes=max(0, max_bytes - used))
        used += sum(len(d.path.encode()) + len((d.payload_json or '').encode()) for d in documents.records)
        if type(proof_keys) is not tuple:
            raise ValueError('proof keys must be a tuple')
        available = {d.path for d in indexed.documents}
        for item in proof_keys:
            if type(item) is not tuple or len(item) != 2 or item[0] not in available:
                raise ValueError('proof key must refer to a selected document')
        evidence = overlays._proofs(conn, path, index, proof_keys)
        used += sum(len(name.encode()) + len(pub.encode()) for name, pub in proof_keys)
        if used > max_bytes:
            raise OverflowError('page dependencies exceed byte budget')
        rows = {}
        for ident, (key, kind, size) in unique.items():
            payload = conn.execute('SELECT payload FROM messages WHERE chat_id=? AND id=?',
                                   (index.chat_id, ident)).fetchone()[0]
            if type(payload) is not str or len(payload.encode()) != size:
                raise PageInputsChanged('malformed_message_payload')
            rows[ident] = SerializedMessage(key, kind, payload)
        result = RawPageInputs(position, tuple(rows[k.id] for k, _, _ in selected), lookahead,
                             tuple(rows[k.id] for k, _, _ in exact_meta), tuple(absent),
                             documents, indexed,
                             tuple((name, pub, value) for (name, pub), value in zip(proof_keys, evidence)), used, raw_limit > 0)
    finally:
        conn.close()
    # Decode/validate bounded payload identities after releasing the snapshot.
    # Immutable raw strings remain the captured data; no live lookups occur here.
    for row in {r.key.id: r for r in result.rows + result.exact_rows}.values():
        row.decoded()
    return result
