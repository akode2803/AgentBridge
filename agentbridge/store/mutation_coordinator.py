"""Local root-wide mutation ordering across GUI/harness Store databases.

Inactive: transport interception, page publication gates and lifecycle integration
must be installed together. This coordinator never executes or retries a remote
operation and never clears an ambiguous intent based on age or process death.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import local_source as owner, source_selectors as scopes

MAX_STORES = 64
MAX_PENDING = 64
MAX_QUARANTINED = 256
_NORMAL_TOKEN = re.compile(r'[0-9a-f]{64}\Z')
_SENTINEL_TOKEN = re.compile(r'q[0-9a-f]{63}\Z')
_SCHEMA = {
    'mutation_root': 'CREATE TABLE mutation_root(singleton INTEGER PRIMARY KEY CHECK(singleton=1),identity TEXT NOT NULL,epoch TEXT NOT NULL)',
    'mutation_stores': 'CREATE TABLE mutation_stores(path TEXT PRIMARY KEY,incarnation TEXT NOT NULL,source_epoch TEXT NOT NULL)',
    'mutation_intents': 'CREATE TABLE mutation_intents(token TEXT PRIMARY KEY)',
    'mutation_scopes': 'CREATE TABLE mutation_scopes(token TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(token,kind,value))',
}
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
            quarantine = [conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                          for name in _QUARANTINE_SCHEMA]
            if all(value is None for value in quarantine):
                for statement in _QUARANTINE_SCHEMA.values():
                    conn.execute(statement)
            elif any(value is None for value in quarantine):
                raise owner.SourceChanged('quarantine_schema_incomplete')
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
        tables = tuple(_SCHEMA) + tuple(_QUARANTINE_SCHEMA)
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN ({','.join('?' for _ in tables)}) LIMIT 1", tables).fetchone():
            raise owner.SourceChanged('unexpected_mutation_trigger')
        for name, sql in {**_SCHEMA, **_QUARANTINE_SCHEMA}.items():
            if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
                raise owner.SourceChanged('mutation_schema_changed')
        row = conn.execute('SELECT identity,epoch FROM mutation_root WHERE singleton=1').fetchone()
        if (row is None or row[0] != self.identity or type(row[1]) is not str or len(row[1]) != 32
                or any(c not in '0123456789abcdef' for c in row[1])
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
        if conn.execute("SELECT 1 FROM mutation_scopes WHERE typeof(token)!='text' OR length(CAST(token AS BLOB))>64 OR typeof(kind)!='text' OR length(CAST(kind AS BLOB))>16 OR typeof(value)!='text' OR length(CAST(value AS BLOB))>4096 LIMIT 1").fetchone():
            raise owner.SourceChanged('invalid_pending_selector')
        sentinels = {}
        for (token,) in tokens:
            rows = conn.execute('SELECT kind,value FROM mutation_scopes WHERE token=? ORDER BY kind,value LIMIT 9',
                                (token,)).fetchall()
            if not 1 <= len(rows) <= 8:
                raise owner.SourceChanged('pending_intent_scope_mismatch')
            if _SENTINEL_TOKEN.fullmatch(token):
                sentinels[token] = tuple(rows)
        if conn.execute('SELECT 1 FROM mutation_scopes s LEFT JOIN mutation_intents i ON s.token=i.token WHERE i.token IS NULL LIMIT 1').fetchone():
            raise owner.SourceChanged('orphan_mutation_scope')
        self._quarantine_audit(conn, sentinels)
        return row[1]

    @staticmethod
    def _quarantine_audit(conn, sentinels):
        """Bounded archive audit: an archived token must retain its old fence."""
        if conn.execute('SELECT 1 FROM quarantine_intents LIMIT 1 OFFSET ?',
                        (MAX_QUARANTINED,)).fetchone():
            raise owner.SourceChanged('quarantine_budget')
        if conn.execute('SELECT 1 FROM quarantine_scopes LIMIT 1 OFFSET ?',
                        (MAX_QUARANTINED * 8,)).fetchone():
            raise owner.SourceChanged('quarantine_scope_budget')
        if conn.execute("SELECT 1 FROM quarantine_intents WHERE typeof(token)!='text' OR length(CAST(token AS BLOB))>64 OR typeof(sentinel)!='text' OR length(CAST(sentinel AS BLOB))>64 LIMIT 1").fetchone():
            raise owner.SourceChanged('invalid_quarantine_intent')
        if conn.execute("SELECT 1 FROM quarantine_scopes WHERE typeof(token)!='text' OR length(CAST(token AS BLOB))>64 OR typeof(kind)!='text' OR length(CAST(kind AS BLOB))>16 OR typeof(value)!='text' OR length(CAST(value AS BLOB))>4096 LIMIT 1").fetchone():
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
            self._retire(conn, changes)
            result = MutationIntent(str(self.path), self.epoch, token, changes)
        return result

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
                issued.append((sentinel, rows, len(members)))
            self._schema(conn)  # Validate the replacement fence before commit.
            return tuple(issued)

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
        if _SENTINEL_TOKEN.fullmatch(token):
            raise ValueError('quarantine fence cannot complete')
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
