"""Bounded raw pin manifest and message anchors at one admitted local cut.

These observations prove only source-generation-bound input completeness. They
do not verify pins, authorize a viewer, or project a canonical message body.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import document_observation as docs, membership_input_position as messages
from . import overlay_index as overlays, page_inputs

MAX_PINS = 64
MAX_PIN_BYTES = 1024 * 1024


class PageMetadataUnavailable(RuntimeError):
    """The bounded raw evidence cannot establish a complete input set."""


@dataclass(frozen=True)
class PinManifest:
    position: page_inputs.PageInputPosition
    documents: docs.DocumentObservation


@dataclass(frozen=True)
class MessageAnchor:
    position: page_inputs.PageInputPosition
    key: page_inputs.MessageKey | None


def _position(conn: sqlite3.Connection, path: Path, index: overlays.OverlayIndexPosition):
    if not conn.in_transaction:
        raise sqlite3.OperationalError('page metadata requires an active read transaction')
    index = overlays._wanted(index, path)
    page_inputs._schema(conn)
    overlays._ready(conn, path, index)
    return page_inputs.PageInputPosition(messages._capture(conn, path, index.chat_id), index)


def _pin_path(name, chat):
    if type(name) is not str:
        raise PageMetadataUnavailable('malformed_pin_path')
    try:
        docs._validate_document_path(name)
    except (TypeError, ValueError) as exc:
        raise PageMetadataUnavailable('malformed_pin_path') from exc
    pieces = name.split('/')
    if (len(pieces) != 5 or pieces[:4] != ['chats', chat, 'overlays', 'pins']
            or not pieces[4].endswith('.json') or pieces[4] == '.json'):
        raise PageMetadataUnavailable('malformed_pin_path')
    return name


def capture_pin_manifest(conn: sqlite3.Connection, path: Path,
                         index: overlays.OverlayIndexPosition) -> PinManifest:
    """Enumerate a complete pin prefix before copying any payload bytes.

    The `(source_id,path)` primary key limits the query to 65 paths. A 65th
    path is an explicit failure, not a truncated set that could prove absence.
    Deleted/tombstone rows are rejected rather than silently reinterpreted.
    """
    position = _position(conn, path, index)
    prefix = f'chats/{index.chat_id}/overlays/pins/'
    upper = prefix[:-1] + '0'
    docs._selection_index(conn)
    rows = conn.execute(
        f'SELECT path FROM document_observation_records INDEXED BY {docs._SELECTION_INDEX} '
        'WHERE source_id=? AND path>=? AND path<? ORDER BY path LIMIT ?',
        (index.source.source_id, prefix, upper, MAX_PINS + 1),
    ).fetchall()
    if len(rows) > MAX_PINS:
        raise PageMetadataUnavailable('pin_manifest_exceeds_limit')
    names = [_pin_path(name, index.chat_id) for (name,) in rows]
    used = sum(len(name.encode('utf-8')) for name in names)
    for name in names:
        row = conn.execute(
            f'SELECT typeof(path),typeof(payload),typeof(deleted),deleted,'
            f'coalesce(length(CAST(payload AS BLOB)),0) '
            f'FROM document_observation_records INDEXED BY {docs._SELECTION_INDEX} '
            'WHERE source_id=? AND path=?', (index.source.source_id, name),
        ).fetchone()
        if row is None or row[:4] != ('text', 'text', 'integer', 0):
            raise PageMetadataUnavailable('malformed_or_deleted_pin_document')
        if type(row[4]) is not int or row[4] < 0:
            raise PageMetadataUnavailable('malformed_pin_size')
        used += row[4]
        if used > MAX_PIN_BYTES:
            raise PageMetadataUnavailable('pin_manifest_byte_budget')
    # This helper performs the exact-document byte preflight and source check
    # in the same transaction. No raw pin is returned on overflow.
    try:
        captured = docs._capture_selected(conn, path, index.source, tuple(names),
                                          max_documents=MAX_PINS, max_bytes=MAX_PIN_BYTES)
    except OverflowError as exc:
        raise PageMetadataUnavailable('pin_manifest_byte_budget') from exc
    if any(row.deleted or type(row.payload_json) is not str for row in captured.records):
        raise PageMetadataUnavailable('deleted_pin_document')
    return PinManifest(position, captured)


def capture_message_anchor(conn: sqlite3.Connection, path: Path,
                           index: overlays.OverlayIndexPosition, target_id: str) -> MessageAnchor:
    """Exact primary-key seek for one target; no message payload materialization."""
    overlays._text(target_id, 'message id')
    position = _position(conn, path, index)
    row = conn.execute(
        'SELECT ns,sender,id,kind FROM messages INDEXED BY sqlite_autoindex_messages_1 '
        'WHERE chat_id=? AND id=?', (index.chat_id, target_id),
    ).fetchone()
    if row is None:
        return MessageAnchor(position, None)
    try:
        key, _kind, _size = page_inputs._metadata((*row, 0))
    except (TypeError, ValueError, page_inputs.PageInputsChanged) as exc:
        raise PageMetadataUnavailable('malformed_message_anchor') from exc
    return MessageAnchor(position, key)
