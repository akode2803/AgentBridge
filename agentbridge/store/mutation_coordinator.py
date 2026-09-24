"""Local root-wide mutation ordering across GUI/harness Store databases.

Inactive: transport interception, page publication gates and lifecycle integration
must be installed together. This coordinator never executes or retries a remote
operation and never clears an ambiguous intent based on age or process death.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import local_source as owner, source_selectors as scopes

MAX_STORES = 64
MAX_PENDING = 64
_SCHEMA = {
    'mutation_root': 'CREATE TABLE mutation_root(singleton INTEGER PRIMARY KEY CHECK(singleton=1),identity TEXT NOT NULL,epoch TEXT NOT NULL)',
    'mutation_stores': 'CREATE TABLE mutation_stores(path TEXT PRIMARY KEY,incarnation TEXT NOT NULL,source_epoch TEXT NOT NULL)',
    'mutation_intents': 'CREATE TABLE mutation_intents(token TEXT PRIMARY KEY)',
    'mutation_scopes': 'CREATE TABLE mutation_scopes(token TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(token,kind,value))',
}


@dataclass(frozen=True)
class MutationIntent:
    coordinator_path: str
    epoch: str
    token: str
    selectors: tuple[scopes.Selector, ...]


def _overlap(left, right):
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
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / (hashlib.sha256(root_identity.encode()).hexdigest() + '.sqlite')
        # SQLite serializes concurrent initialization, including empty-file races.
        with self._transaction(create=True) as conn:
            found = conn.execute("SELECT name FROM sqlite_master WHERE name GLOB 'mutation_*'").fetchall()
            if not found:
                for statement in _SCHEMA.values():
                    conn.execute(statement)
                conn.execute('INSERT INTO mutation_root VALUES(1,?,?)', (self.identity, secrets.token_hex(16)))
            self.epoch = self._schema(conn)
        self.path.chmod(0o600)

    @contextmanager
    def _transaction(self, *, create=False):
        mode = 'rwc' if create else 'rw'
        conn = sqlite3.connect(f'{self.path.as_uri()}?mode={mode}', uri=True, timeout=1)
        try:
            if create:
                conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _schema(self, conn):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?,?,?) LIMIT 1", tuple(_SCHEMA)).fetchone():
            raise owner.SourceChanged('unexpected_mutation_trigger')
        for name, sql in _SCHEMA.items():
            if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
                raise owner.SourceChanged('mutation_schema_changed')
        row = conn.execute('SELECT identity,epoch FROM mutation_root WHERE singleton=1').fetchone()
        if (row is None or row[0] != self.identity or type(row[1]) is not str or len(row[1]) != 32
                or any(c not in '0123456789abcdef' for c in row[1])
                or (hasattr(self, 'epoch') and row[1] != self.epoch)):
            raise owner.SourceChanged('mutation_root_changed')
        tokens = conn.execute('SELECT token FROM mutation_intents LIMIT ?', (MAX_PENDING + 1,)).fetchall()
        if len(tokens) > MAX_PENDING or any(type(t[0]) is not str or len(t[0]) != 64 for t in tokens):
            raise owner.SourceChanged('invalid_pending_intents')
        for (token,) in tokens:
            count = conn.execute('SELECT count(*) FROM (SELECT 1 FROM mutation_scopes WHERE token=? LIMIT 9)', (token,)).fetchone()[0]
            if not 1 <= count <= 8:
                raise owner.SourceChanged('pending_intent_scope_mismatch')
        if conn.execute('SELECT 1 FROM mutation_scopes s LEFT JOIN mutation_intents i ON s.token=i.token WHERE i.token IS NULL LIMIT 1').fetchone():
            raise owner.SourceChanged('orphan_mutation_scope')
        return row[1]

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
                invalid = conn.execute('SELECT 1 FROM local_sources WHERE typeof(revision)!=\'integer\' OR revision>=? OR revision<1 LIMIT 1', (owner.MAX,)).fetchone()
                if invalid:
                    raise owner.SourceChanged('registration_revision_unavailable')
                conn.execute('UPDATE local_sources SET revision=revision+1,ready_incarnation=NULL,ready_generation=NULL,ready_cursor=NULL')
            root.execute('INSERT INTO mutation_stores VALUES(?,?,?)', (path, current.raw.incarnation, current.epoch))

    def _retire(self, root, changes):
        for path, incarnation, epoch in self._stores(root):
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
        with self._transaction() as conn:
            self._schema(conn)
            count = conn.execute('SELECT count(*) FROM (SELECT 1 FROM mutation_intents LIMIT ?)', (MAX_PENDING,)).fetchone()[0]
            if count >= MAX_PENDING:
                raise owner.SourceChanged('pending_mutation_budget')
            token = secrets.token_hex(32)
            conn.execute('INSERT INTO mutation_intents VALUES(?)', (token,))
            conn.executemany('INSERT INTO mutation_scopes VALUES(?,?,?)', ((token, s.kind, s.value) for s in changes))
            self._retire(conn, changes)
            result = MutationIntent(str(self.path), self.epoch, token, changes)
        return result

    def complete(self, value):
        """Only after this exact external call definitively returns success.

        Not a recovery API. Other pending intents, including earlier ambiguous
        retries of the same write, remain pending. Never restores readiness.
        """
        if type(value) is not MutationIntent:
            raise ValueError('invalid mutation intent')
        path, epoch, token = value.coordinator_path, value.epoch, value.token
        changes = scopes.selectors(value.selectors, limit=8)
        if path != str(self.path) or epoch != self.epoch or type(token) is not str or len(token) != 64:
            raise ValueError('foreign mutation intent')
        with self._transaction() as conn:
            self._schema(conn)
            pending = conn.execute('SELECT kind,value FROM mutation_scopes WHERE token=? ORDER BY kind,value LIMIT 9', (token,)).fetchall()
            if tuple(scopes.Selector(*row) for row in pending) != changes:
                raise owner.SourceChanged('mutation_intent_changed')
            self._retire(conn, changes)
            if conn.execute('DELETE FROM mutation_intents WHERE token=?', (token,)).rowcount != 1:
                raise owner.SourceChanged('mutation_intent_missing')
            conn.execute('DELETE FROM mutation_scopes WHERE token=?', (token,))

    @contextmanager
    def publication_gate(self, store, value):
        """Serialize registration/admission with mutation start/finish.

        Only already-collected bounded LOCAL work may execute in this scope.
        Transport reads, callbacks, signature work and scans belong outside it.
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
                scopes.register_in_transaction(conn, store, value)
            self._require_no_pending(root, value)
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
    def finalization_cut(self, store, value, *, companions=()):
        """Internal root -> Store cut for bounded canonical final comparisons.

        Requires existing coverage and ready inputs; never registers, repairs or
        ingests. Enter epoch/identity/pin scopes before this context. No provider,
        crypto, or application callbacks belong inside it. The caller may persist
        a verified retained lifecycle head, then must recheck its other captured
        inputs. We independently recheck source readiness/coverage before commit.
        Root exclusion lasts through the Store commit, including exception paths.
        """
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
            self._schema(root)
            row = self._registered_store(root, str(store.path))
            for definition in definitions:
                self._require_no_pending(root, definition)
            with owner._writer(store) as conn:
                self._check_store(conn, store, *row)
                positions = tuple(scopes.require_registered_in_transaction(conn, store, item)
                                  for item in definitions)
                if any(not item.ready or item.writes_pending for item in positions):
                    raise owner.SourceChanged('source_not_ready')
                yield conn, positions[0]
                self._check_store(conn, store, *row)
                if tuple(scopes.require_registered_in_transaction(conn, store, item)
                         for item in definitions) != positions:
                    raise owner.SourceChanged('source_changed_during_finalization')
