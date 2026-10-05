"""Fresh catalog checks reject changes after an earlier valid read."""

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store


@pytest.mark.parametrize('selectors', [False, True])
@pytest.mark.parametrize('damage', ['missing', 'modified'])
def test_catalog_changes_after_valid_check_fail_closed(tmp_path, selectors, damage):
    store = Store(tmp_path / 'disposable.sqlite')
    try:
        local_source.initialize(store)
        source_selectors.initialize(store)
        module = source_selectors if selectors else local_source
        name = 'idx_local_source_selector_match' if selectors else 'local_source_writes_source'
        reason = 'selector_schema_changed' if selectors else 'local_source_schema_changed'
        conn = store._conn()
        conn.execute('BEGIN')
        module._schema(conn)
        conn.commit()
        if damage in {'missing', 'modified'}:
            conn.execute(f'DROP INDEX {name}')
            if damage == 'modified':
                table, column = ('local_source_selectors', 'kind') if selectors else ('local_source_writes', 'source')
                conn.execute(f'CREATE INDEX {name} ON {table}({column}) WHERE 1')
        conn.commit()
        conn.execute('BEGIN')
        with pytest.raises(local_source.SourceChanged, match=reason):
            module._schema(conn)
        conn.rollback()
    finally:
        store.close()


@pytest.mark.parametrize('selectors', [False, True])
def test_unrelated_catalog_growth_does_not_retire_valid_schema(tmp_path, selectors):
    store = Store(tmp_path / 'disposable.sqlite')
    try:
        local_source.initialize(store)
        source_selectors.initialize(store)
        conn = store._conn()
        for index in range(10):
            conn.execute(f'CREATE TABLE unrelated_{index}(value INTEGER)')
        conn.commit()
        conn.execute('BEGIN')
        (source_selectors if selectors else local_source)._schema(conn)
        conn.rollback()
    finally:
        store.close()


@pytest.mark.parametrize('capture', ['require', 'register'])
@pytest.mark.parametrize('commit_damage', [False, True])
@pytest.mark.parametrize('index,reason', [
    ('local_source_writes_source', 'local_source_schema_changed'),
    ('idx_local_source_selector_match', 'selector_schema_changed'),
])
def test_registered_capture_revalidates_schema_after_success(
        tmp_path, capture, commit_damage, index, reason):
    store = Store(tmp_path / 'disposable.sqlite')
    try:
        local_source.initialize(store)
        source_selectors.initialize(store)
        definition = source_selectors.definition(
            'disposable-root', (source_selectors.Selector('doc_exact', 'accounts/viewer.json'),))
        conn = store._conn()
        conn.execute('BEGIN')
        source_selectors.register_in_transaction(conn, store, definition)
        conn.commit()
        call = (source_selectors.require_registered_in_transaction if capture == 'require'
                else source_selectors.register_in_transaction)
        conn.execute('BEGIN')
        call(conn, store, definition)
        conn.execute(f'DROP INDEX {index}')
        if commit_damage:
            conn.commit()
            conn.execute('BEGIN')
        with pytest.raises(local_source.SourceChanged, match=reason):
            call(conn, store, definition)
        conn.rollback()
    finally:
        store.close()
