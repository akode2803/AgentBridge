"""Capped prefix manifest from one admitted raw source; no policy verdict."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import document_observation as docs

MAX_DOCUMENTS = 10_000
MAX_BYTES = 8 * 1024 * 1024


class AuxInputsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AuxInputs:
    position: docs.DocumentPosition
    prefix: str
    documents: docs.DocumentObservation
    captured_bytes: int


def capture_prefix(conn: sqlite3.Connection, path: Path,
                   expected: docs.DocumentPosition, prefix: str,
                   *, max_documents=MAX_DOCUMENTS, max_bytes=MAX_BYTES) -> AuxInputs:
    """Range-seek at most limit+1 names; preflight bytes before payload copy."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError('aux capture requires a read transaction')
    expected = docs._validate_expected(expected, path)
    prefix = docs._validate_document_path(prefix)
    if (type(max_documents) is not int or not 0 <= max_documents <= MAX_DOCUMENTS
            or type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES):
        raise ValueError('invalid auxiliary capture budget')
    if not expected.initialized or docs._capture_position(conn, path, expected.source_id) != expected:
        raise AuxInputsUnavailable('aux_source_changed')
    docs._selection_index(conn)
    lower, upper = prefix + '/', prefix + '0'
    rows = conn.execute(
        f'SELECT path FROM document_observation_records INDEXED BY {docs._SELECTION_INDEX} '
        'WHERE source_id=? AND path>=? AND path<? ORDER BY path LIMIT ?',
        (expected.source_id, lower, upper, max_documents + 1),
    ).fetchall()
    if len(rows) > max_documents:
        raise AuxInputsUnavailable('aux_document_budget')
    used, names = 0, []
    for (name,) in rows:
        if type(name) is not str or not name.startswith(lower):
            raise AuxInputsUnavailable('aux_path_shape')
        try:
            docs._validate_document_path(name)
        except (TypeError, ValueError) as exc:
            raise AuxInputsUnavailable('aux_path_shape') from exc
        size = conn.execute(
            f'SELECT typeof(path),typeof(payload),typeof(deleted),deleted,'
            f'coalesce(length(CAST(payload AS BLOB)),0) '
            f'FROM document_observation_records INDEXED BY {docs._SELECTION_INDEX} '
            'WHERE source_id=? AND path=?', (expected.source_id, name),
        ).fetchone()
        if size is None or size[:4] != ('text', 'text', 'integer', 0) or type(size[4]) is not int:
            raise AuxInputsUnavailable('aux_deleted_or_malformed')
        used += len(name.encode()) + size[4]
        if used > max_bytes:
            raise AuxInputsUnavailable('aux_byte_budget')
        names.append(name)
    captured = docs._capture_selected(conn, path, expected, tuple(names),
                                      max_documents=max_documents, max_bytes=max_bytes)
    if any(record.deleted for record in captured.records):
        raise AuxInputsUnavailable('aux_deleted_or_malformed')
    return AuxInputs(expected, prefix, captured, used)
