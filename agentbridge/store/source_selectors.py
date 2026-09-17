"""Bounded registered raw-source dependencies for local mutation retirement.

Inactive. Callers must hold the root mutation coordinator before these Store
transactions. Selectors record consumed raw scope, never permission decisions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import document_observation as docs, local_source as owner

MAX_SOURCES = 1024
MAX_SELECTORS = 256
_KINDS = ('doc_exact', 'doc_prefix', 'log_chat')
_SCHEMA = {
    'local_source_definitions': 'CREATE TABLE local_source_definitions(source TEXT PRIMARY KEY,definition TEXT NOT NULL)',
    'local_source_selectors': 'CREATE TABLE local_source_selectors(source TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(source,kind,value))',
    'idx_local_source_selector_match': 'CREATE INDEX idx_local_source_selector_match ON local_source_selectors(kind,value,source)',
}


@dataclass(frozen=True, order=True)
class Selector:
    kind: str
    value: str


@dataclass(frozen=True)
class Definition:
    source: str
    serialized: str
    selectors: tuple[Selector, ...]


def selectors(values, *, limit=MAX_SELECTORS):
    if type(values) is not tuple or not 1 <= len(values) <= limit:
        raise ValueError('invalid selector count')
    copied = set()
    used = 0
    for item in values:
        if type(item) is not Selector:
            raise ValueError('invalid selector')
        kind, value = item.kind, item.value
        if type(kind) is not str or kind not in _KINDS:
            raise ValueError('invalid selector kind')
        value = docs._validate_document_path(value)
        if len(value.split('/')) > 32 or (kind == 'log_chat' and '/' in value):
            raise ValueError('invalid selector depth')
        used += len(kind) + len(value.encode())
        if used > 64 * 1024:
            raise ValueError('selector byte budget')
        copied.add(Selector(kind, value))
    return tuple(sorted(copied))


def definition(root_identity, values, *, build='phase1-v1'):
    if (type(root_identity) is not str or not root_identity or len(root_identity) > 4096
            or type(build) is not str or not build or len(build) > 128):
        raise ValueError('invalid source identity')
    copied = selectors(values)
    serialized = json.dumps([root_identity, build, [(s.kind, s.value) for s in copied]],
                            ensure_ascii=False, separators=(',', ':'))
    if len(serialized.encode()) > 64 * 1024:
        raise ValueError('definition byte budget')
    return Definition('local-inputs-v1:' + hashlib.sha256(serialized.encode()).hexdigest(),
                      serialized, copied)


def _definition(value):
    if type(value) is not Definition or type(value.serialized) is not str or len(value.serialized) > 65536:
        raise ValueError('invalid source definition')
    source, serialized, selected = value.source, value.serialized, value.selectors
    if type(source) is not str or len(source) > 128 or len(serialized.encode()) > 65536:
        raise ValueError('invalid definition binding')
    selected = selectors(selected)
    decoded = json.loads(serialized)
    if type(decoded) is not list or len(decoded) != 3 or type(decoded[2]) is not list or len(decoded[2]) > MAX_SELECTORS:
        raise ValueError('invalid serialized definition')
    raw = []
    for pair in decoded[2]:
        if type(pair) is not list or len(pair) != 2:
            raise ValueError('invalid serialized selector')
        raw.append(Selector(*pair))
    rebuilt = definition(decoded[0], tuple(raw), build=decoded[1])
    if (rebuilt.source, rebuilt.serialized, rebuilt.selectors) != (source, serialized, selected):
        raise ValueError('definition binding mismatch')
    return rebuilt


def _schema(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?) LIMIT 1", ('local_source_definitions', 'local_source_selectors')).fetchone():
        raise owner.SourceChanged('unexpected_selector_trigger')
    owner._schema(conn)
    for name, sql in _SCHEMA.items():
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
            raise owner.SourceChanged('selector_schema_changed')


def initialize(store):
    with owner._writer(store) as conn:
        owner._schema(conn)
        count = conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN (?,?,?)", tuple(_SCHEMA)).fetchone()[0]
        if count:
            _schema(conn)
            return
        for sql in _SCHEMA.values():
            conn.execute(sql)


def register_in_transaction(conn, store, value):
    """Register immutable coverage; caller owns coordinator -> Store ordering."""
    value = _definition(value)
    current = owner.capture_in_transaction(conn, store, value.source)
    _schema(conn)
    size = conn.execute('SELECT typeof(definition),length(CAST(definition AS BLOB)) FROM local_source_definitions WHERE source=?', (value.source,)).fetchone()
    if size is not None and (size[0] != 'text' or size[1] > 65536):
        raise owner.SourceChanged('registered_definition_budget')
    existing = conn.execute('SELECT definition FROM local_source_definitions WHERE source=?', (value.source,)).fetchone()
    if existing:
        if existing != (value.serialized,):
            raise owner.SourceChanged('definition_changed')
        bad = conn.execute("SELECT 1 FROM local_source_selectors WHERE source=? AND (typeof(kind)!='text' OR typeof(value)!='text' OR length(CAST(kind AS BLOB))>16 OR length(CAST(value AS BLOB))>4096) LIMIT 1", (value.source,)).fetchone()
        if bad:
            raise owner.SourceChanged('registered_selector_budget')
        registered = tuple(Selector(*row) for row in conn.execute('SELECT kind,value FROM local_source_selectors WHERE source=? ORDER BY kind,value LIMIT ?', (value.source, MAX_SELECTORS + 1)))
        if registered != value.selectors:
            raise owner.SourceChanged('registered_selectors_changed')
        return current
    count = conn.execute('SELECT count(*) FROM (SELECT 1 FROM local_source_definitions LIMIT ?)', (MAX_SOURCES,)).fetchone()[0]
    if count >= MAX_SOURCES:
        raise owner.SourceChanged('source_registration_budget')
    conn.execute('INSERT INTO local_source_definitions VALUES(?,?)', (value.source, value.serialized))
    conn.executemany('INSERT INTO local_source_selectors VALUES(?,?,?)',
                     ((value.source, s.kind, s.value) for s in value.selectors))
    owner._advance(conn, value.source, current.revision)
    return owner.capture_in_transaction(conn, store, value.source)


def matching_in_transaction(conn, changes):
    """Return at most 1024 affected source IDs using bounded selector queries."""
    if not conn.in_transaction:
        raise ValueError('selector matching requires a transaction')
    changes = selectors(changes, limit=8)
    _schema(conn)
    found = set()
    for change in changes:
        if change.kind == 'log_chat':
            clauses, args = ['(kind=? AND value=?)'], ['log_chat', change.value]
        else:
            parts = change.value.split('/')
            ancestors = ['/'.join(parts[:i]) for i in range(1, len(parts))]
            if change.kind == 'doc_prefix':
                ancestors.append(change.value)
            clauses, args = ['(kind=? AND value=?)'], ['doc_exact', change.value]
            if ancestors:
                clauses.append("(kind='doc_prefix' AND value IN (" + ','.join('?' for _ in ancestors) + '))')
                args.extend(ancestors)
            if change.kind == 'doc_prefix':
                clauses.append("(kind IN ('doc_exact','doc_prefix') AND value>=? AND value<?)")
                args.extend((change.value + '/', change.value + '0'))
        query = 'SELECT DISTINCT source FROM local_source_selectors WHERE ' + ' OR '.join(clauses) + ' LIMIT ?'
        rows = conn.execute(query, (*args, MAX_SOURCES + 1)).fetchall()
        found.update(docs._validate_source_id(row[0]) for row in rows)
        if len(found) > MAX_SOURCES:
            raise owner.SourceChanged('mutation_source_fanout_budget')
    return tuple(sorted(found))


def retire_in_transaction(conn, store, changes):
    """Set-retire all matched sources in the caller's single Store transaction.

    No transport call may run until this transaction durably commits, and the
    root coordinator must finish every registered Store before external write.
    """
    # Validate transaction/database using an observation-only sentinel lookup.
    owner.capture_in_transaction(conn, store, 'selector-owner-check')
    affected = matching_in_transaction(conn, changes)
    for offset in range(0, len(affected), 400):
        batch = affected[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        rows = conn.execute(f'SELECT source,revision FROM local_sources WHERE source IN ({marks})', batch).fetchall()
        if len(rows) != len(batch) or any(type(r[1]) is not int or not 1 <= r[1] < owner.MAX for r in rows):
            raise owner.SourceChanged('mutation_revision_unavailable')
        changed = conn.execute(f'UPDATE local_sources SET revision=revision+1,ready_incarnation=NULL,ready_generation=NULL,ready_cursor=NULL WHERE source IN ({marks})', batch).rowcount
        after = conn.execute(f'SELECT source,revision,ready_incarnation,ready_generation,ready_cursor FROM local_sources WHERE source IN ({marks})', batch).fetchall()
        expected = {source: revision + 1 for source, revision in rows}
        if (changed != len(batch) or len(after) != len(batch)
                or any(row[1] != expected[row[0]] or row[2:] != (None, None, None) for row in after)):
            raise owner.SourceChanged('mutation_retirement_incomplete')
    return len(affected)
