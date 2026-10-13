"""Durable, bounded semantic actions for one signed per-user state document.

The action row is the authority for a logical delta. A provider CAS ticket is
linked in the same root transaction as its request and intent; a conflict is
settled on disk before that ticket is released and a new CAS is considered.
No callback or provider I/O occurs while the coordinator transaction is held.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from ..mesh.paths import P
from ..transport.mutation_protocol import _uuid
from ..transport.receipt_retirement import OperationTicket
from ..transport.scoped_sources import validate_source_paths
from ..transport.typed_mutations import PreparedMutation
from . import local_source

MAX_ACTIONS = 256
MAX_ACTION_BYTES = 16384
MAX_ACTION_TOTAL_BYTES = 2 * 1024 * 1024
_STATES = frozenset(('queued', 'linked', 'conflict', 'applied'))
_FLAGS = frozenset(('pinned', 'archived', 'deleted', 'forced_unread', 'mute'))
_SCHEMA = ('CREATE TABLE user_state_actions('
           'seq INTEGER PRIMARY KEY AUTOINCREMENT,action_id TEXT NOT NULL UNIQUE,'
           'actor_id TEXT NOT NULL,path TEXT NOT NULL,chat TEXT NOT NULL,user TEXT NOT NULL,'
           'kind TEXT NOT NULL,args BLOB NOT NULL,attempt INTEGER NOT NULL CHECK(attempt>=0),'
           'state TEXT NOT NULL,ticket_epoch TEXT,ticket_operation TEXT)')


def _fail(reason):
    raise local_source.SourceChanged('user_state_action_' + reason)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('invalid state action JSON') from None


def _component(value, label):
    validate_source_paths((value,))
    if '/' in value or not value or len(value.encode('utf-8')) > 1024:
        raise ValueError('invalid state action ' + label)


def _action_args(kind, args):
    if type(kind) is not str or type(args) is not dict:
        raise ValueError('invalid state action')
    shapes = {
        'mark_read': {'up_to_ns', 'at'},
        'mark_delivered': {'up_to_ns', 'at'},
        'star': {'msg_ids'}, 'unstar': {'msg_ids'},
        'hide': {'msg_ids'}, 'unhide': {'msg_ids'},
        'clear': {'up_to_ns', 'keep_starred', 'runtime_ids', 'at'},
        'set_flag': {'name', 'value'},
    }
    if kind not in shapes or set(args) != shapes[kind]:
        raise ValueError('invalid state action arguments')
    if kind in {'mark_read', 'mark_delivered', 'clear'}:
        if type(args['up_to_ns']) is not int or not 0 <= args['up_to_ns'] <= 2**63 - 1:
            raise ValueError('invalid state action cursor')
        if type(args['at']) is not str or not args['at'] or len(args['at'].encode('utf-8')) > 128:
            raise ValueError('invalid state action timestamp')
    if kind == 'clear' and type(args['keep_starred']) is not bool:
        raise ValueError('invalid state action clear flag')
    if kind in {'star', 'unstar', 'hide', 'unhide', 'clear'}:
        values = args['runtime_ids'] if kind == 'clear' else args['msg_ids']
        if (type(values) is not list or len(values) > 1024
                or any(type(item) is not str or not item or len(item.encode('utf-8')) > 1024
                       for item in values)):
            raise ValueError('invalid state action ids')
    if kind == 'set_flag' and (type(args['name']) is not str or args['name'] not in _FLAGS):
        raise ValueError('invalid state action flag')
    raw = _canonical(args)
    if len(raw) > MAX_ACTION_BYTES:
        raise ValueError('state action exceeds limit')
    return raw


@dataclass(frozen=True)
class StateAction:
    seq: int
    action_id: str
    actor_id: str
    path: str
    chat: str
    user: str
    kind: str
    args_json: bytes
    attempt: int
    state: str
    ticket_epoch: str | None
    ticket_operation: str | None

    @property
    def args(self):
        return json.loads(self.args_json)

    @property
    def ticket(self):
        if self.ticket_epoch is None:
            return None
        return OperationTicket(self.ticket_epoch, self.ticket_operation)


_COLUMNS = ('seq,action_id,actor_id,path,chat,user,kind,args,attempt,state,'
            'ticket_epoch,ticket_operation')


def _row(row, actor):
    try:
        action = StateAction(*row)
        if type(action.seq) is not int or action.seq < 1 or type(action.attempt) is not int or action.attempt < 0:
            raise ValueError('invalid action ordinal')
        _uuid(action.action_id, 'state action id')
        _uuid(action.actor_id, 'state action actor')
        if action.actor_id != actor:
            raise ValueError('state action actor changed')
        _component(action.chat, 'chat')
        _component(action.user, 'user')
        if action.path != P.state(action.chat, action.user):
            raise ValueError('state action path changed')
        if type(action.args_json) is not bytes or len(action.args_json) > MAX_ACTION_BYTES:
            raise ValueError('invalid state action bytes')
        args = json.loads(action.args_json)
        if action.args_json != _action_args(action.kind, args):
            raise ValueError('noncanonical state action')
        if action.state not in _STATES or ((action.ticket_epoch is None) != (action.ticket_operation is None)):
            raise ValueError('invalid state action phase')
        if (action.state == 'queued') != (action.ticket_epoch is None):
            raise ValueError('invalid state action ticket')
        if action.ticket_epoch is not None:
            _uuid(action.ticket_epoch, 'state action epoch')
            _uuid(action.ticket_operation, 'state action operation')
        return action
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail('invalid')


def initialize_actions(conn):
    """Called only as part of format-6 lifecycle installation."""
    conn.execute(_SCHEMA)


def audit_actions(conn):
    """Bounded structural audit on every format-6 root transaction."""
    if conn.execute("SELECT sql FROM sqlite_master WHERE name='user_state_actions'").fetchone() != (_SCHEMA,):
        _fail('schema')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='user_state_actions' LIMIT 1").fetchone():
        _fail('trigger')
    if conn.execute(
        "SELECT 1 FROM user_state_actions WHERE "
        "typeof(seq)!='integer' OR seq<1 OR "
        "typeof(action_id)!='text' OR length(CAST(action_id AS BLOB))>36 OR "
        "typeof(actor_id)!='text' OR length(CAST(actor_id AS BLOB))>36 OR "
        "typeof(path)!='text' OR length(CAST(path AS BLOB))>2300 OR "
        "typeof(chat)!='text' OR length(CAST(chat AS BLOB))>1024 OR "
        "typeof(user)!='text' OR length(CAST(user AS BLOB))>1024 OR "
        "typeof(kind)!='text' OR length(CAST(kind AS BLOB))>32 OR "
        "typeof(args)!='blob' OR length(args)>? OR "
        "typeof(attempt)!='integer' OR attempt<0 OR "
        "typeof(state)!='text' OR length(CAST(state AS BLOB))>16 OR "
        "(ticket_epoch IS NOT NULL AND (typeof(ticket_epoch)!='text' OR length(CAST(ticket_epoch AS BLOB))>36)) OR "
        "(ticket_operation IS NOT NULL AND (typeof(ticket_operation)!='text' OR length(CAST(ticket_operation AS BLOB))>36)) "
        "LIMIT 1", (MAX_ACTION_BYTES,)).fetchone():
        _fail('invalid')
    if conn.execute('SELECT 1 FROM user_state_actions LIMIT 1 OFFSET ?', (MAX_ACTIONS,)).fetchone():
        _fail('budget')
    if conn.execute('SELECT coalesce(sum(length(args)),0) FROM user_state_actions').fetchone()[0] > MAX_ACTION_TOTAL_BYTES:
        _fail('budget')
    owner = conn.execute('SELECT actor_id FROM typed_operation_owner WHERE singleton=1').fetchone()
    if owner is None:
        _fail('owner')
    # Audit identity and phase metadata only. Action payloads are validated
    # when enqueued and again on targeted recovery; ordinary publication
    # gates must not parse every queued semantic body.
    for (seq, action_id, actor, path, chat, user, kind, attempt, state,
         ticket_epoch, ticket_operation) in conn.execute(
            'SELECT seq,action_id,actor_id,path,chat,user,kind,attempt,state,'
            'ticket_epoch,ticket_operation FROM user_state_actions'):
        try:
            _uuid(action_id, 'state action id')
            _uuid(actor, 'state action actor')
            _component(chat, 'chat')
            _component(user, 'user')
            if (actor != owner[0] or path != P.state(chat, user)
                    or type(seq) is not int or seq < 1
                    or type(attempt) is not int or attempt < 0
                    or kind not in {'mark_read', 'mark_delivered', 'star', 'unstar',
                                    'hide', 'unhide', 'clear', 'set_flag'}
                    or state not in _STATES
                    or ((ticket_epoch is None) != (ticket_operation is None))
                    or ((state == 'queued') != (ticket_epoch is None))):
                raise ValueError('invalid state action metadata')
            if ticket_epoch is not None:
                _uuid(ticket_epoch, 'state action epoch')
                _uuid(ticket_operation, 'state action operation')
        except (ValueError, TypeError, UnicodeError):
            _fail('invalid')
    # Do not deserialize retained replay bodies on every coordinator gate.
    # A linked action must still point to an unreleased ticket and its exact
    # immutable document selector; targeted resume checks the full request.
    for path, selectors, ticket in conn.execute(
            "SELECT a.path,o.selectors,t.released FROM user_state_actions a "
            "LEFT JOIN typed_operations o ON o.protocol_epoch=a.ticket_epoch "
            "AND o.operation_id=a.ticket_operation "
            "LEFT JOIN typed_operation_tickets t ON t.protocol_epoch=a.ticket_epoch "
            "AND t.operation_id=a.ticket_operation WHERE a.state='linked'"):
        expected = _canonical([['doc_exact', path]])
        if selectors != expected or ticket != 0:
            _fail('linked_ticket_invalid')


def _action_for(conn, action_id, actor):
    row = conn.execute('SELECT ' + _COLUMNS + ' FROM user_state_actions WHERE action_id=?',
                       (action_id,)).fetchone()
    if row is None:
        _fail('missing')
    return _row(row, actor)


def _validate_request(action: StateAction, request: PreparedMutation):
    request.__post_init__()
    if (request.expectation.kind != 'doc_compare_put'
            or request.expectation.actor_id != action.actor_id
            or request.expectation.targets != (('path', action.path),)
            or json.loads(request.arguments_json)['p_path'] != action.path):
        raise ValueError('state action ticket target changed')


def validate_action_allocation(conn, action_id, expected_attempt, request: PreparedMutation):
    """Journal calls this before ticket allocation in the root transaction."""
    _uuid(action_id, 'state action id')
    if type(expected_attempt) is not int or expected_attempt < 0:
        raise ValueError('invalid state action attempt')
    owner = conn.execute('SELECT actor_id FROM typed_operation_owner WHERE singleton=1').fetchone()
    if owner is None:
        _fail('owner')
    action = _action_for(conn, action_id, owner[0])
    if action.state != 'queued' or action.attempt != expected_attempt:
        _fail('allocation_changed')
    if conn.execute('SELECT seq FROM user_state_actions WHERE path=? ORDER BY seq LIMIT 1',
                    (action.path,)).fetchone() != (action.seq,):
        _fail('out_of_order')
    _validate_request(action, request)


def link_action_ticket(conn, action_id, expected_attempt, request: PreparedMutation):
    """Journal calls this after inserting the request, before root commit."""
    validate_action_allocation(conn, action_id, expected_attempt, request)
    expected = request.expectation
    changed = conn.execute(
        "UPDATE user_state_actions SET state='linked',ticket_epoch=?,ticket_operation=? "
        "WHERE action_id=? AND attempt=? AND state='queued' AND ticket_epoch IS NULL",
        (expected.protocol_epoch, expected.operation_id, action_id, expected_attempt)).rowcount
    if changed != 1:
        _fail('link_changed')


class UserStateActionQueue:
    """Inactive signed-state writer; transport binding controls activation."""

    def __init__(self, driver):
        from ..transport.journaled_mutations import JournaledMutations
        if type(driver) is not JournaledMutations:
            raise TypeError('invalid state action driver')
        driver._check_owner()
        self.driver = driver
        self.journal = driver.journal
        self._owner = (driver.journal, driver.rpc, driver.journal.actor_id,
                       driver.journal.root, driver.journal.endpoint)

    def _check(self, conn):
        journal, rpc, actor, root, endpoint = self._owner
        if (self.driver.journal is not journal or self.driver.rpc is not rpc
                or (journal.actor_id, journal.root, journal.endpoint) != (actor, root, endpoint)):
            _fail('owner_changed')
        self.driver._check_owner()
        journal._check(conn)
        if conn.execute('SELECT format FROM mutation_root WHERE singleton=1').fetchone() != (6,):
            _fail('lifecycle_required')

    def enqueue(self, chat: str, user: str, kind: str, args: dict) -> str:
        _component(chat, 'chat')
        _component(user, 'user')
        raw = _action_args(kind, args)
        action_id = str(uuid4())
        path = P.state(chat, user)
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            if conn.execute('SELECT count(*) FROM user_state_actions').fetchone()[0] >= MAX_ACTIONS:
                _fail('budget')
            if conn.execute('SELECT coalesce(sum(length(args)),0) FROM user_state_actions').fetchone()[0] + len(raw) > MAX_ACTION_TOTAL_BYTES:
                _fail('budget')
            conn.execute('INSERT INTO user_state_actions(action_id,actor_id,path,chat,user,kind,args,attempt,state) '
                         'VALUES(?,?,?,?,?,?,?,0,\'queued\')',
                         (action_id, self.journal.actor_id, path, chat, user, kind, raw))
        return action_id

    def _head(self, path):
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            row = conn.execute('SELECT ' + _COLUMNS + ' FROM user_state_actions WHERE path=? ORDER BY seq LIMIT 1',
                               (path,)).fetchone()
            return None if row is None else _row(row, self.journal.actor_id)

    def _settle(self, action: StateAction, state: str):
        if state not in {'conflict', 'applied'}:
            raise ValueError('invalid action settlement')
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            live = _action_for(conn, action.action_id, self.journal.actor_id)
            if live != action:
                _fail('changed')
            conn.execute('UPDATE user_state_actions SET state=? WHERE action_id=? AND state=\'linked\'',
                         (state, action.action_id))

    def _finish(self, action: StateAction):
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            live = _action_for(conn, action.action_id, self.journal.actor_id)
            if live != action:
                _fail('changed')
            if action.state == 'applied':
                conn.execute('DELETE FROM user_state_actions WHERE action_id=?', (action.action_id,))
            elif action.state == 'conflict':
                conn.execute("UPDATE user_state_actions SET state='queued',attempt=attempt+1,"
                             'ticket_epoch=NULL,ticket_operation=NULL WHERE action_id=?', (action.action_id,))
            else:
                _fail('finish_phase')

    def _remove_noop(self, action: StateAction):
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            live = _action_for(conn, action.action_id, self.journal.actor_id)
            if live != action or live.state != 'queued':
                _fail('changed')
            conn.execute('DELETE FROM user_state_actions WHERE action_id=?', (action.action_id,))

    def run(self, chat: str, user: str, action_id: str, *, signer, verifier):
        """Drain older actions first, then this one; return its doc and change."""
        from ..mesh.state_actions import reduce_state_action, sign_state_action

        _component(chat, 'chat')
        _component(user, 'user')
        _uuid(action_id, 'state action id')
        if not callable(signer) or not callable(verifier):
            raise ValueError('signed state action owner required')
        path = P.state(chat, user)
        document, changed = None, False
        conflicts: dict[str, int] = {}
        for _ in range(MAX_ACTIONS * 8):
            action = self._head(path)
            if action is None:
                _fail('target_missing')
            target = action.action_id == action_id
            if action.state == 'linked':
                retained = self.journal.get(action.ticket_epoch, action.ticket_operation)
                _validate_request(action, retained.request)
                record = self.driver.resume(action.ticket_epoch, action.ticket_operation)
                if record.receipt is None or record.request.expectation.kind != 'doc_compare_put':
                    _fail('outcome_invalid')
                applied = dict(record.receipt.result)['applied']
                if type(applied) is not bool:
                    _fail('outcome_invalid')
                if target and applied:
                    document = json.loads(record.request.arguments_json)['p_data']
                    changed = True
                if not applied:
                    conflicts[action.action_id] = conflicts.get(action.action_id, 0) + 1
                self._settle(action, 'applied' if applied else 'conflict')
                continue
            if action.state in {'conflict', 'applied'}:
                if target and action.state == 'applied':
                    changed = True
                    # A previous call may have durably settled the receipt
                    # before losing its local reply. The retained request is
                    # authoritative while its ticket remains present.
                    try:
                        retained = self.journal.get_ticket(action.ticket)
                    except local_source.SourceChanged:
                        retained = None  # already released/pruned after settlement
                    if retained is not None:
                        _validate_request(action, retained.request)
                        document = json.loads(retained.request.arguments_json)['p_data']
                self.journal.release_ticket(action.ticket)
                self._finish(action)
                if target and action.state == 'applied':
                    return document, changed
                if action.state == 'conflict' and conflicts.get(action.action_id, 0) >= 3:
                    raise RuntimeError('state document changed repeatedly; retry the action')
                continue
            observed = self.driver.rpc.read_document_for_update(path)
            current = observed.payload
            try:
                verified = type(current) is dict and verifier(current)
            except (TypeError, ValueError, OverflowError):
                verified = False
            current = current if verified else {}
            fields = reduce_state_action(current, action.kind, action.args)
            if fields is None:
                if target:
                    document, changed = current, False
                self._remove_noop(action)
                if target:
                    return document, changed
                continue
            signed = sign_state_action(chat, user, current, fields, action.kind,
                                       action.args, signer, verifier)
            ticket = self.journal.allocate_action_ticket(
                action.action_id, action.attempt, observed.arguments(signed))
            if type(ticket) is not OperationTicket:
                _fail('allocation_invalid')
        _fail('retry_budget')

    def write(self, chat: str, user: str, kind: str, args: dict, *, signer, verifier):
        if not callable(signer) or not callable(verifier):
            raise ValueError('signed state action owner required')
        _component(chat, 'chat')
        _component(user, 'user')
        _action_args(kind, args)
        # Prove the supplied signer is accepted by this exact state owner's
        # verifier before a new durable action can occupy the FIFO. Replaying
        # an already-linked operation remains independent of this fresh proof.
        from ..mesh.events import state_signing_bytes
        probe = {'ns': 1, 'sig': signer(state_signing_bytes(chat, user, 1, {}))}
        try:
            proven = verifier(probe) is True
        except (TypeError, ValueError, OverflowError):
            proven = False
        if not proven:
            raise ValueError('signed state action owner did not verify')
        action_id = self.enqueue(chat, user, kind, args)
        return self.run(chat, user, action_id, signer=signer, verifier=verifier)


__all__ = ['UserStateActionQueue', 'StateAction', 'initialize_actions',
           'audit_actions', 'validate_action_allocation', 'link_action_ticket']
