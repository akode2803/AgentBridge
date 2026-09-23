"""Generation-bound overlay candidates and policy-free signature evidence.

Nothing here admits a viewer or trusts a key. Indexed candidates include invalid
signatures; consumers must obtain current authority and select the exact key.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from .. import crypto
from . import document_observation as source

SCHEMA = 2
MAX_DOCUMENTS = 20_000
MAX_CANDIDATES = 500_000
MAX_BUILD_BYTES = 64 * 1024 * 1024
MAX_SELECT_BYTES = 1024 * 1024
MAX_TARGETS = 200
MAX_DEPENDENCIES = 256
_SHAPE_ERRORS = frozenset(('', 'document_shape', 'signature_shape',
                           'reaction_map', 'signing_input', 'viewer_ids'))


class OverlayIndexUnavailable(RuntimeError):
    """Source or index changed, is pending, or cannot be interpreted safely."""


@dataclass(frozen=True)
class DocumentShape:
    is_dict: bool
    signature_truthy: bool
    signature_string: bool
    signing_available: bool
    viewer_ids_compatible: bool


@dataclass(frozen=True)
class IndexedDocument:
    path: str
    kind: str
    actor: str
    source_digest: str
    source_bytes: int
    signature: str
    signing_bytes: bytes | None
    empty: bool
    shape_error: str
    scalars_json: str
    shape: DocumentShape | None = None


@dataclass(frozen=True)
class OverlayCandidate:
    path: str
    kind: str
    target: str
    value: str


@dataclass(frozen=True)
class PreparedOverlayIndex:
    source: source.DocumentPosition
    chat_id: str
    documents: tuple[IndexedDocument, ...]
    candidates: tuple[OverlayCandidate, ...]


@dataclass(frozen=True)
class OverlayIndexPosition:
    source: source.DocumentPosition
    chat_id: str
    build: str
    schema: int = SCHEMA


@dataclass(frozen=True)
class DocumentSummary:
    path: str
    kind: str
    actor: str
    empty: bool
    has_signature: bool
    shape_error: str
    scalars_json: str
    shape: DocumentShape | None = None


@dataclass(frozen=True)
class IndexedOverlayInputs:
    position: OverlayIndexPosition
    documents: tuple[DocumentSummary, ...]
    candidates: tuple[OverlayCandidate, ...]
    absent_states: tuple[str, ...]
    reactions_complete: bool = False


_TABLES = {
    'overlay_index_ready': 'CREATE TABLE overlay_index_ready(source TEXT PRIMARY KEY,chat TEXT NOT NULL,generation INTEGER NOT NULL,cursor INTEGER NOT NULL,incarnation TEXT NOT NULL,build TEXT NOT NULL,schema INTEGER NOT NULL)',
    'overlay_index_docs': 'CREATE TABLE overlay_index_docs(source TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,actor TEXT NOT NULL,digest TEXT NOT NULL,raw_bytes INTEGER NOT NULL,signature TEXT NOT NULL,signing BLOB,signing_size INTEGER NOT NULL,empty INTEGER NOT NULL,shape_error TEXT NOT NULL,scalars TEXT NOT NULL,size INTEGER NOT NULL,PRIMARY KEY(source,path))',
    'overlay_index_candidates': 'CREATE TABLE overlay_index_candidates(source TEXT NOT NULL,kind TEXT NOT NULL,target TEXT NOT NULL,path TEXT NOT NULL,value TEXT NOT NULL,size INTEGER NOT NULL,PRIMARY KEY(source,kind,target,path))',
    'overlay_index_proofs': 'CREATE TABLE overlay_index_proofs(source TEXT NOT NULL,path TEXT NOT NULL,pub BLOB NOT NULL,valid INTEGER NOT NULL,PRIMARY KEY(source,path,pub))',
}


_V1_TABLES = dict(_TABLES)
_TABLES['overlay_index_shapes'] = 'CREATE TABLE overlay_index_shapes(source TEXT NOT NULL,path TEXT NOT NULL,is_dict INTEGER NOT NULL,sig_truthy INTEGER NOT NULL,sig_string INTEGER NOT NULL,signing_available INTEGER NOT NULL,ids_compatible INTEGER NOT NULL,PRIMARY KEY(source,path))'
_MANIFEST_INDEX = 'CREATE INDEX overlay_index_manifest ON overlay_index_docs(source,kind,path,size)'


def _triggers(legacy=False):
    result = {}
    for table in (('docs', 'candidates', 'proofs') if legacy else ('docs', 'candidates', 'proofs', 'shapes')):
        for event, refs in (('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW'))):
            name = f'overlay_index_dirty_{table}_{event.lower()}'
            actions = ' '.join('DELETE FROM overlay_index_ready WHERE source=' + r + '.source;' for r in refs)
            result[name] = f'CREATE TRIGGER {name} AFTER {event} ON overlay_index_{table} BEGIN {actions} END'
    return result


def _schema(conn, legacy=False):
    for name, sql in (_V1_TABLES if legacy else _TABLES).items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row != (sql,):
            raise OverlayIndexUnavailable('index_schema_changed')
    actual = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
        "(name GLOB 'overlay_index_dirty_*' OR tbl_name IN "
        "('overlay_index_ready','overlay_index_docs','overlay_index_candidates','overlay_index_proofs','overlay_index_shapes')) LIMIT 23"
    ).fetchall())
    expected_triggers = _triggers(legacy)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='staged_sources'").fetchone():
        from . import staged_source
        staged_source._schema(conn)
        expected_triggers = {**expected_triggers, **staged_source._triggers()}
    if actual != expected_triggers:
        raise OverlayIndexUnavailable('index_triggers_changed')
    index = conn.execute("SELECT sql FROM sqlite_master WHERE name='overlay_index_manifest'").fetchone()
    if index != (None if legacy else (_MANIFEST_INDEX,)):
        raise OverlayIndexUnavailable('manifest_index_changed')


def initialize(conn):
    conn.execute('BEGIN IMMEDIATE')
    with conn:
        present = conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'overlay_index_*' AND type='table'").fetchall()
        if not present:
            for sql in _TABLES.values():
                conn.execute(sql)
            for sql in _triggers().values():
                conn.execute(sql)
            conn.execute(_MANIFEST_INDEX)
        elif {r[0] for r in present} == set(_V1_TABLES):
            # Only the exact known v1 layout may migrate. Keep source and derived
            # rows, but invalidate readiness; a background rebuild supplies facts
            # that cannot be inferred from v1 normalized signatures.
            _schema(conn, legacy=True)
            conn.execute(_TABLES['overlay_index_shapes'])
            conn.execute(_MANIFEST_INDEX)
            for name, sql in _triggers().items():
                if name not in _triggers(legacy=True):
                    conn.execute(sql)
            conn.execute('DELETE FROM overlay_index_ready')
        _schema(conn)


def _text(value, label, limit=4096, *, empty=False):
    if type(value) is not str or (not value and not empty) or '\x00' in value or len(value.encode('utf-8')) > limit:
        raise ValueError('invalid ' + label)
    return value


def _shape(shape):
    if type(shape) is not DocumentShape:
        raise ValueError('missing document shape evidence')
    values = (shape.is_dict, shape.signature_truthy, shape.signature_string,
              shape.signing_available, shape.viewer_ids_compatible)
    if any(type(v) is not bool for v in values):
        raise ValueError('invalid document shape evidence')
    return DocumentShape(*values)


def _chat(value):
    _text(value, 'chat', 256)
    if value in ('.', '..') or any(c in value for c in '/\\:'):
        raise ValueError('invalid chat')
    return value


def _doc_path(path, chat):
    source._validate_document_path(path)
    parts = path.split('/')
    if (len(parts) != 5 or parts[:3] != ['chats', chat, 'overlays']
            or parts[3] not in ('reactions', 'state') or not parts[4].endswith('.json')
            or parts[4] == '.json'):
        raise ValueError('path is not a reaction/state document in this chat')
    return parts[3], parts[4][:-5]


def _wanted(position, path):
    if type(position) is not OverlayIndexPosition:
        raise ValueError('invalid overlay index position')
    source_position, chat, build, schema = position.source, position.chat_id, position.build, position.schema
    position = OverlayIndexPosition(source._validate_expected(source_position, path), chat, build, schema)
    if type(position.schema) is not int or position.schema != SCHEMA:
        raise ValueError('invalid overlay index position')
    _chat(position.chat_id)
    if type(position.build) is not str or len(position.build) != 32 or any(c not in '0123456789abcdef' for c in position.build):
        raise ValueError('invalid index build identity')
    return position


def _ready(conn, path, expected):
    _schema(conn)
    if source._capture_position(conn, path, expected.source.source_id) != expected.source or not expected.source.initialized:
        raise OverlayIndexUnavailable('source_changed_or_pending')
    row = conn.execute('SELECT chat,generation,cursor,incarnation,build,schema FROM overlay_index_ready WHERE source=?', (expected.source.source_id,)).fetchone()
    wanted = (expected.chat_id, expected.source.generation, expected.source.cursor, expected.source.incarnation, expected.build, SCHEMA)
    if row != wanted or any(type(row[i]) is not int for i in (1, 2, 5)):
        raise OverlayIndexUnavailable('index_changed_or_pending')


def _write_ready(conn, position):
    p = position.source
    conn.execute('INSERT INTO overlay_index_ready VALUES(?,?,?,?,?,?,?)',
                 (p.source_id, position.chat_id, p.generation, p.cursor, p.incarnation, position.build, SCHEMA))


def _validated_rows(prepared, path):
    """Bound one normalized batch before either replacement or staged insertion."""
    if type(prepared) is not PreparedOverlayIndex:
        raise ValueError('expected prepared overlay index')
    expected = source._validate_expected(prepared.source, path)
    chat = _chat(prepared.chat_id)
    if not expected.initialized:
        raise OverlayIndexUnavailable('source_pending')
    if type(prepared.documents) is not tuple or type(prepared.candidates) is not tuple:
        raise ValueError('index batches must be tuples')
    if len(prepared.documents) > MAX_DOCUMENTS or len(prepared.candidates) > MAX_CANDIDATES:
        raise OverflowError('index row budget exceeded')
    docs, candidates, shapes, names, keys, used = [], [], [], set(), set(), 0
    for doc in prepared.documents:
        if type(doc) is not IndexedDocument:
            raise ValueError('invalid indexed document')
        kind, actor = _doc_path(doc.path, chat)
        if (doc.kind, doc.actor) != (kind, actor) or doc.path in names:
            raise ValueError('inconsistent or duplicate indexed document')
        names.add(doc.path)
        if (type(doc.source_digest) is not str or len(doc.source_digest) != 64
                or any(c not in '0123456789abcdef' for c in doc.source_digest)
                or type(doc.source_bytes) is not int or not 0 <= doc.source_bytes <= MAX_BUILD_BYTES
                or (doc.signing_bytes is not None and type(doc.signing_bytes) is not bytes) or type(doc.empty) is not bool):
            raise ValueError('invalid document identity')
        shape = _shape(doc.shape)
        if shape.signing_available != (doc.signing_bytes is not None):
            raise ValueError('inconsistent signing shape evidence')
        shapes.append((expected.source_id, doc.path, *(int(v) for v in shape.__dict__.values())))
        _text(doc.signature, 'signature', 8192, empty=True)
        _text(doc.shape_error, 'shape error', 64, empty=True)
        if doc.shape_error not in _SHAPE_ERRORS:
            raise ValueError('unknown document shape error')
        _text(doc.scalars_json, 'scalars', 65536)
        scalar = json.loads(doc.scalars_json)
        if type(scalar) is not dict:
            raise ValueError('scalars must be an object')
        if json.dumps(scalar, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')) != doc.scalars_json:
            raise ValueError('scalars must use canonical JSON')
        size = 5 + sum(len(s.encode()) for s in (doc.path, kind, actor, doc.shape_error, doc.scalars_json))
        used += size + len(doc.signing_bytes or b'') + len(doc.signature.encode()) + 64
        if used > MAX_BUILD_BYTES:
            raise OverflowError('index byte budget exceeded')
        docs.append((expected.source_id, doc.path, kind, actor, doc.source_digest, doc.source_bytes,
                     doc.signature, doc.signing_bytes, len(doc.signing_bytes or b''), int(doc.empty), doc.shape_error, doc.scalars_json, size))
    for item in prepared.candidates:
        if type(item) is not OverlayCandidate or item.path not in names:
            raise ValueError('candidate has no indexed document')
        kind, _ = _doc_path(item.path, chat)
        if item.kind not in (('reaction',) if kind == 'reactions' else ('hidden', 'starred')):
            raise ValueError('candidate kind mismatch')
        _text(item.target, 'target')
        _text(item.value, 'candidate value', 4096, empty=True)
        key = (item.kind, item.target, item.path)
        if key in keys:
            raise ValueError('duplicate candidate')
        keys.add(key)
        size = sum(len(s.encode()) for s in (*key, item.value))
        used += size
        if used > MAX_BUILD_BYTES:
            raise OverflowError('index byte budget exceeded')
        candidates.append((expected.source_id, *key, item.value, size))
    return expected, chat, docs, candidates, shapes, names, used


def publish(conn, path, prepared, *, shared_source=False):
    """Prepare outside SQLite; replace all rows and readiness in one owned CAS.

    A shared source still requires complete coverage of this chat's overlay
    namespace. Other raw dependencies are excluded by an indexed path range,
    not by trusting the preparer's selection or by using a different generation.
    """
    if type(shared_source) is not bool:
        raise ValueError('shared_source must be a bool')
    expected, chat, docs, candidates, shapes, names, used = _validated_rows(prepared, path)
    if conn.in_transaction:
        raise sqlite3.OperationalError('index publication requires an owned transaction')
    position = OverlayIndexPosition(expected, chat, uuid.uuid4().hex)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _schema(conn)
        if source._capture_position(conn, path, expected.source_id) != expected:
            raise OverlayIndexUnavailable('source_changed')
        # A partial prepared batch cannot establish absent-state evidence. The
        # background publication checks coverage using metadata only; no such
        # source-wide scan belongs on the foreground capture path.
        if shared_source:
            prefix = f'chats/{chat}/overlays/'
            raw_paths = conn.execute(
                'SELECT path FROM document_observation_records '
                'WHERE source_id=? AND path>=? AND path<? AND deleted=0 LIMIT ?',
                (expected.source_id, prefix, prefix[:-1] + '0', MAX_DOCUMENTS + 1),
            ).fetchall()
        else:
            raw_paths = conn.execute(
                'SELECT path FROM document_observation_records WHERE source_id=? AND deleted=0 LIMIT ?',
                (expected.source_id, MAX_DOCUMENTS + 1),
            ).fetchall()
        if len(raw_paths) > MAX_DOCUMENTS:
            raise OverflowError('source document budget exceeded')
        required = set()
        for (raw_path,) in raw_paths:
            source._validate_document_path(raw_path)
            parts = raw_path.split('/')
            if (len(parts) != 5 or parts[:3] != ['chats', chat, 'overlays']
                    or parts[3] not in ('reactions', 'state', 'edits', 'redactions', 'pins')
                    or not parts[4].endswith('.json') or parts[4] == '.json'):
                raise ValueError('source contains an unsupported overlay path')
            if parts[3] in ('reactions', 'state'):
                required.add(raw_path)
        if required != names:
            raise OverlayIndexUnavailable('incomplete_prepared_documents')
        # Raw payload identity is checked in the same source transaction, before
        # deleting an older complete index. Candidate semantics belong to the
        # mesh preparer; caller-supplied verdicts are never accepted as proofs.
        for doc in prepared.documents:
            row = conn.execute('SELECT payload,deleted FROM document_observation_records WHERE source_id=? AND path=?', (expected.source_id, doc.path)).fetchone()
            if (row is None or row[1] != 0 or type(row[0]) is not str
                    or len(row[0].encode()) != doc.source_bytes
                    or hashlib.sha256(row[0].encode()).hexdigest() != doc.source_digest):
                raise OverlayIndexUnavailable('source_document_changed')
        for table in ('ready', 'proofs', 'candidates', 'shapes', 'docs'):
            conn.execute(f'DELETE FROM overlay_index_{table} WHERE source=?', (expected.source_id,))
        conn.executemany('INSERT INTO overlay_index_docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', docs)
        conn.executemany('INSERT INTO overlay_index_shapes VALUES(?,?,?,?,?,?,?)', shapes)
        conn.executemany('INSERT INTO overlay_index_candidates VALUES(?,?,?,?,?,?)', candidates)
        _write_ready(conn, position)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return position


def capture(path, expected, targets, state_paths=(), *, max_rows=2048, max_bytes=MAX_SELECT_BYTES, include_reactions=False):
    conn = source._open_reader(path)
    try:
        conn.execute('BEGIN')
        return _capture(conn, path, expected, targets, state_paths,
                        max_rows=max_rows, max_bytes=max_bytes, include_reactions=include_reactions)
    finally:
        conn.close()


def _capture(conn, path, expected, targets, state_paths=(), *, max_rows=2048, max_bytes=MAX_SELECT_BYTES, include_reactions=False):
    if not conn.in_transaction:
        raise sqlite3.OperationalError('capture requires an active read transaction')
    expected = _wanted(expected, path)
    if type(include_reactions) is not bool:
        raise ValueError('invalid manifest selector')
    if type(targets) is not tuple or type(state_paths) is not tuple or len(targets) > MAX_TARGETS or len(state_paths) > MAX_DEPENDENCIES:
        raise ValueError('invalid index selectors')
    if type(max_rows) is not int or not 0 <= max_rows <= 4096 or type(max_bytes) is not int or not 0 <= max_bytes <= MAX_SELECT_BYTES:
        raise ValueError('invalid selection budgets')
    for target in targets:
        _text(target, 'target')
    for state in state_paths:
        if _doc_path(state, expected.chat_id)[0] != 'state':
            raise ValueError('state selector must name a viewer state document')
    if len(set(targets)) != len(targets) or len(set(state_paths)) != len(state_paths):
        raise ValueError('duplicate selector')
    if len(targets) * (1 + 2 * len(state_paths)) > 4096:
        raise OverflowError('overlay lookup budget exceeded')
    selector_bytes = sum(len(s.encode()) for s in targets + state_paths)
    if selector_bytes > max_bytes:
        raise OverflowError('overlay selector byte budget exceeded')
    source_id = expected.source.source_id
    _ready(conn, path, expected)
    selected, paths, used = [], set(state_paths), selector_bytes
    if include_reactions:
        manifest = conn.execute(
            'SELECT path,size FROM overlay_index_docs INDEXED BY overlay_index_manifest '
            "WHERE source=? AND kind='reactions' ORDER BY path LIMIT ?",
            (source_id, MAX_DEPENDENCIES + 1),
        ).fetchall()
        if len(manifest) > MAX_DEPENDENCIES:
            raise OverflowError('reaction manifest exceeds dependency budget')
        paths.update(name for name, _ in manifest)
        if len(paths) > MAX_DEPENDENCIES:
            raise OverflowError('complete input exceeds dependency budget')
    for target in targets:
        queries = [('reaction', None)] + [(kind, state) for kind in ('hidden', 'starred') for state in state_paths]
        for kind, state in queries:
            sql = 'SELECT kind,target,path,size FROM overlay_index_candidates WHERE source=? AND kind=? AND target=?'
            params = (source_id, kind, target)
            if state is not None:
                sql += ' AND path=?'
                params += (state,)
            sql += ' ORDER BY path LIMIT ?'
            rows = conn.execute(sql, (*params, max_rows - len(selected) + 1)).fetchall()
            for k, t, p, size in rows:
                if (type(k) is not str or k != kind or type(t) is not str or t != target
                        or type(p) is not str):
                    raise OverlayIndexUnavailable('malformed_candidate')
                try:
                    doc_kind, _ = _doc_path(p, expected.chat_id)
                except (TypeError, ValueError) as exc:
                    raise OverlayIndexUnavailable('malformed_candidate') from exc
                if doc_kind != ('reactions' if k == 'reaction' else 'state'):
                    raise OverlayIndexUnavailable('malformed_candidate')
                if type(size) is not int or size < 0:
                    raise OverlayIndexUnavailable('malformed_candidate')
                selected.append((k, t, p))
                paths.add(p)
                used += size
                if len(selected) > max_rows or len(paths) > MAX_DEPENDENCIES or used > max_bytes:
                    raise OverflowError('overlay selection exceeds budget')
    present, absent = [], []
    for name in sorted(paths):
        size = conn.execute('SELECT size FROM overlay_index_docs WHERE source=? AND path=?', (source_id, name)).fetchone()
        if size is None:
            if name not in state_paths:
                raise OverlayIndexUnavailable('candidate_document_missing')
            absent.append(name)
            continue
        if type(size[0]) is not int or size[0] < 0:
            raise OverlayIndexUnavailable('malformed_document_size')
        used += size[0]
        if used > max_bytes:
            raise OverflowError('overlay selection exceeds byte budget')
        present.append(name)
    documents = []
    for name in present:
        row = conn.execute("SELECT kind,actor,empty,signature<>'',shape_error,scalars FROM overlay_index_docs WHERE source=? AND path=?", (source_id, name)).fetchone()
        kind, actor = _doc_path(name, expected.chat_id)
        if (row is None or type(row[0]) is not str or row[0] != kind
                or type(row[1]) is not str or row[1] != actor
                or type(row[2]) is not int or row[2] not in (0, 1)
                or type(row[3]) is not int or row[3] not in (0, 1)
                or type(row[4]) is not str or row[4] not in _SHAPE_ERRORS
                or type(row[5]) is not str):
            raise OverlayIndexUnavailable('malformed_document')
        try:
            scalar = json.loads(row[5])
            if type(scalar) is not dict:
                raise ValueError('scalars must be object')
            encoded = json.dumps(scalar, ensure_ascii=False, allow_nan=False,
                                 sort_keys=True, separators=(',', ':'))
            if encoded != row[5]:
                raise ValueError('noncanonical scalars')
        except (TypeError, ValueError, RecursionError) as exc:
            raise OverlayIndexUnavailable('malformed_document_scalars') from exc
        flags = conn.execute('SELECT is_dict,sig_truthy,sig_string,signing_available,ids_compatible FROM overlay_index_shapes WHERE source=? AND path=?', (source_id, name)).fetchone()
        if flags is None or any(type(v) is not int or v not in (0, 1) for v in flags):
            raise OverlayIndexUnavailable('malformed_document_shape')
        shape = DocumentShape(*(bool(v) for v in flags))
        documents.append(DocumentSummary(name, row[0], row[1], row[2] == 1, row[3] == 1, row[4], row[5], shape))
    candidates = []
    for kind, target, name in selected:
        row = conn.execute('SELECT value FROM overlay_index_candidates WHERE source=? AND kind=? AND target=? AND path=?', (source_id, kind, target, name)).fetchone()
        if row is None or type(row[0]) is not str:
            raise OverlayIndexUnavailable('malformed_candidate')
        candidates.append(OverlayCandidate(name, kind, target, row[0]))
    return IndexedOverlayInputs(expected, tuple(documents), tuple(candidates), tuple(absent), include_reactions)


def _key(value):
    _text(value, 'public key', 128)
    try:
        raw = crypto.b64d(value)
    except Exception as exc:
        raise ValueError('invalid public key') from exc
    if len(raw) != 32:
        raise ValueError('invalid public key')
    return raw


def verify_signature(conn, path, expected, document_path, public_key, *, max_bytes=16 * 1024 * 1024):
    """Background pure verification. Recheck source/build before committing it."""
    expected = _wanted(expected, path)
    _doc_path(document_path, expected.chat_id)
    key = _key(public_key)
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BUILD_BYTES:
        raise ValueError('invalid signing byte budget')
    reader = source._open_reader(path)
    try:
        reader.execute('BEGIN')
        _ready(reader, path, expected)
        size = reader.execute('SELECT signing_size FROM overlay_index_docs WHERE source=? AND path=?', (expected.source.source_id, document_path)).fetchone()
        if size is None:
            raise OverlayIndexUnavailable('document_missing')
        if type(size[0]) is not int or not 0 <= size[0] <= max_bytes:
            raise OverflowError('signing input exceeds budget')
        sig, data = reader.execute('SELECT signature,signing FROM overlay_index_docs WHERE source=? AND path=?', (expected.source.source_id, document_path)).fetchone()
        if type(sig) is not str or type(data) is not bytes or len(data) != size[0]:
            raise OverlayIndexUnavailable('malformed_signing_input')
    finally:
        reader.close()
    valid = crypto.verify(crypto.b64e(key), sig, data)
    if conn.in_transaction:
        raise sqlite3.OperationalError('proof publication requires an owned transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        _ready(conn, path, expected)
        # Keep at most one key's evidence per document. Rotation or competing
        # workers may evict useful evidence (yielding pending), never authority.
        # This bounds storage even when the same source sees many distinct keys.
        conn.execute('DELETE FROM overlay_index_proofs WHERE source=? AND path=? AND pub<>?',
                     (expected.source.source_id, document_path, key))
        conn.execute('INSERT INTO overlay_index_proofs VALUES(?,?,?,?) ON CONFLICT(source,path,pub) DO UPDATE SET valid=excluded.valid', (expected.source.source_id, document_path, key, int(valid)))
        # Proof-table mutation clears ready. Re-publish the SAME index identity:
        # candidate rows did not change and evidence is a deterministic predicate.
        _write_ready(conn, expected)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return valid


def proofs(path, expected, keys):
    conn = source._open_reader(path)
    try:
        conn.execute('BEGIN')
        return _proofs(conn, path, expected, keys)
    finally:
        conn.close()


def _proofs(conn, path, expected, keys):
    if not conn.in_transaction:
        raise sqlite3.OperationalError('capture requires an active read transaction')
    expected = _wanted(expected, path)
    if type(keys) is not tuple or len(keys) > MAX_DEPENDENCIES:
        raise ValueError('invalid proof selectors')
    normalized = []
    for item in keys:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError('invalid proof selector')
        name, key = item
        _doc_path(name, expected.chat_id)
        normalized.append((name, _key(key)))
    if len(set(normalized)) != len(normalized):
        raise ValueError('duplicate proof selector')
    _ready(conn, path, expected)
    out = []
    for name, key in normalized:
        row = conn.execute('SELECT valid FROM overlay_index_proofs WHERE source=? AND path=? AND pub=?', (expected.source.source_id, name, key)).fetchone()
        if row is not None and (type(row[0]) is not int or row[0] not in (0, 1)):
            raise OverlayIndexUnavailable('malformed_proof')
        out.append(None if row is None else row[0] == 1)
    return tuple(out)
