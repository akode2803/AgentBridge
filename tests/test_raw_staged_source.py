"""Raw-only sealed stages share bounded lifecycle without chat-index semantics."""
from __future__ import annotations

import pytest

from agentbridge.store import local_source, staged_source as stage
from agentbridge.store.db import Store


LOGICAL = 'presence-logical'


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'cache.sqlite')
    local_source.initialize(value)
    yield value
    value.close()


def _retired(store):
    return local_source.retire_for_publication(store, local_source.capture(store, LOGICAL))


def _raw(store, *, expected=None, **limits):
    return stage.begin_raw(store, LOGICAL, expected=expected, **limits)


def _verify(store, handle, expected=None):
    conn = store._conn()
    conn.execute('BEGIN')
    try:
        return stage.verify_raw_sealed(conn, store, handle, expected=expected)
    finally:
        conn.rollback()


def test_raw_batches_seal_without_chat_index_or_premature_readiness(store):
    expected = _retired(store)
    handle = _raw(store, expected=expected)
    assert handle.kind == 'raw' and handle.chat_id == ''
    stage.append_raw(store, handle, {'presence/a.json': {'user': 'alice', 'last_seen_ns': 4}})
    stage.append(store, handle, {'presence/b.json': {'user': 'bob', 'last_seen_ns': 9}})
    assert not store.capture_document_position(handle.source_id).initialized
    assert store._conn().execute(
        'SELECT count(*) FROM overlay_index_ready WHERE source=?',
        (handle.source_id,)).fetchone() == (0,)
    raw = stage.finish_raw(store, handle)
    assert raw.initialized and _verify(store, handle, expected) == raw
    assert store.capture_document_observation(raw.source_id).documents() == {
        'presence/a.json': {'user': 'alice', 'last_seen_ns': 4},
        'presence/b.json': {'user': 'bob', 'last_seen_ns': 9},
    }
    for table in ('overlay_index_docs', 'overlay_index_shapes', 'overlay_index_candidates',
                  'overlay_index_proofs', 'overlay_index_ready'):
        assert store._conn().execute(
            f'SELECT count(*) FROM {table} WHERE source=?', (handle.source_id,)
        ).fetchone() == (0,)
    with pytest.raises(ValueError, match='chat stage'):
        stage.finish(store, handle)
    with pytest.raises(ValueError, match='chat stage'):
        conn = store._conn()
        conn.execute('BEGIN')
        try:
            stage.verify_sealed(conn, store, handle)
        finally:
            conn.rollback()


def test_legacy_chat_stage_is_migrated_preserving_active_row(store):
    conn = store._conn()
    with conn:
        conn.execute(stage._LEGACY_SCHEMA)
        conn.execute(stage._ACTIVE)
        for sql in stage._triggers().values():
            conn.execute(sql)
        incarnation = store.capture_document_position('legacy').incarnation
        physical = 'stage:' + '1' * 32
        conn.execute(
            "INSERT INTO staged_sources VALUES(?,?,?,?,?,'building',0,0,0,?, ?,0,NULL,NULL,NULL,0,0)",
            (physical, 'legacy', 'room', '2' * 32, incarnation,
             stage.MAX_TOTAL_BYTES, stage.MAX_DOCUMENTS),
        )
        conn.execute('INSERT INTO document_observation_sources VALUES(?,0,0,0)', (physical,))
    old_handle = stage.StageHandle(str(store.path), incarnation, physical,
                                   'legacy', 'room', '2' * 32)
    stage.initialize(store)
    assert conn.execute('SELECT kind FROM staged_sources WHERE source=?',
                        (physical,)).fetchone() == ('chat',)
    stage.append(store, old_handle, {'chats/room/meta.json': {'members': []}})
    position, indexed = stage.finish(store, old_handle)
    assert indexed.source == position
    stage.initialize(store)  # migration is idempotent
    assert stage._schema(conn) is None


def test_raw_owner_revision_and_partial_seal_are_fenced(store):
    first = _retired(store)
    handle = _raw(store, expected=first)
    stage.append_raw(store, handle, {'presence/a.json': {'last_seen_ns': 4}})
    with pytest.raises(stage.StageChanged, match='stage_not_sealed'):
        _verify(store, handle, first)
    with pytest.raises(stage.StageChanged, match='duplicate'):
        stage.append_raw(store, handle, {'presence/a.json': {'last_seen_ns': 8}})
    assert store._conn().execute('SELECT count(*) FROM document_observation_records '
                                 'WHERE source_id=?', (handle.source_id,)).fetchone() == (1,)
    stage.finish_raw(store, handle)
    later = local_source.retire_for_publication(store, first)
    with pytest.raises(stage.StageChanged, match='owner_revision'):
        _verify(store, handle, later)
    assert _verify(store, handle, first).initialized
    with pytest.raises(stage.StageChanged, match='stage_not_building'):
        stage.append_raw(store, handle, {'presence/new.json': {}})


def test_raw_mutation_and_overlay_pollution_reject_seal(store):
    handle = _raw(store)
    stage.append_raw(store, handle, {'presence/a.json': {'last_seen_ns': 4}})
    stage.finish_raw(store, handle)
    with store._conn():
        store._conn().execute('UPDATE document_observation_records SET payload=? '
                              'WHERE source_id=?', ('{}', handle.source_id))
    with pytest.raises(stage.StageChanged, match='raw'):
        _verify(store, handle)
    other = _raw(store)
    stage.append_raw(store, other, {'presence/a.json': {}})
    stage.finish_raw(store, other)
    with store._conn():
        store._conn().execute(
            'INSERT INTO overlay_index_ready VALUES(?,?,?,?,?,?,?)',
            (other.source_id, 'room', 1, 0, other.incarnation, 'a' * 32, 1),
        )
    with pytest.raises(stage.StageChanged, match='overlay'):
        _verify(store, other)


def test_raw_batch_and_aggregate_quotas_leave_build_unchanged(store):
    handle = _raw(store, max_total_bytes=100, max_documents=1)
    with pytest.raises(ValueError, match='128'):
        stage.append_raw(store, handle, {f'presence/{i}.json': {} for i in range(129)})
    with pytest.raises(OverflowError, match='budget'):
        stage.append_raw(store, handle, {'presence/large.json': {'v': 'x' * 200}})
    assert store._conn().execute('SELECT count(*) FROM document_observation_records '
                                 'WHERE source_id=?', (handle.source_id,)).fetchone() == (0,)
    stage.append_raw(store, handle, {'presence/a.json': {}})
    with pytest.raises(OverflowError, match='budget'):
        stage.append_raw(store, handle, {'presence/b.json': {}})
    assert _verify_after_seal(store, handle).initialized


def _verify_after_seal(store, handle):
    stage.finish_raw(store, handle)
    return _verify(store, handle)


def test_raw_cleanup_uses_shared_budget_and_respects_admitted_pointer(store):
    expected = _retired(store)
    handle = _raw(store, expected=expected)
    stage.append_raw(store, handle, {'presence/a.json': {}, 'presence/b.json': {}})
    stage.finish_raw(store, handle)
    conn = store._conn()
    with conn:
        conn.execute('CREATE TABLE presence_index_rows('
                     'source TEXT,user TEXT,ns INTEGER,PRIMARY KEY(source,user))')
        conn.execute('CREATE TABLE presence_index_ready(source TEXT PRIMARY KEY,build TEXT)')
        conn.execute('CREATE TABLE presence_index_builds(source TEXT PRIMARY KEY,revision INTEGER)')
        conn.execute('INSERT INTO presence_index_rows VALUES(?,?,?)', (handle.source_id, 'alice', 1))
        conn.execute('INSERT INTO presence_index_ready VALUES(?,?)', (handle.source_id, 'ready'))
        conn.execute('INSERT INTO presence_index_builds VALUES(?,?)', (handle.source_id, 1))
        conn.execute('UPDATE local_input_generations SET physical=? WHERE source=?',
                     (handle.source_id, LOGICAL))
    stage.abort(store, handle)
    assert stage.cleanup(store, max_rows=1) == 0
    assert not stage.retire_generation(store, handle.source_id)
    with conn:
        conn.execute('UPDATE local_input_generations SET physical=? WHERE source=?',
                     (LOGICAL, LOGICAL))
    assert stage.retire_generation(store, handle.source_id)
    assert stage.cleanup(store, max_rows=1) == 1
    assert conn.execute('SELECT count(*) FROM presence_index_rows WHERE source=?',
                        (handle.source_id,)).fetchone() == (1,)
    while stage.cleanup(store, max_rows=1):
        pass
    assert conn.execute('SELECT count(*) FROM presence_index_rows WHERE source=?',
                        (handle.source_id,)).fetchone() == (0,)
    assert conn.execute('SELECT 1 FROM presence_index_ready WHERE source=?',
                        (handle.source_id,)).fetchone() is None
    assert conn.execute('SELECT 1 FROM presence_index_builds WHERE source=?',
                        (handle.source_id,)).fetchone() is None
    assert conn.execute('SELECT 1 FROM staged_sources WHERE source=?',
                        (handle.source_id,)).fetchone() is None
