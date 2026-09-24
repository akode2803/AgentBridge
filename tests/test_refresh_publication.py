"""Admitted snapshots survive candidate work and transactional admit failure."""
from __future__ import annotations

import sqlite3

import pytest

from agentbridge.mesh import local_input_runtime, presence_input_runtime
from agentbridge.store import local_source, presence_publication, staged_publication, staged_source
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import owned_transport

import test_staged_runtime as chat_rig


def test_chat_pointer_swap_rollback_and_failure_record_failure_keep_old_ready(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    chat_rig._seed(folder)
    mesh = chat_rig._mesh(tmp_path, folder, 'atomic-chat')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(chat_rig.CHAT)
        reader, previous, index = runtime.inputs(chat_rig.CHAT)
        folder.put_doc(chat_rig.META,
                       {'id': chat_rig.CHAT, 'members': ['alice'], 'revision': 2})
        advance = staged_publication.owner._advance
        calls = 0

        def fail_after_retirement(conn, source, revision):
            nonlocal calls
            calls += 1
            advance(conn, source, revision)
            if calls == 2:  # collection claim commits; admission must roll back
                raise sqlite3.OperationalError('disk full after transactional retirement')

        monkeypatch.setattr(staged_publication.owner, '_advance', fail_after_retirement)
        monkeypatch.setattr(local_input_runtime.local_source, 'record_failure',
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('failure record not durable')))
        with pytest.raises(OSError, match='failure record not durable'):
            runtime.ingest(chat_rig.CHAT)
        active_reader, active, active_index = runtime.inputs(chat_rig.CHAT)
        assert active.source.ready and active.source.raw == previous.source.raw
        assert active_index == index
        assert active_reader.capture_authority(active).documents.document(chat_rig.META)['id'] == chat_rig.CHAT
    finally:
        mesh.close()


def test_presence_pointer_swap_rollback_and_failure_record_failure_keep_old_ready(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    folder.put_doc('presence/bob.json', {'user': 'bob', 'last_seen_ns': 5})
    owned = owned_transport(folder, tmp_path / 'owner')
    store = Store(tmp_path / 'store.sqlite')
    runtime = presence_input_runtime.PresenceInputRuntime(owned, store)
    try:
        assert runtime.ingest().ready
        prior = runtime.inputs(('bob',))
        folder.put_doc('presence/bob.json', {'user': 'bob', 'last_seen_ns': 9})
        advance = presence_publication.owner._advance
        calls = 0

        def fail_after_retirement(conn, source, revision):
            nonlocal calls
            calls += 1
            advance(conn, source, revision)
            if calls == 2:
                raise sqlite3.OperationalError('disk full after transactional retirement')

        monkeypatch.setattr(presence_publication.owner, '_advance', fail_after_retirement)
        monkeypatch.setattr(presence_input_runtime.local_source, 'record_failure',
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('failure record not durable')))
        with pytest.raises(OSError, match='failure record not durable'):
            runtime.ingest()
        assert runtime.inputs(('bob',))[1].source.ready
        assert runtime.inputs(('bob',))[1].source.raw == prior[1].source.raw
        assert runtime.inputs(('bob',))[2].floors == (('bob', 5),)
    finally:
        runtime.stop()
        store.close()
        owned.close()


@pytest.mark.parametrize('kind', ['chat', 'presence'])
def test_post_pointer_ready_failure_rolls_back_old_admission_across_reopen(
        tmp_path, monkeypatch, kind):
    folder = FolderTransport(tmp_path / 'provider')
    if kind == 'chat':
        chat_rig._seed(folder)
        owner = chat_rig._mesh(tmp_path, folder, 'late-chat')
        runtime, store = owner.local_inputs, owner.store
        assert runtime.ingest(chat_rig.CHAT)
        original = runtime.inputs(chat_rig.CHAT)[1].source.raw
        folder.put_doc(chat_rig.META,
                       {'id': chat_rig.CHAT, 'members': ['alice'], 'revision': 3})
        module = staged_publication
        handler = local_input_runtime.local_source
    else:
        folder.put_doc('presence/bob.json', {'user': 'bob', 'last_seen_ns': 5})
        owner = owned_transport(folder, tmp_path / 'owner')
        store = Store(tmp_path / 'store.sqlite')
        runtime = presence_input_runtime.PresenceInputRuntime(owner, store)
        assert runtime.ingest().ready
        original = runtime.inputs(('bob',))[1].source.raw
        folder.put_doc('presence/bob.json', {'user': 'bob', 'last_seen_ns': 9})
        module = presence_publication
        handler = presence_input_runtime.local_source
    capture = module.owner.capture_in_transaction
    seen_new_ready = []

    def fail_after_pointer_ready(conn, target_store, source):
        value = capture(conn, target_store, source)
        if value.ready and value.raw != original:
            seen_new_ready.append(value.raw)
            raise sqlite3.OperationalError('failure after pointer and ready update')
        return value

    monkeypatch.setattr(module.owner, 'capture_in_transaction', fail_after_pointer_ready)
    monkeypatch.setattr(handler, 'record_failure',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('failure record not durable')))
    try:
        with pytest.raises(OSError, match='failure record not durable'):
            runtime.ingest(chat_rig.CHAT) if kind == 'chat' else runtime.ingest()
        assert seen_new_ready
        assert (runtime.inputs(chat_rig.CHAT)[1].source.raw if kind == 'chat'
                else runtime.inputs(('bob',))[1].source.raw) == original
    finally:
        runtime.stop()
        if kind == 'chat':
            owner.close()
        else:
            store.close()
            owner.close()
        monkeypatch.setattr(module.owner, 'capture_in_transaction', capture)
    if kind == 'chat':
        reopened = chat_rig._mesh(tmp_path, FolderTransport(folder.root), 'late-chat')
        try:
            reader, receipt, index = reopened.local_inputs.inputs(chat_rig.CHAT)
            assert receipt.source.ready and receipt.source.raw == original
            assert index.source == original
            assert reader.capture_authority(receipt).documents.document(chat_rig.META)['id'] == chat_rig.CHAT
        finally:
            reopened.close()
    else:
        reopened_store = Store(tmp_path / 'store.sqlite')
        reopened_owner = owned_transport(FolderTransport(folder.root), tmp_path / 'owner')
        reopened = presence_input_runtime.PresenceInputRuntime(reopened_owner, reopened_store)
        try:
            assert reopened.inputs(('bob',))[1].source.raw == original
            assert reopened.inputs(('bob',))[2].floors == (('bob', 5),)
        finally:
            reopened.stop()
            reopened_store.close()
            reopened_owner.close()


def test_failed_external_mutation_during_chat_collection_remains_pending(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    chat_rig._seed(folder)
    mesh = chat_rig._mesh(tmp_path, folder, 'mutation-chat')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(chat_rig.CHAT)
        original_collect = local_input_runtime.collect_document_batches

        def cross(transport, definition, *, consume, **kwargs):
            def sink(batch):
                consume(batch)
                monkeypatch.setattr(folder, 'put_doc',
                                    lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('external write failed')))
                mesh.tx.put_doc(chat_rig.META,
                                {'id': chat_rig.CHAT, 'members': ['alice'], 'revision': 2})
            return original_collect(transport, definition, consume=sink,
                                    batch_documents=1, **kwargs)

        monkeypatch.setattr(local_input_runtime, 'collect_document_batches', cross)
        with pytest.raises(local_source.SourceChanged, match='source_mutation_pending'):
            runtime.ingest(chat_rig.CHAT)
        assert not runtime.health(chat_rig.CHAT)['ready']
        with sqlite3.connect(runtime.coordinator.path) as conn:
            assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0] >= 1
        with pytest.raises(local_source.SourceChanged):
            runtime.inputs(chat_rig.CHAT)
    finally:
        mesh.close()


def test_stale_candidate_and_failure_handler_do_not_retire_newer_chat_winner(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    chat_rig._seed(folder)
    mesh = chat_rig._mesh(tmp_path, folder, 'stale-chat')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(chat_rig.CHAT)
        old = runtime.inputs(chat_rig.CHAT)[1].source
        definition = runtime.reader(chat_rig.CHAT).definition
        candidate = staged_source.begin(mesh.store, definition.source, chat_rig.CHAT,
                                        expected=old)
        staged_source.finish(mesh.store, candidate)
        folder.put_doc(chat_rig.META,
                       {'id': chat_rig.CHAT, 'members': ['alice'], 'revision': 2})
        assert runtime.ingest(chat_rig.CHAT)
        winner = runtime.inputs(chat_rig.CHAT)[1].source
        with runtime.coordinator.publication_gate(mesh.store, definition):
            with pytest.raises(local_source.SourceChanged):
                staged_publication.admit(mesh.store, old, candidate, observed_ns=1)
            assert not local_source.record_failure(mesh.store, definition.source,
                                                   reason='unavailable', expected=old)
        assert runtime.inputs(chat_rig.CHAT)[1].source == winner
    finally:
        mesh.close()


def test_live_builder_is_superseded_by_new_claim_without_retiring_old_snapshot(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    chat_rig._seed(folder)
    mesh = chat_rig._mesh(tmp_path, folder, 'superseded-chat')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(chat_rig.CHAT)
        definition = runtime.reader(chat_rig.CHAT).definition
        original = runtime.inputs(chat_rig.CHAT)[1].source.raw
        with runtime.coordinator.publication_gate(mesh.store, definition):
            claim_a = local_source.claim_collection(mesh.store,
                local_source.capture(mesh.store, definition.source))
        stage_a = staged_source.begin(mesh.store, definition.source, chat_rig.CHAT,
                                      expected=claim_a)
        staged_source.append(mesh.store, stage_a, {
            chat_rig.META: {'id': chat_rig.CHAT, 'members': ['alice']},
        })
        with runtime.coordinator.publication_gate(mesh.store, definition):
            claim_b = local_source.claim_collection(mesh.store,
                local_source.capture(mesh.store, definition.source))
        stage_b = staged_source.begin(mesh.store, definition.source, chat_rig.CHAT,
                                      expected=claim_b)
        assert claim_a.revision < claim_b.revision
        assert runtime.inputs(chat_rig.CHAT)[1].source.raw == original
        with pytest.raises(staged_source.StageChanged):
            staged_source.append(mesh.store, stage_a,
                                 {'users/alice.json': {'name': 'alice'}})
        assert not local_source.record_failure(mesh.store, definition.source,
                                               reason='unavailable', expected=claim_a)
        staged_source.append(mesh.store, stage_b, {
            chat_rig.META: {'id': chat_rig.CHAT, 'members': ['alice']},
            'users/alice.json': {'name': 'alice'},
        })
        staged_source.finish(mesh.store, stage_b)
        with runtime.coordinator.publication_gate(mesh.store, definition):
            winner, _index = staged_publication.admit(mesh.store, claim_b, stage_b,
                                                       observed_ns=123)
        assert winner.ready and winner.raw != original
        assert runtime.inputs(chat_rig.CHAT)[1].source == winner
        with runtime.coordinator.publication_gate(mesh.store, definition):
            with pytest.raises(local_source.SourceChanged):
                staged_publication.admit(mesh.store, claim_a, stage_a, observed_ns=124)
    finally:
        mesh.close()


def test_pending_mutation_forbids_new_collection_claim(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    chat_rig._seed(folder)
    mesh = chat_rig._mesh(tmp_path, folder, 'pending-claim')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(chat_rig.CHAT)
        definition = runtime.reader(chat_rig.CHAT).definition
        before = local_source.capture(mesh.store, definition.source)
        from agentbridge.store.source_selectors import Selector

        intent = runtime.coordinator.begin((Selector('doc_exact', chat_rig.META),))
        try:
            with pytest.raises(local_source.SourceChanged, match='source_mutation_pending'):
                with runtime.coordinator.publication_gate(mesh.store, definition):
                    local_source.claim_collection(mesh.store, before)
            assert not local_source.capture(mesh.store, definition.source).ready
        finally:
            runtime.coordinator.complete(intent)
    finally:
        mesh.close()


def test_post_admission_cleanup_failure_cannot_retire_new_winner(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    chat_rig._seed(folder)
    mesh = chat_rig._mesh(tmp_path, folder, 'cleanup-fault')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(chat_rig.CHAT)
        old = runtime.inputs(chat_rig.CHAT)[1].source.raw
        folder.put_doc(chat_rig.META,
                       {'id': chat_rig.CHAT, 'members': ['alice'], 'revision': 2})
        monkeypatch.setattr(staged_source, 'retire_generation',
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('cleanup failed')))
        with pytest.raises(OSError, match='cleanup failed'):
            runtime.ingest(chat_rig.CHAT)
        assert runtime.health(chat_rig.CHAT)['ready']
        reader, current, index = runtime.inputs(chat_rig.CHAT)
        assert current.source.ready and current.source.raw != old
        assert index.source == current.source.raw
        assert reader.capture_authority(current).documents.document(chat_rig.META)['revision'] == 2
    finally:
        mesh.close()
