"""Local root-wide mutation ordering across GUI/harness Store databases.

Inactive: transport interception, page publication gates and lifecycle integration
must be installed together. This coordinator never executes or retries a remote
operation and never clears an ambiguous intent based on age or process death.
"""
from __future__ import annotations

from ..core.input_phase_timings import span
from ..core import delivery_trace

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import local_source as owner, source_selectors as scopes

MAX_STORES = 64
MAX_NODES = 16
MAX_PENDING = 64
MAX_QUARANTINED = 256
ROOT_LOCK_TIMEOUT_S = 5.0
_WINDOWS = os.name == 'nt'
_NORMAL_TOKEN = re.compile(r'[0-9a-f]{64}\Z')
_SENTINEL_TOKEN = re.compile(r'q[0-9a-f]{63}\Z')
_COORDINATOR_EPOCH = re.compile(r'[0-9a-f]{32}\Z')
_SCHEMA = {
    'mutation_root': 'CREATE TABLE mutation_root(singleton INTEGER PRIMARY KEY CHECK(singleton=1),identity TEXT NOT NULL,epoch TEXT NOT NULL,format INTEGER NOT NULL CHECK(format=3),revision INTEGER NOT NULL CHECK(revision>=0))',
    'mutation_stores': 'CREATE TABLE mutation_stores(path TEXT PRIMARY KEY,incarnation TEXT NOT NULL,source_epoch TEXT NOT NULL)',
    'mutation_nodes': 'CREATE TABLE mutation_nodes(path TEXT PRIMARY KEY,database_incarnation TEXT NOT NULL,identity_digest TEXT NOT NULL,provider_endpoint TEXT NOT NULL,root TEXT NOT NULL)',
    'mutation_intents': 'CREATE TABLE mutation_intents(token TEXT PRIMARY KEY)',
    'mutation_scopes': 'CREATE TABLE mutation_scopes(token TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(token,kind,value))',
}
_V4_ROOT_SCHEMA = _SCHEMA['mutation_root'].replace('format=3', 'format=4')
_V5_ROOT_SCHEMA = _SCHEMA['mutation_root'].replace('format=3', 'format=5')
_V6_ROOT_SCHEMA = _SCHEMA['mutation_root'].replace('format=3', 'format=6')
_V7_ROOT_SCHEMA = _SCHEMA['mutation_root'].replace('format=3', 'format=7')
_V8_ROOT_SCHEMA = _SCHEMA['mutation_root'].replace('format=3', 'format=8')
_V1_ROOT_SCHEMA = ('CREATE TABLE mutation_root(singleton INTEGER PRIMARY KEY '
                   'CHECK(singleton=1),identity TEXT NOT NULL,epoch TEXT NOT NULL)')
_V2_ROOT_SCHEMA = ('CREATE TABLE mutation_root(singleton INTEGER PRIMARY KEY '
                   'CHECK(singleton=1),identity TEXT NOT NULL,epoch TEXT NOT NULL,'
                   'format INTEGER NOT NULL CHECK(format=2))')
_QUARANTINE_SCHEMA = {
    'quarantine_intents': 'CREATE TABLE quarantine_intents(token TEXT PRIMARY KEY,sentinel TEXT NOT NULL)',
    'quarantine_scopes': 'CREATE TABLE quarantine_scopes(token TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(token,kind,value))',
}


@dataclass(frozen=True)
class MutationIntent:
    coordinator_path: str
    epoch: str
    token: str
    selectors: tuple[scopes.Selector, ...]


@dataclass(frozen=True)
class MutationRecoveryInventory:
    """Content-free summary bound to one exact legacy mutation/node cut."""

    schema_version: int
    plan_digest: str
    coordinator_identity_digest: str
    coordinator_epoch: str
    root_revision: int
    normal_intents: int
    sentinel_intents: int
    quarantined_intents: int
    pending_scope_counts: tuple[tuple[str, int], ...]
    quarantine_scope_counts: tuple[tuple[str, int], ...]
    registered_stores: int
    registered_nodes: int
    admitted_generation: int
    node_health: str
    node_fence_counts: tuple[tuple[str, int], ...]


def _overlap(left, right):
    if left.kind == 'root_all' or right.kind == 'root_all':
        return True
    if left.kind == 'log_chat' or right.kind == 'log_chat':
        return left.kind == right.kind and left.value == right.value
    if left.kind == right.kind == 'doc_exact':
        return left.value == right.value
    if left.kind == 'doc_prefix' and (right.value == left.value or right.value.startswith(left.value + '/')):
        return True
    return right.kind == 'doc_prefix' and (left.value == right.value or left.value.startswith(right.value + '/'))


class MutationCoordinator:
    def __init__(self, home, root_identity):
        if type(root_identity) is not str or not root_identity or len(root_identity.encode()) > 4096:
            raise ValueError('invalid mutation root')
        self.identity = root_identity
        directory = Path(home).resolve() / 'local-input-owners'
        from ..node.security import private_directory
        private_directory(directory)
        self.path = directory / (hashlib.sha256(root_identity.encode()).hexdigest() + '.sqlite')
        # SQLite serializes concurrent initialization, including empty-file races.
        with self._transaction(create=True) as conn:
            found = conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'mutation_*'").fetchall()
            if not found:
                for statement in _SCHEMA.values():
                    conn.execute(statement)
                conn.execute('INSERT INTO mutation_root VALUES(1,?,?,3,0)',
                             (self.identity, secrets.token_hex(16)))
            else:
                root_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='mutation_root'"
                ).fetchone()
                if root_sql == (_V1_ROOT_SCHEMA,):
                    row = conn.execute(
                        'SELECT identity,epoch FROM mutation_root WHERE singleton=1'
                    ).fetchone()
                    if (row is None or row[0] != self.identity
                            or type(row[1]) is not str
                            or _COORDINATOR_EPOCH.fullmatch(row[1]) is None):
                        raise owner.SourceChanged('mutation_root_changed')
                    # Changing a table known to V1 makes every old binary fail
                    # its exact schema check, even if it starts after migration.
                    conn.execute('ALTER TABLE mutation_root RENAME TO mutation_root_v1')
                    conn.execute(_SCHEMA['mutation_root'])
                    conn.execute('INSERT INTO mutation_root VALUES(1,?,?,3,0)',
                                 (self.identity, secrets.token_hex(16)))
                    conn.execute('DROP TABLE mutation_root_v1')
                elif root_sql == (_V2_ROOT_SCHEMA,):
                    row = conn.execute(
                        'SELECT identity,epoch FROM mutation_root WHERE singleton=1'
                    ).fetchone()
                    if (row is None or row[0] != self.identity
                            or type(row[1]) is not str
                            or _COORDINATOR_EPOCH.fullmatch(row[1]) is None):
                        raise owner.SourceChanged('mutation_root_changed')
                    # Preserve the coordinator epoch so an already-bound v9
                    # node does not acquire a new owner by migration alone.
                    conn.execute('ALTER TABLE mutation_root RENAME TO mutation_root_v2')
                    conn.execute(_SCHEMA['mutation_root'])
                    conn.execute('INSERT INTO mutation_root VALUES(1,?,?,3,0)', row)
                    conn.execute('DROP TABLE mutation_root_v2')
                node_schema = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='mutation_nodes'"
                ).fetchone()
                if node_schema is None:
                    conn.execute(_SCHEMA['mutation_nodes'])
            quarantine = [conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                          for name in _QUARANTINE_SCHEMA]
            if all(value is None for value in quarantine):
                for statement in _QUARANTINE_SCHEMA.values():
                    conn.execute(statement)
            elif any(value is None for value in quarantine):
                raise owner.SourceChanged('quarantine_schema_incomplete')
            self.epoch = self._schema(conn)

    def _private_files(self, *, create=False):
        from ..node.security import LocalSecurityError, protect_path, verify_private_path

        verify_private_path(self.path.parent, directory=True)
        if create:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            protect_path(self.path, directory=False)
        verify_private_path(self.path, directory=False)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                try:
                    if create or _WINDOWS:
                        protect_path(sidecar, directory=False)
                    verify_private_path(sidecar, directory=False)
                except LocalSecurityError:
                    # SQLite may remove a sidecar between lstat and its final
                    # close. A still-present unsafe file always fails closed.
                    if sidecar.exists() or sidecar.is_symlink():
                        raise

    @contextmanager
    def _transaction(self, *, create=False, timeout_s=1.0):
        self._private_files(create=create)
        with delivery_trace.transaction('root', self.path) as trace:
            mode = 'rwc' if create else 'rw'
            with span('root_open'):
                conn = sqlite3.connect(
                    f'{self.path.as_uri()}?mode={mode}', uri=True,
                    timeout=timeout_s,
                )
            try:
                if create:
                    conn.execute('PRAGMA journal_mode=WAL')
                with span('root_setup'):
                    conn.execute('PRAGMA synchronous=FULL')
                with span('root_begin'):
                    with trace.acquiring():
                        conn.execute('BEGIN IMMEDIATE')
                yield conn
                with span('root_commit'):
                    with trace.finishing('db_commit'):
                        conn.commit()
            except BaseException:
                with span('root_rollback'):
                    with trace.finishing('db_rollback'):
                        conn.rollback()
                raise
            finally:
                with span('root_close'):
                    conn.close()

    def _schema(self, conn):
        tables = tuple(_SCHEMA) + tuple(_QUARANTINE_SCHEMA)
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN ({','.join('?' for _ in tables)}) LIMIT 1", tables).fetchone():
            raise owner.SourceChanged('unexpected_mutation_trigger')
        root_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='mutation_root'").fetchone()
        reservation_format = root_sql == (_V8_ROOT_SCHEMA,)
        consumer_format = reservation_format or root_sql == (_V7_ROOT_SCHEMA,)
        lifecycle_format = consumer_format or root_sql == (_V6_ROOT_SCHEMA,)
        root_all_format = lifecycle_format or root_sql == (_V5_ROOT_SCHEMA,)
        journal_format = root_all_format or root_sql == (_V4_ROOT_SCHEMA,)
        expected_schema = {**_SCHEMA, **_QUARANTINE_SCHEMA}
        if journal_format:
            expected_schema['mutation_root'] = (_V8_ROOT_SCHEMA if reservation_format else
                                               _V7_ROOT_SCHEMA if consumer_format else
                                               _V6_ROOT_SCHEMA if lifecycle_format else
                                               _V5_ROOT_SCHEMA if root_all_format else _V4_ROOT_SCHEMA)
        for name, sql in expected_schema.items():
            if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
                raise owner.SourceChanged('mutation_schema_changed')
        row = conn.execute(
            'SELECT identity,epoch,format,revision FROM mutation_root WHERE singleton=1'
        ).fetchone()
        if (row is None or row[0] != self.identity or type(row[1]) is not str or len(row[1]) != 32
                or any(c not in '0123456789abcdef' for c in row[1])
                or row[2] != (8 if reservation_format else 7 if consumer_format else 6 if lifecycle_format else 5 if root_all_format else 4 if journal_format else 3) or type(row[3]) is not int
                or not 0 <= row[3] <= 2**63 - 1
                or (hasattr(self, 'epoch') and row[1] != self.epoch)):
            raise owner.SourceChanged('mutation_root_changed')
        tokens = conn.execute('SELECT token FROM mutation_intents LIMIT ?', (MAX_PENDING + 1,)).fetchall()
        if len(tokens) > MAX_PENDING or any(type(t[0]) is not str or
                not (_NORMAL_TOKEN.fullmatch(t[0]) or _SENTINEL_TOKEN.fullmatch(t[0]))
                for t in tokens):
            raise owner.SourceChanged('invalid_pending_intents')
        if conn.execute('SELECT 1 FROM mutation_scopes LIMIT 1 OFFSET ?',
                        (MAX_PENDING * 8,)).fetchone():
            raise owner.SourceChanged('pending_selector_budget')
        if conn.execute("SELECT 1 FROM mutation_scopes WHERE typeof(token)!='text' OR length(CAST(token AS BLOB))>64 OR typeof(kind)!='text' OR kind NOT IN ('doc_exact','doc_prefix','log_chat','root_all') OR length(CAST(kind AS BLOB))>16 OR typeof(value)!='text' OR length(CAST(value AS BLOB))>4096 OR (kind='root_all' AND (?=0 OR value!='*')) LIMIT 1", (root_all_format,)).fetchone():
            raise owner.SourceChanged('invalid_pending_selector')
        # Preflight above bounds every field before fetching values. Audit
        # the same fresh cut in one bounded query rather than one per intent.
        scope_rows = conn.execute(
            'SELECT token,kind,value FROM mutation_scopes ORDER BY token,kind,value LIMIT ?',
            (MAX_PENDING * 8 + 1,),
        ).fetchall()
        if len(scope_rows) > MAX_PENDING * 8:
            raise owner.SourceChanged('pending_selector_budget')
        grouped = {}
        for token, kind, value in scope_rows:
            grouped.setdefault(token, []).append((kind, value))
        sentinels = {}
        for (token,) in tokens:
            rows = grouped.get(token, ())
            if not 1 <= len(rows) <= 8:
                raise owner.SourceChanged('pending_intent_scope_mismatch')
            try:
                scopes.selectors(tuple(scopes.Selector(*item) for item in rows), limit=8, allow_root=root_all_format)
            except ValueError:
                raise owner.SourceChanged('invalid_pending_selector') from None
            if _SENTINEL_TOKEN.fullmatch(token):
                sentinels[token] = tuple(rows)
        if set(grouped) != {token for (token,) in tokens}:
            raise owner.SourceChanged('orphan_mutation_scope')
        self._quarantine_audit(conn, sentinels, allow_root=root_all_format)
        from .operation_journal import audit_journal
        audit_journal(conn, self, required=journal_format)
        return row[1]

    @staticmethod
    def _root_revision(conn):
        row = conn.execute(
            'SELECT revision FROM mutation_root WHERE singleton=1').fetchone()
        if row is None or type(row[0]) is not int or not 0 <= row[0] <= 2**63 - 1:
            raise owner.SourceChanged('mutation_root_changed')
        return row[0]

    @staticmethod
    def _advance_root_revision(conn):
        revision = MutationCoordinator._root_revision(conn)
        if revision >= 2**63 - 1:
            raise owner.SourceChanged('mutation_root_revision_exhausted')
        conn.execute(
            'UPDATE mutation_root SET revision=revision+1 WHERE singleton=1')
        return revision + 1

    @staticmethod
    def _quarantine_audit(conn, sentinels, *, allow_root=False):
        """Bounded archive audit: an archived token must retain its old fence."""
        if conn.execute('SELECT 1 FROM quarantine_intents LIMIT 1 OFFSET ?',
                        (MAX_QUARANTINED,)).fetchone():
            raise owner.SourceChanged('quarantine_budget')
        if conn.execute('SELECT 1 FROM quarantine_scopes LIMIT 1 OFFSET ?',
                        (MAX_QUARANTINED * 8,)).fetchone():
            raise owner.SourceChanged('quarantine_scope_budget')
        if conn.execute("SELECT 1 FROM quarantine_intents WHERE typeof(token)!='text' OR length(CAST(token AS BLOB))>64 OR typeof(sentinel)!='text' OR length(CAST(sentinel AS BLOB))>64 LIMIT 1").fetchone():
            raise owner.SourceChanged('invalid_quarantine_intent')
        if conn.execute("SELECT 1 FROM quarantine_scopes WHERE typeof(token)!='text' OR length(CAST(token AS BLOB))>64 OR typeof(kind)!='text' OR kind NOT IN ('doc_exact','doc_prefix','log_chat','root_all') OR length(CAST(kind AS BLOB))>16 OR typeof(value)!='text' OR length(CAST(value AS BLOB))>4096 OR (kind='root_all' AND (?=0 OR value!='*')) LIMIT 1", (allow_root,)).fetchone():
            raise owner.SourceChanged('invalid_quarantine_scope')
        archived = conn.execute('SELECT token,sentinel FROM quarantine_intents LIMIT ?',
                                (MAX_QUARANTINED + 1,)).fetchall()
        if len(archived) > MAX_QUARANTINED:
            raise owner.SourceChanged('quarantine_budget')
        grouped = {}
        for token, kind, value in conn.execute(
                'SELECT token,kind,value FROM quarantine_scopes ORDER BY token,kind,value LIMIT ?',
                (MAX_QUARANTINED * 8 + 1,)):
            grouped.setdefault(token, []).append((kind, value))
        seen = set()
        for token, sentinel in archived:
            if (not _NORMAL_TOKEN.fullmatch(token) or sentinel not in sentinels
                    or token in seen):
                raise owner.SourceChanged('quarantine_fence_missing')
            seen.add(token)
            if tuple(grouped.get(token, ())) != sentinels[sentinel]:
                raise owner.SourceChanged('quarantine_scope_mismatch')
        if set(grouped) != seen:
            raise owner.SourceChanged('orphan_quarantine_scope')
        if set(sentinels) != {sentinel for _, sentinel in archived}:
            raise owner.SourceChanged('orphan_quarantine_fence')

    def _stores(self, conn):
        if conn.execute("SELECT 1 FROM mutation_stores WHERE typeof(path)!='text' OR length(CAST(path AS BLOB))>16384 OR typeof(incarnation)!='text' OR length(CAST(incarnation AS BLOB))>4096 OR typeof(source_epoch)!='text' OR length(source_epoch)!=32 LIMIT 1").fetchone():
            raise owner.SourceChanged('invalid_store_registration')
        rows = conn.execute('SELECT path,incarnation,source_epoch FROM mutation_stores ORDER BY path LIMIT ?', (MAX_STORES + 1,)).fetchall()
        if len(rows) > MAX_STORES:
            raise owner.SourceChanged('mutation_store_budget')
        for path, incarnation, epoch in rows:
            if (type(path) is not str or len(path) > 4096 or not Path(path).is_absolute()
                    or str(Path(path).resolve()) != path or type(incarnation) is not str
                    or type(epoch) is not str or len(epoch) != 32):
                raise owner.SourceChanged('invalid_store_registration')
        return rows

    def _nodes(self, conn):
        from ..node.admission import NodeCoordinatorRegistration

        if conn.execute("SELECT 1 FROM mutation_nodes WHERE typeof(path)!='text' OR length(CAST(path AS BLOB))>16384 OR typeof(database_incarnation)!='text' OR length(CAST(database_incarnation AS BLOB))>4096 OR typeof(identity_digest)!='text' OR length(identity_digest)!=64 OR typeof(provider_endpoint)!='text' OR length(CAST(provider_endpoint AS BLOB))>2048 OR typeof(root)!='text' OR length(CAST(root AS BLOB))>1024 LIMIT 1").fetchone():
            raise owner.SourceChanged('invalid_node_registration')
        rows = conn.execute(
            'SELECT path,database_incarnation,identity_digest,provider_endpoint,root '
            'FROM mutation_nodes ORDER BY path LIMIT ?', (MAX_NODES + 1,),
        ).fetchall()
        if len(rows) > MAX_NODES:
            raise owner.SourceChanged('mutation_node_budget')
        values = []
        for path, incarnation, digest, endpoint, root in rows:
            if (type(path) is not str or not Path(path).is_absolute()
                    or str(Path(path).resolve()) != path
                    or type(incarnation) is not str or not incarnation
                    or type(digest) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', digest) is None
                    or type(endpoint) is not str or not endpoint
                    or type(root) is not str or not root):
                raise owner.SourceChanged('invalid_node_registration')
            values.append(NodeCoordinatorRegistration(
                path, incarnation, digest, endpoint, root,
            ))
        return tuple(values)

    @staticmethod
    def _registered_node(root, path):
        if root.execute(
                "SELECT 1 FROM mutation_nodes WHERE path=? AND ("
                "typeof(path)!='text' OR length(CAST(path AS BLOB))>16384 OR "
                "typeof(database_incarnation)!='text' OR "
                "length(CAST(database_incarnation AS BLOB))>4096 OR "
                "typeof(identity_digest)!='text' OR length(identity_digest)!=64 OR "
                "typeof(provider_endpoint)!='text' OR "
                "length(CAST(provider_endpoint AS BLOB))>2048 OR "
                "typeof(root)!='text' OR length(CAST(root AS BLOB))>1024) LIMIT 1",
                (path,),
        ).fetchone():
            raise owner.SourceChanged('invalid_node_registration')
        row = root.execute(
            'SELECT database_incarnation,identity_digest,provider_endpoint,root '
            'FROM mutation_nodes WHERE path=?', (path,),
        ).fetchone()
        if row is None:
            raise owner.SourceChanged('node_not_registered')
        from ..node.admission import NodeCoordinatorRegistration
        return NodeCoordinatorRegistration(path, *row)

    @staticmethod
    def _pending_tokens(root):
        rows = root.execute(
            'SELECT token FROM mutation_intents ORDER BY token LIMIT ?',
            (MAX_PENDING + 1,),
        ).fetchall()
        if len(rows) > MAX_PENDING:
            raise owner.SourceChanged('pending_mutation_budget')
        return tuple(token for (token,) in rows)

    def register_node(self, node):
        """Join one node before activation and conservatively retire old readiness."""
        from ..node.store import NodeStore

        if type(node) is not NodeStore:
            raise ValueError('invalid local node')
        registration = node.coordinator_registration()
        try:
            expected = json.loads(self.identity)
        except (TypeError, ValueError):
            raise ValueError('invalid mutation root identity') from None
        if (type(expected) is not list or len(expected) != 4
                or expected[:2] != ['local-root-v1', 'supabase']
                or type(expected[2]) is not str
                or type(expected[3]) is not str
                or registration.provider_endpoint != expected[2]
                or registration.root != 'supabase://' + expected[3]):
            raise ValueError('local node belongs to another transport root')
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            nodes = self._nodes(root)
            existing = next((item for item in nodes
                             if item.database_path == registration.database_path), None)
            if existing is not None:
                if existing != registration:
                    raise owner.SourceChanged('registered_node_replaced')
                with NodeStore.coordinator_cut(registration) as node_conn:
                    NodeStore.coordinator_bind_owner(
                        node_conn, coordinator_path=str(self.path),
                        coordinator_identity=self.identity,
                        coordinator_epoch=self.epoch,
                    )
                    NodeStore.coordinator_require_fences(
                        node_conn, coordinator_epoch=self.epoch,
                        tokens=self._pending_tokens(root),
                    )
                return registration
            if len(nodes) >= MAX_NODES:
                raise owner.SourceChanged('mutation_node_budget')
            pending = self._pending_tokens(root)
            with NodeStore.coordinator_cut(registration) as node_conn:
                NodeStore.coordinator_bind_owner(
                    node_conn, coordinator_path=str(self.path),
                    coordinator_identity=self.identity,
                    coordinator_epoch=self.epoch,
                )
                # Private work collected before coordinator ownership cannot
                # prove it observed mutations that already completed.
                NodeStore.coordinator_abandon_candidates(node_conn)
                for token in pending:
                    NodeStore.coordinator_add_fence(
                        node_conn, coordinator_epoch=self.epoch, token=token,
                    )
                admitted = node_conn.execute(
                    'SELECT admitted_generation FROM node_meta WHERE singleton=1'
                ).fetchone()
                if admitted is None:
                    raise owner.SourceChanged('invalid_node_registration')
                if admitted[0] > 0:
                    NodeStore.coordinator_add_fence(
                        node_conn, coordinator_epoch=self.epoch,
                        token=secrets.token_hex(32), state='completed',
                    )
                NodeStore.coordinator_require_fences(
                    node_conn, coordinator_epoch=self.epoch, tokens=pending,
                )
            root.execute('INSERT INTO mutation_nodes VALUES(?,?,?,?,?)', (
                registration.database_path, registration.database_incarnation,
                registration.identity_digest, registration.provider_endpoint,
                registration.root,
            ))
            self._advance_root_revision(root)
        return registration

    @contextmanager
    def node_activation_gate(self, node):
        """Exclude every root mutation across one node-writer handoff.

        Registration has already copied the exact durable intent set into the
        node.  Holding the root writer makes the empty-intent observation and
        the caller's legacy retirement one cut: a new mutation cannot begin
        until the node source is installed, and an existing mutation prevents
        retirement altogether.
        """
        from ..node.store import NodeStore

        if type(node) is not NodeStore:
            raise ValueError('invalid local node')
        registration = node.coordinator_registration()
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            if self._registered_node(root, registration.database_path) != registration:
                raise owner.SourceChanged('registered_node_replaced')
            tokens = self._pending_tokens(root)
            with NodeStore.coordinator_cut(registration) as node_conn:
                NodeStore.coordinator_require_fences(
                    node_conn, coordinator_epoch=self.epoch, tokens=tokens,
                )
                # Node fences commit independently before the root transaction.
                # A later root rollback can therefore leave an active orphan
                # that is absent from ``tokens``.  It remains ambiguous and
                # must prevent the irreversible legacy-writer retirement.
                if tokens or NodeStore.coordinator_has_active_fences(node_conn):
                    yield False
                    return
                yield True

    def capture_legacy_recovery_inventory(self, node) -> MutationRecoveryInventory:
        """Bind an operator-safe summary to the complete root/node evidence.

        This is inspection only.  It does not complete, quarantine, archive or
        consume an intent, and the digest is not provider terminality evidence.
        A future provider seal and full recovery may bind this exact digest;
        admission must recompute it under the same root-to-node order.
        """
        from ..node.store import NodeStore

        if type(node) is not NodeStore:
            raise ValueError('invalid local node')
        registration = node.coordinator_registration()
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            if self._registered_node(root, registration.database_path) != registration:
                raise owner.SourceChanged('registered_node_replaced')
            revision = self._root_revision(root)
            stores = tuple(self._stores(root))
            nodes = tuple(self._nodes(root))
            if len(nodes) != 1 or nodes[0] != registration:
                raise owner.SourceChanged(
                    'legacy_recovery_requires_one_node')
            tokens = self._pending_tokens(root)
            pending_scopes = tuple(root.execute(
                'SELECT token,kind,value FROM mutation_scopes '
                'ORDER BY token,kind,value LIMIT ?',
                (MAX_PENDING * 8 + 1,),
            ).fetchall())
            quarantined = tuple(root.execute(
                'SELECT token,sentinel FROM quarantine_intents '
                'ORDER BY token LIMIT ?',
                (MAX_QUARANTINED + 1,),
            ).fetchall())
            quarantine_scopes = tuple(root.execute(
                'SELECT token,kind,value FROM quarantine_scopes '
                'ORDER BY token,kind,value LIMIT ?',
                (MAX_QUARANTINED * 8 + 1,),
            ).fetchall())
            with NodeStore.coordinator_cut(registration) as node_conn:
                NodeStore.coordinator_require_fences(
                    node_conn, coordinator_epoch=self.epoch, tokens=tokens,
                )
                node_inventory = NodeStore.coordinator_legacy_recovery_inventory(
                    node_conn)
                if node_inventory[5] != (
                        str(self.path), self.identity, self.epoch):
                    raise owner.SourceChanged(
                        'node_coordinator_owner_changed')

            registration_rows = tuple((
                value.database_path, value.database_incarnation,
                value.identity_digest, value.provider_endpoint, value.root,
            ) for value in nodes)
            payload = (
                'agentbridge-legacy-mutation-recovery-plan-v1',
                str(self.path), self.identity, self.epoch, revision,
                stores, registration_rows, tokens, pending_scopes,
                quarantined, quarantine_scopes, node_inventory,
            )
            digest = hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, separators=(',', ':'),
            ).encode('utf-8')).hexdigest()

        def count_kinds(rows, index):
            values = {}
            for row in rows:
                values[row[index]] = values.get(row[index], 0) + 1
            return tuple(sorted(values.items()))

        normal = sum(_NORMAL_TOKEN.fullmatch(token) is not None for token in tokens)
        sentinels = len(tokens) - normal
        fence_counts = count_kinds(node_inventory[6], 2)
        return MutationRecoveryInventory(
            1, digest, hashlib.sha256(self.identity.encode('utf-8')).hexdigest(),
            self.epoch, revision, normal, sentinels, len(quarantined),
            count_kinds(pending_scopes, 1),
            count_kinds(quarantine_scopes, 1),
            len(stores), len(nodes), node_inventory[1], node_inventory[2],
            fence_counts,
        )

    def claim_node_recovery(self, node):
        """Capture one immutable root/node cut without reading the provider."""
        from ..node.store import NodeStore

        if type(node) is not NodeStore:
            raise ValueError('invalid local node')
        registration = node.coordinator_registration()
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            if self._registered_node(root, registration.database_path) != registration:
                raise owner.SourceChanged('registered_node_replaced')
            if (root.execute('SELECT 1 FROM mutation_intents LIMIT 1').fetchone()
                    or root.execute('SELECT 1 FROM quarantine_intents LIMIT 1').fetchone()):
                raise owner.SourceChanged('root_mutation_pending')
            revision = self._root_revision(root)
            with NodeStore.coordinator_cut(registration) as node_conn:
                return NodeStore.coordinator_claim_recovery(
                    node_conn, registration,
                    coordinator_path=str(self.path),
                    coordinator_identity=self.identity,
                    coordinator_epoch=self.epoch,
                    root_revision=revision,
                )

    def begin_node_recovery(self, node, claim, current_provider_cut, *, created_ns):
        """Persist one fresh provider cut only while the claimed root is unchanged."""
        from ..node.admission import NodeRecoveryClaim
        from ..node.store import NodeStore

        if type(node) is not NodeStore or type(claim) is not NodeRecoveryClaim:
            raise ValueError('invalid claimed local node recovery')
        registration = node.coordinator_registration()
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            if self._registered_node(root, registration.database_path) != registration:
                raise owner.SourceChanged('registered_node_replaced')
            if (root.execute('SELECT 1 FROM mutation_intents LIMIT 1').fetchone()
                    or root.execute('SELECT 1 FROM quarantine_intents LIMIT 1').fetchone()):
                raise owner.SourceChanged('root_mutation_pending')
            if (claim.coordinator_path != str(self.path)
                    or claim.coordinator_identity != self.identity
                    or claim.coordinator_epoch != self.epoch
                    or claim.root_revision != self._root_revision(root)):
                raise owner.SourceChanged('node_recovery_claim_changed')
            with NodeStore.coordinator_cut(registration) as node_conn:
                recovery_id, state = node.coordinator_begin_claimed_recovery(
                    node_conn, registration, claim, current_provider_cut,
                    created_ns=created_ns,
                )
        if state is not None:
            return state
        return node.recovery_state(
            recovery_id, current_provider_cut, include_counts=False)

    def admit_node_recovery(self, node, claim, recovery_id):
        """Admit one claimed recovery while excluding every root mutation."""
        from ..node.admission import NodeRecoveryClaim
        from ..node.store import NodeStore

        if (type(node) is not NodeStore or type(claim) is not NodeRecoveryClaim
                or type(recovery_id) is not str):
            raise ValueError('invalid claimed local node admission')
        if (claim.coordinator_path != str(self.path)
                or claim.coordinator_identity != self.identity
                or claim.coordinator_epoch != self.epoch):
            raise owner.SourceChanged('node_recovery_claim_changed')
        registration = node.coordinator_registration()
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            if self._registered_node(root, registration.database_path) != registration:
                raise owner.SourceChanged('registered_node_replaced')
            with NodeStore.coordinator_cut(registration) as node_conn:
                receipt = node.coordinator_recovery_receipt(
                    node_conn, registration, claim, recovery_id)
                if receipt is not None:
                    return receipt
                if (root.execute(
                        'SELECT 1 FROM mutation_intents LIMIT 1').fetchone()
                        or root.execute(
                            'SELECT 1 FROM quarantine_intents LIMIT 1').fetchone()):
                    raise owner.SourceChanged('root_mutation_pending')
                if claim.root_revision != self._root_revision(root):
                    raise owner.SourceChanged('node_recovery_claim_changed')
                return node.coordinator_admit_claimed_recovery(
                    node_conn, registration, claim, recovery_id)

    def abandon_node_recovery(self, node, claim, recovery_id):
        """Retire one exact private recovery while preserving every fence."""
        from ..node.admission import NodeRecoveryClaim
        from ..node.store import NodeStore

        if (type(node) is not NodeStore or type(claim) is not NodeRecoveryClaim
                or type(recovery_id) is not str):
            raise ValueError('invalid claimed local node abandonment')
        if (claim.coordinator_path != str(self.path)
                or claim.coordinator_identity != self.identity
                or claim.coordinator_epoch != self.epoch):
            raise owner.SourceChanged('node_recovery_claim_changed')
        registration = node.coordinator_registration()
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as root:
            self._schema(root)
            if self._registered_node(root, registration.database_path) != registration:
                raise owner.SourceChanged('registered_node_replaced')
            with NodeStore.coordinator_cut(registration) as node_conn:
                return node.coordinator_abandon_claimed_recovery(
                    node_conn, registration, claim, recovery_id)

    def _retire_nodes(self, root, token, *, state='active', skip_path=None):
        from ..node.store import NodeStore

        for registration in self._nodes(root):
            if registration.database_path == skip_path:
                continue
            with NodeStore.coordinator_cut(registration) as conn:
                NodeStore.coordinator_add_fence(
                    conn, coordinator_epoch=self.epoch, token=token, state=state,
                )
                NodeStore.coordinator_require_fences(
                    conn, coordinator_epoch=self.epoch,
                    tokens=self._pending_tokens(root),
                )

    def _complete_nodes(self, root, token):
        from ..node.store import NodeStore

        for registration in self._nodes(root):
            with NodeStore.coordinator_cut(registration) as conn:
                NodeStore.coordinator_require_fences(
                    conn, coordinator_epoch=self.epoch,
                    tokens=self._pending_tokens(root),
                )
                NodeStore.coordinator_complete_fence(
                    conn, coordinator_epoch=self.epoch, token=token,
                )

    @staticmethod
    def _registered_store(root, path):
        # These unconstrained registry fields must be preflighted before fetching
        # their values on the bounded foreground path.
        sizes = root.execute(
            'SELECT typeof(incarnation),length(CAST(incarnation AS BLOB)),'
            'typeof(source_epoch),length(CAST(source_epoch AS BLOB)) '
            'FROM mutation_stores WHERE path=?', (path,),
        ).fetchone()
        if sizes is None:
            raise owner.SourceChanged('store_not_registered')
        if sizes[0] != 'text' or sizes[1] > 4096 or sizes[2:] != ('text', 32):
            raise owner.SourceChanged('invalid_store_registration')
        return root.execute('SELECT incarnation,source_epoch FROM mutation_stores WHERE path=?',
                            (path,)).fetchone()

    @staticmethod
    def _check_store(conn, store, incarnation, epoch):
        current = owner.capture_in_transaction(conn, store, 'root-registration-check')
        if (current.raw.incarnation, current.epoch) != (incarnation, epoch):
            raise owner.SourceChanged('registered_store_replaced')

    def register_store(self, store):
        """Must precede serving; a new registration retires all previous readiness."""
        store = SimpleNamespace(path=Path(store.path).resolve())
        path = str(store.path)
        if len(path) > 4096:
            raise ValueError('store path too long')
        with self._transaction() as root:
            self._schema(root)
            rows = self._stores(root)
            existing = next((row for row in rows if row[0] == path), None)
            if existing is None and len(rows) >= MAX_STORES:
                raise owner.SourceChanged('mutation_store_budget')
            with owner._writer(store) as conn:
                current = owner.capture_in_transaction(conn, store, 'root-registration-check')
                scopes._schema(conn)
                if existing:
                    self._check_store(conn, store, *existing[1:])
                    return
                # The Store is root-specific. New coordinator registration must
                # not inherit ready rows from a previous coordinator incarnation.
                scopes.all_sources_in_transaction(conn)
                invalid = conn.execute('SELECT 1 FROM local_sources WHERE typeof(revision)!=\'integer\' OR revision>=? OR revision<1 LIMIT 1', (owner.MAX,)).fetchone()
                if invalid:
                    raise owner.SourceChanged('registration_revision_unavailable')
                conn.execute('UPDATE local_sources SET revision=revision+1,ready_incarnation=NULL,ready_generation=NULL,ready_cursor=NULL')
            root.execute('INSERT INTO mutation_stores VALUES(?,?,?)', (path, current.raw.incarnation, current.epoch))
            self._advance_root_revision(root)

    def _retire(self, root, changes, *, skip_path=None):
        for path, incarnation, epoch in self._stores(root):
            if path == skip_path:
                continue
            store = SimpleNamespace(path=Path(path))
            with owner._writer(store) as conn:
                self._check_store(conn, store, incarnation, epoch)
                scopes.retire_in_transaction(conn, store, changes)

    def begin(self, changes):
        """Return only after every registered Store invalidation durably commits.

        Any error forbids the external call. Earlier Store commits remain pending
        if a later Store or the root commit fails; never compensate them to ready.
        """
        changes = scopes.selectors(changes, limit=8)
        # Finalization and publication hold this root writer while committing
        # their bounded Store cut.  Slower filesystems can legitimately exceed
        # one second (notably Windows CI).  A new mutation may wait through
        # that local cut because its provider operation has not started yet.
        with self._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._schema(conn)
            token = self._insert_intent(conn, changes)
            self._retire_nodes(conn, token)
            self._retire(conn, changes)
            self._advance_root_revision(conn)
            result = MutationIntent(str(self.path), self.epoch, token, changes)
        return result

    def _insert_intent(self, conn, changes):
        if any(change.kind == 'root_all' for change in changes):
            if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() not in ((5,), (6,), (7,), (8,)):
                raise owner.SourceChanged('whole_root_mutation_requires_journal')
        for kind, value in conn.execute("SELECT s.kind,s.value FROM mutation_scopes s JOIN mutation_intents i ON i.token=s.token WHERE substr(i.token,1,1)='q' LIMIT ?", (MAX_PENDING * 8 + 1,)):
            blocked = scopes.selectors((scopes.Selector(kind, value),), limit=1)[0]
            if any(_overlap(blocked, change) for change in changes):
                raise owner.SourceChanged('mutation_scope_quarantined')
        count = conn.execute('SELECT count(*) FROM (SELECT 1 FROM mutation_intents LIMIT ?)',
                             (MAX_PENDING,)).fetchone()[0]
        if count >= MAX_PENDING:
            raise owner.SourceChanged('pending_mutation_budget')
        token = secrets.token_hex(32)
        conn.execute('INSERT INTO mutation_intents VALUES(?)', (token,))
        conn.executemany('INSERT INTO mutation_scopes VALUES(?,?,?)', ((token, s.kind, s.value) for s in changes))
        return token

    def quarantine(self, expected_tokens: tuple[str, ...]):
        """Operator CAS: preserve opaque intents and keep old-process fences.

        This isolates active capacity; it is not proof of a remote outcome and
        does not make any affected page source ready. Call only after stopping
        old writers, which cannot reject overlap before creating new intents.
        """
        if (type(expected_tokens) is not tuple or not 1 <= len(expected_tokens) <= MAX_PENDING
                or len(set(expected_tokens)) != len(expected_tokens)
                or any(type(token) is not str or _NORMAL_TOKEN.fullmatch(token) is None
                       for token in expected_tokens)):
            raise ValueError('invalid expected pending tokens')
        with self._transaction() as conn:
            self._schema(conn)
            pending = conn.execute('SELECT token FROM mutation_intents ORDER BY token LIMIT ?',
                                   (MAX_PENDING + 1,)).fetchall()
            current = tuple(token for (token,) in pending if _NORMAL_TOKEN.fullmatch(token))
            if set(current) != set(expected_tokens):
                raise owner.SourceChanged('quarantine_pending_changed')
            from .operation_journal import require_legacy_intent
            for token in current:
                require_legacy_intent(conn, token)
            existing = conn.execute('SELECT count(*) FROM (SELECT 1 FROM quarantine_intents LIMIT ?)',
                                    (MAX_QUARANTINED + 1,)).fetchone()[0]
            if existing + len(current) > MAX_QUARANTINED:
                raise owner.SourceChanged('quarantine_budget')
            groups = {}
            for token in current:
                rows = tuple(conn.execute('SELECT kind,value FROM mutation_scopes WHERE token=? ORDER BY kind,value',
                                          (token,)).fetchall())
                groups.setdefault(rows, []).append(token)
            sentinel_count = len(pending) - len(current)
            if not groups or sentinel_count + len(groups) >= MAX_PENDING:
                raise owner.SourceChanged('quarantine_no_capacity_gain')
            issued = []
            for rows, members in groups.items():
                sentinel = 'q' + secrets.token_hex(32)[:63]
                while conn.execute('SELECT 1 FROM mutation_intents WHERE token=?', (sentinel,)).fetchone():
                    sentinel = 'q' + secrets.token_hex(32)[:63]
                for token in members:
                    conn.execute('INSERT INTO quarantine_intents VALUES(?,?)', (token, sentinel))
                    conn.executemany('INSERT INTO quarantine_scopes VALUES(?,?,?)',
                                     ((token, kind, value) for kind, value in rows))
                    conn.execute('DELETE FROM mutation_scopes WHERE token=?', (token,))
                    conn.execute('DELETE FROM mutation_intents WHERE token=?', (token,))
                conn.execute('INSERT INTO mutation_intents VALUES(?)', (sentinel,))
                conn.executemany('INSERT INTO mutation_scopes VALUES(?,?,?)',
                                 ((sentinel, kind, value) for kind, value in rows))
                self._retire_nodes(conn, sentinel)
                issued.append((sentinel, rows, len(members)))
            self._schema(conn)  # Validate the replacement fence before commit.
            self._advance_root_revision(conn)
            return tuple(issued)

    def complete(self, value):
        """Only after this exact external call definitively returns success.

        Not a recovery API. Other pending intents, including earlier ambiguous
        retries of the same write, remain pending. Never restores readiness.
        """
        # A definite provider success must not immediately become a remote
        # replay because the local terminal cut briefly met a busy writer.
        # Retry only this exact completion, with a fresh transaction/schema
        # check each time. The bound is attempts, not elapsed wall time.
        for attempt in range(3):
            try:
                return self._finish(value)
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', None)
                if (type(code) is not int or code & 255 not in
                        (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or attempt == 2):
                    raise

    def _finish(self, value):
        # Reservations may prove a legacy call unstarted. Typed operations
        # always require the journal's durable accepted outcome instead.
        with self._transaction() as conn:
            self._schema(conn)
            self._finish_in_transaction(conn, value)

    def _finish_in_transaction(self, conn, value, *, typed_receipt=None):
        if not conn.in_transaction or type(value) is not MutationIntent:
            raise ValueError('invalid mutation intent')
        path, epoch, token = value.coordinator_path, value.epoch, value.token
        changes = scopes.selectors(value.selectors, limit=8)
        if path != str(self.path) or epoch != self.epoch or type(token) is not str or len(token) != 64:
            raise ValueError('foreign mutation intent')
        if _SENTINEL_TOKEN.fullmatch(token):
            raise ValueError('quarantine fence cannot complete')
        from .operation_journal import require_completion_proof
        require_completion_proof(conn, token, typed_receipt)
        pending = conn.execute('SELECT kind,value FROM mutation_scopes WHERE token=? ORDER BY kind,value LIMIT 9', (token,)).fetchall()
        if tuple(scopes.Selector(*row) for row in pending) != changes:
            raise owner.SourceChanged('mutation_intent_changed')
        self._complete_nodes(conn, token)
        self._retire(conn, changes)
        if conn.execute('DELETE FROM mutation_intents WHERE token=?', (token,)).rowcount != 1:
            raise owner.SourceChanged('mutation_intent_missing')
        conn.execute('DELETE FROM mutation_scopes WHERE token=?', (token,))
        self._advance_root_revision(conn)

    def capture_source(self, store, value):
        """Register/capture a collection position without admitting readiness.

        Node publishers need this before obtaining a detached node position.
        There is no callback or yielded connection through which a legacy
        collector could publish underneath the node-only admission guard.
        """
        store = SimpleNamespace(path=Path(store.path).resolve())
        value = scopes._definition(value)
        if json.loads(value.serialized)[0] != self.identity:
            raise ValueError('definition belongs to another transport root')
        with self._transaction() as root:
            self._schema(root)
            row = self._registered_store(root, str(store.path))
            with owner._writer(store) as conn:
                self._check_store(conn, store, *row)
                position = scopes.register_in_transaction(conn, store, value)
            self._require_no_pending(root, value)
            if position.writes_pending:
                raise owner.SourceChanged('source_mutation_pending')
            return position

    @contextmanager
    def publication_gate(self, store, value, *, node_position=None):
        """Serialize registration/admission with mutation start/finish.

        Only already-collected bounded LOCAL work may execute in this scope.
        Transport reads, callbacks, signature work and scans belong outside it.
        When supplied, ``node_position`` holds the exact admitted node cut under
        root -> node ordering through the caller's later Store admission.
        """
        from ..node.store import NodeStore, _node_position

        if node_position is not None:
            node_position = _node_position(node_position)
        store = SimpleNamespace(path=Path(store.path).resolve())
        value = scopes._definition(value)
        if node_position is not None:
            required_scopes = {("root", "")}
            required_scopes.update(
                ("chat", item.value) for item in value.selectors
                if item.kind == "log_chat"
            )
            actual_scopes = {
                (item.scope_kind, item.scope_id)
                for item in node_position.scope_positions
            }
            if not required_scopes.issubset(actual_scopes):
                raise ValueError('node position does not cover source definition')
        if json.loads(value.serialized)[0] != self.identity:
            raise ValueError('definition belongs to another transport root')
        with self._transaction() as root:
            self._schema(root)
            row = self._registered_store(root, str(store.path))
            with owner._writer(store) as conn:
                self._check_store(conn, store, *row)
                scopes.register_in_transaction(conn, store, value)
            self._require_no_pending(root, value)
            if (node_position is None
                    and root.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() in ((5,), (6,), (7,), (8,))):
                raise owner.SourceChanged('node_publication_required')
            if node_position is None:
                yield
            else:
                registration = self._registered_node(root, node_position.database_path)
                with NodeStore.coordinator_cut(registration, node_position) as node_conn:
                    NodeStore.coordinator_require_fences(
                        node_conn, coordinator_epoch=self.epoch,
                        tokens=self._pending_tokens(root),
                    )
                    yield

    def _require_no_pending(self, root, value):
        if root.execute("SELECT 1 FROM mutation_scopes WHERE typeof(kind)!='text' OR length(CAST(kind AS BLOB))>16 OR typeof(value)!='text' OR length(CAST(value AS BLOB))>4096 LIMIT 1").fetchone():
            raise owner.SourceChanged('invalid_pending_selector')
        pending = root.execute('SELECT kind,value FROM mutation_scopes LIMIT ?', (MAX_PENDING * 8 + 1,)).fetchall()
        if len(pending) > MAX_PENDING * 8:
            raise owner.SourceChanged('pending_selector_budget')
        for raw in pending:
            change = scopes.selectors((scopes.Selector(*raw),), limit=1)[0]
            if any(_overlap(change, selected) for selected in value.selectors):
                raise owner.SourceChanged('source_mutation_pending')

    @contextmanager
    def finalization_cut(self, store, value, *, companions=(), mutation=None,
                         node_position=None):
        """Internal root -> Store cut for bounded canonical final comparisons.

        Requires existing coverage and ready inputs; never registers, repairs or
        ingests. Enter epoch/identity/pin scopes before this context. No provider,
        crypto, or application callbacks belong inside it. The caller may persist
        a verified retained lifecycle head, then must recheck its other captured
        inputs. We independently recheck source readiness/coverage before commit.
        Root exclusion lasts through the Store commit, including exception paths.
        """
        from .mutation_reservation import FinalizationMutation
        from ..node.admission import NodeFinalizationPosition
        from ..node.store import NodeStore

        if mutation is not None:
            if type(mutation) is not FinalizationMutation:
                raise ValueError('invalid finalization mutation')
            mutation._enter(self)
        if node_position is not None and type(node_position) is not NodeFinalizationPosition:
            raise ValueError('invalid node finalization position')
        intent = None
        store = SimpleNamespace(path=Path(store.path).resolve())
        value = scopes._definition(value)
        if type(companions) is not tuple or len(companions) > 4:
            raise ValueError('invalid companion source selection')
        definitions = (value,) + tuple(scopes._definition(item) for item in companions)
        if len({item.source for item in definitions}) != len(definitions):
            raise ValueError('duplicate companion source')
        if any(json.loads(item.serialized)[0] != self.identity for item in definitions):
            raise ValueError('definition belongs to another transport root')
        with self._transaction() as root:
            with span('root_checks'):
                self._schema(root)
                row = self._registered_store(root, str(store.path))
                for definition in definitions:
                    self._require_no_pending(root, definition)
            with ExitStack() as stack:
                node_conn = None
                node_path = None
                if node_position is not None:
                    node_path = node_position.database_path
                    registration = self._registered_node(root, node_path)
                    node_conn = stack.enter_context(NodeStore.coordinator_cut(
                        registration, node_position,
                    ))
                    NodeStore.coordinator_require_fences(
                        node_conn, coordinator_epoch=self.epoch,
                        tokens=self._pending_tokens(root),
                    )
                with owner._writer(store) as conn:
                    with span('source_checks'):
                        self._check_store(conn, store, *row)
                        positions = tuple(scopes.require_registered_in_transaction(conn, store, item)
                                          for item in definitions)
                        if any(not item.ready or item.writes_pending for item in positions):
                            raise owner.SourceChanged('source_not_ready')
                    yield conn, positions[0]
                    with span('source_rechecks'):
                        self._check_store(conn, store, *row)
                        if tuple(scopes.require_registered_in_transaction(conn, store, item)
                                 for item in definitions) != positions:
                            raise owner.SourceChanged('source_changed_during_finalization')
                    if mutation is not None and mutation.accepted:
                        changes = scopes.selectors(mutation.changes, limit=8)
                        token = self._insert_intent(root, changes)
                        scopes.retire_in_transaction(conn, store, changes)
                        if node_conn is not None:
                            NodeStore.coordinator_add_fence(
                                node_conn, coordinator_epoch=self.epoch,
                                token=token,
                            )
                        intent = MutationIntent(str(self.path), self.epoch, token, changes)
            if intent is not None:
                # The current writer committed before opening other Store writers.
                # Root exclusion persists throughout fanout and the root commit.
                self._retire_nodes(root, intent.token, skip_path=node_path)
                self._retire(root, intent.selectors, skip_path=str(store.path))
                self._advance_root_revision(root)
        if intent is not None:
            mutation._committed(intent)
