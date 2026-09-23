"""Bounded staging, sealed proof and explicit safe reclamation."""
import pytest

from agentbridge.store import staged_source as stage
from agentbridge.store import local_source
from agentbridge.store.db import Store


CHAT = 'room'
REACTION = f'chats/{CHAT}/overlays/reactions/alice.json'
STATE = f'chats/{CHAT}/overlays/state/alice.json'
PIN = f'chats/{CHAT}/overlays/pins/m1.json'


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / 'cache.db')
    yield instance
    instance.close()


def test_append_batches_hidden_until_complete_seal(store):
    handle = stage.begin(store, 'logical', CHAT)
    assert not store.capture_document_position(handle.source_id).initialized
    stage.append(store, handle, {REACTION: {'v': {'m1': '👍'}, 'sig': 'bad'}})
    stage.append(store, handle, {STATE: {'hidden': ['m1'], 'sig': 'bad'}, PIN: {}})
    assert not store.capture_document_position(handle.source_id).initialized
    assert store._conn().execute('SELECT 1 FROM overlay_index_ready WHERE source=?', (handle.source_id,)).fetchone() is None
    raw, index = stage.finish(store, handle)
    assert raw.initialized and raw.source_id == handle.source_id and index.source == raw
    conn = store._conn()
    conn.execute('BEGIN')
    assert stage.verify_sealed(conn, store, handle) == (raw, index)
    conn.rollback()
    with pytest.raises(stage.StageChanged):
        stage.append(store, handle, {f'chats/{CHAT}/overlays/state/bob.json': {}})
    result = store.capture_overlay_index(index, ('m1',), (STATE,))
    assert {v.kind for v in result.candidates} == {'reaction', 'hidden'}


def test_duplicate_path_is_atomic_and_unsupported_paths_fail(store):
    handle = stage.begin(store, 'logical', CHAT)
    stage.append(store, handle, {REACTION: {'v': {}}})
    with pytest.raises(stage.StageChanged, match='duplicate'):
        stage.append(store, handle, {REACTION: {'v': {'m1': 'new'}}})
    with pytest.raises(ValueError, match='unsupported'):
        stage.append(store, handle, {'chats/room/overlays/keys/alice.json': {}})
    assert store._conn().execute('SELECT count(*) FROM document_observation_records WHERE source_id=?', (handle.source_id,)).fetchone() == (1,)
    stage.finish(store, handle)


def test_budget_and_bad_batch_leave_build_recoverable(store):
    handle = stage.begin(store, 'logical', CHAT, max_total_bytes=200)
    with pytest.raises(OverflowError):
        stage.append(store, handle, {REACTION: {'v': {'m1': 'x' * 1000}}})
    with pytest.raises(ValueError):
        stage.append(store, handle, {REACTION: {}, STATE: {1: 'bad'}})
    assert store._conn().execute('SELECT count(*) FROM document_observation_records WHERE source_id=?', (handle.source_id,)).fetchone() == (0,)
    stage.append(store, handle, {PIN: {}})
    stage.finish(store, handle)


def test_raw_and_index_changes_invalidate_exact_seal(store):
    handle = stage.begin(store, 'logical', CHAT)
    stage.append(store, handle, {REACTION: {'v': {'m1': 'ok'}}})
    stage.finish(store, handle)
    conn = store._conn()
    with conn:
        conn.execute('UPDATE overlay_index_docs SET actor=? WHERE source=?', ('wrong', handle.source_id))
    conn.execute('BEGIN')
    with pytest.raises(stage.StageChanged, match='index'):
        stage.verify_sealed(conn, store, handle)
    conn.rollback()
    with conn:
        conn.execute('UPDATE document_observation_records SET payload=? WHERE source_id=?', ('{}', handle.source_id))
    conn.execute('BEGIN')
    with pytest.raises(stage.StageChanged, match='raw'):
        stage.verify_sealed(conn, store, handle)
    conn.rollback()


def test_aborted_cleanup_is_bounded_and_current_mapping_is_protected(store):
    handle = stage.begin(store, 'logical', CHAT)
    stage.append(store, handle, {REACTION: {}, STATE: {}})
    stage.finish(store, handle)
    conn = store._conn()
    with conn:
        conn.execute('INSERT INTO local_input_generations VALUES(?,?)', ('logical', handle.source_id))
    stage.abort(store, handle)
    assert stage.cleanup(store, max_rows=1) == 0
    assert not stage.retire_generation(store, handle.source_id)
    with conn:
        conn.execute('DELETE FROM local_input_generations WHERE source=?', ('logical',))
    assert stage.retire_generation(store, handle.source_id)
    while stage.cleanup(store, max_rows=1):
        pass
    assert conn.execute('SELECT 1 FROM staged_sources WHERE source=?', (handle.source_id,)).fetchone() is None


def test_active_build_does_not_get_stolen(store):
    first = stage.begin(store, 'logical', CHAT)
    with pytest.raises(stage.StageChanged, match='active'):
        stage.begin(store, 'logical', CHAT)
    stage.abort(store, first)
    second = stage.begin(store, 'logical', CHAT)
    assert second.source_id != first.source_id


def test_non_overlay_documents_remain_raw_only(store):
    handle = stage.begin(store, 'logical', CHAT)
    stage.append(store, handle, {'chats/room/meta.json': {'members': []},
                                 'users/alice.json': {'name': 'Alice'},
                                 REACTION: {'v': {'m1': 'ok'}}})
    raw, indexed = stage.finish(store, handle)
    assert store.capture_document_observation(raw.source_id).documents()['users/alice.json'] == {'name': 'Alice'}
    assert store._conn().execute('SELECT count(*) FROM overlay_index_docs WHERE source=?', (handle.source_id,)).fetchone() == (1,)
    assert indexed.source == raw


def test_build_index_mutation_cannot_seal(store):
    handle = stage.begin(store, 'logical', CHAT)
    stage.append(store, handle, {REACTION: {'v': {'m1': 'ok'}}})
    with store._conn():
        store._conn().execute('DELETE FROM overlay_index_docs WHERE source=?', (handle.source_id,))
    with pytest.raises(stage.StageChanged, match='index'):
        stage.finish(store, handle)


def test_new_owner_revision_reclaims_interrupted_build_and_unadmitted_seal(store):
    local_source.initialize(store)
    first_owner = local_source.capture(store, 'logical')
    retired = local_source.retire_for_publication(store, first_owner)
    interrupted = stage.begin(store, 'logical', CHAT, expected=retired)
    with pytest.raises(stage.StageChanged, match='active'):
        stage.begin(store, 'logical', CHAT, expected=retired)
    newer = local_source.retire_for_publication(store, retired)
    another = stage.begin(store, 'logical', CHAT, expected=newer)
    assert another.source_id != interrupted.source_id
    assert store._conn().execute('SELECT phase FROM staged_sources WHERE source=?', (interrupted.source_id,)).fetchone() == ('abandoned',)
    stage.finish(store, another)
    newest = local_source.retire_for_publication(store, newer)
    replacement = stage.begin(store, 'logical', CHAT, expected=newest)
    assert replacement.source_id != another.source_id
    assert store._conn().execute('SELECT phase FROM staged_sources WHERE source=?', (another.source_id,)).fetchone() == ('abandoned',)


def test_sealed_candidate_cannot_cross_owner_revision(store):
    local_source.initialize(store)
    first = local_source.retire_for_publication(store, local_source.capture(store, 'logical'))
    candidate = stage.begin(store, 'logical', CHAT, expected=first)
    stage.finish(store, candidate)
    later = local_source.retire_for_publication(store, first)
    conn = store._conn()
    conn.execute('BEGIN')
    with pytest.raises(stage.StageChanged, match='owner_revision'):
        stage.verify_sealed(conn, store, candidate, expected=later)
    conn.rollback()
