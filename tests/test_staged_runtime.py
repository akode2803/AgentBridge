"""Adversarial end-to-end admission of streaming local document inputs."""
from __future__ import annotations

import time

import pytest

from agentbridge.mesh import local_input_runtime
from agentbridge.mesh.service import Mesh
from agentbridge.store import local_source, staged_publication, staged_source
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.raw_documents import RawCollectionUnavailable


CHAT = 'room'
META = 'chats/room/meta.json'


def _mesh(tmp_path, transport, label):
    return Mesh(transport, 'alice', label, home=tmp_path / (label + '-home'),
                store_path=tmp_path / (label + '.sqlite'), local_inputs=True)


def _seed(folder):
    folder.put_doc(META, {'id': CHAT, 'members': ['alice']})
    folder.put_doc('users/alice.json', {'name': 'alice'})


def test_over_twenty_thousand_overlay_edits_admit_and_stable_repoll(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    edits = folder.root / 'chats' / CHAT / 'overlays' / 'edits'
    edits.mkdir(parents=True)
    for n in range(20_031):
        (edits / f'{n:05}.json').write_bytes(b'{}')
    mesh = _mesh(tmp_path, folder, 'large')
    try:
        assert mesh.local_inputs.ingest(CHAT) is True
        reader, receipt, index = mesh.local_inputs.inputs(CHAT)
        with reader._read(receipt) as (conn, _receipt):
            count = conn.execute('SELECT count(*) FROM document_observation_records '
                                 'WHERE source_id=?', (receipt.source.raw.source_id,)).fetchone()[0]
        assert count >= 20_033
        assert mesh.local_inputs.ingest(CHAT) is False
        _reader, stable, same_index = mesh.local_inputs.inputs(CHAT)
        assert stable.source.raw == receipt.source.raw
        assert same_index == index
    finally:
        mesh.close()


def test_more_than_sixteen_megabytes_folder_and_cached_parity(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    # Encoded source exceeds the legacy 16 MiB whole-collection ceiling while
    # each record remains far below the independent per-document ceiling.
    payload = 'x' * (70 * 1024)
    for n in range(256):
        folder.put_doc(f'users/bulk-{n:03}.json', {'body': payload})
    cached_transport = CachingTransport(FolderTransport(folder.root), auto_refresh=False)
    cached_transport.refresh()
    direct, cached = (_mesh(tmp_path, folder, 'direct'),
                      _mesh(tmp_path, cached_transport, 'cached'))
    try:
        assert direct.local_inputs.ingest(CHAT)
        assert cached.local_inputs.ingest(CHAT)
        direct_reader, direct_receipt, _ = direct.local_inputs.inputs(CHAT)
        cached_reader, cached_receipt, _ = cached.local_inputs.inputs(CHAT)
        with direct_reader._read(direct_receipt) as (conn, _receipt):
            direct_rows = conn.execute('SELECT path,payload FROM document_observation_records '
                                       'WHERE source_id=? ORDER BY path',
                                       (direct_receipt.source.raw.source_id,)).fetchall()
        with cached_reader._read(cached_receipt) as (conn, _receipt):
            cached_rows = conn.execute('SELECT path,payload FROM document_observation_records '
                                       'WHERE source_id=? ORDER BY path',
                                       (cached_receipt.source.raw.source_id,)).fetchall()
        assert direct_rows == cached_rows
        assert sum(len(row[1].encode() if isinstance(row[1], str) else row[1])
                   for row in direct_rows) > 16 * 1024 * 1024
    finally:
        cached.close()
        direct.close()


def test_collector_failure_after_provisional_batch_never_admits_old_ready(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    mesh = _mesh(tmp_path, folder, 'failure')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(CHAT)
        old = runtime.inputs(CHAT)[1].source.raw
        original = local_input_runtime.collect_document_batches
        delivered = []

        def fail_after_first(transport, definition, *, consume, **limits):
            def provisional(batch):
                consume(batch)
                delivered.append(len(batch))
                raise RawCollectionUnavailable('interrupted_after_batch')
            return original(transport, definition, consume=provisional,
                            batch_documents=1, **limits)

        monkeypatch.setattr(local_input_runtime, 'collect_document_batches', fail_after_first)
        with pytest.raises(RawCollectionUnavailable, match='interrupted_after_batch'):
            runtime.ingest(CHAT)
        assert delivered
        assert not runtime.health(CHAT)['ready']
        assert local_source.capture(mesh.store, runtime.reader(CHAT).definition.source).raw == old
    finally:
        mesh.close()


def test_local_mutation_between_batches_rejects_staged_admission(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    mesh = _mesh(tmp_path, folder, 'movement')
    try:
        runtime = mesh.local_inputs
        original = local_input_runtime.collect_document_batches
        mutated = []

        def mutate_after_first(transport, definition, *, consume, **limits):
            def crossing(batch):
                consume(batch)
                if not mutated:
                    mutated.append(True)
                    mesh.tx.put_doc(META, {'id': CHAT, 'members': ['alice'], 'revision': 2})
            return original(transport, definition, consume=crossing,
                            batch_documents=1, **limits)

        monkeypatch.setattr(local_input_runtime, 'collect_document_batches', mutate_after_first)
        with pytest.raises(local_source.SourceChanged):
            runtime.ingest(CHAT)
        assert mutated and not runtime.health(CHAT)['ready']
    finally:
        mesh.close()


def test_local_mutation_at_admission_rejects_staged_result(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    mesh = _mesh(tmp_path, folder, 'admission')
    try:
        runtime = mesh.local_inputs
        original = staged_publication.identical
        crossed = []

        def mutation_after_comparison(*args, **kwargs):
            result = original(*args, **kwargs)
            if not crossed:
                crossed.append(True)
                mesh.tx.put_doc(META, {'id': CHAT, 'members': ['alice'], 'revision': 3})
            return result

        monkeypatch.setattr(staged_publication, 'identical', mutation_after_comparison)
        with pytest.raises(local_source.SourceChanged):
            runtime.ingest(CHAT)
        assert crossed and not runtime.health(CHAT)['ready']
    finally:
        mesh.close()


def test_pointer_swap_rejects_old_receipt_and_preserves_unchanged_identity(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    mesh = _mesh(tmp_path, folder, 'swap')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(CHAT)
        reader, prior, old_index = runtime.inputs(CHAT)
        assert runtime.ingest(CHAT) is False
        _reader, same, same_index = runtime.inputs(CHAT)
        assert (same.source.raw, same_index) == (prior.source.raw, old_index)

        mesh.tx.put_doc(META, {'id': CHAT, 'members': ['alice'], 'revision': 2})
        assert runtime.ingest(CHAT)
        _reader, current, new_index = runtime.inputs(CHAT)
        assert current.source.raw != prior.source.raw
        assert new_index.source == current.source.raw
        with pytest.raises(local_source.SourceChanged):
            reader.capture_authority(prior, ('alice',))
        with pytest.raises(local_source.SourceChanged):
            reader.capture_page(prior, old_index)
        assert reader.capture_authority(current).documents.document(META)['revision'] == 2
    finally:
        mesh.close()


def test_successful_replacement_records_deletion_as_absence(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    folder.put_doc('users/bob.json', {'name': 'bob'})
    mesh = _mesh(tmp_path, folder, 'deletion')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(CHAT)
        reader, first, _index = runtime.inputs(CHAT)
        assert reader.capture_authority(first, ('bob',)).documents.document('users/bob.json') == {'name': 'bob'}
        mesh.tx.delete_doc('users/bob.json')
        assert runtime.ingest(CHAT)
        reader, second, _index = runtime.inputs(CHAT)
        assert second.source.raw != first.source.raw
        assert reader.capture_authority(second, ('bob',)).documents.document('users/bob.json') is None
    finally:
        mesh.close()


def test_partial_failed_replacement_cannot_expose_missing_subset(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    folder.put_doc('users/bob.json', {'name': 'bob'})
    mesh = _mesh(tmp_path, folder, 'partial')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(CHAT)
        reader, prior, _index = runtime.inputs(CHAT)
        original = local_input_runtime.collect_document_batches
        delivered = []

        def break_after_batch(transport, definition, *, consume, **limits):
            def sink(batch):
                consume(batch)
                delivered.append(True)
                raise RawCollectionUnavailable('incomplete_scan')
            return original(transport, definition, consume=sink,
                            batch_documents=1, **limits)

        monkeypatch.setattr(local_input_runtime, 'collect_document_batches', break_after_batch)
        with pytest.raises(RawCollectionUnavailable, match='incomplete_scan'):
            runtime.ingest(CHAT)
        assert delivered and not runtime.health(CHAT)['ready']
        with pytest.raises(local_source.SourceChanged):
            reader.capture_authority(prior, ('bob',))
        with pytest.raises(local_source.SourceChanged):
            runtime.inputs(CHAT)
    finally:
        mesh.close()
    reopened = _mesh(tmp_path, FolderTransport(folder.root), 'partial')
    try:
        assert not reopened.local_inputs.health(CHAT)['ready']
        with pytest.raises(local_source.SourceChanged):
            reopened.local_inputs.inputs(CHAT)
        monkeypatch.setattr(local_input_runtime, 'collect_document_batches', original)
        assert reopened.local_inputs.ingest(CHAT) is False
        reader, receipt, _index = reopened.local_inputs.inputs(CHAT)
        assert reader.capture_authority(receipt, ('bob',)).documents.document('users/bob.json') == {'name': 'bob'}
    finally:
        reopened.close()


def test_interrupted_stage_persists_unready_across_reopen(tmp_path, monkeypatch):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    mesh = _mesh(tmp_path, folder, 'crash')
    original_collect = local_input_runtime.collect_document_batches
    original_abort, original_cleanup = staged_source.abort, staged_source.cleanup
    try:
        assert mesh.local_inputs.ingest(CHAT)
        old = mesh.local_inputs.inputs(CHAT)[1].source.raw
        delivered = []

        def interrupt(transport, definition, *, consume, **limits):
            def sink(batch):
                consume(batch)
                delivered.append(True)
                raise KeyboardInterrupt('simulated process interruption')
            return original_collect(transport, definition, consume=sink,
                                    batch_documents=1, **limits)

        monkeypatch.setattr(local_input_runtime, 'collect_document_batches', interrupt)
        # Model process loss before best-effort candidate cleanup, rather than
        # treating the ordinary exception handler as crash recovery evidence.
        monkeypatch.setattr(staged_source, 'abort', lambda *_args, **_kwargs: None)
        monkeypatch.setattr(staged_source, 'cleanup', lambda *_args, **_kwargs: None)
        with pytest.raises(KeyboardInterrupt):
            mesh.local_inputs.ingest(CHAT)
        assert delivered
    finally:
        mesh.close()
    monkeypatch.setattr(local_input_runtime, 'collect_document_batches', original_collect)
    monkeypatch.setattr(staged_source, 'abort', original_abort)
    monkeypatch.setattr(staged_source, 'cleanup', original_cleanup)
    reopened = _mesh(tmp_path, FolderTransport(folder.root), 'crash')
    try:
        assert not reopened.local_inputs.health(CHAT)['ready']
        assert local_source.capture(reopened.store, reopened.local_inputs.reader(CHAT).definition.source).raw == old
        with pytest.raises(local_source.SourceChanged):
            reopened.local_inputs.inputs(CHAT)
        assert reopened.local_inputs.ingest(CHAT) is False
        assert reopened.local_inputs.inputs(CHAT)[1].source.ready
    finally:
        reopened.close()


def test_background_cleanup_reclaims_old_generation_without_touching_admitted(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    _seed(folder)
    users = folder.root / 'users'
    for n in range(300):
        (users / f'extra-{n:03}.json').write_bytes(b'{}')
    mesh = _mesh(tmp_path, folder, 'cleanup')
    try:
        runtime = mesh.local_inputs
        assert runtime.ingest(CHAT)
        old = runtime.inputs(CHAT)[1].source.raw.source_id
        mesh.tx.put_doc(META, {'id': CHAT, 'members': ['alice'], 'revision': 2})
        assert runtime.ingest(CHAT)
        current = runtime.inputs(CHAT)[1].source.raw.source_id
        assert current != old
        runtime.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = mesh.store._conn().execute('SELECT source FROM staged_sources '
                                             'WHERE source IN (?,?)', (old, current)).fetchall()
            if rows == [(current,)]:
                break
            time.sleep(0.02)
        else:
            pytest.fail('bounded background cleanup did not reclaim old generation')
        assert runtime.inputs(CHAT)[1].source.raw.source_id == current
    finally:
        mesh.close()
