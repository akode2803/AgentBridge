"""Exact presence display inputs share a complete raw source, not receipt policy."""
from __future__ import annotations

import pytest

from agentbridge.store import (document_observation as docs, local_source,
                               presence_index, presence_publication,
                               source_selectors, staged_source)
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / 'presence.sqlite')
    local_source.initialize(store)
    source_selectors.initialize(store)
    staged_source.initialize(store)
    presence_index.initialize(store)
    root = MutationCoordinator(tmp_path / 'home', 'display-root')
    root.register_store(store)
    definition = source_selectors.definition(root.identity,
        (source_selectors.Selector('doc_prefix', 'presence'),), build='presence-floor-v1')
    try:
        yield store, root, definition
    finally:
        store.close()


def _built(rig, documents):
    store, root, definition = rig
    publisher = SourcePublisher(root, store, definition)
    captured = publisher.capture()
    with root.publication_gate(store, definition):
        expected = local_source.retire_for_publication(store, captured.source)
    handle = staged_source.begin_raw(store, definition.source, expected=expected)
    staged_source.append_raw(store, handle, documents)
    raw = staged_source.finish_raw(store, handle)
    token = presence_index.build(store, handle, expected=expected)
    with root.publication_gate(store, definition):
        admitted = presence_publication.admit(store, expected, handle, observed_ns=123)
    return handle, raw, token, admitted


def _capture(store, raw, users):
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        return presence_index.capture_display(conn, store, raw, users)
    finally:
        conn.close()


def test_online_device_max_and_last_seen_follow_payload_user_not_filename(rig):
    store = rig[0]
    _handle, raw, token, admitted = _built(rig, {
        'presence/alice-new.json': {'user': 'alice', 'last_seen_ns': 100,
                                    'last_seen': 'newest offline', 'online': False},
        'presence/alice-old.json': {'user': 'alice', 'last_seen_ns': 90,
                                    'last_seen': 'older online', 'online': True},
        'presence/alice-named-bob.json': {'user': 'bob', 'last_seen_ns': 75,
                                          'last_seen': 'bob online', 'online': True},
    })
    assert admitted.ready
    captured = _capture(store, raw, ('alice', 'bob', 'missing'))
    assert captured.build == token
    assert captured.subjects == (
        ('alice', 100, 'newest offline', 90),
        ('bob', 75, 'bob online', 75),
        ('missing', 0, '', 0),
    )


def test_exact_capture_has_no_raw_document_scan_and_display_mutation_revokes_ready(rig):
    store = rig[0]
    _handle, raw, _token, _admitted = _built(rig, {
        'presence/a.json': {'user': 'alice', 'last_seen_ns': 7, 'online': True},
    })
    conn = docs._open_reader(store.path)
    traced = []
    try:
        conn.set_trace_callback(traced.append)
        conn.execute('BEGIN')
        assert presence_index.capture_display(conn, store, raw, ('alice',)).subjects[0][3] == 7
    finally:
        conn.close()
    assert not any('FROM document_observation_records' in query for query in traced)
    with local_source._writer(store) as writer:
        writer.execute('UPDATE presence_display_rows SET online_ns=0 WHERE source=?',
                       (raw.source_id,))
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='pending'):
        _capture(store, raw, ('alice',))


@pytest.mark.parametrize('value', ['true', 1, None])
def test_nonboolean_online_blocks_display_without_erasing_valid_receipt_floor(rig, value):
    store, root, definition = rig
    captured = SourcePublisher(root, store, definition).capture()
    with root.publication_gate(store, definition):
        expected = local_source.retire_for_publication(store, captured.source)
    handle = staged_source.begin_raw(store, definition.source, expected=expected)
    staged_source.append_raw(store, handle, {
        'presence/a.json': {'user': 'alice', 'online': value, 'last_seen_ns': 7},
    })
    raw = staged_source.finish_raw(store, handle)
    presence_index.build(store, handle, expected=expected)
    with root.publication_gate(store, definition):
        assert presence_publication.admit(store, expected, handle, observed_ns=123).ready
    reader = docs._open_reader(store.path)
    try:
        reader.execute('BEGIN')
        assert presence_index.capture(reader, store, raw, ('alice',)).floors == (('alice', 7),)
    finally:
        reader.close()
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='display_pending'):
        _capture(store, raw, ('alice',))


def test_floor_only_schema_upgrade_preserves_receipts_but_display_is_pending(rig):
    store = rig[0]
    _handle, raw, token, _admitted = _built(rig, {
        'presence/a.json': {'user': 'alice', 'last_seen_ns': 7, 'online': True},
    })
    conn = store._conn()
    with conn:
        for name in presence_index._DISPLAY_TRIGGERS:
            conn.execute(f'DROP TRIGGER {name}')
        for name in presence_index._DISPLAY_TABLES:
            conn.execute(f'DROP TABLE {name}')
    presence_index.initialize(store)
    reader = docs._open_reader(store.path)
    try:
        reader.execute('BEGIN')
        assert presence_index.capture(reader, store, raw, ('alice',)).floors == (('alice', 7),)
        assert presence_index.capture(reader, store, raw, ()).build == token
        with pytest.raises(presence_index.PresenceIndexUnavailable, match='display_pending'):
            presence_index.capture_display(reader, store, raw, ('alice',))
    finally:
        reader.close()


def test_abandoned_display_rows_use_shared_cleanup_budget(rig):
    store = rig[0]
    handle, raw, _token, _admitted = _built(rig, {
        'presence/a.json': {'user': 'alice', 'last_seen_ns': 7},
        'presence/b.json': {'user': 'bob', 'last_seen_ns': 8},
    })
    # A referenced generation is untouchable even if its stage is corrupted
    # to abandoned. The admission mapping wins in the same cleanup transaction.
    stage = store._conn()
    with stage:
        stage.execute("UPDATE staged_sources SET phase='abandoned' WHERE source=?", (raw.source_id,))
    assert staged_source.cleanup(store, max_rows=1) == 0
    with stage:
        stage.execute('UPDATE local_input_generations SET physical=source WHERE source=?',
                      (rig[2].source,))
    while staged_source.cleanup(store, max_rows=1):
        pass
    assert stage.execute('SELECT 1 FROM presence_display_rows WHERE source=?',
                         (raw.source_id,)).fetchone() is None
    assert stage.execute('SELECT 1 FROM presence_display_ready WHERE source=?',
                         (raw.source_id,)).fetchone() is None
    assert stage.execute('SELECT 1 FROM staged_sources WHERE source=?',
                         (handle.source_id,)).fetchone() is None
