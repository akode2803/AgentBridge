"""Bounded selected lifecycle inputs; neither freshness nor authority leases.

Size indexes are prepared explicitly off-path. Readers never build an index,
scan a namespace, decode JSON, or authorize a viewer.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import document_observation as documents
from . import lifecycle_heads as heads

MAX_HEADS = 128
MAX_RECORDS = 256
MAX_BYTES = 4 * 1024 * 1024
_HEAD_WHERE = "substr(path,1,15)='lifecycle/head/' AND length(path)>15"
HEAD_INDEX = 'idx_lifecycle_selected_head_size'
SUBJECT_INDEX = 'idx_lifecycle_subject_range_size'
_INDEXES = {
    HEAD_INDEX: (f'CREATE INDEX {HEAD_INDEX} ON docs('
                 f'path,typeof(payload),length(CAST(payload AS BLOB))) WHERE {_HEAD_WHERE}'),
    SUBJECT_INDEX: (f'CREATE INDEX {SUBJECT_INDEX} ON document_observation_records('
                    'source_id,path,deleted,coalesce(length(CAST(payload AS BLOB)),0),'
                    'typeof(payload),length(CAST(path AS BLOB)),typeof(path))'),
}


class LifecycleInputsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class HeadSelection:
    database_path: str
    incarnation: str
    entries: tuple[heads.LifecycleHeadPosition, ...]
    serialized_bytes: int


@dataclass(frozen=True)
class SubjectSelection:
    position: documents.DocumentPosition
    subject: str
    prefix: str
    records: tuple[documents.SerializedDocumentRecord, ...]
    serialized_bytes: int


def _index(conn, name):
    if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (_INDEXES[name],):
        raise LifecycleInputsUnavailable('lifecycle_size_index_pending_or_changed')
    columns = ['path', None, None] if name == HEAD_INDEX else ['source_id', 'path', 'deleted', None, None, None, None]
    actual = [(r[2], r[3], r[4], r[5]) for r in conn.execute(f'PRAGMA index_xinfo({name})')]
    if actual != [(c, 0, 'BINARY', 1) for c in columns] + [(None, 0, 'BINARY', 0)]:
        raise LifecycleInputsUnavailable('lifecycle_size_index_changed')


def _base_schema(conn):
    heads._verify_schema(conn)
    documents._validate_readiness_schema(conn)
    shape = [(r[1], r[2], r[5]) for r in conn.execute('PRAGMA table_info(docs)')]
    if shape != [('path', 'TEXT', 1), ('payload', 'TEXT', 0), ('fetched_ns', 'INTEGER', 0)]:
        raise LifecycleInputsUnavailable('docs_schema_changed')
    # Exact lookups must use the same BINARY identity as the range/size indexes.
    for table, names in (('docs', ['path']), ('lifecycle_head_versions', ['path'])):
        indexes = [r[1] for r in conn.execute(f'PRAGMA index_list({table})') if r[3] == 'pk']
        if len(indexes) != 1:
            raise LifecycleInputsUnavailable('lifecycle_identity_index_changed')
        columns = [(r[2], r[3], r[4]) for r in conn.execute(f'PRAGMA index_xinfo({indexes[0]})') if r[5]]
        if columns != [(n, 0, 'BINARY') for n in names]:
            raise LifecycleInputsUnavailable('lifecycle_identity_index_changed')


def prepare(conn):
    """Build additive size indexes in an explicit off-path transaction."""
    if conn.in_transaction:
        raise sqlite3.OperationalError('lifecycle preparation requires an owned transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        _base_schema(conn)
        for name, sql in _INDEXES.items():
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
                conn.execute(sql)
            _index(conn, name)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _transaction(conn, path):
    if not conn.in_transaction:
        raise sqlite3.OperationalError('lifecycle capture requires an active transaction')
    path = Path(path).resolve()
    actual = conn.execute('PRAGMA database_list').fetchone()[2]
    if not actual or Path(actual).resolve() != path:
        raise ValueError('lifecycle capture belongs to another database')
    return path


def _budget(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('invalid lifecycle input budget')
    return value


def _charge(used, maximum):
    if used > maximum:
        raise OverflowError('lifecycle inputs exceed byte budget')
    return used


def capture_heads(conn, path, subjects, *, max_heads=MAX_HEADS, max_bytes=MAX_BYTES):
    path = _transaction(conn, path)
    _budget(max_heads, MAX_HEADS)
    _budget(max_bytes, MAX_BYTES)
    if type(subjects) is not tuple or len(subjects) > max_heads:
        raise ValueError('invalid lifecycle subject selection')
    names = tuple(heads._subject_path(s)[0] for s in subjects)
    if len(set(names)) != len(names):
        raise ValueError('duplicate lifecycle subjects')
    _base_schema(conn)
    _index(conn, HEAD_INDEX)
    identities = conn.execute('SELECT incarnation FROM ingestion_identity WHERE singleton=1').fetchall()
    if len(identities) != 1:
        raise LifecycleInputsUnavailable('missing database identity')
    incarnation = identities[0][0]
    heads._stored_identity(incarnation)
    used = _charge(16 + len(str(path).encode()) + len(incarnation.encode()), max_bytes)
    metadata = []
    for subject in names:
        doc_path = heads.PREFIX + subject
        version = conn.execute('SELECT generation FROM lifecycle_head_versions WHERE path=?', (doc_path,)).fetchone()
        row = conn.execute(
            f'SELECT typeof(payload),length(CAST(payload AS BLOB)) FROM docs INDEXED BY {HEAD_INDEX} '
            f'WHERE path=? AND {_HEAD_WHERE}', (doc_path,),
        ).fetchone()
        if row is not None and version is None:
            raise LifecycleInputsUnavailable('selected head missing generation')
        generation = version[0] if version is not None else 0
        if type(generation) is not int or not (1 <= generation <= heads.MAX_SQLITE_INTEGER if version else generation == 0):
            raise LifecycleInputsUnavailable('invalid selected head generation')
        if row is not None and (row[0] != 'text' or type(row[1]) is not int or row[1] < 0):
            raise LifecycleInputsUnavailable('invalid selected head payload')
        size = row[1] if row else 0
        if size > heads.MAX_BYTES:
            raise OverflowError('selected head exceeds byte budget')
        used = _charge(used + 17 + len(subject.encode()) + size, max_bytes)
        metadata.append((subject, generation, row is not None, size))
    entries = []
    for subject, generation, present, size in metadata:
        row = conn.execute('SELECT payload FROM docs WHERE path=?', (heads.PREFIX + subject,)).fetchone() if present else None
        if present and (row is None or type(row[0]) is not str or len(row[0].encode()) != size):
            raise LifecycleInputsUnavailable('selected head payload changed')
        entries.append(heads.LifecycleHeadPosition(str(path), incarnation, subject, generation, row[0] if row else None))
    return HeadSelection(str(path), incarnation, tuple(entries), used)


def matches_heads(conn, path, expected):
    path = _transaction(conn, path)
    if type(expected) is not HeadSelection:
        raise ValueError('invalid head selection')
    database, incarnation, entries, size = expected.database_path, expected.incarnation, expected.entries, expected.serialized_bytes
    if database != str(path) or type(entries) is not tuple or len(entries) > MAX_HEADS:
        raise ValueError('invalid head selection binding')
    _budget(size, MAX_BYTES)
    copied = tuple(heads._copy_expected(e, path) for e in entries)
    if any(e.incarnation != incarnation for e in copied):
        raise ValueError('inconsistent head selection incarnation')
    wanted = HeadSelection(database, incarnation, copied, size)
    return capture_heads(conn, path, tuple(e.subject for e in copied)) == wanted


def capture_subject(conn, path, expected, subject, *, max_records=MAX_RECORDS, max_bytes=MAX_BYTES):
    path = _transaction(conn, path)
    _budget(max_records, MAX_RECORDS)
    _budget(max_bytes, MAX_BYTES)
    subject = heads._subject_path(subject)[0]
    prefix = 'lifecycle/' + subject + '/'
    documents._validate_document_path(prefix + 'x')
    wanted = documents._validate_expected(expected, path)
    if documents._capture_position(conn, path, wanted.source_id) != wanted or not wanted.initialized:
        raise documents.DocumentObservationConflict('lifecycle source changed or pending')
    _index(conn, SUBJECT_INDEX)
    # '/' is the final code point: replacing it with '0' is its exact BINARY
    # prefix successor, including Unicode subjects, with no LIKE escaping.
    upper = prefix[:-1] + '0'
    used = _charge(48 + sum(len(s.encode()) for s in (
        wanted.database_path, wanted.incarnation, wanted.source_id, subject, prefix)), max_bytes)
    meta = conn.execute(
        'SELECT deleted,coalesce(length(CAST(payload AS BLOB)),0),typeof(payload),'
        'length(CAST(path AS BLOB)),typeof(path) '
        f'FROM document_observation_records INDEXED BY {SUBJECT_INDEX} '
        'WHERE source_id=? AND path>=? AND path<? ORDER BY path LIMIT ?',
        (wanted.source_id, prefix, upper, max_records + 1),
    ).fetchall()
    if len(meta) > max_records:
        raise OverflowError('lifecycle subject exceeds row budget')
    for deleted, size, payload_type, name_size, name_type in meta:
        if (type(deleted) is not int or deleted not in (0, 1) or type(size) is not int or size < 0
                or name_type != 'text' or type(name_size) is not int or name_size < 1
                or (deleted == 1 and (payload_type != 'null' or size != 0))
                or (deleted == 0 and payload_type != 'text')):
            raise LifecycleInputsUnavailable('invalid selected lifecycle document')
        if name_size > documents.MAX_DOCUMENT_PATH_BYTES:
            raise OverflowError('lifecycle path exceeds byte budget')
        used = _charge(used + 9 + name_size + size, max_bytes)
    rows = conn.execute(
        'SELECT path,payload,deleted FROM document_observation_records '
        'WHERE source_id=? AND path>=? AND path<? ORDER BY path LIMIT ?',
        (wanted.source_id, prefix, upper, max_records),
    ).fetchall()
    if len(rows) != len(meta):
        raise LifecycleInputsUnavailable('lifecycle selection changed')
    records = []
    for (name, payload, deleted), (_deleted, size, _type, name_size, _) in zip(rows, meta):
        documents._validate_document_path(name)
        if (len(name.encode()) != name_size or deleted != _deleted
                or (payload is not None and (type(payload) is not str or len(payload.encode()) != size))):
            raise LifecycleInputsUnavailable('lifecycle selection changed')
        records.append(documents.SerializedDocumentRecord(name, payload, deleted == 1))
    return SubjectSelection(wanted, subject, prefix, tuple(records), used)


def matches_subject(conn, path, expected):
    """Compare one complete subject selection in the final caller transaction.

    False or an unavailable/changed/budget exception discards the candidate.
    The caller still owns live-mirror and current-authority/session fences.
    """
    path = _transaction(conn, path)
    if type(expected) is not SubjectSelection:
        raise ValueError('invalid lifecycle subject selection')
    position, subject, prefix, records, size = (
        expected.position, expected.subject, expected.prefix,
        expected.records, expected.serialized_bytes,
    )
    position = documents._validate_expected(position, path)
    subject = heads._subject_path(subject)[0]
    if prefix != 'lifecycle/' + subject + '/' or type(records) is not tuple or len(records) > MAX_RECORDS:
        raise ValueError('invalid lifecycle subject binding')
    _budget(size, MAX_BYTES)
    copied = []
    used = 0
    for record in records:
        if type(record) is not documents.SerializedDocumentRecord:
            raise ValueError('invalid lifecycle document record')
        name, payload, deleted = record.path, record.payload_json, record.deleted
        documents._validate_document_path(name)
        if not name.startswith(prefix) or type(deleted) is not bool:
            raise ValueError('invalid lifecycle document scope')
        if (deleted and payload is not None) or (not deleted and type(payload) is not str):
            raise ValueError('invalid lifecycle document payload')
        used = _charge(used + len(name.encode()) + (len(payload.encode()) if payload is not None else 0), MAX_BYTES)
        copied.append(documents.SerializedDocumentRecord(name, payload, deleted))
    wanted = SubjectSelection(position, subject, prefix, tuple(copied), size)
    return capture_subject(conn, path, position, subject) == wanted


def publish_head_in_transaction(conn, path, expected, proposed_json, fetched_ns):
    """CAS one prepared raw proposal without owning transaction completion.

    The mesh owner validates/canonicalizes the proposal before taking locks and
    holds its pin/mirror/input/clock fence around this write. This primitive does
    not authorize the proposal. Caller uses BEGIN IMMEDIATE and rolls back any
    changed final fence; no result derived before a successful write is served.
    """
    path = _transaction(conn, path)
    wanted = heads._copy_expected(expected, path)
    if type(proposed_json) is not str or len(proposed_json) > heads.MAX_BYTES:
        raise ValueError('invalid prepared head proposal')
    if len(proposed_json.encode()) > heads.MAX_BYTES:
        raise OverflowError('prepared head proposal exceeds byte budget')
    if type(fetched_ns) is not int or not 0 <= fetched_ns <= heads.MAX_SQLITE_INTEGER:
        raise ValueError('invalid prepared head timestamp')
    current = capture_heads(conn, path, (wanted.subject,)).entries[0]
    if current != wanted:
        return False
    if current.payload_json == proposed_json:
        return True
    name = heads.PREFIX + wanted.subject
    if current.payload_json is None:
        conn.execute('INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)',
                     (name, proposed_json, fetched_ns))
    else:
        changed = conn.execute('UPDATE docs SET payload=?,fetched_ns=? WHERE path=?',
                               (proposed_json, fetched_ns, name))
        if changed.rowcount != 1:
            raise LifecycleInputsUnavailable('selected head disappeared')
    return True
