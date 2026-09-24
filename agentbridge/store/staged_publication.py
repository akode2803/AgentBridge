"""Complete staged raw/index admission; no permission verdicts are retained."""
from __future__ import annotations

from dataclasses import dataclass

from . import document_observation as docs, local_source as owner, overlay_index
from . import staged_source


_COMPARISON = object()


@dataclass(frozen=True)
class _EqualInputs:
    expected: owner.SourcePosition
    candidate: staged_source.StageHandle
    raw: docs.DocumentPosition
    index: overlay_index.OverlayIndexPosition
    seal: object


def identical(store, expected, candidate):
    """Compare complete raw generations off the root gate with bounded buffers.

    The admission CAS must still check expected afterward. SQLite snapshot
    equality is content evidence only, never a membership or freshness verdict.
    """
    if not expected.raw.initialized:
        return False
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        if owner.capture_in_transaction(conn, store, expected.source_id) != expected:
            raise owner.SourceChanged('source_changed_during_comparison')
        raw, _index = staged_source.verify_sealed(conn, store, candidate, expected=expected)
        cursors = [conn.execute('SELECT path,payload,deleted FROM document_observation_records '
                                'WHERE source_id=? ORDER BY path', (source,))
                   for source in (expected.raw.source_id, raw.source_id)]
        # One row at a time also bounds memory for the maximum-size document.
        while True:
            left, right = (cursor.fetchone() for cursor in cursors)
            if left != right:
                return False
            if left is None:
                return _EqualInputs(expected, candidate, raw, _index, _COMPARISON)
    finally:
        conn.close()


def admit(store, expected, candidate, *, observed_ns, reuse=None, comparison=None):
    """Small final transaction. Caller owns the root publication gate.

    ``reuse`` is an existing index after off-gate complete byte comparison.
    The source CAS protects that comparison from intervening local mutations.
    """
    expected = owner._expected(store, expected)
    if type(observed_ns) is not int or not 0 <= observed_ns <= owner.MAX:
        raise ValueError('invalid observation time')
    with owner._writer(store) as conn:
        current = owner.capture_in_transaction(conn, store, expected.source_id)
        if current != expected or current.writes_pending:
            raise owner.SourceChanged('source_changed_during_ingestion')
        raw, index = staged_source.verify_sealed(conn, store, candidate, expected=expected)
        if candidate.logical_source != expected.source_id or raw.incarnation != current.raw.incarnation:
            raise owner.SourceChanged('foreign_staged_source')
        if reuse is not None:
            if (type(comparison) is not _EqualInputs or comparison.seal is not _COMPARISON
                    or comparison.expected != expected or comparison.candidate != candidate
                    or comparison.raw != raw or comparison.index != index):
                raise owner.SourceChanged('unproven_source_equality')
            reuse = overlay_index._wanted(reuse, store.path)
            if reuse.source != current.raw or reuse.chat_id != index.chat_id:
                raise owner.SourceChanged('foreign_reused_index')
            overlay_index._ready(conn, store.path, reuse)
            raw, index = current.raw, reuse
        # Retirement and pointer/readiness publication share this transaction.
        # Initial registration needs _advance's self mapping before replacing
        # it; rollback exposes the original admitted snapshot, never a subset.
        owner._advance(conn, expected.source_id, current.revision)
        if reuse is None:
            conn.execute('INSERT INTO local_input_generations(source,physical) VALUES(?,?) '
                         'ON CONFLICT(source) DO UPDATE SET physical=excluded.physical',
                         (expected.source_id, raw.source_id))
        conn.execute("UPDATE local_sources SET ready_incarnation=?,ready_generation=?,"
                     "ready_cursor=?,last_success_ns=?,failures=0,error='' WHERE source=?",
                     (raw.incarnation, raw.generation, raw.cursor, observed_ns, expected.source_id))
        result = owner.capture_in_transaction(conn, store, expected.source_id)
        if not result.ready or result.raw != raw:
            raise owner.SourceChanged('staged_admission_failed')
    return result, index
