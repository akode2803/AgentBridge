"""Bounded, invisible physical generations for complete background collection.

A staged source has a unique raw source ID. Append commits never set readiness;
finish seals exact raw and index positions after the enumerator declares completion.
Admission to a logical source is a separate, caller-owned transaction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from . import document_observation as docs, overlay_index as index
from ..mesh.overlay_index import prepare_overlay_index

MAX_BATCH_DOCUMENTS = 128
MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DOCUMENTS = 1_000_000
MAX_STAGES = 1040
MAX_ACTIVE_STAGES = 16
MAX_GLOBAL_BYTES = 4 * 1024 * 1024 * 1024

_SCHEMA = 'CREATE TABLE staged_sources(source TEXT PRIMARY KEY,logical_source TEXT NOT NULL,chat TEXT NOT NULL,epoch TEXT NOT NULL,incarnation TEXT NOT NULL,phase TEXT NOT NULL CHECK(phase IN (\'building\',\'sealed\',\'abandoned\')),document_count INTEGER NOT NULL,index_count INTEGER NOT NULL,total_bytes INTEGER NOT NULL,max_bytes INTEGER NOT NULL,max_documents INTEGER NOT NULL,generation INTEGER NOT NULL,build TEXT,owner_epoch TEXT,owner_revision INTEGER,index_revision INTEGER NOT NULL DEFAULT 0,committed_index_revision INTEGER NOT NULL DEFAULT 0)'
_ACTIVE = "CREATE UNIQUE INDEX staged_source_active ON staged_sources(logical_source) WHERE phase='building'"


def _triggers():
    result = {}
    for table in ('docs', 'candidates', 'shapes'):
        for event, refs in (('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW'))):
            name = f'staged_index_dirty_{table}_{event.lower()}'
            updates = ' '.join("SELECT CASE WHEN EXISTS(SELECT 1 FROM staged_sources "
                               f"WHERE source={ref}.source AND index_revision>=9223372036854775807) "
                               "THEN RAISE(ABORT,'stage index revision exhausted') END; "
                               "UPDATE staged_sources SET index_revision=index_revision+1 "
                               f"WHERE source={ref}.source AND phase='building';" for ref in refs)
            result[name] = f'CREATE TRIGGER {name} AFTER {event} ON overlay_index_{table} BEGIN {updates} END'
    return result


class StageChanged(RuntimeError):
    """The physical generation or its stage ownership is no longer valid."""


@dataclass(frozen=True)
class StageHandle:
    database_path: str
    incarnation: str
    source_id: str
    logical_source: str
    chat_id: str
    epoch: str


def initialize(store):
    from . import local_source
    local_source.initialize(store)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('stage initialization needs an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='staged_sources'").fetchone()
        if existing is None:
            conn.execute(_SCHEMA)
            conn.execute(_ACTIVE)
            for statement in _triggers().values():
                conn.execute(statement)
        _schema(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _schema(conn):
    if conn.execute("SELECT sql FROM sqlite_master WHERE name='staged_sources'").fetchone() != (_SCHEMA,):
        raise StageChanged('staging_schema_changed')
    if conn.execute("SELECT sql FROM sqlite_master WHERE name='staged_source_active'").fetchone() != (_ACTIVE,):
        raise StageChanged('staging_index_changed')
    actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name GLOB 'staged_index_dirty_*' OR tbl_name='staged_sources') LIMIT 10").fetchall())
    if actual != _triggers():
        raise StageChanged('staging_triggers_changed')


def _handle(store, stage):
    if type(stage) is not StageHandle or stage.database_path != str(store.path):
        raise ValueError('foreign stage handle')
    docs._validate_source_id(stage.source_id)
    docs._validate_source_id(stage.logical_source)
    index._chat(stage.chat_id)
    if (not stage.source_id.startswith('stage:') or len(stage.source_id) != 38
            or any(c not in '0123456789abcdef' for c in stage.source_id[6:])
            or type(stage.epoch) is not str or len(stage.epoch) != 32
            or any(c not in '0123456789abcdef' for c in stage.epoch)):
        raise ValueError('invalid stage handle')
    return stage


def _row(conn, store, stage, phase=None):
    stage = _handle(store, stage)
    _schema(conn)
    safe = conn.execute('SELECT typeof(logical_source),length(CAST(logical_source AS BLOB)),typeof(chat),length(CAST(chat AS BLOB)),typeof(epoch),length(CAST(epoch AS BLOB)),typeof(incarnation),length(CAST(incarnation AS BLOB)),typeof(phase),length(CAST(phase AS BLOB)),typeof(document_count),typeof(index_count),typeof(total_bytes),typeof(max_bytes),typeof(max_documents),typeof(generation),typeof(build),length(CAST(build AS BLOB)),typeof(index_revision),typeof(committed_index_revision) FROM staged_sources WHERE source=?', (stage.source_id,)).fetchone()
    if (safe is None or any(safe[i] != 'text' or safe[i+1] > limit for i, limit in ((0,512),(2,256),(4,32),(6,128),(8,16)))
            or any(safe[i] != 'integer' for i in (10,11,12,13,14,15,18,19))
            or (safe[16] != 'null' and (safe[16] != 'text' or safe[17] > 32))):
        raise StageChanged('malformed_stage_metadata')
    row = conn.execute('SELECT logical_source,chat,epoch,incarnation,phase,document_count,index_count,total_bytes,max_bytes,max_documents,generation,build,index_revision,committed_index_revision FROM staged_sources WHERE source=?', (stage.source_id,)).fetchone()
    if row is None or row[:4] != (stage.logical_source, stage.chat_id, stage.epoch, stage.incarnation):
        raise StageChanged('stage_owner_changed')
    if (row[4] not in ('building', 'sealed', 'abandoned')
            or not 0 <= row[5] <= row[9] <= MAX_DOCUMENTS
            or not 0 <= row[6] <= row[5]
            or not 0 <= row[7] <= row[8] <= MAX_TOTAL_BYTES
            or not 0 <= row[10] <= docs.MAX_SQLITE_INTEGER
            or not 0 <= row[13] <= row[12] <= docs.MAX_SQLITE_INTEGER
            or (row[4] == 'sealed' and (type(row[11]) is not str or len(row[11]) != 32
                                        or any(c not in '0123456789abcdef' for c in row[11])))):
        raise StageChanged('malformed_stage_metadata')
    if phase is not None and row[4] != phase:
        raise StageChanged('stage_not_' + phase)
    return row


def begin(store, logical_source, chat, *, expected=None, max_total_bytes=MAX_TOTAL_BYTES, max_documents=MAX_DOCUMENTS):
    logical_source = docs._validate_source_id(logical_source)
    chat = index._chat(chat)
    if (type(max_total_bytes) is not int or not 0 <= max_total_bytes <= MAX_TOTAL_BYTES
            or type(max_documents) is not int or not 0 <= max_documents <= MAX_DOCUMENTS):
        raise ValueError('invalid stage budgets')
    initialize(store)
    conn = store._conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _schema(conn)
        identity = docs._capture_position(conn, store.path, logical_source).incarnation
        owner_epoch, owner_revision = None, None
        if expected is not None:
            from . import local_source
            expected = local_source._expected(store, expected)
            current = local_source.capture_in_transaction(conn, store, logical_source)
            if current != expected or expected.source_id != logical_source:
                raise StageChanged('stage_owner_changed')
            owner_epoch, owner_revision = expected.epoch, expected.revision
            # A new invalidation/revision can abandon an interrupted old build;
            # never steal one owned by the same revision or another active call.
            conn.execute("UPDATE staged_sources SET phase='abandoned' WHERE logical_source=? AND phase='building' AND (owner_epoch IS NULL OR owner_epoch<>? OR owner_revision IS NULL OR owner_revision<>?)",
                         (logical_source, owner_epoch, owner_revision))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_input_generations'").fetchone():
                conn.execute("UPDATE staged_sources SET phase='abandoned' WHERE logical_source=? AND phase='sealed' AND (owner_epoch IS NULL OR owner_epoch<>? OR owner_revision IS NULL OR owner_revision<>?) AND NOT EXISTS (SELECT 1 FROM local_input_generations WHERE physical=staged_sources.source)",
                             (logical_source, owner_epoch, owner_revision))
        existing = conn.execute("SELECT count(*),coalesce(sum(total_bytes),0),coalesce(sum(phase='building'),0) FROM (SELECT total_bytes,phase FROM staged_sources LIMIT ?)", (MAX_STAGES + 1,)).fetchone()
        if existing[0] >= MAX_STAGES or existing[1] >= MAX_GLOBAL_BYTES or existing[2] >= MAX_ACTIVE_STAGES:
            raise OverflowError('global stage capacity exceeded')
        source_id = 'stage:' + uuid.uuid4().hex
        epoch = uuid.uuid4().hex
        conn.execute('INSERT INTO staged_sources VALUES(?,?,?,?,?,\'building\',0,0,0,?,?,0,NULL,?,?,0,0)',
                     (source_id, logical_source, chat, epoch, identity, max_total_bytes, max_documents,
                      owner_epoch, owner_revision))
        conn.execute('INSERT INTO document_observation_sources VALUES(?,0,0,0)', (source_id,))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise StageChanged('active_stage_exists') from exc
    except BaseException:
        conn.rollback()
        raise
    return StageHandle(str(store.path), identity, source_id, logical_source, chat, epoch)


def _path(path, chat):
    docs._validate_document_path(path)
    if not path.startswith(f'chats/{chat}/overlays/'):
        # Logical source selection belongs to the caller's registered scope.
        # Index validation still owns every path in this overlay namespace.
        return None
    parts = path.split('/')
    if (len(parts) != 5 or parts[:3] != ['chats', chat, 'overlays']
            or parts[3] not in ('reactions', 'state', 'edits', 'redactions', 'pins')
            or not parts[4].endswith('.json') or parts[4] == '.json'):
        raise ValueError('unsupported staged overlay path')
    return parts[3]


def append(store, stage, documents):
    stage = _handle(store, stage)
    if type(documents) is not dict or not 1 <= len(documents) <= MAX_BATCH_DOCUMENTS:
        raise ValueError('stage append requires 1..128 documents')
    raw, bytes_used, observed = [], 0, []
    for path, value in documents.items():
        kind = _path(path, stage.chat_id)
        docs._validate_json_keys(value)
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        size = len(path.encode()) + len(payload.encode())
        bytes_used += size
        if bytes_used > MAX_BATCH_BYTES:
            raise OverflowError('stage batch byte budget exceeded')
        raw.append((stage.source_id, path, payload, 0))
        if kind is not None:
            observed.append(docs.SerializedDocumentRecord(path, payload, False))
    # The mesh normalizer requires a ready observation, but this temporary position
    # is only a pure preparation input and never makes the SQLite stage readable.
    temporary = docs.DocumentPosition(str(store.path), stage.incarnation, stage.source_id, 0, 0, True)
    prepared = prepare_overlay_index(docs.DocumentObservation(temporary, tuple(observed)), stage.chat_id)
    _, _, normalized, candidates, shapes, names, index_bytes = index._validated_rows(prepared, store.path)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('stage append needs an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = _row(conn, store, stage, 'building')
        count, indexed, total, limit, max_docs, generation = row[5:11]
        if count + len(raw) > max_docs or total + bytes_used + index_bytes > limit:
            raise OverflowError('stage cumulative budget exceeded')
        other_total = conn.execute('SELECT coalesce(sum(total_bytes),0) FROM (SELECT total_bytes FROM staged_sources WHERE source<>? LIMIT ?)', (stage.source_id, MAX_STAGES + 1)).fetchone()[0]
        if other_total + total + bytes_used + index_bytes > MAX_GLOBAL_BYTES:
            raise OverflowError('global stage capacity exceeded')
        current = docs._capture_position(conn, store.path, stage.source_id)
        if (current.incarnation != stage.incarnation or current.generation != generation
                or current.initialized or current.cursor != 0):
            raise StageChanged('stage_raw_changed')
        if row[12] != row[13]:
            raise StageChanged('stage_index_changed')
        for source_id, path, payload, deleted in raw:
            if conn.execute('SELECT 1 FROM document_observation_records WHERE source_id=? AND path=?', (source_id, path)).fetchone():
                raise StageChanged('duplicate_stage_path')
            conn.execute('INSERT INTO document_observation_records VALUES(?,?,?,?)', (source_id, path, payload, deleted))
        for item in prepared.documents:
            payload = next(raw_row[2] for raw_row in raw if raw_row[1] == item.path)
            if len(payload.encode()) != item.source_bytes or hashlib.sha256(payload.encode()).hexdigest() != item.source_digest:
                raise StageChanged('prepared_raw_mismatch')
        conn.executemany('INSERT INTO overlay_index_docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', normalized)
        conn.executemany('INSERT INTO overlay_index_shapes VALUES(?,?,?,?,?,?,?)', shapes)
        conn.executemany('INSERT INTO overlay_index_candidates VALUES(?,?,?,?,?,?)', candidates)
        position = docs._capture_position(conn, store.path, stage.source_id)
        conn.execute('UPDATE staged_sources SET document_count=?,index_count=?,total_bytes=?,generation=?,committed_index_revision=index_revision WHERE source=?',
                     (count + len(raw), indexed + len(names), total + bytes_used + index_bytes,
                      position.generation, stage.source_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return stage


def finish(store, stage):
    stage = _handle(store, stage)
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('stage finish needs an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = _row(conn, store, stage, 'building')
        current = docs._capture_position(conn, store.path, stage.source_id)
        if current.incarnation != stage.incarnation or current.generation != row[10] or current.initialized or current.cursor != 0:
            raise StageChanged('stage_raw_changed')
        # Every append updates these owner counters with its raw+index writes.
        # Raw triggers advance generation; index triggers advance revision.
        if row[12] != row[13]:
            raise StageChanged('stage_index_changed')
        index._schema(conn)
        conn.execute('UPDATE document_observation_sources SET initialized=1 WHERE source_id=?', (stage.source_id,))
        position = docs._capture_position(conn, store.path, stage.source_id)
        indexed = index.OverlayIndexPosition(position, stage.chat_id, uuid.uuid4().hex)
        index._write_ready(conn, indexed)
        conn.execute("UPDATE staged_sources SET phase='sealed',build=? WHERE source=?", (indexed.build, stage.source_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return position, indexed


def verify_sealed(conn, store, stage, *, expected=None):
    """Verify exact sealed identities in the caller's admission transaction."""
    if not conn.in_transaction:
        raise sqlite3.OperationalError('sealed verification requires a transaction')
    row = _row(conn, store, stage, 'sealed')
    if expected is not None:
        from . import local_source
        expected = local_source._expected(store, expected)
        owner_matches = conn.execute("SELECT 1 FROM staged_sources WHERE source=? AND owner_epoch=? AND owner_revision=? AND typeof(owner_revision)='integer' LIMIT 1",
                                     (stage.source_id, expected.epoch, expected.revision)).fetchone()
        if expected.source_id != stage.logical_source or owner_matches is None:
            raise StageChanged('stage_owner_revision_changed')
    position = docs._capture_position(conn, store.path, stage.source_id)
    if position.incarnation != stage.incarnation or position.generation != row[10] or position.cursor != 0 or not position.initialized:
        raise StageChanged('sealed_raw_changed')
    indexed = index.OverlayIndexPosition(position, stage.chat_id, row[11])
    try:
        index._ready(conn, store.path, indexed)
    except index.OverlayIndexUnavailable as exc:
        raise StageChanged('sealed_index_changed') from exc
    return position, indexed


def abort(store, stage):
    """Mark this exact build reclaimable; an admitted stage remains protected."""
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('stage abort needs an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = _row(conn, store, stage)
        if row[4] not in ('building', 'sealed', 'abandoned'):
            raise StageChanged('invalid_stage_phase')
        if not _admitted(conn, stage.source_id):
            conn.execute("UPDATE staged_sources SET phase='abandoned' WHERE source=?", (stage.source_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _admitted(conn, physical):
    from . import local_source
    try:
        local_source._schema(conn)
    except local_source.SourceChanged as exc:
        raise StageChanged('admission_mapping_unavailable') from exc
    return conn.execute('SELECT 1 FROM local_input_generations WHERE physical=? LIMIT 1', (physical,)).fetchone() is not None


def retire_generation(store, physical):
    """After a pointer swap, mark an unreferenced older physical source reclaimable."""
    physical = docs._validate_source_id(physical)
    if not physical.startswith('stage:'):
        return False
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('stage retirement needs an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        _schema(conn)
        changed = False
        if not _admitted(conn, physical):
            changed = conn.execute("UPDATE staged_sources SET phase='abandoned' WHERE source=? AND phase='sealed'", (physical,)).rowcount == 1
        conn.commit()
        return changed
    except BaseException:
        conn.rollback()
        raise


def cleanup(store, *, protected_ids=(), max_rows=128):
    """Reclaim only explicitly abandoned stages in bounded row chunks.

    The admission mapping is checked in the same write transaction. A caller may
    add protected physical IDs, but it cannot remove protection from admission.
    """
    if type(protected_ids) not in (tuple, frozenset) or len(protected_ids) > 1024:
        raise ValueError('invalid protected stage IDs')
    protected = set(docs._validate_source_id(s) for s in protected_ids)
    if type(max_rows) is not int or not 1 <= max_rows <= 1024:
        raise ValueError('invalid cleanup row budget')
    conn = store._conn()
    if conn.in_transaction:
        raise sqlite3.OperationalError('stage cleanup needs an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        _schema(conn)
        candidates = conn.execute("SELECT source FROM staged_sources WHERE phase='abandoned' ORDER BY source LIMIT ?", (MAX_STAGES + 1,)).fetchall()
        for (source_id,) in candidates:
            if source_id in protected:
                continue
            if _admitted(conn, source_id):
                continue
            remaining = max_rows
            for table, key in [('document_observation_records', 'source_id'), ('overlay_index_candidates', 'source'),
                               ('overlay_index_shapes', 'source'), ('overlay_index_docs', 'source'),
                               ('overlay_index_proofs', 'source')]:
                if remaining <= 0:
                    break
                rows = conn.execute(f'SELECT rowid FROM {table} WHERE {key}=? LIMIT ?', (source_id, remaining)).fetchall()
                conn.executemany(f'DELETE FROM {table} WHERE rowid=?', rows)
                remaining -= len(rows)
            if remaining > 0:
                conn.execute('DELETE FROM overlay_index_ready WHERE source=?', (source_id,))
                conn.execute('DELETE FROM document_observation_sources WHERE source_id=?', (source_id,))
                conn.execute('DELETE FROM staged_sources WHERE source=?', (source_id,))
            conn.commit()
            return max_rows - remaining
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return 0
