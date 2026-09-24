"""Raw presence stage admission is a distinct crash-safe logical source."""
from __future__ import annotations

import pytest

from agentbridge.store import local_source, presence_index, staged_source

from test_presence_index import _admit, _capture, _setup, _stage


@pytest.fixture
def rig(tmp_path):
    result = _setup(tmp_path)
    try:
        yield result
    finally:
        result[0].close()


def _current(rig):
    return local_source.capture(rig[0], rig[2].source)


def test_staged_phases_never_expose_partially_collected_floors(rig):
    store, root, definition, publisher = rig
    captured = publisher.capture()
    with root.publication_gate(store, definition):
        retired = local_source.retire_for_publication(store, captured.source)
    stage = staged_source.begin_raw(store, definition.source, expected=retired)
    assert not _current(rig).ready
    staged_source.append_raw(store, stage, {
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 11},
    })
    assert not _current(rig).ready
    raw = staged_source.finish_raw(store, stage)
    assert not _current(rig).ready
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        _capture(rig, raw, ('alice',))
    presence_index.build(store, stage, expected=retired)
    assert not _current(rig).ready
    _admit(rig, retired, stage)
    assert _current(rig).ready and _capture(rig, raw, ('alice',)).floors == (('alice', 11),)


def test_changed_logical_owner_blocks_admission_then_abandoned_stage_is_bounded_gc(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 11},
        'presence/two.json': {'user': 'alice', 'last_seen_ns': 12},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    with rig[1].publication_gate(rig[0], rig[2]):
        local_source.invalidate(rig[0], rig[2].source)
    with pytest.raises((local_source.SourceChanged, staged_source.StageChanged)):
        _admit(rig, retired, stage)
    assert not _current(rig).ready
    staged_source.abort(rig[0], stage)
    assert staged_source.cleanup(rig[0], max_rows=1) <= 1
    assert rig[0]._conn().execute('SELECT count(*) FROM staged_sources WHERE source=?',
                                  (stage.source_id,)).fetchone() == (1,)
    for _ in range(16):
        staged_source.cleanup(rig[0], max_rows=1)
    assert rig[0]._conn().execute('SELECT count(*) FROM staged_sources WHERE source=?',
                                  (stage.source_id,)).fetchone() == (0,)
    assert rig[0]._conn().execute('SELECT count(*) FROM presence_index_rows WHERE source=?',
                                  (stage.source_id,)).fetchone() == (0,)


def test_admitted_stage_is_protected_from_abort_and_cleanup(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 19},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    admitted = _admit(rig, retired, stage)
    staged_source.abort(rig[0], stage)
    staged_source.cleanup(rig[0], max_rows=128)
    assert _current(rig) == admitted
    assert _capture(rig, raw, ('alice',)).floors == (('alice', 19),)


def test_raw_change_after_seal_and_build_rejects_admission(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 22},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    with local_source._writer(rig[0]) as conn:
        conn.execute('UPDATE document_observation_records SET payload=? '
                     'WHERE source_id=? AND path=?',
                     ('{"user":"alice","last_seen_ns":999}', raw.source_id, 'presence/one.json'))
    with pytest.raises((presence_index.PresenceIndexUnavailable, staged_source.StageChanged)):
        _capture(rig, raw, ('alice',))
    with pytest.raises((presence_index.PresenceIndexUnavailable, staged_source.StageChanged)):
        _admit(rig, retired, stage)
    assert not _current(rig).ready


def test_second_presence_generation_retirement_does_not_serve_stale_old_floor(rig):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 33},
    }])
    presence_index.build(rig[0], stage, expected=retired)
    old = _admit(rig, retired, stage)
    assert old.ready
    captured = rig[3].capture()
    with rig[1].publication_gate(rig[0], rig[2]):
        retired2 = local_source.retire_for_publication(rig[0], captured.source)
    assert not _current(rig).ready and _current(rig).raw == raw
    stage2 = staged_source.begin_raw(rig[0], rig[2].source, expected=retired2)
    staged_source.append_raw(rig[0], stage2, {
        'presence/two.json': {'user': 'bob', 'last_seen_ns': 44},
    })
    raw2 = staged_source.finish_raw(rig[0], stage2)
    presence_index.build(rig[0], stage2, expected=retired2)
    _admit(rig, retired2, stage2)
    assert _current(rig).raw == raw2 and _current(rig).raw != old.raw
    assert _capture(rig, raw2, ('alice', 'bob')).floors == (('alice', 0), ('bob', 44))


def test_foreign_stage_or_unbuilt_index_cannot_be_admitted(rig, tmp_path):
    retired, stage, raw = _stage(rig, [{
        'presence/one.json': {'user': 'alice', 'last_seen_ns': 11},
    }])
    with pytest.raises(presence_index.PresenceIndexUnavailable):
        _admit(rig, retired, stage)
    other = _setup(tmp_path / 'other')
    try:
        with pytest.raises(ValueError, match='foreign stage'):
            presence_index.build(other[0], stage, expected=retired)
    finally:
        other[0].close()
