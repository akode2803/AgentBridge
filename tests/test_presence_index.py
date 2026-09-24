"""Complete staged presence floor normalization and exact subject selection."""
from __future__ import annotations

import pytest

from agentbridge.store import (document_observation as docs, local_source,
                               presence_index, presence_publication,
                               source_selectors, staged_source)
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


def _setup(tmp_path):
    store = Store(tmp_path / 'presence.sqlite')
    local_source.initialize(store)
    source_selectors.initialize(store)
    staged_source.initialize(store)
    presence_index.initialize(store)
    root = MutationCoordinator(tmp_path / 'home', 'presence-root')
    root.register_store(store)
    definition = source_selectors.definition(root.identity,
        (source_selectors.Selector('doc_prefix', 'presence'),), build='presence-floor-v1')
    publisher = SourcePublisher(root, store, definition)
    return store, root, definition, publisher


@pytest.fixture
def rig(tmp_path):
    result = _setup(tmp_path)
    try:
        yield result
    finally:
        result[0].close()


def _stage(rig, batches, *, max_total_bytes=None):
    store, root, definition, publisher = rig
    captured = publisher.capture()
    with root.publication_gate(store, definition):
        retired = local_source.retire_for_publication(store, captured.source)
    options = {} if max_total_bytes is None else {'max_total_bytes': max_total_bytes}
    stage = staged_source.begin_raw(store, definition.source, expected=retired, **options)
    for batch in batches:
        staged_source.append_raw(store, stage, batch)
    raw = staged_source.finish_raw(store, stage)
    return retired, stage, raw


def _capture(rig, raw, members):
    store = rig[0]
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        return presence_index.capture(conn, store, raw, members)
    finally:
        conn.close()


def _admit(rig, retired, stage):
    store, root, definition, _publisher = rig
    with root.publication_gate(store, definition):
        return presence_publication.admit(store, retired, stage, observed_ns=123)


def test_payload_user_merge_mismatched_filename_and_complete_absence(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/wrong-name@box.json': {'user': 'alice', 'last_seen_ns': 9, 'online': False},
        'presence/other@box.json': {'user': 'alice', 'last_seen_ns': 31, 'online': True},
        'presence/alice@box.json': {'user': 'bob', 'last_seen_ns': 17, 'online': False},
    }])
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='pending'):
        _capture(rig, raw, ('alice',))
    token = presence_index.build(rig[0], stage, expected=retired)
    inputs = _capture(rig, raw, ('alice', 'bob', 'absent'))
    assert inputs.position == raw and inputs.build == token
    assert inputs.floors == (('alice', 31), ('bob', 17), ('absent', 0))
    admitted = _admit(rig, retired, stage)
    assert admitted.ready and admitted.raw == raw


@pytest.mark.parametrize('value, expected', [
    (True, 1), (2.5, 2.5), (-7, 0), (0, 0),
])
def test_legacy_numeric_presence_comparisons_are_bounded(rig, value, expected):
    retired, stage, raw = _stage(rig, [{
        'presence/device.json': {'user': 'alice', 'last_seen_ns': value},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    assert _capture(rig, raw, ('alice',)).floors == (('alice', expected),)


@pytest.mark.parametrize('bad', ['123', None, float('nan'), 2**70])
def test_noncomparable_or_unrepresentable_cursor_never_admits_zero(rig, bad):
    if bad != bad:  # JSON normalization rejects NaN before a sealed stage.
        retired = _stage(rig, [{'presence/other.json': {'user': 'bob', 'last_seen_ns': 1}}])[0]
        with pytest.raises(ValueError, match='Out of range'):
            staged_source.append_raw(rig[0], staged_source.begin_raw(
                rig[0], rig[2].source, expected=retired),
                {'presence/bad.json': {'user': 'alice', 'last_seen_ns': bad}})
        return
    retired, stage, raw = _stage(rig, [{
        'presence/device.json': {'user': 'alice', 'last_seen_ns': bad},
    }])
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        presence_index.build(rig[0], stage, expected=retired)
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        _capture(rig, raw, ('alice',))


def test_many_devices_in_128_document_batches_have_constant_exact_capture(rig):
    batches = []
    for start in (0, 128, 256):
        batches.append({f'presence/device-{i:04d}.json':
                        {'user': 'alice', 'last_seen_ns': i + 1, 'online': False}
                        for i in range(start, start + 128)})
    retired, stage, raw = _stage(rig, batches)
    before = rig[0]._conn().execute('SELECT total_bytes FROM staged_sources WHERE source=?',
                                    (stage.source_id,)).fetchone()[0]
    presence_index.build(rig[0], stage, expected=retired)
    after = rig[0]._conn().execute('SELECT total_bytes FROM staged_sources WHERE source=?',
                                   (stage.source_id,)).fetchone()[0]
    assert after - before == 2 * (len(raw.source_id.encode()) + len('alice')) + 32 + 256 + 48
    conn = docs._open_reader(rig[0].path)
    queries = []
    try:
        conn.set_trace_callback(queries.append)
        conn.execute('BEGIN')
        result = presence_index.capture(conn, rig[0], raw, ('alice', 'missing'))
    finally:
        conn.close()
    assert result.floors == (('alice', 384), ('missing', 0))
    assert not any('FROM document_observation_records' in q for q in queries)
    assert sum('FROM presence_index_rows WHERE source=' in q for q in queries) == 2


def test_derived_floor_exhausts_stage_budget_before_readiness(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 7},
    }], max_total_bytes=100)
    assert rig[0]._conn().execute('SELECT total_bytes FROM staged_sources WHERE source=?',
                                  (stage.source_id,)).fetchone()[0] < 100
    with pytest.raises(OverflowError, match='presence index stage byte budget'):
        presence_index.build(rig[0], stage, expected=retired)
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='pending'):
        _capture(rig, raw, ('alice',))
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        _admit(rig, retired, stage)
    assert not local_source.capture(rig[0], rig[2].source).ready


def test_raw_or_index_mutation_after_build_invalidates_position(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 7},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    with local_source._writer(rig[0]) as conn:
        conn.execute('UPDATE presence_index_rows SET ns=99 WHERE source=? AND user=?',
                     (raw.source_id, 'alice'))
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='pending'):
        _capture(rig, raw, ('alice',))
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        _admit(rig, retired, stage)


def test_index_row_source_move_invalidates_both_namespaces(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 7},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    moved_to = 'stage:' + 'f' * 32
    with local_source._writer(rig[0]) as conn:
        conn.execute('INSERT INTO presence_index_builds VALUES(?,0)', (moved_to,))
        conn.execute('INSERT INTO presence_index_ready VALUES(?,?,?,?,?)',
                     (moved_to, raw.incarnation, raw.generation, raw.cursor, 'a' * 32))
        conn.execute('UPDATE presence_index_rows SET source=? WHERE source=? AND user=?',
                     (moved_to, raw.source_id, 'alice'))
        assert conn.execute('SELECT source FROM presence_index_ready WHERE source IN (?,?)',
                            (raw.source_id, moved_to)).fetchall() == []
        assert conn.execute('SELECT revision FROM presence_index_builds WHERE source=?',
                            (raw.source_id,)).fetchone() == (3,)
        assert conn.execute('SELECT revision FROM presence_index_builds WHERE source=?',
                            (moved_to,)).fetchone() == (1,)
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='pending'):
        _capture(rig, raw, ('alice',))


def test_midbuild_index_injection_changes_revision_and_fails_closed(rig, monkeypatch):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 7},
    }])
    original = presence_index.docs._capture_selected

    def injected(*args, **kwargs):
        selected = original(*args, **kwargs)
        with local_source._writer(rig[0]) as writer:
            writer.execute('INSERT INTO presence_index_rows VALUES(?,?,?)',
                           (raw.source_id, 'intruder', 999))
        return selected

    monkeypatch.setattr(presence_index.docs, '_capture_selected', injected)
    with pytest.raises(presence_index.PresenceIndexUnavailable, match='presence_build_changed'):
        presence_index.build(rig[0], stage, expected=retired)
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        _capture(rig, raw, ('alice',))
