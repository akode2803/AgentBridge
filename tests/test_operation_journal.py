"""Durable typed operations and conservative local recovery boundaries."""
from __future__ import annotations

import os
import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.node.admission import NodeCaptureRequest, NodeInputBatch, NodeLogRequest
from agentbridge.node import security
from agentbridge.store import local_source, source_selectors
from agentbridge.store import operation_journal as module
from agentbridge.store import mutation_coordinator as coordinator_module
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.mutation_receipts import decode_mutation_receipt
from agentbridge.transport.supabase import SupabaseTransport
from agentbridge.transport.typed_mutations import prepare_mutation

ACTOR = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
EPOCH = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
ENDPOINT = 'https://example.supabase.co'
S = source_selectors.Selector


def _request(index=1, *, kind='doc_put', arguments=None, actor=ACTOR):
    if arguments is None:
        arguments = {'p_path': 'chats/room/meta.json', 'p_data': {'private': 'content'}}
    return prepare_mutation('mesh', EPOCH, str(uuid.UUID(int=index)), actor, kind, arguments)


def _receipt(request, *, provider_digest='c' * 64):
    expected = request.expectation
    result = dict(expected.targets)
    if expected.kind == 'doc_put':
        result['updated'] = True
    if expected.kind == 'doc_compare_put':
        result['applied'] = True
    raw = {name: getattr(expected, name) for name in (
        'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
    raw.update(schema_version=2, provider_digest=provider_digest, result=result)
    return decode_mutation_receipt(raw, expected)


@pytest.fixture
def world(tmp_path):
    transport = SupabaseTransport('mesh', env={'SUPABASE_URL': ENDPOINT}, client=object())
    root = MutationCoordinator(tmp_path / 'home', root_identity(transport))
    node = NodeStore(tmp_path / 'node' / 'replica.sqlite',
                     ReplicaIdentity(ENDPOINT, 'supabase://mesh', ACTOR, 'machine'))
    root.register_node(node)
    store = Store(tmp_path / 'store.sqlite')
    local_source.initialize(store)
    source_selectors.initialize(store)
    root.register_store(store)
    definition = source_selectors.definition(root.identity, (S('doc_prefix', 'chats/room'),), build='journal')
    with root.publication_gate(store, definition):
        current = local_source.capture(store, definition.source)
        local_source.publish(store, current, {'ready': True}, observed_ns=1)
    journal = OperationJournal(root, node, actor_id=ACTOR)
    try:
        yield root, node, store, definition, journal
    finally:
        store.close()


def _reload(world):
    root, node, _, _, _ = world
    reopened = MutationCoordinator(root.path.parent.parent, root.identity)
    return OperationJournal(reopened, NodeStore(node.path, node.identity), actor_id=ACTOR)


def test_exact_replay_survives_restart_without_another_intent(world):
    root, _, store, definition, journal = world
    request = _request()
    prepared = journal.prepare(request)
    assert prepared.state == 'prepared'
    assert not local_source.capture(store, definition.source).ready
    restored = _reload(world).get(EPOCH, request.expectation.operation_id)
    assert restored == prepared
    assert restored.request.request_json == request.request_json
    assert _reload(world).prepare(restored.request) == prepared
    with root._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (1,)
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (5,)
    assert 'private' not in repr(prepared)


def test_durable_receipt_precedes_completion_and_ack(world):
    root, _, store, definition, journal = world
    request = _request()
    original = journal.prepare(request)
    receipt = _receipt(request)
    with pytest.raises(local_source.SourceChanged, match='not_attempted'):
        journal.accept(request, receipt)
    with pytest.raises(local_source.SourceChanged, match='receipt_required'):
        journal.complete(request)
    assert journal.mark_attempted(request).state == 'attempted'
    assert _reload(world).get(EPOCH, request.expectation.operation_id).state == 'attempted'
    assert journal.accept(request, receipt).state == 'accepted'
    accepted = _reload(world).get(EPOCH, request.expectation.operation_id)
    assert accepted.receipt == receipt
    with pytest.raises(local_source.SourceChanged, match='not_completed'):
        journal.mark_acknowledged(request)
    assert journal.complete(request).state == 'completed'
    assert not local_source.capture(store, definition.source).ready
    with root._transaction() as conn:
        assert conn.execute('SELECT token FROM mutation_intents WHERE token=?', (original.intent.token,)).fetchone() is None
    assert _reload(world).get(EPOCH, request.expectation.operation_id).state == 'completed'
    assert journal.mark_acknowledged(request).state == 'acknowledged'
    assert _reload(world).get(EPOCH, request.expectation.operation_id).state == 'acknowledged'


@pytest.mark.parametrize('state', ['prepared', 'attempted', 'accepted'])
def test_generic_completion_and_quarantine_never_bypass_journal(world, state):
    root, _, _, _, journal = world
    request = _request()
    record = journal.prepare(request)
    if state != 'prepared':
        journal.mark_attempted(request)
    if state == 'accepted':
        journal.accept(request, _receipt(request))
    for finish in (root.complete, root._finish):
        with pytest.raises(local_source.SourceChanged, match='receipt_required'):
            finish(record.intent)
    with pytest.raises(local_source.SourceChanged, match='receipt_required'):
        root.quarantine((record.intent.token,))
    assert journal.get(EPOCH, request.expectation.operation_id).state == state


def test_late_replies_are_monotonic_and_conflicting_receipts_fail(world):
    journal = world[-1]
    request = _request()
    journal.prepare(request)
    journal.mark_attempted(request)
    receipt = _receipt(request)
    journal.accept(request, receipt)
    journal.complete(request)
    journal.mark_acknowledged(request)
    assert journal.mark_attempted(request).state == 'acknowledged'
    assert journal.accept(request, receipt).state == 'acknowledged'
    assert journal.complete(request).state == 'acknowledged'
    assert journal.prepare(request).state == 'acknowledged'
    with pytest.raises(local_source.SourceChanged, match='receipt_changed'):
        journal.accept(request, _receipt(request, provider_digest='d' * 64))


def test_divergent_request_and_foreign_owner_fail_without_intent(world):
    root, node, _, _, journal = world
    request = _request()
    journal.prepare(request)
    changed = _request(arguments={'p_path': 'chats/room/meta.json', 'p_data': {'changed': True}})
    with pytest.raises(local_source.SourceChanged, match='request_changed'):
        journal.prepare(changed)
    with pytest.raises(local_source.SourceChanged, match='request_owner_changed'):
        journal.prepare(_request(2, actor=str(uuid.uuid4())))
    with pytest.raises(ValueError, match='node principal'):
        OperationJournal(root, node, actor_id=str(uuid.uuid4()))
    with root._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (1,)


@pytest.mark.parametrize('kind,args,expected', [
    ('reaction_pair', {'p_chat': 'room', 'p_user': 'alice',
                       'p_state_data': {}, 'p_reaction_data': {},
                       'p_expected_source_epoch': ACTOR,
                       'p_expected_state_seq': 0, 'p_expected_state_deleted': False,
                       'p_expected_reaction_seq': 0, 'p_expected_reaction_deleted': False},
     (S('doc_exact', 'chats/room/overlays/reactions/alice.json'),
      S('doc_exact', 'chats/room/overlays/state/alice.json'))),
    ('meta_append', {'p_chat': 'room', 'p_data': {}, 'p_expected_source_epoch': ACTOR,
                     'p_expected_seq': 1, 'p_expected_deleted': False, 'p_log': 'alice|m1',
                     'p_records': [{'id': 'x'}]},
     (S('doc_exact', 'chats/room/meta.json'), S('log_chat', 'room'))),
    ('doc_delete', {'p_path': 'accounts/alice.json'}, (S('doc_exact', 'accounts/alice.json'),)),
    ('log_append', {'p_chat': 'room', 'p_log': 'nested/messages.jsonl', 'p_record': {'id': 'x'}}, (S('log_chat', 'room'),)),
    ('effect_transition', {'p_path': 'chats/room/runtime/effects/agent/id/claim.json', 'p_data': {}, 'p_grant_ask': {}, 'p_grant_decision': {}},
     tuple(S('doc_exact', 'chats/room/runtime/effects/agent/id/' + name) for name in ('claim.json', 'grant-ask.json', 'grant-decision.json'))),
    ('chat_delete', {'p_chat': 'room'}, (S('doc_prefix', 'chats/room'), S('log_chat', 'room'))),
    ('blob_manifest', {'p_chat': 'room', 'p_blob': 'blob.bin', 'p_object_id': ACTOR, 'p_sealed_bytes': 20, 'p_sealed_sha256': 'a' * 64, 'p_manifest_digest': 'b' * 64},
     (S('doc_prefix', 'chats/room'), S('log_chat', 'room'))),
])
def test_selectors_are_derived_from_complete_request(world, kind, args, expected):
    record = world[-1].prepare(_request(kind=kind, arguments=args))
    assert record.intent.selectors == expected


@pytest.mark.parametrize('kind,args', [('member_leave', {}), ('member_claim', {'p_username': 'alice'})])
def test_member_mutations_derive_a_whole_root_fence(world, kind, args):
    record = world[-1].prepare(_request(kind=kind, arguments=args))
    assert record.intent.selectors == (S('root_all', '*'),)
    with world[0]._transaction() as conn:
        assert conn.execute('SELECT kind,value FROM mutation_scopes').fetchall() == [('root_all', '*')]


def test_capacity_keeps_terminal_records_and_reserves_receipt_space(world, monkeypatch):
    journal = world[-1]
    request = _request()
    monkeypatch.setattr(module, 'MAX_RECORDS', 1)
    journal.prepare(request)
    journal.mark_attempted(request)
    journal.accept(request, _receipt(request))
    journal.complete(request)
    journal.mark_acknowledged(request)
    assert journal.prepare(request).state == 'acknowledged'
    with pytest.raises(local_source.SourceChanged, match='record_budget'):
        journal.prepare(_request(2))
    monkeypatch.setattr(module, 'MAX_RECORDS', 4096)
    with world[0]._transaction() as conn:
        used = module._bytes_used(conn)
    monkeypatch.setattr(module, 'MAX_BYTES', used + len(_request(2).request_json) + module.MAX_RECEIPT_BYTES - 1)
    with pytest.raises(local_source.SourceChanged, match='byte_budget'):
        journal.prepare(_request(2))
    with world[0]._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)


def test_failed_prepare_keeps_earlier_store_retired_and_no_request(world, monkeypatch):
    root, _, store, definition, journal = world
    original = root._retire
    def fail(conn, changes):
        original(conn, changes)
        raise RuntimeError('late fanout failure')
    monkeypatch.setattr(root, '_retire', fail)
    with pytest.raises(RuntimeError, match='late fanout'):
        journal.prepare(_request())
    assert not local_source.capture(store, definition.source).ready
    with sqlite3.connect(world[1].path) as conn:
        assert conn.execute('SELECT state FROM node_mutation_fences').fetchall() == [('active',)]
    with root._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)


def test_failed_completion_keeps_accepted_receipt_and_retry_finishes(world, monkeypatch):
    root, _, _, _, journal = world
    request = _request()
    journal.prepare(request)
    journal.mark_attempted(request)
    journal.accept(request, _receipt(request))
    original = root._retire
    def fail(conn, changes):
        raise RuntimeError('local completion failure')
    monkeypatch.setattr(root, '_retire', fail)
    with pytest.raises(RuntimeError, match='completion failure'):
        journal.complete(request)
    assert _reload(world).get(EPOCH, request.expectation.operation_id).state == 'accepted'
    with sqlite3.connect(world[1].path) as conn:
        assert conn.execute('SELECT state FROM node_mutation_fences').fetchall() == [('completed',)]
    with root._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (1,)
    monkeypatch.setattr(root, '_retire', original)
    assert journal.complete(request).state == 'completed'


@pytest.mark.parametrize('damage', ['missing_tables', 'missing_intent', 'wrong_scope', 'owner', 'bad_state', 'trigger'])
def test_coordinator_fails_closed_on_journal_corruption(world, damage):
    root, _, _, _, journal = world
    record = journal.prepare(_request())
    with sqlite3.connect(root.path) as conn:
        if damage == 'missing_tables':
            conn.execute('DROP TABLE typed_operations')
            conn.execute('DROP TABLE typed_operation_owner')
        elif damage == 'missing_intent':
            conn.execute('DELETE FROM mutation_scopes')
            conn.execute('DELETE FROM mutation_intents')
        elif damage == 'wrong_scope':
            conn.execute("UPDATE mutation_scopes SET value='unrelated.json'")
        elif damage == 'owner':
            conn.execute("UPDATE typed_operation_owner SET coordinator_epoch=?", ('f' * 32,))
        elif damage == 'bad_state':
            conn.execute("UPDATE typed_operations SET state='completed'")
        else:
            conn.execute('CREATE TRIGGER corrupt AFTER DELETE ON typed_operations BEGIN SELECT 1; END')
    with pytest.raises(local_source.SourceChanged):
        root.complete(record.intent)


def test_targeted_replay_rejects_payload_corruption_without_foreground_decode(world, monkeypatch):
    root, _, _, _, journal = world
    request = _request()
    journal.prepare(request)
    journal.mark_attempted(request)
    journal.accept(request, _receipt(request))
    journal.complete(request)
    with sqlite3.connect(root.path) as conn:
        conn.execute('UPDATE typed_operations SET request=?', (b'{}',))
    with root._transaction() as conn:
        root._schema(conn)  # No terminal payload decoding on publication checks.
    with pytest.raises(local_source.SourceChanged, match='record_invalid'):
        journal.get(EPOCH, request.expectation.operation_id)


def test_concurrent_exact_preparation_and_completion_have_one_record(world):
    root, _, _, _, journal = world
    request = _request()
    barrier = threading.Barrier(3)
    errors, results = [], []
    def run():
        try:
            barrier.wait(timeout=10)
            record = journal.prepare(request)
            journal.mark_attempted(request)
            journal.accept(request, _receipt(request))
            journal.complete(request)
            results.append(record.intent.token)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(15)
        assert not thread.is_alive()
    assert not errors
    assert len(set(results)) == 1
    with root._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)


def test_private_boundary_failure_precedes_sqlite_creation(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise security.LocalSecurityError('privacy unavailable')
    monkeypatch.setattr(security, 'private_directory', fail)
    with pytest.raises(security.LocalSecurityError, match='privacy unavailable'):
        MutationCoordinator(tmp_path / 'home', 'mesh')
    assert not list(tmp_path.rglob('*.sqlite'))


def test_private_boundary_failure_precedes_journal_write(world, monkeypatch):
    root, _, _, _, journal = world
    original = security.verify_private_path
    def fail(path, *, directory):
        if path == root.path:
            raise security.LocalSecurityError('privacy unavailable')
        return original(path, directory=directory)
    monkeypatch.setattr(security, 'verify_private_path', fail)
    with pytest.raises(security.LocalSecurityError, match='privacy unavailable'):
        journal.prepare(_request())
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)


def test_windows_inherited_sidecar_is_protected_before_verify(world, monkeypatch):
    root = world[0]
    sidecar = Path(str(root.path) + '-wal')
    sidecar.write_bytes(b'')
    protected = set()
    original_protect, original_verify = security.protect_path, security.verify_private_path
    def protect(path, *, directory):
        protected.add(path)
        original_protect(path, directory=directory)
    def verify(path, *, directory):
        if path == sidecar and path not in protected:
            raise security.LocalSecurityError('inherited DACL')
        original_verify(path, directory=directory)
    monkeypatch.setattr(coordinator_module, '_WINDOWS', True)
    monkeypatch.setattr(security, 'protect_path', protect)
    monkeypatch.setattr(security, 'verify_private_path', verify)
    root._private_files()
    assert sidecar in protected
    sidecar.unlink()


def test_disappearing_sidecar_wrapped_oserror_is_safe(world, monkeypatch):
    root = world[0]
    sidecar = Path(str(root.path) + '-wal')
    sidecar.write_bytes(b'')
    original = security.verify_private_path
    def vanish(path, *, directory):
        if path == sidecar:
            sidecar.unlink()
            raise security.LocalSecurityError('gone') from FileNotFoundError()
        return original(path, directory=directory)
    monkeypatch.setattr(security, 'verify_private_path', vanish)
    root._private_files()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file mode evidence')
def test_private_file_modes(world):
    root = world[0]
    assert root.path.stat().st_mode & 0o077 == 0
    assert root.path.parent.stat().st_mode & 0o077 == 0


def test_pending_keyset_discovery_survives_restart_and_excludes_acknowledged(world):
    journal = world[-1]
    first, second, third = (_request(i) for i in range(1, 4))
    for request in (first, second, third):
        journal.prepare(request)
    journal.mark_attempted(second)
    journal.accept(second, _receipt(second))
    journal.complete(second)
    journal.mark_acknowledged(second)
    reopened = _reload(world)
    keys = reopened.pending_keys(limit=1)
    assert keys == ((EPOCH, first.expectation.operation_id),)
    assert reopened.pending_keys(limit=1, after=keys[-1]) == ((EPOCH, third.expectation.operation_id),)
    assert reopened.pending_keys(after=(EPOCH, third.expectation.operation_id)) == ()
    with pytest.raises(ValueError):
        reopened.pending_keys(limit=True)
    with pytest.raises(ValueError):
        reopened.pending_keys(after=('bad', 'bad'))


def test_node_principal_corruption_blocks_replay_even_with_old_digest(world):
    _, node, _, _, journal = world
    request = _request()
    journal.prepare(request)
    with sqlite3.connect(node.path) as conn:
        conn.execute('UPDATE node_identity SET principal=?', (str(uuid.uuid4()),))
    with pytest.raises((ValueError, sqlite3.DatabaseError), match='identity'):
        journal.mark_attempted(request)


def test_corrupt_active_count_rejected_before_selector_blobs_are_read(world):
    root, _, _, _, journal = world
    journal.prepare(_request())
    with sqlite3.connect(root.path) as conn:
        row = conn.execute('SELECT protocol_epoch,operation_id,token,state,request,receipt,selectors FROM typed_operations').fetchone()
        for index in range(2, 66):
            conn.execute('INSERT INTO typed_operations VALUES(?,?,?,?,?,?,?)',
                         (row[0], str(uuid.UUID(int=index)), f'{index:064x}', *row[3:]))
    statements = []
    with root._transaction() as conn:
        conn.set_trace_callback(statements.append)
        with pytest.raises(local_source.SourceChanged, match='active_budget'):
            root._schema(conn)
    assert not any('SELECT token,selectors FROM typed_operations' in query for query in statements)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink evidence')
def test_existing_database_symlink_is_rejected_before_sqlite_open(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh')
    saved = root.path.with_suffix('.saved')
    root.path.rename(saved)
    root.path.symlink_to(saved)
    before = saved.read_bytes()
    with pytest.raises(security.LocalSecurityError, match='symlink'):
        MutationCoordinator(tmp_path / 'home', 'mesh')
    assert saved.read_bytes() == before


def _member_receipt(request):
    expected = request.expectation
    raw = {name: getattr(expected, name) for name in (
        'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
    result = {'username': 'alice', 'claimed' if expected.kind == 'member_claim' else 'left': True}
    raw.update(schema_version=2, provider_digest='d' * 64, result=result)
    return decode_mutation_receipt(raw, expected)


@pytest.mark.parametrize('kind,args', [('member_leave', {}), ('member_claim', {'p_username': 'alice'})])
def test_membership_fences_all_stores_and_new_sources_until_receipt_completion(world, tmp_path, kind, args):
    root, node, first, first_definition, journal = world
    second = Store(tmp_path / 'second.sqlite')
    newcomer = Store(tmp_path / 'newcomer.sqlite')
    try:
        for store in (second, newcomer):
            local_source.initialize(store)
            source_selectors.initialize(store)
        root.register_store(second)
        generation = node.begin_candidate(created_ns=1)
        node.seal_candidate(generation, NodeInputBatch(), observed_ns=1)
        node.admit_candidate(generation)
        position = node.finalization_position(node.capture(NodeCaptureRequest(
            log_requests=(NodeLogRequest('elsewhere', 'alice'),))))
        definitions = [first_definition]
        for selector in (S('doc_exact', 'accounts/bob.json'), S('log_chat', 'elsewhere')):
            definition = source_selectors.definition(root.identity, (selector,), build=selector.kind)
            definitions.append(definition)
            with root.publication_gate(second, definition, node_position=position):
                local_source.publish(second, local_source.capture(second, definition.source), {'value': 1}, observed_ns=1)
        unregistered = 'legacy-unregistered'
        local_source.publish(second, local_source.capture(second, unregistered), {'value': 2}, observed_ns=2)
        before = local_source.capture(second, unregistered)
        request = _request(kind=kind, arguments=args)
        prepared = journal.prepare(request)
        assert prepared.intent.selectors == (S('root_all', '*'),)
        assert not local_source.capture(first, first_definition.source).ready
        for definition in definitions[1:]:
            assert not local_source.capture(second, definition.source).ready
        assert not local_source.capture(second, unregistered).ready
        assert local_source.capture(second, unregistered).revision == before.revision + 1
        root.register_store(newcomer)
        new_definition = source_selectors.definition(root.identity, (S('log_chat', 'brand-new'),))
        for store, definition in ((first, first_definition), (second, definitions[-1]), (newcomer, new_definition)):
            with pytest.raises(local_source.SourceChanged, match='source_mutation_pending'):
                with root.publication_gate(store, definition):
                    pytest.fail('whole-root pending publication escaped')
        assert _reload(world).prepare(request) == prepared
        journal.mark_attempted(request)
        journal.accept(request, _member_receipt(request))
        assert journal.complete(request).state == 'completed'
        assert not local_source.capture(first, first_definition.source).ready
        assert not local_source.capture(second, unregistered).ready
        assert local_source.capture(second, unregistered).revision == before.revision + 2
        assert not local_source.capture(newcomer, new_definition.source).ready
        with sqlite3.connect(node.path) as conn:
            assert conn.execute('SELECT state FROM node_mutation_fences').fetchall() == [('completed',)]
        # Success never permits a legacy mirror to restore readiness.
        with pytest.raises(local_source.SourceChanged, match='node_publication_required'):
            with root.publication_gate(newcomer, new_definition):
                pytest.fail('legacy publication after membership completion')
        assert not local_source.capture(newcomer, new_definition.source).ready
    finally:
        second.close()
        newcomer.close()


@pytest.mark.parametrize('failure', ['budget', 'revision'])
def test_membership_prepare_fails_before_provider_with_partial_node_fence(world, monkeypatch, failure):
    root, node, store, definition, journal = world
    if failure == 'budget':
        monkeypatch.setattr(source_selectors, 'MAX_SOURCES', 1)
        local_source.publish(store, local_source.capture(store, 'extra-unregistered'), {'value': 1}, observed_ns=1)
        match = 'fanout_budget'
    else:
        store._conn().execute('UPDATE local_sources SET revision=? WHERE source=?', (local_source.MAX, definition.source))
        store._conn().commit()
        match = 'revision_unavailable'
    with pytest.raises(local_source.SourceChanged, match=match):
        journal.prepare(_request(kind='member_leave', arguments={}))
    with root._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute('SELECT state FROM node_mutation_fences').fetchall() == [('active',)]


def test_membership_ambiguous_attempt_survives_restart_and_blocks_every_scope(world):
    root, _, store, _, journal = world
    request = _request(kind='member_leave', arguments={})
    journal.prepare(request)
    journal.mark_attempted(request)
    assert _reload(world).get_operation(request.expectation.operation_id).state == 'attempted'
    definition = source_selectors.definition(root.identity, (S('doc_exact', 'unrelated.json'),))
    with pytest.raises(local_source.SourceChanged, match='source_mutation_pending'):
        with root.publication_gate(store, definition):
            pytest.fail('ambiguous membership allowed publication')
    with pytest.raises(local_source.SourceChanged, match='receipt_required'):
        journal.complete(request)


def test_membership_completion_retry_after_node_commit_and_root_rollback(world, monkeypatch):
    root, node, _, _, journal = world
    request = _request(kind='member_leave', arguments={})
    journal.prepare(request)
    journal.mark_attempted(request)
    journal.accept(request, _member_receipt(request))
    original = root._retire
    def fail(conn, changes):
        raise RuntimeError('after node commit')
    monkeypatch.setattr(root, '_retire', fail)
    with pytest.raises(RuntimeError, match='after node commit'):
        journal.complete(request)
    with sqlite3.connect(node.path) as conn:
        assert conn.execute('SELECT state FROM node_mutation_fences').fetchall() == [('completed',)]
    assert _reload(world).get_operation(request.expectation.operation_id).state == 'accepted'
    monkeypatch.setattr(root, '_retire', original)
    assert journal.complete(request).state == 'completed'


def test_journal_four_to_five_upgrade_preserves_epoch_and_advances_retirement_revision(world):
    root, node, _, _, journal = world
    request = _request()
    record = journal.prepare(request)
    with sqlite3.connect(root.path) as conn:
        row = conn.execute('SELECT identity,epoch,revision FROM mutation_root').fetchone()
        conn.execute('ALTER TABLE mutation_root RENAME TO root_v5')
        conn.execute(coordinator_module._V4_ROOT_SCHEMA)
        conn.execute('INSERT INTO mutation_root VALUES(1,?,?,4,?)', row)
        conn.execute('DROP TABLE root_v5')
    reopened_root = MutationCoordinator(root.path.parent.parent, root.identity)
    with reopened_root._transaction() as conn:
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (4,)
    upgraded = OperationJournal(reopened_root, node, actor_id=ACTOR)
    assert upgraded.get_operation(request.expectation.operation_id) == record
    with reopened_root._transaction() as conn:
        assert conn.execute('SELECT identity,epoch,revision FROM mutation_root').fetchone() == (*row[:2], row[2] + 1)
        assert conn.execute('SELECT format FROM mutation_root').fetchone() == (5,)


@pytest.mark.parametrize('value', ['all', '**', '', '*/room'])
def test_mutated_root_scope_fails_closed(world, value):
    root, _, _, _, journal = world
    request = _request(kind='member_leave', arguments={})
    journal.prepare(request)
    with sqlite3.connect(root.path) as conn:
        conn.execute('UPDATE mutation_scopes SET value=?', (value,))
    with pytest.raises(local_source.SourceChanged, match='invalid_pending_selector'):
        journal.mark_attempted(request)


def test_explicit_application_key_retains_original_epoch(world):
    journal = world[-1]
    first = _request()
    original = journal.prepare_operation(first)
    next_epoch = prepare_mutation('mesh', str(uuid.uuid4()), first.expectation.operation_id, ACTOR,
                                 'doc_put', {'p_path': 'chats/room/meta.json', 'p_data': {'private': 'content'}})
    assert journal.prepare_operation(next_epoch) == original
    assert journal.get_operation(first.expectation.operation_id) == original
    assert _reload(world).get_operation(first.expectation.operation_id) == original
    assert journal.get_operation(str(uuid.uuid4())) is None
    changed = prepare_mutation('mesh', next_epoch.expectation.protocol_epoch, first.expectation.operation_id, ACTOR,
                              'doc_put', {'p_path': 'chats/room/meta.json', 'p_data': {'different': True}})
    with pytest.raises(local_source.SourceChanged, match='request_changed'):
        journal.prepare_operation(changed)


def test_application_operations_cannot_overtake_overlapping_intents(world):
    journal = world[-1]
    first = _request()
    journal.prepare_operation(first)
    journal.mark_attempted(first)
    assert journal.prepare_operation(first).state == 'attempted'
    with pytest.raises(local_source.SourceChanged, match='overlapping_intent'):
        journal.prepare_operation(_request(2))
    unrelated = _request(3, arguments={'p_path': 'accounts/bob.json', 'p_data': {}})
    assert journal.prepare_operation(unrelated).state == 'prepared'
    with pytest.raises(local_source.SourceChanged, match='overlapping_intent'):
        journal.prepare_operation(_request(4, kind='member_leave', arguments={}))
    journal.mark_attempted(unrelated)
    journal.accept(unrelated, _receipt(unrelated))
    journal.complete(unrelated)
    journal.accept(first, _receipt(first))
    journal.complete(first)
    member = _request(4, kind='member_leave', arguments={})
    assert journal.prepare_operation(member).state == 'prepared'
    with pytest.raises(local_source.SourceChanged, match='overlapping_intent'):
        journal.prepare_operation(_request(5, arguments={'p_path': 'entirely/new.json', 'p_data': {}}))


def test_application_operation_respects_legacy_pending_intent(world):
    root, _, _, _, journal = world
    intent = root.begin((S('doc_prefix', 'chats/room'),))
    with pytest.raises(local_source.SourceChanged, match='overlapping_intent'):
        journal.prepare_operation(_request())
    root.complete(intent)
    assert journal.prepare_operation(_request()).state == 'prepared'


def test_strict_preparation_scopes_operation_id_by_epoch(world):
    journal = world[-1]
    first = _request()
    second = prepare_mutation('mesh', str(uuid.uuid4()), first.expectation.operation_id, ACTOR,
                              'doc_put', {'p_path': 'another.json', 'p_data': {'new': True}})
    first_record = journal.prepare(first)
    second_record = journal.prepare(second)
    assert first_record.intent.token != second_record.intent.token
    restored = _reload(world)
    assert restored.prepare(first) == first_record
    assert restored.prepare(second) == second_record
    assert restored.get(EPOCH, first.expectation.operation_id) == first_record
    assert restored.get(second.expectation.protocol_epoch, second.expectation.operation_id) == second_record
    with pytest.raises(local_source.SourceChanged, match='duplicate_operation_id'):
        restored.get_operation(first.expectation.operation_id)
    with pytest.raises(local_source.SourceChanged, match='duplicate_operation_id'):
        restored.prepare_operation(first)


def test_document_inventory_keeps_completed_ack_retry_and_excludes_other_scopes(world):
    _, _, _, _, journal = world
    path = 'presence/alice.json'
    target = _request(arguments={'p_path': path, 'p_data': {'ns': 1}})
    journal.prepare(target)
    other = _request(2, arguments={'p_path': 'presence/bob.json', 'p_data': {}})
    journal.prepare(other)
    expected = ((EPOCH, target.expectation.operation_id),)
    assert journal.pending_document_keys(path) == expected
    journal.mark_attempted(target)
    journal.accept(target, _receipt(target))
    journal.complete(target)
    assert _reload(world).pending_document_keys(path) == expected
    journal.mark_acknowledged(target)
    assert journal.pending_document_keys(path) == ()


def test_document_inventory_is_bounded_even_for_completed_records(world, monkeypatch):
    _, _, _, _, journal = world
    request = _request()
    journal.prepare(request)
    journal.mark_attempted(request)
    journal.accept(request, _receipt(request))
    journal.complete(request)
    monkeypatch.setattr(module, 'MAX_PENDING', 0)
    with pytest.raises(local_source.SourceChanged, match='document_inventory_budget'):
        journal.pending_document_keys('chats/room/meta.json')


def test_document_inventory_checks_owner_and_rejects_invalid_path(world):
    root, _, _, _, journal = world
    with pytest.raises(ValueError):
        journal.pending_document_keys('../escape')
    with sqlite3.connect(root.path) as conn:
        conn.execute('UPDATE typed_operation_owner SET actor_id=?', (str(uuid.uuid4()),))
    with pytest.raises(local_source.SourceChanged):
        journal.pending_document_keys('presence/alice.json')


def test_node_publication_and_normal_page_capture_survive_journal_upgrade_and_restart(tmp_path):
    from agentbridge.mesh.local_page_source import LocalPageSource
    from agentbridge.mesh.node_input_publisher import NodeInputPublisher
    from agentbridge.node.admission import NodeDocument, NodeLogRow
    from agentbridge.node.client import LocalNodeClient
    from agentbridge.node.server import LocalNodeServer
    from agentbridge.store.source_publication import SourcePublisher

    identity = ReplicaIdentity(ENDPOINT, 'supabase://mesh', ACTOR, 'machine')
    transport = SupabaseTransport('mesh', env={'SUPABASE_URL': ENDPOINT}, client=object())
    root = MutationCoordinator(tmp_path / 'home', root_identity(transport))
    store = Store(tmp_path / 'client.sqlite')
    server = LocalNodeServer(tmp_path / 'service', identity).start()
    try:
        root.register_node(server.store)
        generation = server.store.begin_candidate(created_ns=1)
        server.store.seal_candidate(generation, NodeInputBatch(
            documents=(NodeDocument('chats/room/meta.json', 1, False,
                                    b'{"members":{"alice":true}}'),),
            log_rows=(NodeLogRow(1, 'room', 'alice@machine', json.dumps({
                'id': 'first', 'ns': 1, 'from': 'alice', 'kind': 'message',
                'text': 'node-observed',
            }).encode()),), documents_mode='replace', logs_mode='replace',
        ), observed_ns=1)
        server.store.admit_candidate(generation)
        client = LocalNodeClient(tmp_path / 'service', identity)
        publisher = NodeInputPublisher(root, store, client)
        reader = LocalPageSource(root, store, 'room')
        legacy = SourcePublisher(root, store, reader.definition)
        legacy.publish(legacy.capture(), {'chats/room/meta.json': {'legacy': True}}, observed_ns=1)
        old = reader.capture()
        assert old.source.ready and old.node_position is None

        OperationJournal(root, server.store, actor_id=ACTOR)
        assert not local_source.capture(store, reader.definition.source).ready
        with pytest.raises(local_source.SourceChanged, match='source_not_ready'):
            with reader.finalization(old):
                pytest.fail('pre-upgrade legacy cut finalized')
        captured = legacy.capture()  # Collection is permitted; admission is not.
        assert not captured.source.ready
        with pytest.raises(local_source.SourceChanged, match='node_publication_required'):
            legacy.publish(captured, {'chats/room/meta.json': {'legacy': True}}, observed_ns=2)

        result = publisher.publish('room')
        assert result.state == 'published'
        current = reader.capture()  # This real read path supplies no node_position.
        assert current.source.ready and current.node_position.admitted_generation == generation
        with reader.finalization(current):
            pass
        authority = reader.capture_authority(current)
        assert authority.documents.decoded_records() == [
            ('chats/room/meta.json', {'members': {'alice': True}}, False)]

        reopened = MutationCoordinator(tmp_path / 'home', root.identity)
        OperationJournal(reopened, server.store, actor_id=ACTOR)
        restored_reader = LocalPageSource(reopened, store, 'room')
        assert restored_reader.capture() == current
        with restored_reader.finalization(current):
            pass
        assert NodeInputPublisher(reopened, store, client).publish('room').state == 'unchanged'
        with pytest.raises(local_source.SourceChanged, match='node_publication_required'):
            with reopened.publication_gate(store, restored_reader.definition):
                pytest.fail('restarted legacy publisher admitted')
        assert restored_reader.capture() == current
    finally:
        server.stop()
        store.close()
