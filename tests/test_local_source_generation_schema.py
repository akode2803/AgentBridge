"""Local source admission-pointer migration and tamper fences."""
from __future__ import annotations

import pytest

from agentbridge.store import local_source
from agentbridge.store.db import Store


def _legacy(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    conn = store._conn()
    for statement in local_source._V1_TABLES.values():
        conn.execute(statement)
    conn.execute(local_source._INDEX)
    epoch = 'a' * 32
    conn.execute('INSERT INTO local_source_schema VALUES(1,1,?)', (epoch,))
    conn.commit()
    ready = store.publish_document_batch(
        store.capture_document_position('ready'), {'doc': 1}, cursor=1, full=True,
    )
    conn.execute(
        'INSERT INTO local_sources(source,revision,ready_incarnation,ready_generation,ready_cursor) '
        'VALUES(?,?,?,?,?)',
        ('ready', 7, ready.incarnation, ready.generation, ready.cursor),
    )
    conn.execute('INSERT INTO local_sources(source,revision) VALUES(?,?)', ('pending', 2))
    conn.commit()
    return store, epoch, ready


def test_legacy_migration_preserves_epoch_positions_and_readiness(tmp_path):
    store, epoch, raw = _legacy(tmp_path)
    try:
        local_source.initialize(store)
        admitted = local_source.capture(store, 'ready')
        assert admitted.epoch == epoch and admitted.revision == 7
        assert admitted.raw == raw and admitted.ready
        assert admitted.logical_source is None
        pending = local_source.capture(store, 'pending')
        assert pending.epoch == epoch and pending.revision == 2 and not pending.ready
        assert pending.logical_source is None
        assert store._conn().execute(
            'SELECT source,physical FROM local_input_generations ORDER BY source',
        ).fetchall() == [('pending', 'pending'), ('ready', 'ready')]
        local_source.initialize(store)
        assert local_source.capture(store, 'ready') == admitted
    finally:
        store.close()


def test_missing_mapping_rejects_existing_local_source_and_reinitialization(tmp_path):
    store, _, _ = _legacy(tmp_path)
    try:
        local_source.initialize(store)
        conn = store._conn()
        conn.execute("DELETE FROM local_input_generations WHERE source='ready'")
        conn.commit()
        with pytest.raises(local_source.SourceChanged, match='missing_generation_mapping'):
            local_source.capture(store, 'ready')
        with pytest.raises(local_source.SourceChanged, match='missing_generation_mapping'):
            local_source.invalidate(store, 'ready')
        # Schema initialization is not a data-repair operation.
        local_source.initialize(store)
        with pytest.raises(local_source.SourceChanged, match='missing_generation_mapping'):
            local_source.capture(store, 'ready')
    finally:
        store.close()


def test_mapping_change_retires_readiness_and_fences_old_revision(tmp_path):
    store, _, _ = _legacy(tmp_path)
    try:
        local_source.initialize(store)
        first = local_source.capture(store, 'ready')
        conn = store._conn()
        conn.execute("UPDATE local_input_generations SET physical='candidate' WHERE source='ready'")
        conn.commit()
        changed = local_source.capture(store, 'ready')
        assert changed.revision == first.revision + 1 and not changed.ready
        assert changed.raw.source_id == 'candidate' and changed.logical_source == 'ready'
        conn.execute("UPDATE local_input_generations SET physical='ready' WHERE source='ready'")
        conn.commit()
        restored = local_source.capture(store, 'ready')
        assert restored.raw == first.raw
        assert restored.revision == first.revision + 2 and not restored.ready
        with pytest.raises(local_source.SourceChanged, match='source_changed'):
            local_source.retire_for_publication(store, first)
    finally:
        store.close()


def test_missing_mapping_table_is_not_recreated(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    try:
        local_source.initialize(store)
        conn = store._conn()
        for name in local_source._MAPPING_TRIGGERS:
            conn.execute('DROP TRIGGER local_input_generation_dirty_' + name.lower())
        conn.execute('DROP TABLE local_input_generations')
        conn.commit()
        with pytest.raises(local_source.SourceChanged, match='local_generation_schema_changed'):
            local_source.initialize(store)
        with pytest.raises(local_source.SourceChanged, match='local_generation_schema_changed'):
            local_source.capture(store, 'missing')
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='local_input_generations'"
        ).fetchone() is None
    finally:
        store.close()


def test_oversized_physical_pointer_rejected_before_value_fetch(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    try:
        local_source.initialize(store)
        local_source.invalidate(store, 'logical')
        conn = store._conn()
        conn.execute(
            'UPDATE local_input_generations SET physical=? WHERE source=?',
            ('x' * 513, 'logical'),
        )
        conn.commit()
        with pytest.raises(local_source.SourceChanged, match='invalid_generation_mapping'):
            local_source.capture(store, 'logical')
    finally:
        store.close()
