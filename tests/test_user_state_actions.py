"""Durable semantic state actions over provider CAS and journal tickets."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from agentbridge.mesh.events import state_signing_bytes
from agentbridge.mesh.overlays import UserState
from agentbridge.mesh.state_actions import reduce_state_action
from agentbridge.mesh.paths import P
from agentbridge.store import local_source
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.store.user_state_actions import UserStateActionQueue
from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.receipt_retirement import ProviderEpoch
from test_mutable_document_writers import Provider, ACTOR, EPOCH, URL

CHAT = 'room'
USER = 'alice'
PATH = P.state(CHAT, USER)
LIFECYCLE = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'


def signer(data):
    return hashlib.sha256(b'owner-key' + data).hexdigest()


def verifier(document):
    if not document:
        return True
    if type(document) is not dict or type(document.get('ns')) is not int:
        return False
    fields = UserState.signed_fields(document)
    return document.get('sig') == signer(state_signing_bytes(CHAT, USER, document['ns'], fields))


def signed(fields, ns=1):
    return {**fields, 'ns': ns, 'sig': signer(state_signing_bytes(CHAT, USER, ns, fields))}


@pytest.fixture
def world(tmp_path):
    identity = json.dumps(['local-root-v1', 'supabase', URL, 'mesh'], separators=(',', ':'))
    coordinator = MutationCoordinator(tmp_path / 'home', identity)
    node = NodeStore(tmp_path / 'node' / 'replica.sqlite',
                     ReplicaIdentity(URL, 'supabase://mesh', ACTOR, 'machine'))
    coordinator.register_node(node)
    provider = Provider()

    def reopen():
        journal = OperationJournal(coordinator, node, actor_id=ACTOR)
        if not journal.lifecycle_enabled():
            journal._adopt_epoch(ProviderEpoch(URL, 'mesh', ACTOR, LIFECYCLE, EPOCH, 0))
        return UserStateActionQueue(JournaledMutations(journal, provider))

    return coordinator, provider, reopen


def _write(queue, kind, args):
    return queue.write(CHAT, USER, kind, args, signer=signer, verifier=verifier)


def test_prior_star_delta_survives_lost_conflict_reply_and_restart(world):
    coordinator, provider, reopen = world
    provider.documents[PATH] = (1, signed({'starred': ['base']}))

    def concurrent(args):
        provider.before_write = None
        provider.documents[PATH] = (2, signed({'starred': ['base'], 'mute': True}, ns=2))

    provider.before_write = concurrent
    provider.lost_reply = True
    with pytest.raises(OSError, match='reply lost'):
        _write(reopen(), 'star', {'msg_ids': ['wanted']})
    assert not provider.effects
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT state,attempt FROM user_state_actions').fetchall() == [('linked', 0)]
    provider.lost_reply = False
    document, changed = _write(reopen(), 'hide', {'msg_ids': ['hidden']})
    assert changed and verifier(document)
    final = provider.documents[PATH][1]
    assert final['starred'] == ['base', 'wanted']
    assert final['hidden'] == ['hidden'] and final['mute'] is True
    assert len(provider.effects) == 2
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM user_state_actions').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM typed_operation_tickets WHERE released=0').fetchone() == (0,)


def test_ambiguous_prior_action_replays_before_next_provider_read(world):
    _, provider, reopen = world
    provider.lost_reply = True
    with pytest.raises(OSError):
        _write(reopen(), 'set_flag', {'name': 'mute', 'value': True})
    assert len(provider.requests) == 1 and len(provider.effects) == 1
    retained = provider.requests[0]
    provider.lost_reply = False
    provider.read_error = True
    with pytest.raises(OSError, match='provider unavailable'):
        _write(reopen(), 'star', {'msg_ids': ['m']})
    # The old exact operation was recovered and settled before the new action
    # reached its fresh provider read. Its provider effect remains single.
    assert provider.requests[1].request_json == retained.request_json
    assert len(provider.effects) == 1
    provider.read_error = False
    _write(reopen(), 'hide', {'msg_ids': ['x']})
    assert provider.documents[PATH][1]['starred'] == ['m']
    assert provider.documents[PATH][1]['hidden'] == ['x']


def test_conflict_retry_budget_is_per_call_and_action_remains_queued(world):
    coordinator, provider, reopen = world

    def concurrent(args):
        seq, _ = provider.documents.get(PATH, (0, None))
        provider.documents[PATH] = (seq + 1, signed({'peer': seq + 1}, ns=seq + 1))

    provider.before_write = concurrent
    with pytest.raises(RuntimeError, match='changed repeatedly'):
        _write(reopen(), 'star', {'msg_ids': ['kept']})
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT state,attempt FROM user_state_actions').fetchall() == [('queued', 3)]
    provider.before_write = None
    _write(reopen(), 'hide', {'msg_ids': ['later']})
    final = provider.documents[PATH][1]
    assert final['starred'] == ['kept'] and final['hidden'] == ['later']


def test_unverified_source_is_not_laundered_and_owner_signature_is_required(world):
    _, provider, reopen = world
    provider.documents[PATH] = (1, {'starred': ['forged'], 'ns': 1, 'sig': 'bad'})
    final, changed = _write(reopen(), 'star', {'msg_ids': ['real']})
    assert changed and final['starred'] == ['real'] and verifier(final)
    with pytest.raises(ValueError, match='owner required'):
        reopen().write(CHAT, USER, 'star', {'msg_ids': ['next']}, signer=None, verifier=verifier)
    with pytest.raises(ValueError, match='owner did not verify'):
        reopen().write(CHAT, USER, 'star', {'msg_ids': ['next']},
                       signer=lambda data: 'wrong', verifier=verifier)


def test_action_schema_and_large_row_fail_closed_before_materialization(world):
    coordinator, _, reopen = world
    queue = reopen()
    action_id = queue.enqueue(CHAT, USER, 'star', {'msg_ids': ['x']})
    with sqlite3.connect(coordinator.path) as conn:
        conn.execute('UPDATE user_state_actions SET path=? WHERE action_id=?', ('x' * 100000, action_id))
    with pytest.raises(local_source.SourceChanged, match='user_state_action_invalid'):
        queue.enqueue(CHAT, USER, 'star', {'msg_ids': ['y']})


def test_mark_read_noop_never_allocates_provider_ticket(world):
    coordinator, provider, reopen = world
    provider.documents[PATH] = (1, signed({'read_ns': 10}))
    doc, changed = _write(reopen(), 'mark_read', {'up_to_ns': 5, 'at': 'fixed'})
    assert not changed and doc['read_ns'] == 10
    assert not provider.requests
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM user_state_actions').fetchone() == (0,)


def test_crash_after_durable_outcome_before_release_replays_without_second_effect(world, monkeypatch):
    coordinator, provider, reopen = world
    queue = reopen()
    original = queue._settle

    def crash_after_settle(action, state):
        original(action, state)
        raise RuntimeError('crash after settlement')

    monkeypatch.setattr(queue, '_settle', crash_after_settle)
    with pytest.raises(RuntimeError, match='crash after settlement'):
        _write(queue, 'star', {'msg_ids': ['kept']})
    assert len(provider.effects) == 1
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT state FROM user_state_actions').fetchall() == [('applied',)]
    _write(reopen(), 'hide', {'msg_ids': ['later']})
    assert len(provider.effects) == 2
    assert provider.documents[PATH][1]['starred'] == ['kept']


def test_crash_after_release_and_epoch_prune_drains_settled_action(world, monkeypatch):
    coordinator, provider, reopen = world
    queue = reopen()
    original = queue._finish

    def crash_before_finish(action):
        raise RuntimeError('crash after ticket release')

    monkeypatch.setattr(queue, '_finish', crash_before_finish)
    with pytest.raises(RuntimeError, match='crash after ticket release'):
        _write(queue, 'star', {'msg_ids': ['kept']})
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT state FROM user_state_actions').fetchall() == [('applied',)]
        assert conn.execute('SELECT released FROM typed_operation_tickets').fetchall() == [(1,)]
    journal = reopen().journal
    next_epoch = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
    assert journal._adopt_epoch(ProviderEpoch(URL, 'mesh', ACTOR, LIFECYCLE, next_epoch, 1)) == 1
    _write(reopen(), 'hide', {'msg_ids': ['later']})
    assert len(provider.effects) == 2
    assert provider.documents[PATH][1]['starred'] == ['kept']
    assert provider.documents[PATH][1]['hidden'] == ['later']
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM user_state_actions').fetchone() == (0,)
    monkeypatch.setattr(queue, '_finish', original)


def test_action_ticket_link_rolls_back_with_journal_prepare(world, monkeypatch):
    coordinator, provider, reopen = world
    queue = reopen()
    action_id = queue.enqueue(CHAT, USER, 'star', {'msg_ids': ['retained']})
    original = coordinator._advance_root_revision

    def crash(conn):
        raise RuntimeError('root commit interrupted')

    monkeypatch.setattr(coordinator, '_advance_root_revision', crash)
    with pytest.raises(RuntimeError, match='root commit interrupted'):
        queue.run(CHAT, USER, action_id, signer=signer, verifier=verifier)
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT state,ticket_epoch,ticket_operation FROM user_state_actions').fetchall() == [
            ('queued', None, None)]
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM typed_operation_tickets').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)
    assert not provider.requests
    monkeypatch.setattr(coordinator, '_advance_root_revision', original)
    queue.run(CHAT, USER, action_id, signer=signer, verifier=verifier)
    assert provider.documents[PATH][1]['starred'] == ['retained']


def test_bound_user_state_routes_semantics_without_mirror_read():
    class Bound:
        _journal_writer = object()

        def __init__(self):
            self.calls = []

        def get_doc(self, *args, **kwargs):
            pytest.fail('bound state writer read the mirror')

        def write_state_action(self, chat, user, kind, args, *, signer, verifier):
            self.calls.append((chat, user, kind, args))
            return {}, True

    tx = Bound()
    state = UserState(tx, CHAT, USER, signer=signer, verifier=verifier)
    state.star(['a'])
    state.unstar(['a'])
    state.hide(['b'])
    state.unhide(['b'])
    state.mark_read(12)
    assert state.mark_delivered(11)
    state.clear(10, keep_starred=True, runtime_ids=('r',))
    state.set_flag('mute', True)
    assert [call[2] for call in tx.calls] == [
        'star', 'unstar', 'hide', 'unhide', 'mark_read',
        'mark_delivered', 'clear', 'set_flag']
    with pytest.raises(NotImplementedError, match='compound reaction journal'):
        state.checkpoint(13)
    assert len(tx.calls) == 8


def test_read_delivered_and_clear_reducers_match_legacy_fields(monkeypatch):
    from agentbridge.mesh import overlays

    monkeypatch.setattr(overlays, 'utcnow_iso', lambda: 'fixed')

    class Legacy:
        def __init__(self, document):
            self.document = document

        def get_doc(self, path, default=None):
            return self.document

        def put_doc(self, path, value):
            self.document = value

    initial = {'read_ns': 4, 'forced_unread': True, 'delivered_ns': 2,
               'hidden_runtime': ['old'], 'starred': ['a']}
    tx = Legacy(dict(initial))
    state = UserState(tx, CHAT, USER)
    state.mark_read(3)
    state.mark_delivered(7)
    state.clear(9, keep_starred=True, runtime_ids=('new', 'old'))
    reduced = reduce_state_action(initial, 'mark_read', {'up_to_ns': 3, 'at': 'fixed'})
    reduced = reduce_state_action(reduced, 'mark_delivered', {'up_to_ns': 7, 'at': 'fixed'})
    reduced = reduce_state_action(reduced, 'clear', {
        'up_to_ns': 9, 'keep_starred': True, 'runtime_ids': ['new', 'old'], 'at': 'fixed'})
    assert reduced == tx.document


def test_wrong_signer_never_occupies_action_fifo(world):
    coordinator, provider, reopen = world
    with pytest.raises(ValueError, match='owner did not verify'):
        reopen().write(CHAT, USER, 'star', {'msg_ids': ['x']},
                       signer=lambda _data: 'wrong', verifier=verifier)
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM user_state_actions').fetchone() == (0,)
    assert not provider.requests
