"""Typed outbox consumer settlement and stale-lease crash boundaries."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.store.db import Store
from agentbridge.store.outbox import OutboxWorker
from agentbridge.transport.outbox_mutations import OutboxMutationOwner


class Owner:
    def __init__(self):
        self.dispatches = []
        self.releases = []
        self.fail_release = False
        self.fail_dispatch = False

    def dispatch(self, item):
        self.dispatches.append((item.seq, item.owner_digest))
        if self.fail_dispatch:
            raise ValidationError('ambiguous typed operation')

    def release(self, item):
        self.releases.append((item.seq, item.owner_digest))
        if self.fail_release:
            raise OSError('release interrupted')


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'client.sqlite')
    try:
        yield value
    finally:
        value.close()


def test_typed_worker_marks_sent_releases_and_deletes_exact_row(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    owner = Owner()
    legacy = []
    worker = OutboxWorker(store, {'append_log': lambda *_: legacy.append(1)},
                          typed_owner=owner)
    assert worker.flush_once() == 1
    assert len(owner.dispatches) == len(owner.releases) == 1
    assert owner.dispatches == owner.releases
    assert not legacy
    assert store.outbox_owner_row(seq) is None


def test_sent_tombstone_survives_release_crash_and_restart_without_redispatch(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    owner = Owner()
    owner.fail_release = True
    worker = OutboxWorker(store, {}, typed_owner=owner)
    assert worker.flush_once() == 0
    assert store.outbox_owner_row(seq)[0] == 'sent'
    assert len(owner.dispatches) == 1
    owner.fail_release = False
    restarted = OutboxWorker(store, {}, typed_owner=owner)
    assert restarted.flush_once() == 1
    assert len(owner.dispatches) == 1
    assert len(owner.releases) == 2
    assert store.outbox_owner_row(seq) is None


def test_sent_tombstone_survives_cleanup_crash_after_ticket_release(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    owner = Owner()
    attempts = []

    def cleanup(_target, _payload):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('spool cleanup interrupted')

    worker = OutboxWorker(store, {}, typed_owner=owner,
                          success_hooks={'append_log': cleanup})
    assert worker.flush_once() == 0
    assert store.outbox_owner_row(seq)[0] == 'sent'
    assert len(owner.dispatches) == len(owner.releases) == 1
    assert OutboxWorker(store, {}, typed_owner=owner,
                        success_hooks={'append_log': cleanup}).flush_once() == 1
    assert len(owner.dispatches) == 1
    assert len(owner.releases) == len(attempts) == 2


def test_stale_lease_cannot_mark_sent_or_clear_new_claim(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    old = store.outbox_claim_due(lease_s=0.0)[0]
    current = store.outbox_claim_due()[0]
    assert old.owner_digest == current.owner_digest
    assert not store.outbox_mark_sent(old)
    assert not store.outbox_retry_claim(old, 'late failure', 1.0)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT state,lease_ns FROM outbox WHERE seq=?',
                            (seq,)).fetchone() == ('pending', current.lease_ns)
    assert store.outbox_mark_sent(current)
    assert store.outbox_owner_row(seq)[0] == 'sent'


def test_sent_row_blocks_later_same_target_until_release_and_cleanup(store):
    first = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    second = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm2'})
    claim = store.outbox_claim_due()[0]
    assert claim.seq == first and store.outbox_mark_sent(claim)
    assert store.outbox_claim_due() == []
    sent = store.outbox_sent_items()[0]
    assert store.outbox_finalize_sent(sent)
    assert [item.seq for item in store.outbox_claim_due()] == [second]


def test_unbound_worker_never_processes_a_typed_sent_tombstone(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    assert store.outbox_mark_sent(store.outbox_claim_due()[0])
    with pytest.raises(RuntimeError, match='typed outbox owner is unavailable'):
        OutboxWorker(store, {'append_log': lambda *_: pytest.fail('legacy fallback')}).flush_once()
    assert store.outbox_owner_row(seq)[0] == 'sent'


def test_typed_validation_failure_retries_without_deadletter(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    owner = Owner()
    owner.fail_dispatch = True
    worker = OutboxWorker(store, {}, typed_owner=owner)
    assert worker.flush_once() == 0
    assert store.outbox_owner_row(seq)[0] == 'pending'
    assert store.outbox_counts() == {'pending': 1}
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT attempts FROM outbox WHERE seq=?', (seq,)).fetchone() == (1,)


def test_exact_payload_digest_changes_with_persisted_json(store):
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    claim = store.outbox_claim_due()[0]
    original = claim.owner_digest
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE outbox SET payload=? WHERE seq=?', ('{"id":"changed"}', seq))
    assert store.outbox_owner_row(seq)[1] != original
    assert not store.outbox_mark_sent(claim)


def test_owner_context_rejects_missing_changed_and_stale_claims(store):
    store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    item = store.outbox_claim_due()[0]
    journal = SimpleNamespace(lifecycle_enabled=lambda: True)
    writer = SimpleNamespace(driver=SimpleNamespace(journal=journal))
    transport = SimpleNamespace(_bound_writer=lambda: writer)
    seen = []

    def dispatch(_target, _payload, *, _context):
        seen.append(_context._arguments('room', 'alice@machine', {'id': 'm1'}))

    owner = OutboxMutationOwner(transport, store, dispatch)
    owner.dispatch(item)
    assert seen == [{'p_chat': 'room', 'p_log': 'alice@machine',
                     'p_record': {'id': 'm1'}}]
    for wrong in (
        replace(item, payload_json=''),
        replace(item, payload={'id': 'changed'}),
        replace(item, payload_json='{"id":"changed"}',
                payload={'id': 'changed'}),
        replace(item, target='elsewhere|alice@machine'),
    ):
        with pytest.raises(ValueError):
            owner.dispatch(wrong)
    assert store.outbox_mark_sent(item)
    with pytest.raises(ValueError, match='no longer current'):
        owner.dispatch(item)
    assert len(seen) == 1


def test_owner_releases_manifest_legs_before_append_for_outer_v1_payload(store):
    payload = {'v': 1, 'envelope': {'id': 'm1'},
               'attachments': [{'v': 2, 'blob_id': 'f2-sealed'}]}
    store.outbox_add('append_log', 'room|alice@machine', payload)
    item = store.outbox_claim_due()[0]
    calls = []
    journal = SimpleNamespace(
        lifecycle_enabled=lambda: True,
        consume_outbox_leg=lambda _store, seq, leg, digest:
            calls.append(('consume', seq, leg, digest)),
        finish_outbox=lambda _store, seq, digest:
            calls.append(('finish', seq, digest)),
    )
    writer = SimpleNamespace(driver=SimpleNamespace(journal=journal))
    transport = SimpleNamespace(_bound_writer=lambda: writer)
    owner = OutboxMutationOwner(transport, store, lambda *_a, **_kw: None)
    assert store.outbox_mark_sent(item)
    sent = store.outbox_sent_items()[0]
    owner.release(sent)
    assert calls == [
        ('consume', sent.seq, 'manifest:f2-sealed', sent.owner_digest),
        ('consume', sent.seq, 'append', sent.owner_digest),
        ('finish', sent.seq, sent.owner_digest),
    ]


@pytest.mark.parametrize('payload', [
    {'v': 0, 'envelope': {'id': 'm1'},
     'attachments': [{'v': 2, 'blob_id': 'f2-sealed'}]},
    {'v': 1, 'envelope': {'id': 'm1'}, 'attachments': []},
    {'v': 1, 'envelope': {'id': 'm1'},
     'attachments': [{'v': 1, 'blob_id': 'f2-sealed'}]},
    {'v': 1, 'envelope': {'id': 'm1'},
     'attachments': [{'v': 2, 'blob_id': 'f2-sealed'},
                     {'v': 2, 'blob_id': 'f2-sealed'}]},
    {'v': 1, 'envelope': 'wrong',
     'attachments': [{'v': 2, 'blob_id': 'f2-sealed'}]},
])
def test_owner_rejects_malformed_attachment_wrapper_before_dispatch_or_release(
        store, payload):
    store.outbox_add('append_log', 'room|alice@machine', payload)
    item = store.outbox_claim_due()[0]
    calls = []
    journal = SimpleNamespace(
        lifecycle_enabled=lambda: True,
        consume_outbox_leg=lambda *_a: calls.append('consume'),
        finish_outbox=lambda *_a: calls.append('finish'),
    )
    writer = SimpleNamespace(driver=SimpleNamespace(journal=journal))
    transport = SimpleNamespace(_bound_writer=lambda: writer)
    owner = OutboxMutationOwner(transport, store, lambda *_a, **_kw: calls.append('dispatch'))
    with pytest.raises(ValueError):
        owner.dispatch(item)
    assert store.outbox_mark_sent(item)
    with pytest.raises(ValueError):
        owner.release(store.outbox_sent_items()[0])
    assert calls == []


def test_outbox_commits_use_full_sync_and_restore_cache_connection(store, monkeypatch):
    connection = store._conn()
    observed = []

    class RecordingConnection:
        def __enter__(self):
            connection.__enter__()
            return self

        def __exit__(self, *args):
            return connection.__exit__(*args)

        @property
        def in_transaction(self):
            return connection.in_transaction

        def execute(self, *args, **kwargs):
            return connection.execute(*args, **kwargs)

        def commit(self):
            observed.append(connection.execute('PRAGMA synchronous').fetchone()[0])
            connection.commit()

        def rollback(self):
            connection.rollback()

    monkeypatch.setattr(store, '_conn', lambda: RecordingConnection())
    assert connection.execute('PRAGMA synchronous').fetchone() == (1,)
    seq = store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    assert observed == [2]
    store.cache_and_outbox_add(
        'room', {'id': 'm2', 'ns': 2, 'kind': 'message'},
        'append_log', 'other|alice@machine', {'id': 'm2'})
    store.cache_doc_and_outbox_add(
        'chats/room/state', {'v': 1}, 'append_log',
        'third|alice@machine', {'id': 'm3'})
    store.cache_docs_and_outbox_add_many(
        {'chats/room/extra': {'v': 2}},
        [('append_log', 'fourth|alice@machine', {'id': 'm4'})])
    assert observed == [2] * 4
    assert connection.execute('PRAGMA synchronous').fetchone() == (1,)
    item = next(item for item in store.outbox_claim_due() if item.seq == seq)
    assert store.outbox_mark_sent(item)
    assert observed == [2] * 5
    sent = store.outbox_sent_items()[0]
    assert store.outbox_finalize_sent(sent)
    assert observed == [2] * 6
    assert connection.execute('PRAGMA synchronous').fetchone() == (1,)
    assert store.outbox_owner_row(seq) is None


def test_outbox_sync_restores_after_rollback_and_rejects_nested_transaction(store):
    conn = store._conn()
    with pytest.raises(sqlite3.IntegrityError):
        with store._outbox_transaction() as c:
            c.execute('INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)',
                      ('append_log', 'room|alice@machine', '{}', 1))
            c.execute('INSERT INTO outbox(seq,kind,target,payload,created_ns) '
                      'VALUES(1,?,?,?,?)', ('append_log', 'room|alice@machine', '{}', 1))
    assert conn.execute('PRAGMA synchronous').fetchone() == (1,)
    assert store.outbox_counts() == {}
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(RuntimeError, match='cannot nest'):
            store.outbox_add('append_log', 'room|alice@machine', {'id': 'm1'})
    finally:
        conn.rollback()
    assert conn.execute('PRAGMA synchronous').fetchone() == (1,)
