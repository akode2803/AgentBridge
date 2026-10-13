"""Inactive durable typed mutation recovery, owned by one local replica.

No network, timers, eviction, or readiness transitions live here. Request and
intent publication share the coordinator transaction; node/Store commits retain
the existing conservative failure behavior when that transaction rolls back.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from ..node.admission import NodeCoordinatorRegistration
from ..node.store import NodeStore
from ..transport.mutation_protocol import _uuid
from ..transport.mutation_receipts import ProviderMutationReceipt, decode_mutation_receipt
from ..transport.typed_mutations import PreparedMutation
from . import local_source, source_selectors
from .mutation_coordinator import MutationCoordinator, MutationIntent, MAX_PENDING, ROOT_LOCK_TIMEOUT_S, _V5_ROOT_SCHEMA, _overlap

MAX_RECORDS = 4096
MAX_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 8_388_608
MAX_RECEIPT_BYTES = 32_768
_ACTIVE = frozenset(('prepared', 'attempted', 'accepted'))
_STATES = _ACTIVE | {'completed', 'acknowledged'}
_SCHEMA = {
    'typed_operation_owner': 'CREATE TABLE typed_operation_owner(singleton INTEGER PRIMARY KEY CHECK(singleton=1),endpoint TEXT NOT NULL,root TEXT NOT NULL,actor_id TEXT NOT NULL,node_path TEXT NOT NULL,node_incarnation TEXT NOT NULL,node_identity_digest TEXT NOT NULL,coordinator_epoch TEXT NOT NULL)',
    'typed_operations': 'CREATE TABLE typed_operations(protocol_epoch TEXT NOT NULL,operation_id TEXT NOT NULL,token TEXT NOT NULL UNIQUE,state TEXT NOT NULL,request BLOB NOT NULL,receipt BLOB,selectors BLOB NOT NULL,PRIMARY KEY(protocol_epoch,operation_id))',
}
_COLUMNS = 'protocol_epoch,operation_id,token,state,request,receipt,selectors'


def _fail(reason):
    raise local_source.SourceChanged('typed_operation_' + reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def mutation_selectors(request: PreparedMutation):
    """Derive complete local invalidation from the validated replay request."""
    if type(request) is not PreparedMutation:
        raise ValueError('invalid prepared mutation')
    request.__post_init__()
    kind = request.expectation.kind
    args = json.loads(request.arguments_json)
    S = source_selectors.Selector
    if kind.startswith('doc_'):
        changes = (S('doc_exact', args['p_path']),)
    elif kind == 'log_append':
        changes = (S('log_chat', args['p_chat']),)
    elif kind == 'effect_transition':
        path = args['p_path']
        changes = [S('doc_exact', path)]
        if path.endswith('/claim.json'):
            parent = path.rsplit('/', 1)[0]
            changes.extend((S('doc_exact', parent + '/grant-ask.json'),
                            S('doc_exact', parent + '/grant-decision.json')))
    elif kind == 'chat_delete':
        changes = (S('doc_prefix', 'chats/' + args['p_chat']),
                   S('log_chat', args['p_chat']))
    elif kind == 'blob_manifest':
        # A manifest can change attachment availability throughout the room.
        changes = (S('doc_prefix', 'chats/' + args['p_chat']),
                   S('log_chat', args['p_chat']))
    elif kind in {'member_claim', 'member_leave'}:
        changes = (S('root_all', '*'),)
    else:
        raise ValueError('unsupported mutation family')
    return source_selectors.selectors(tuple(changes), limit=8)


def _receipt_bytes(receipt):
    if type(receipt) is not ProviderMutationReceipt:
        raise ValueError('invalid typed operation receipt')
    receipt.__post_init__()
    identity = receipt.identity
    raw = {name: getattr(identity, name) for name in (
        'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind',
        'client_digest', 'provider_digest')}
    raw.update(schema_version=2, result=dict(receipt.result))
    encoded = _canonical(raw)
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValueError('typed operation receipt exceeds limit')
    return encoded


@dataclass(frozen=True)
class OperationRecord:
    request: PreparedMutation = field(repr=False)
    intent: MutationIntent = field(repr=False)
    state: str
    receipt: ProviderMutationReceipt | None = field(default=None, repr=False)


def _tables_present(conn):
    rows = tuple(conn.execute("SELECT name FROM sqlite_master WHERE name IN (?,?)", tuple(_SCHEMA)))
    return len(rows)


def _owner(conn):
    # Preflight every untrusted column before materializing its contents.
    names = ('endpoint', 'root', 'actor_id', 'node_path', 'node_incarnation',
             'node_identity_digest', 'coordinator_epoch')
    sizes = (2048, 1024, 36, 16384, 4096, 64, 32)
    bad = ' OR '.join(f"typeof({name})!='text' OR length(CAST({name} AS BLOB))>{size}"
                      for name, size in zip(names, sizes))
    if conn.execute(f'SELECT 1 FROM typed_operation_owner WHERE {bad} LIMIT 1').fetchone():
        _fail('owner_invalid')
    rows = conn.execute('SELECT ' + ','.join(names) + ' FROM typed_operation_owner LIMIT 2').fetchall()
    if len(rows) != 1:
        _fail('owner_missing')
    endpoint, root, actor, path, incarnation, digest, epoch = rows[0]
    try:
        _uuid(actor, 'typed operation actor')
    except ValueError:
        _fail('owner_invalid')
    if (not root or not endpoint or not incarnation
            or re.fullmatch('[0-9a-f]{64}', digest) is None
            or re.fullmatch('[0-9a-f]{32}', epoch) is None):
        _fail('owner_invalid')
    return rows[0]


def _record(row, coordinator, owner):
    epoch, operation, token, state, raw, receipt_raw, selector_raw = row
    try:
        _uuid(epoch, 'typed operation epoch')
        _uuid(operation, 'typed operation id')
        if re.fullmatch('[0-9a-f]{64}', token) is None or state not in _STATES:
            raise ValueError('invalid operation state')
        request = PreparedMutation.from_request_json(raw)
        expected = request.expectation
        if (expected.protocol_epoch, expected.operation_id, expected.root,
                expected.actor_id) != (epoch, operation, owner[1], owner[2]):
            raise ValueError('request identity mismatch')
        changes = mutation_selectors(request)
        if selector_raw != _canonical([(item.kind, item.value) for item in changes]):
            raise ValueError('request selector mismatch')
        receipt = None
        if receipt_raw is not None:
            payload = json.loads(receipt_raw)
            receipt = decode_mutation_receipt(payload, expected)
            if receipt_raw != _receipt_bytes(receipt):
                raise ValueError('noncanonical receipt')
        if (receipt is not None) != (state in {'accepted', 'completed', 'acknowledged'}):
            raise ValueError('receipt state mismatch')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail('record_invalid')
    return OperationRecord(request, MutationIntent(str(coordinator.path), owner[6], token, changes), state, receipt)


def audit_journal(conn, coordinator, *, required):
    """Coordinator guard, including on generic completion/recovery paths."""
    present = _tables_present(conn)
    if not required:
        if present:
            _fail('unexpected_schema')
        return
    if present != len(_SCHEMA):
        _fail('schema_missing')
    for name, sql in _SCHEMA.items():
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
            _fail('schema_changed')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?) LIMIT 1", tuple(_SCHEMA)).fetchone():
        _fail('unexpected_trigger')
    owner = _owner(conn)
    epoch = conn.execute('SELECT epoch FROM mutation_root WHERE singleton=1').fetchone()[0]
    registration = coordinator._registered_node(conn, owner[3])
    if (owner[6] != epoch or registration != NodeCoordinatorRegistration(
            owner[3], owner[4], owner[5], owner[0], 'supabase://' + owner[1])):
        _fail('owner_changed')
    if conn.execute('SELECT 1 FROM typed_operations LIMIT 1 OFFSET ?', (MAX_RECORDS,)).fetchone():
        _fail('record_budget')
    if conn.execute("SELECT 1 FROM typed_operations WHERE typeof(protocol_epoch)!='text' OR length(protocol_epoch)!=36 OR typeof(operation_id)!='text' OR length(operation_id)!=36 OR typeof(token)!='text' OR length(token)!=64 OR typeof(state)!='text' OR length(state)>16 OR typeof(selectors)!='blob' OR length(selectors)>32768 OR typeof(request)!='blob' OR length(request)>? OR (receipt IS NOT NULL AND (typeof(receipt)!='blob' OR length(receipt)>?)) LIMIT 1", (MAX_REQUEST_BYTES, MAX_RECEIPT_BYTES)).fetchone():
        _fail('record_invalid')
    if conn.execute('SELECT 1 FROM typed_operations GROUP BY operation_id HAVING count(*)>1 LIMIT 1').fetchone():
        _fail('duplicate_operation_id')
    if _bytes_used(conn) > MAX_BYTES:
        _fail('byte_budget')
    # Publication checks never deserialize replay payloads. Only at most 64
    # active selector sets are decoded, and exact requests/receipts are checked
    # when a journal operation targets their immutable epoch/operation key.
    pending_tokens = {row[0] for row in conn.execute('SELECT token FROM mutation_intents')}
    pending_scopes = {}
    for token, kind, value in conn.execute('SELECT token,kind,value FROM mutation_scopes ORDER BY token,kind,value'):
        pending_scopes.setdefault(token, []).append(source_selectors.Selector(kind, value))
    if conn.execute(
            "SELECT 1 FROM typed_operations WHERE state IN ('prepared','attempted','accepted') LIMIT 1 OFFSET ?",
            (MAX_PENDING,)).fetchone():
        _fail('active_budget')
    active_selectors = dict(conn.execute(
        "SELECT token,selectors FROM typed_operations WHERE state IN ('prepared','attempted','accepted')"))
    for epoch, operation, token, state, has_receipt in conn.execute(
            'SELECT protocol_epoch,operation_id,token,state,receipt IS NOT NULL FROM typed_operations'):
        try:
            _uuid(epoch, 'typed operation epoch')
            _uuid(operation, 'typed operation id')
        except ValueError:
            _fail('record_invalid')
        if (re.fullmatch('[0-9a-f]{64}', token) is None or state not in _STATES
                or bool(has_receipt) != (state in {'accepted', 'completed', 'acknowledged'})):
            _fail('record_invalid')
        pending = token in pending_tokens
        if state in _ACTIVE:
            if not pending:
                _fail('intent_changed')
            encoded = active_selectors[token]
            try:
                decoded = json.loads(encoded)
                changes = source_selectors.selectors(
                    tuple(source_selectors.Selector(*item) for item in decoded), limit=8)
                if encoded != _canonical([(item.kind, item.value) for item in changes]):
                    raise ValueError('noncanonical selectors')
            except (TypeError, ValueError, RecursionError):
                _fail('selectors_invalid')
            found = tuple(pending_scopes.get(token, ()))
            if found != changes:
                _fail('intent_changed')
        elif pending:
            _fail('terminal_intent_present')


def _bytes_used(conn):
    return conn.execute('SELECT coalesce(sum(length(request)+coalesce(length(receipt),0)),0) FROM typed_operations').fetchone()[0]


def require_legacy_intent(conn, token):
    if _tables_present(conn) and conn.execute('SELECT 1 FROM typed_operations WHERE token=?', (token,)).fetchone():
        _fail('receipt_required')


def require_completion_proof(conn, token, proof):
    if not _tables_present(conn):
        if proof is not None:
            _fail('unknown_completion')
        return
    row = conn.execute('SELECT state,receipt FROM typed_operations WHERE token=?', (token,)).fetchone()
    if row is None:
        if proof is not None:
            _fail('unknown_completion')
        return
    if row[0] != 'accepted' or proof is None or _receipt_bytes(proof) != row[1]:
        _fail('receipt_required')


class OperationJournal:
    """Owner-bound durable state; all methods perform local work only."""

    def __init__(self, coordinator: MutationCoordinator, node: NodeStore, *, actor_id: str):
        if type(coordinator) is not MutationCoordinator or type(node) is not NodeStore:
            raise TypeError('invalid typed operation owner')
        _uuid(actor_id, 'typed operation actor')
        if node.identity.principal != actor_id:
            raise ValueError('typed operation actor does not match node principal')
        self.coordinator = coordinator
        self.node = node
        registration = node.coordinator_registration()
        if not registration.root.startswith('supabase://'):
            raise ValueError('invalid typed operation root')
        self._owner = (registration.provider_endpoint, registration.root[len('supabase://'):],
                       actor_id, registration.database_path,
                       registration.database_incarnation, registration.identity_digest, coordinator.epoch)
        with coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            coordinator._schema(conn)
            if coordinator._registered_node(conn, registration.database_path) != registration:
                _fail('owner_changed')
            with NodeStore.coordinator_cut(registration) as node_conn:
                node._check_identity(node_conn)
            if not _tables_present(conn):
                for sql in _SCHEMA.values():
                    conn.execute(sql)
                conn.execute('INSERT INTO typed_operation_owner VALUES(1,?,?,?,?,?,?,?)', self._owner)
                coordinator._advance_root_revision(conn)
            if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() != (5,):
                # Once legacy admission is disabled, every remaining ready cut
                # must have originated in verified node publication. Retire all
                # old cuts before committing that durable capability boundary.
                coordinator._retire(conn, (source_selectors.Selector('root_all', '*'),))
                coordinator._advance_root_revision(conn)
                conn.execute('ALTER TABLE mutation_root RENAME TO mutation_root_previous')
                conn.execute(_V5_ROOT_SCHEMA)
                conn.execute('INSERT INTO mutation_root SELECT singleton,identity,epoch,5,revision FROM mutation_root_previous')
                conn.execute('DROP TABLE mutation_root_previous')
            self._check(conn)

    @property
    def endpoint(self):
        return self._owner[0]

    @property
    def root(self):
        return self._owner[1]

    @property
    def actor_id(self):
        return self._owner[2]

    def _check(self, conn):
        self.coordinator._schema(conn)
        if _owner(conn) != self._owner:
            _fail('owner_changed')
        registration = self.coordinator._registered_node(conn, self._owner[3])
        with NodeStore.coordinator_cut(registration) as node_conn:
            self.node._check_identity(node_conn)
            if self.node.identity.principal != self.actor_id:
                _fail('owner_changed')

    def _find(self, conn, epoch, operation):
        row = conn.execute('SELECT ' + _COLUMNS + ' FROM typed_operations WHERE protocol_epoch=? AND operation_id=?', (epoch, operation)).fetchone()
        return None if row is None else _record(row, self.coordinator, self._owner)

    def _exact(self, conn, request):
        if type(request) is not PreparedMutation:
            raise ValueError('invalid prepared mutation')
        request.__post_init__()
        expected = request.expectation
        if (expected.root, expected.actor_id) != (self.root, self.actor_id):
            _fail('request_owner_changed')
        record = self._find(conn, expected.protocol_epoch, expected.operation_id)
        if record is not None and record.request.request_json != request.request_json:
            _fail('request_changed')
        return record

    def _find_operation(self, conn, operation_id):
        rows = conn.execute('SELECT protocol_epoch FROM typed_operations WHERE operation_id=? LIMIT 2',
                            (operation_id,)).fetchall()
        if len(rows) > 1:
            _fail('duplicate_operation_id')
        return None if not rows else self._find(conn, rows[0][0], operation_id)

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        """Find a durable application key before acquiring new-send authority."""
        _uuid(operation_id, 'typed operation id')
        with self.coordinator._transaction() as conn:
            self._check(conn)
            return self._find_operation(conn, operation_id)

    def prepare(self, request: PreparedMutation) -> OperationRecord:
        """Strict replay requires the original epoch and operation identity."""
        return self._prepare(request, application=False)

    def prepare_operation(self, request: PreparedMutation) -> OperationRecord:
        """Bind an explicit application key once, retaining its original epoch.

        New application keys cannot overtake any overlapping unresolved intent.
        Existing exact keys may resume even after the provider epoch changes.
        """
        return self._prepare(request, application=True)

    def _prepare(self, request: PreparedMutation, *, application: bool) -> OperationRecord:
        changes = mutation_selectors(request)
        raw = request.request_json
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('typed operation request exceeds limit')
        root = self.coordinator
        with root._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            self._exact(conn, request)  # Validate the caller's bound root/actor.
            existing = self._find_operation(conn, request.expectation.operation_id)
            if existing is not None:
                if application:
                    request = PreparedMutation(
                        replace(request.expectation, protocol_epoch=existing.request.expectation.protocol_epoch),
                        request.arguments_json)
                if existing.request.request_json != request.request_json:
                    _fail('request_changed')
                return existing
            if application:
                pending = conn.execute('SELECT kind,value FROM mutation_scopes LIMIT ?', (MAX_PENDING * 8 + 1,))
                for kind, value in pending:
                    if any(_overlap(source_selectors.Selector(kind, value), change) for change in changes):
                        _fail('overlapping_intent')
            if conn.execute('SELECT count(*) FROM typed_operations').fetchone()[0] >= MAX_RECORDS:
                _fail('record_budget')
            # Reserve one maximum-sized receipt per new operation, so a full
            # request budget can never prevent retaining a successful outcome.
            reserved = conn.execute('SELECT count(*) FROM typed_operations WHERE receipt IS NULL').fetchone()[0]
            if _bytes_used(conn) + len(raw) + (reserved + 1) * MAX_RECEIPT_BYTES > MAX_BYTES:
                _fail('byte_budget')
            token = root._insert_intent(conn, changes)
            expected = request.expectation
            conn.execute('INSERT INTO typed_operations VALUES(?,?,?,?,?,NULL,?)',
                         (expected.protocol_epoch, expected.operation_id, token, 'prepared', raw,
                          _canonical([(item.kind, item.value) for item in changes])))
            root._retire_nodes(conn, token)
            root._retire(conn, changes)
            root._advance_root_revision(conn)
            result = self._find(conn, expected.protocol_epoch, expected.operation_id)
        return result

    def get(self, protocol_epoch: str, operation_id: str) -> OperationRecord:
        _uuid(protocol_epoch, 'typed operation epoch')
        _uuid(operation_id, 'typed operation id')
        with self.coordinator._transaction() as conn:
            self._check(conn)
            result = self._find(conn, protocol_epoch, operation_id)
            if result is None:
                _fail('missing')
            return result

    def pending_keys(self, *, limit=64, after=None):
        """Bounded restart discovery, without materializing replay payloads."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('invalid typed operation inventory limit')
        if after is not None:
            if type(after) is not tuple or len(after) != 2:
                raise ValueError('invalid typed operation inventory cursor')
            for value in after:
                _uuid(value, 'typed operation inventory cursor')
        with self.coordinator._transaction() as conn:
            self._check(conn)
            cursor_sql = '' if after is None else ' AND (protocol_epoch,operation_id)>(?,?)'
            arguments = (limit,) if after is None else (*after, limit)
            return tuple(conn.execute(
                "SELECT protocol_epoch,operation_id FROM typed_operations WHERE state!='acknowledged'"
                + cursor_sql + ' ORDER BY protocol_epoch,operation_id LIMIT ?', arguments))

    def _transition(self, request, action, receipt=None):
        root = self.coordinator
        with root._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            record = self._exact(conn, request)
            if record is None:
                _fail('missing')
            state = record.state
            payload = None
            if action == 'attempted':
                if state == 'prepared':
                    state = 'attempted'
            elif action == 'accepted':
                payload = _receipt_bytes(receipt)
                # Exact decode binds every identity field and target before
                # the first provider digest becomes durable.
                try:
                    expected = record.receipt.identity if record.receipt else request.expectation
                    decode_mutation_receipt(json.loads(payload), expected)
                except (TypeError, ValueError):
                    _fail('receipt_changed')
                if record.receipt is not None and payload != _receipt_bytes(record.receipt):
                    _fail('receipt_changed')
                if state == 'prepared':
                    _fail('not_attempted')
                if state == 'attempted':
                    if _bytes_used(conn) + len(payload) > MAX_BYTES:
                        _fail('byte_budget')
                    state = 'accepted'
            elif action == 'completed':
                if state in {'prepared', 'attempted'}:
                    _fail('receipt_required')
                if state == 'accepted':
                    root._finish_in_transaction(conn, record.intent, typed_receipt=record.receipt)
                    state = 'completed'
            elif action == 'acknowledged':
                if state not in {'completed', 'acknowledged'}:
                    _fail('not_completed')
                state = 'acknowledged'
            else:
                raise ValueError('invalid typed operation action')
            conn.execute('UPDATE typed_operations SET state=?,receipt=coalesce(receipt,?) WHERE token=?',
                         (state, payload, record.intent.token))
            if state != record.state:
                root._advance_root_revision(conn)
            result = self._find(conn, request.expectation.protocol_epoch, request.expectation.operation_id)
        return result

    def mark_attempted(self, request):
        """Commit before any provider I/O; exceptions leave replay evidence."""
        return self._transition(request, 'attempted')

    def accept(self, request, receipt):
        """Retain the verified exact outcome before any completion or ACK."""
        return self._transition(request, 'accepted', receipt)

    def complete(self, request):
        """Atomically remove the intent and record local completion."""
        return self._transition(request, 'completed')

    def mark_acknowledged(self, request):
        """Call only after the provider confirms this exact receipt's ACK."""
        return self._transition(request, 'acknowledged')
