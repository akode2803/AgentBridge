"""Atomic admission of an unindexed, complete staged auxiliary raw source."""
from __future__ import annotations

from dataclasses import dataclass

from . import document_observation as docs, local_source as owner, staged_source


_SEAL = object()


@dataclass(frozen=True)
class _Identical:
    expected: owner.SourcePosition
    stage: staged_source.StageHandle
    raw: docs.DocumentPosition
    seal: object


def identical(store, expected, stage):
    """Compare complete raw rows off the root gate, one bounded row at a time."""
    expected = owner._expected(store, expected)
    if not expected.ready or not expected.raw.initialized:
        return None
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        if owner.capture_in_transaction(conn, store, expected.source_id) != expected:
            raise owner.SourceChanged('source_changed_during_comparison')
        raw = staged_source.verify_raw_sealed(conn, store, stage, expected=expected)
        cursors = [conn.execute('SELECT path,payload,deleted FROM document_observation_records '
                                'WHERE source_id=? ORDER BY path', (source,))
                   for source in (expected.raw.source_id, raw.source_id)]
        while True:
            left, right = (cursor.fetchone() for cursor in cursors)
            if left != right:
                return None
            if left is None:
                return _Identical(expected, stage, raw, _SEAL)
    finally:
        conn.close()


def admit(store, expected, stage, *, observed_ns, comparison=None):
    """Caller holds this registered definition's root publication gate."""
    expected = owner._expected(store, expected)
    if type(observed_ns) is not int or not 0 <= observed_ns <= owner.MAX:
        raise ValueError('invalid observation time')
    with owner._writer(store) as conn:
        current = owner.capture_in_transaction(conn, store, expected.source_id)
        if current != expected or current.writes_pending:
            raise owner.SourceChanged('source_changed_during_ingestion')
        raw = staged_source.verify_raw_sealed(conn, store, stage, expected=expected)
        if stage.logical_source != expected.source_id or raw.incarnation != current.raw.incarnation:
            raise owner.SourceChanged('foreign_staged_source')
        unchanged = comparison is not None
        if unchanged:
            if (type(comparison) is not _Identical or comparison.seal is not _SEAL
                    or comparison.expected != expected or comparison.stage != stage
                    or comparison.raw != raw or not expected.ready):
                raise owner.SourceChanged('unproven_source_equality')
            raw = current.raw
        owner._advance(conn, expected.source_id, current.revision)
        if not unchanged:
            conn.execute('INSERT INTO local_input_generations(source,physical) VALUES(?,?) '
                         'ON CONFLICT(source) DO UPDATE SET physical=excluded.physical',
                         (expected.source_id, raw.source_id))
        conn.execute('UPDATE local_sources SET ready_incarnation=?,ready_generation=?,ready_cursor=?,'
                     "last_success_ns=?,failures=0,error='' WHERE source=?",
                     (raw.incarnation, raw.generation, raw.cursor, observed_ns, expected.source_id))
        result = owner.capture_in_transaction(conn, store, expected.source_id)
        if not result.ready or result.raw != raw:
            raise owner.SourceChanged('raw_admission_failed')
    return result
