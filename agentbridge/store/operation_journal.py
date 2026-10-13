"""Inactive durable typed mutation recovery, owned by one local replica.

No network, timers, eviction, or readiness transitions live here. Request and
intent publication share the coordinator transaction; node/Store commits retain
the existing conservative failure behavior when that transaction rolls back.
"""
from __future__ import annotations

import json
import re
from uuid import uuid4
from contextlib import ExitStack
from dataclasses import dataclass, field, replace

from ..node.admission import NodeCoordinatorRegistration
from ..node.store import NodeStore
from ..transport.mutation_protocol import _uuid
from ..transport.mutation_receipts import ProviderMutationReceipt, decode_mutation_receipt
from ..transport.typed_mutations import PreparedMutation, prepare_mutation
from ..transport.receipt_retirement import OperationTicket, ProviderEpoch
from . import local_source, source_selectors
from .mutation_coordinator import MutationCoordinator, MutationIntent, MAX_PENDING, ROOT_LOCK_TIMEOUT_S, _V5_ROOT_SCHEMA, _V6_ROOT_SCHEMA, _V8_ROOT_SCHEMA, _overlap

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
_LIFECYCLE_SCHEMA = {
    'typed_operation_lifecycle': 'CREATE TABLE typed_operation_lifecycle(singleton INTEGER PRIMARY KEY CHECK(singleton=1),lifecycle_id TEXT NOT NULL,protocol_epoch TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>=0))',
    'typed_operation_tickets': 'CREATE TABLE typed_operation_tickets(protocol_epoch TEXT NOT NULL,operation_id TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>=0),released INTEGER NOT NULL CHECK(released IN (0,1)),PRIMARY KEY(protocol_epoch,operation_id))',
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
    elif kind == 'reaction_pair':
        prefix = 'chats/' + args['p_chat'] + '/overlays/'
        changes = (S('doc_exact', prefix + 'state/' + args['p_user'] + '.json'),
                   S('doc_exact', prefix + 'reactions/' + args['p_user'] + '.json'))
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


def _lifecycle(conn):
    if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() not in ((6,), (7,), (8,), (9,)):
        return None
    return conn.execute('SELECT lifecycle_id,protocol_epoch,generation FROM typed_operation_lifecycle WHERE singleton=1').fetchone()


def _unfinished_sql(conn):
    if _lifecycle(conn) is None:
        return "state!='acknowledged'"
    return ("(state!='acknowledged' OR EXISTS (SELECT 1 FROM typed_operation_tickets t "
            'WHERE t.protocol_epoch=typed_operations.protocol_epoch '
            'AND t.operation_id=typed_operations.operation_id AND t.released=0))')


def _audit_lifecycle(conn):
    present = tuple(conn.execute('SELECT name FROM sqlite_master WHERE name IN (?,?)', tuple(_LIFECYCLE_SCHEMA)))
    required = conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() in ((6,), (7,), (8,), (9,))
    if not required:
        if present:
            _fail('unexpected_lifecycle')
        return
    for name, sql in _LIFECYCLE_SCHEMA.items():
        if conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone() != (sql,):
            _fail('lifecycle_schema')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?) LIMIT 1", tuple(_LIFECYCLE_SCHEMA)).fetchone():
        _fail('lifecycle_trigger')
    if conn.execute("SELECT 1 FROM typed_operation_lifecycle WHERE typeof(lifecycle_id)!='text' OR length(lifecycle_id)!=36 OR typeof(protocol_epoch)!='text' OR length(protocol_epoch)!=36 OR typeof(generation)!='integer' OR generation<0 LIMIT 1").fetchone():
        _fail('lifecycle_invalid')
    rows = conn.execute('SELECT lifecycle_id,protocol_epoch,generation FROM typed_operation_lifecycle LIMIT 2').fetchall()
    if len(rows) != 1:
        _fail('lifecycle_missing')
    try:
        _uuid(rows[0][0], 'provider lifecycle')
        _uuid(rows[0][1], 'provider epoch')
    except ValueError:
        _fail('lifecycle_invalid')
    if conn.execute('SELECT 1 FROM typed_operation_tickets LIMIT 1 OFFSET ?', (MAX_RECORDS,)).fetchone():
        _fail('ticket_budget')
    if conn.execute("SELECT 1 FROM typed_operation_tickets t LEFT JOIN typed_operations o USING(protocol_epoch,operation_id) WHERE o.operation_id IS NULL OR typeof(t.generation)!='integer' OR t.generation<0 OR t.generation>? OR typeof(t.released)!='integer' OR t.released NOT IN (0,1) OR ((t.protocol_epoch=?) != (t.generation=?)) OR (t.generation<? AND o.state!='acknowledged') OR (t.released=1 AND o.state NOT IN ('completed','acknowledged')) LIMIT 1", (rows[0][2], rows[0][1], rows[0][2], rows[0][2])).fetchone():
        _fail('ticket_invalid')
    from .user_state_actions import audit_actions
    audit_actions(conn)
    from .operation_consumers import audit_consumers
    audit_consumers(conn, required=conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() in {(7,), (8,), (9,)})
    from .provider_admission_owner import audit_admissions
    audit_admissions(conn, required=conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() == (9,))


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
    if _bytes_used(conn) > MAX_BYTES:
        _fail('byte_budget')
    # The lifecycle's action audit reads selector metadata. Prove the journal
    # record/column limits before any leaf audit materializes those selectors.
    _audit_lifecycle(conn)
    if conn.execute('SELECT format FROM mutation_root').fetchone() == (9,):
        from .operation_consumers import reservation_capacity as consumer_capacity
        from .provider_admission_owner import reservation_capacity as admission_capacity
        consumer_records, consumer_bytes = consumer_capacity(conn)
        admission_records, admission_bytes = admission_capacity(conn)
        pending = conn.execute('SELECT count(*) FROM typed_operations WHERE receipt IS NULL').fetchone()[0]
        if (conn.execute('SELECT count(*) FROM typed_operations').fetchone()[0]
                + consumer_records + admission_records > MAX_RECORDS
                or _bytes_used(conn) + consumer_bytes + admission_bytes
                + (pending + consumer_records + admission_records) * MAX_RECEIPT_BYTES > MAX_BYTES):
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
            if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() not in ((5,), (6,), (7,), (8,), (9,)):
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

    def adopt_epoch(self, rpc) -> int:
        """Fetch authenticated epoch evidence, then opt in or retire tickets.

        Network I/O finishes before the root transaction begins. A caller cannot
        substitute an externally supplied observation for the authenticated read.
        """
        from ..transport.receipt_retirement import observe_provider_epoch
        from ..transport.typed_mutations import TypedMutationRPC

        if type(rpc) is not TypedMutationRPC:
            raise TypeError('invalid mutation epoch observer')
        if (rpc.endpoint, rpc.root, rpc.actor_id) != (self.endpoint, self.root, self.actor_id):
            _fail('epoch_owner_changed')
        return self._adopt_epoch(observe_provider_epoch(rpc))

    def _adopt_epoch(self, evidence: ProviderEpoch) -> int:
        """Commit an observation from adopt_epoch; no network under this cut.

        Only the authenticated observation adapter supplies this evidence in
        production. Administrative closure proves that all older receipts were
        acknowledged; it cannot resolve requests with no durable local receipt.
        Existing opaque-ID records are never deleted. Returns deleted rows.
        """
        if type(evidence) is not ProviderEpoch:
            raise TypeError('invalid provider epoch evidence')
        evidence.__post_init__()
        if (evidence.endpoint, evidence.root, evidence.actor_id) != (self.endpoint, self.root, self.actor_id):
            _fail('epoch_owner_changed')
        with self.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            previous = _lifecycle(conn)
            if previous is None:
                # Initial observation does not prove which historical epochs
                # an old opaque request belongs to. Preserve that ambiguity.
                if conn.execute("SELECT 1 FROM typed_operations WHERE protocol_epoch!=? AND state!='acknowledged' LIMIT 1", (evidence.protocol_epoch,)).fetchone():
                    _fail('unresolved_epoch')
                for sql in _LIFECYCLE_SCHEMA.values():
                    conn.execute(sql)
                from .user_state_actions import initialize_actions
                initialize_actions(conn)
                conn.execute('INSERT INTO typed_operation_lifecycle VALUES(1,?,?,?)',
                             (evidence.lifecycle_id, evidence.protocol_epoch, evidence.generation))
                conn.execute('ALTER TABLE mutation_root RENAME TO mutation_root_previous')
                conn.execute(_V6_ROOT_SCHEMA)
                conn.execute('INSERT INTO mutation_root SELECT singleton,identity,epoch,6,revision FROM mutation_root_previous')
                conn.execute('DROP TABLE mutation_root_previous')
                deleted = 0
            else:
                lifecycle_id, epoch, generation = previous
                if evidence.lifecycle_id != lifecycle_id or evidence.generation < generation:
                    _fail('epoch_regressed')
                if evidence.generation == generation:
                    if evidence.protocol_epoch != epoch:
                        _fail('epoch_changed')
                    return 0
                if evidence.protocol_epoch == epoch:
                    _fail('epoch_changed')
                if conn.execute('SELECT format FROM mutation_root').fetchone() == (9,):
                    # Admitted owners settle and reap first (certify_closure
                    # covers ACK replies lost across rotation); never-admitted
                    # drafts are proven effect-free by this closure evidence.
                    if conn.execute("SELECT 1 FROM typed_admission_owners WHERE protocol_epoch=? AND state='admitted' LIMIT 1", (epoch,)).fetchone():
                        _fail('admission_owner_unresolved_epoch')
                    from .provider_admission_owner import rebase_closed_drafts
                    rebase_closed_drafts(conn, self, epoch, evidence.protocol_epoch)
                if conn.execute("SELECT 1 FROM typed_operations WHERE protocol_epoch=? AND state NOT IN ('completed','acknowledged') LIMIT 1", (epoch,)).fetchone():
                    _fail('unresolved_epoch')
                # ACK may commit remotely before its reply is lost. Rotation
                # certifies ACK, but only a validated local completed receipt
                # proves this client's source completion already committed.
                for row in conn.execute('SELECT ' + _COLUMNS + " FROM typed_operations WHERE (protocol_epoch=? AND state='completed') OR EXISTS (SELECT 1 FROM typed_operation_tickets t WHERE t.protocol_epoch=typed_operations.protocol_epoch AND t.operation_id=typed_operations.operation_id AND t.released=1)", (epoch,)):
                    _record(row, self.coordinator, self._owner)
                conn.execute("UPDATE typed_operations SET state='acknowledged' WHERE protocol_epoch=? AND state='completed'", (epoch,))
                deleted = conn.execute('DELETE FROM typed_operations WHERE (protocol_epoch,operation_id) IN (SELECT protocol_epoch,operation_id FROM typed_operation_tickets WHERE released=1)').rowcount
                conn.execute('DELETE FROM typed_operation_tickets WHERE released=1')
                conn.execute('UPDATE typed_operation_lifecycle SET protocol_epoch=?,generation=? WHERE singleton=1',
                             (evidence.protocol_epoch, evidence.generation))
            self.coordinator._advance_root_revision(conn)
            self._check(conn)
            return deleted

    def allocate_ticket(self, kind: str, arguments: dict) -> OperationTicket:
        """Allocate NEW logical work, durably saving request and intent.

        The caller must persist this returned ticket in its durable work item
        before dispatch. Never use allocation to retry an existing work item.
        A crash before caller binding leaves a discoverable prepared operation;
        production owners must make that binding recoverable before opting in.
        """
        return self._allocate_ticket(kind, arguments)

    def allocate_action_ticket(self, action_id: str, expected_attempt: int,
                               arguments: dict, *, kind='doc_compare_put') -> OperationTicket:
        """Bind a semantic action to its exact CAS attempt in the root commit."""
        if kind not in {'doc_compare_put', 'reaction_pair'}:
            raise ValueError('unsupported action mutation kind')
        return self._allocate_ticket(kind, arguments, action=(action_id, expected_attempt))

    def _allocate_ticket(self, kind, arguments, *, action=None, outbox=None):
        with self.coordinator._transaction() as conn:
            self._check(conn)
            current = _lifecycle(conn)
            if current is None:
                _fail('lifecycle_required')
        request = prepare_mutation(self.root, current[1], str(uuid4()), self.actor_id,
                                   kind, arguments)
        record = self._prepare(request, application=True, ticket=True, action=action, outbox=outbox)
        return OperationTicket(record.request.expectation.protocol_epoch, record.request.expectation.operation_id)

    def _reap_outbox_consumers(self, conn):
        from pathlib import Path
        from types import SimpleNamespace
        from .operation_consumers import reap_deleted_owners

        if conn.execute('SELECT format FROM mutation_root').fetchone() not in {(7,), (8,), (9,)}:
            return 0
        before = conn.execute('SELECT count(*) FROM typed_operation_consumers').fetchone()[0]
        paths = {row[0] for row in conn.execute('SELECT DISTINCT owner_path FROM typed_operation_consumers WHERE consumed=1')}
        for path, incarnation, epoch in self.coordinator._stores(conn):
            if path not in paths:
                continue
            store = SimpleNamespace(path=Path(path))
            with local_source._writer(store) as store_conn:
                self.coordinator._check_store(store_conn, store, incarnation, epoch)
                reap_deleted_owners(conn, store_conn, ('outbox', path, incarnation))
        return before - conn.execute('SELECT count(*) FROM typed_operation_consumers').fetchone()[0]

    def reap_outbox_consumers(self):
        """Bounded cross-Store cleanup after durable exact sent-row deletion."""
        with self.coordinator._transaction() as conn:
            self._check(conn)
            return self._reap_outbox_consumers(conn)

    def _install_consumers(self, conn):
        from pathlib import Path
        from types import SimpleNamespace
        from .operation_consumers import SCHEMA, RESERVATION_SCHEMA, reserve_owner
        from .db import outbox_owner_row

        version = conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone()
        if version in ((8,), (9,)):
            return
        if version not in {(6,), (7,)}:
            _fail('lifecycle_required')
        if version == (6,):
            conn.execute(SCHEMA)
        else:
            self._reap_outbox_consumers(conn)
        conn.execute(RESERVATION_SCHEMA)
        conn.execute('ALTER TABLE mutation_root RENAME TO mutation_root_previous')
        conn.execute(_V8_ROOT_SCHEMA)
        conn.execute('INSERT INTO mutation_root SELECT singleton,identity,epoch,8,revision FROM mutation_root_previous')
        conn.execute('DROP TABLE mutation_root_previous')
        # Old format7 owners are adopted together; never let unrelated new work
        # consume capacity needed by their remaining legs. A failed upgrade
        # rolls back without erasing or releasing any prior evidence.
        read_bytes = 0
        for path, incarnation, epoch in self.coordinator._stores(conn):
            rows = conn.execute('SELECT DISTINCT owner_seq,digest FROM typed_operation_consumers WHERE owner_path=?', (path,)).fetchall()
            if not rows:
                continue
            store = SimpleNamespace(path=Path(path))
            with local_source._writer(store) as store_conn:
                self.coordinator._check_store(store_conn, store, incarnation, epoch)
                for seq, digest in rows:
                    row = outbox_owner_row(store_conn, seq)
                    if row is None or row[1] != digest or row[2] != 'append_log' or '|' not in row[3]:
                        _fail('outbox_upgrade_owner_changed')
                    read_bytes += store_conn.execute('SELECT length(CAST(payload AS BLOB)) FROM outbox WHERE seq=?', (seq,)).fetchone()[0]
                    if read_bytes > MAX_BYTES:
                        _fail('outbox_upgrade_budget')
                    key = ('outbox', path, incarnation, epoch, seq, 'append', digest)
                    reserve_owner(conn, store_conn, key, tuple(row[3].split('|', 1)), self)
        self._capacity(conn)

    def _capacity(self, conn, *, extra_bytes=0, extra_records=0):
        from .operation_consumers import reservation_capacity
        records, size = reservation_capacity(conn)
        from .provider_admission_owner import reservation_capacity as admission_capacity
        admission_records, admission_size = admission_capacity(conn)
        records += admission_records
        size += admission_size
        if conn.execute('SELECT count(*) FROM typed_operations').fetchone()[0] + records + extra_records > MAX_RECORDS:
            _fail('record_budget')
        pending = conn.execute('SELECT count(*) FROM typed_operations WHERE receipt IS NULL').fetchone()[0]
        if _bytes_used(conn) + size + extra_bytes + (pending + records + extra_records) * MAX_RECEIPT_BYTES > MAX_BYTES:
            _fail('byte_budget')

    def get_outbox_leg(self, store, seq, leg, digest):
        """Lookup an immutable Store-owned leg; never allocate on recovery."""
        from .operation_consumers import check_key, validate_owner, find_slot
        check_key(store, seq, leg, digest)
        with self.coordinator._transaction() as conn:
            self._check(conn)
            with local_source._writer(store) as store_conn:
                key, state, _ = validate_owner(self.coordinator, conn, store_conn,
                                               store, seq, leg, digest, states={'pending', 'sent'})
                if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() not in {(7,), (8,), (9,)}:
                    return None
                slot = find_slot(conn, key)
                if slot is None:
                    return None
                if state == 'pending' and slot[1]:
                    _fail('outbox_already_consumed')
                return slot[0]

    def allocate_outbox_ticket(self, store, seq, leg, digest, kind, arguments):
        """Atomically bind a pending durable row to its exact provider attempt."""
        from .operation_consumers import check_key
        check_key(store, seq, leg, digest)
        return self._allocate_ticket(kind, arguments, outbox=(store, seq, leg, digest))

    def consume_outbox_leg(self, store, seq, leg, digest):
        """A durable sent row permits release of its retained exact leg."""
        from .operation_consumers import check_key, validate_owner, find_slot
        check_key(store, seq, leg, digest)
        with ExitStack() as held:
            with self.coordinator._transaction() as conn:
                self._check(conn)
                store_conn = held.enter_context(local_source._writer(store))
                key, _, _ = validate_owner(self.coordinator, conn, store_conn,
                                           store, seq, leg, digest, states={'sent'})
                if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() not in {(7,), (8,), (9,)}:
                    _fail('consumer_schema_required')
                slot = find_slot(conn, key)
                if slot is None:
                    _fail('outbox_missing_leg')
                ticket, consumed = slot
                if not consumed:
                    record = self._find(conn, ticket.protocol_epoch, ticket.operation_id)
                    if record is None or record.state != 'acknowledged':
                        _fail('outbox_unacknowledged')
                    conn.execute('UPDATE typed_operation_consumers SET consumed=1 WHERE protocol_epoch=? AND operation_id=?',
                                 (ticket.protocol_epoch, ticket.operation_id))
                self._release_ticket(conn, ticket)
                return ticket

    def finish_outbox(self, store, seq, digest):
        """Verify settled slots; retain proof until the sent row is deleted."""
        from .operation_consumers import check_key, validate_owner, owner_payload
        check_key(store, seq, 'append', digest)
        with ExitStack() as held:
            with self.coordinator._transaction() as conn:
                self._check(conn)
                store_conn = held.enter_context(local_source._writer(store))
                key, _, _ = validate_owner(self.coordinator, conn, store_conn,
                                           store, seq, 'append', digest, states={'sent'})
                if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() not in {(7,), (8,), (9,)}:
                    _fail('consumer_schema_required')
                owner = (key[0], key[1], key[2], seq)
                _, manifests = owner_payload(store_conn, seq)
                expected_legs = {'append'} | {'manifest:' + blob for blob in manifests}
                actual_legs = {row[0] for row in conn.execute('SELECT leg FROM typed_operation_consumers WHERE owner_kind=? AND owner_path=? AND owner_incarnation=? AND owner_seq=?', owner)}
                if actual_legs != expected_legs:
                    _fail('outbox_missing_leg')
                rows = conn.execute('SELECT c.owner_epoch,c.digest,c.consumed,t.released FROM typed_operation_consumers c LEFT JOIN typed_operation_tickets t USING(protocol_epoch,operation_id) WHERE c.owner_kind=? AND c.owner_path=? AND c.owner_incarnation=? AND c.owner_seq=?', owner)
                for epoch, saved_digest, consumed, released in rows:
                    if (epoch, saved_digest) != (key[3], digest) or not consumed or released not in (None, 1):
                        _fail('outbox_unsettled')
                # A crash before Store deletion must still find exact completion
                # evidence. Later allocation reaps these rows only after deletion.

    def get_ticket(self, ticket: OperationTicket) -> OperationRecord:
        """Lookup only: retired or unknown work is never prepared again."""
        if type(ticket) is not OperationTicket:
            raise TypeError('invalid operation ticket')
        ticket.__post_init__()
        return self.get(ticket.protocol_epoch, ticket.operation_id)

    def lifecycle_enabled(self) -> bool:
        """Inspect the durable opt-in under the normal owner checks."""
        with self.coordinator._transaction() as conn:
            self._check(conn)
            return _lifecycle(conn) is not None

    def protocol_epoch(self) -> str | None:
        """The adopted provider epoch new tickets must use, if opted in."""
        with self.coordinator._transaction() as conn:
            self._check(conn)
            current = _lifecycle(conn)
            return None if current is None else current[1]

    def operation_ticket(self, protocol_epoch: str, operation_id: str) -> OperationTicket | None:
        """Return ticket classification without loading the request body."""
        ticket = OperationTicket(protocol_epoch, operation_id)
        with self.coordinator._transaction() as conn:
            self._check(conn)
            if _lifecycle(conn) is None:
                return None
            found = conn.execute('SELECT 1 FROM typed_operation_tickets WHERE protocol_epoch=? AND operation_id=?',
                                 (protocol_epoch, operation_id)).fetchone()
            return ticket if found else None

    def release_ticket(self, ticket: OperationTicket) -> bool:
        """Record durable consumer settlement, pruning only a closed epoch.

        The caller must first durably consume the exact terminal outcome. This
        must never be called merely because a provider ACK succeeded. Returns
        whether the closed record was deleted; current-epoch releases retain
        replay evidence until authenticated closure. No network is performed.
        """
        if type(ticket) is not OperationTicket:
            raise TypeError('invalid operation ticket')
        ticket.__post_init__()
        with self.coordinator._transaction() as conn:
            self._check(conn)
            return self._release_ticket(conn, ticket)

    def _release_ticket(self, conn, ticket):
        current = _lifecycle(conn)
        if current is None:
            _fail('lifecycle_required')
        from .operation_consumers import require_consumed
        require_consumed(conn, ticket)
        key = (ticket.protocol_epoch, ticket.operation_id)
        if conn.execute('SELECT format FROM mutation_root').fetchone() == (9,):
            if conn.execute('SELECT 1 FROM typed_admission_legs WHERE protocol_epoch=? AND operation_id=? AND linked=1 AND outcome IS NULL LIMIT 1', key).fetchone():
                _fail('admission_owner_unconsumed')
        if conn.execute("SELECT 1 FROM user_state_actions WHERE ticket_epoch=? AND ticket_operation=? AND state='linked' LIMIT 1", key).fetchone():
            _fail('consumer_unsettled')
        row = conn.execute('SELECT generation FROM typed_operation_tickets WHERE protocol_epoch=? AND operation_id=?', key).fetchone()
        if row is None:
            # Cleanup is idempotent after a committed prune. This is not
            # evidence that the operation existed or succeeded.
            return False
        record = self._find(conn, *key)
        if record.state not in {'completed', 'acknowledged'}:
            _fail('ticket_uncompleted')
        if row[0] < current[2]:
            conn.execute('DELETE FROM typed_operation_tickets WHERE protocol_epoch=? AND operation_id=?', key)
            conn.execute('DELETE FROM typed_operations WHERE protocol_epoch=? AND operation_id=?', key)
            return True
        conn.execute('UPDATE typed_operation_tickets SET released=1 WHERE protocol_epoch=? AND operation_id=?', key)
        return False

    def prepare(self, request: PreparedMutation) -> OperationRecord:
        """Strict replay requires the original epoch and operation identity."""
        return self._prepare(request, application=False)

    def prepare_operation(self, request: PreparedMutation) -> OperationRecord:
        """Bind an explicit application key once, retaining its original epoch.

        New application keys cannot overtake any overlapping unresolved intent.
        Existing exact keys may resume even after the provider epoch changes.
        """
        return self._prepare(request, application=True)

    def _prepare(self, request: PreparedMutation, *, application: bool,
                 ticket: bool = False, action=None, outbox=None) -> OperationRecord:
        changes = mutation_selectors(request)
        raw = request.request_json
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('typed operation request exceeds limit')
        root = self.coordinator
        with ExitStack() as held:
            with root._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
                self._check(conn)
                outbox_key = None
                if outbox is not None:
                    from .operation_consumers import validate_owner, validate_payload, find_slot, reserve_owner
                    self._install_consumers(conn)
                    self._reap_outbox_consumers(conn)
                    store, seq, leg, digest = outbox
                    with local_source._writer(store) as store_conn:
                        outbox_key, _, target = validate_owner(root, conn, store_conn, *outbox, states={'pending'})
                        validate_payload(store_conn, seq, outbox_key, target, request)
                        reserve_owner(conn, store_conn, outbox_key, target, self)
                    slot = find_slot(conn, outbox_key)
                    if slot is not None:
                        original = self._find(conn, slot[0].protocol_epoch, slot[0].operation_id)
                        candidate = prepare_mutation(self.root, slot[0].protocol_epoch,
                                                     slot[0].operation_id, self.actor_id,
                                                     request.expectation.kind, json.loads(request.arguments_json))
                        if slot[1] or original is None or original.request != candidate:
                            _fail('outbox_request_changed')
                        store_conn = held.enter_context(local_source._writer(store))
                        validate_owner(root, conn, store_conn, *outbox, states={'pending'})
                        return original
                    from .operation_consumers import require_preceding_manifests
                    with local_source._writer(store) as store_conn:
                        validate_owner(root, conn, store_conn, *outbox, states={'pending'})
                        require_preceding_manifests(conn, store_conn, outbox_key)
                exact = self._exact(conn, request)  # Validate the caller's bound root/actor.
                if conn.execute('SELECT format FROM mutation_root').fetchone() == (9,):
                    if conn.execute('SELECT 1 FROM typed_admission_legs WHERE operation_id=? LIMIT 1',
                                    (request.expectation.operation_id,)).fetchone():
                        _fail('admission_leg_requires_atomic_ticket_binding')
                existing = self._find_operation(conn, request.expectation.operation_id) if application else exact
                if existing is not None:
                    if application:
                        request = PreparedMutation(
                            replace(request.expectation, protocol_epoch=existing.request.expectation.protocol_epoch),
                            request.arguments_json)
                    if existing.request.request_json != request.request_json:
                        _fail('request_changed')
                    return existing
                if conn.execute('SELECT format FROM mutation_root').fetchone() == (7,):
                    self._install_consumers(conn)
                lifecycle = _lifecycle(conn)
                if lifecycle is not None:
                    if not ticket:
                        _fail('ticket_required')
                    if request.expectation.protocol_epoch != lifecycle[1]:
                        _fail('epoch_changed')
                elif ticket:
                    _fail('lifecycle_required')
                if action is not None:
                    from .user_state_actions import validate_action_allocation
                    validate_action_allocation(conn, *action, request)
                if application:
                    pending = conn.execute('SELECT kind,value FROM mutation_scopes LIMIT ?', (MAX_PENDING * 8 + 1,))
                    for kind, value in pending:
                        if any(_overlap(source_selectors.Selector(kind, value), change) for change in changes):
                            _fail('overlapping_intent')
                if outbox is None:
                    from .operation_consumers import require_no_reserved_legacy_append
                    require_no_reserved_legacy_append(conn, request)
                    self._capacity(conn, extra_bytes=len(raw), extra_records=1)
                else:
                    self._capacity(conn)
                result = self._insert_prepared(
                    conn, request, changes, lifecycle,
                    ticket=ticket, action=action, outbox_key=outbox_key,
                )
                if outbox is not None:
                    # Hold the final Store writer through the root commit. No
                    # node locks or provider callbacks are acquired after this.
                    store_conn = held.enter_context(local_source._writer(store))
                    validate_owner(root, conn, store_conn, *outbox, states={'pending'})
        return result

    def _insert_prepared(self, conn, request, changes, lifecycle, *,
                         ticket, action=None, outbox_key=None,
                         admission_link=None):
        """One root-atomic request/intent/ticket insertion for all owners."""
        root = self.coordinator
        raw = request.request_json
        token = root._insert_intent(conn, changes)
        expected = request.expectation
        conn.execute('INSERT INTO typed_operations VALUES(?,?,?,?,?,NULL,?)',
                     (expected.protocol_epoch, expected.operation_id, token, 'prepared', raw,
                      _canonical([(item.kind, item.value) for item in changes])))
        if ticket:
            conn.execute('INSERT INTO typed_operation_tickets VALUES(?,?,?,0)',
                         (expected.protocol_epoch, expected.operation_id, lifecycle[2]))
        if action is not None:
            from .user_state_actions import link_action_ticket
            link_action_ticket(conn, *action, request)
        if outbox_key is not None:
            from .operation_consumers import insert_slot
            insert_slot(conn, outbox_key, request)
        if admission_link is not None:
            from .provider_admission_owner import link_admission_ticket
            link_admission_ticket(conn, *admission_link, request)
        root._retire_nodes(conn, token)
        root._retire(conn, changes)
        root._advance_root_revision(conn)
        return self._find(conn, expected.protocol_epoch, expected.operation_id)

    def _bind_admitted(self, conn, request, owner_id, ordinal):
        """Transfer one admitted reservation to a ticket, without new identity."""
        self._check(conn)
        if conn.execute('SELECT format FROM mutation_root').fetchone() != (9,):
            _fail('admission_schema_required')
        if type(request) is not PreparedMutation:
            raise TypeError('invalid admitted mutation')
        request.__post_init__()
        if (request.expectation.root, request.expectation.actor_id) != (self.root, self.actor_id):
            _fail('request_owner_changed')
        if len(request.request_json) > MAX_REQUEST_BYTES:
            _fail('request_budget')
        existing = self._find_operation(conn, request.expectation.operation_id)
        if existing is not None:
            _fail('admission_operation_already_linked')
        lifecycle = _lifecycle(conn)
        if lifecycle is None or request.expectation.protocol_epoch != lifecycle[1]:
            _fail('epoch_changed')
        changes = mutation_selectors(request)
        pending = conn.execute('SELECT kind,value FROM mutation_scopes LIMIT ?', (MAX_PENDING * 8 + 1,))
        for kind, value in pending:
            if any(_overlap(source_selectors.Selector(kind, value), change) for change in changes):
                _fail('overlapping_intent')
        from .operation_consumers import require_no_reserved_legacy_append
        require_no_reserved_legacy_append(conn, request)
        result = self._insert_prepared(
            conn, request, changes, lifecycle, ticket=True,
            admission_link=(owner_id, ordinal),
        )
        self._capacity(conn)  # linked reservation was transferred, not added
        return result

    def _drop_cancelled(self, conn, request):
        """Retire one admitted, unexecuted ticket inside the owner's root cut.

        The admission owner calls this only with the provider's exact
        ``cancelled`` reply for this leg, which forbids any later execution.
        That reply, not a receipt, proves completion: once the unexecuted row
        is removed, the intent finishes without receipt proof in this commit.
        """
        record = self._exact(conn, request)
        key = (request.expectation.protocol_epoch, request.expectation.operation_id)
        ticket = conn.execute('SELECT released FROM typed_operation_tickets WHERE protocol_epoch=? AND operation_id=?', key).fetchone()
        if (record is None or record.receipt is not None
                or record.state not in {'prepared', 'attempted'} or ticket != (0,)):
            _fail('cancelled_operation_changed')
        conn.execute('DELETE FROM typed_operation_tickets WHERE protocol_epoch=? AND operation_id=?', key)
        conn.execute('DELETE FROM typed_operations WHERE protocol_epoch=? AND operation_id=?', key)
        self.coordinator._finish_in_transaction(conn, record.intent)

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
                "SELECT protocol_epoch,operation_id FROM typed_operations WHERE " + _unfinished_sql(conn)
                + cursor_sql + ' ORDER BY protocol_epoch,operation_id LIMIT ?', arguments))

    def pending_document_keys(self, path: str):
        """Find unfinished exact-document writes without loading replay bodies.

        Replacement owners must resume these keys before constructing a fresh
        snapshot. Broader scopes remain protected by normal intent overlap.
        """
        changes = source_selectors.selectors((source_selectors.Selector('doc_exact', path),))
        encoded = _canonical([(item.kind, item.value) for item in changes])
        with self.coordinator._transaction() as conn:
            self._check(conn)
            keys = tuple(conn.execute(
                "SELECT protocol_epoch,operation_id FROM typed_operations "
                "WHERE " + _unfinished_sql(conn) + " AND selectors=? "
                'ORDER BY protocol_epoch,operation_id LIMIT ?', (encoded, MAX_PENDING + 1)))
            if len(keys) > MAX_PENDING:
                _fail('document_inventory_budget')
            return keys

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
                if (state in {'prepared', 'attempted'}
                        and conn.execute('SELECT format FROM mutation_root').fetchone() == (9,)
                        and conn.execute("SELECT 1 FROM typed_admission_legs WHERE protocol_epoch=? AND operation_id=? AND cancel='requested' LIMIT 1",
                                         (request.expectation.protocol_epoch, request.expectation.operation_id)).fetchone()):
                    # A durable cancellation request resolves first; only its
                    # exact ``executed`` answer may reopen replay.
                    _fail('admission_cancel_requested')
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
