"""Complete staged raw/index admission; no permission verdicts are retained."""
from __future__ import annotations

from dataclasses import dataclass

from . import document_observation as docs, local_source as owner, overlay_index
from . import staged_source


_COMPARISON = object()
_ADMITTED_COMPARISON = object()


class _InputsDifferent(RuntimeError):
    pass


@dataclass(frozen=True)
class _EqualInputs:
    expected: owner.SourcePosition
    candidate: staged_source.StageHandle
    raw: docs.DocumentPosition
    index: overlay_index.OverlayIndexPosition
    seal: object


@dataclass(frozen=True)
class _EqualAdmittedInputs:
    expected: owner.SourcePosition
    raw: docs.DocumentPosition
    index: overlay_index.OverlayIndexPosition
    seal: object


def identical_admitted(store, expected, chat_id, collect):
    """Compare one complete collected selection with the admitted generation.

    ``collect`` must either deliver every selected document exactly once and
    return, or raise. A mismatch may stop collection early because no result is
    admitted; the caller then uses the ordinary complete staged path.
    """
    expected = owner._expected(store, expected)
    if not callable(collect):
        raise ValueError('complete collector must be callable')
    if not expected.ready or not expected.raw.initialized:
        return False
    conn = docs._open_reader(store.path)
    try:
        conn.execute('BEGIN')
        if owner.capture_in_transaction(conn, store, expected.source_id) != expected:
            raise owner.SourceChanged('source_changed_during_comparison')
        row = conn.execute(
            'SELECT build,schema FROM overlay_index_ready WHERE source=?',
            (expected.raw.source_id,),
        ).fetchone()
        if row is None:
            return False
        index = overlay_index.OverlayIndexPosition(expected.raw, chat_id, *row)
        try:
            index = overlay_index._wanted(index, store.path)
            overlay_index._ready(conn, store.path, index)
        except (ValueError, overlay_index.OverlayIndexUnavailable):
            return False
        count = conn.execute(
            'SELECT count(*) FROM document_observation_records '
            'WHERE source_id=?', (expected.raw.source_id,),
        ).fetchone()[0]
        seen = 0

        def compare(batch):
            nonlocal seen
            serialized, _used = docs._serialize_live_documents(
                batch,
                max_documents=staged_source.MAX_BATCH_DOCUMENTS,
                max_bytes=staged_source.MAX_BATCH_BYTES,
            )
            names = tuple(serialized)
            marks = ','.join('?' for _ in names)
            rows = conn.execute(
                'SELECT path,payload,deleted FROM document_observation_records '
                f'WHERE source_id=? AND path IN ({marks})',
                (expected.raw.source_id, *names),
            ).fetchall()
            if (len(rows) != len(serialized)
                    or any(deleted or serialized.get(path) != payload
                           for path, payload, deleted in rows)):
                raise _InputsDifferent
            seen += len(serialized)

        try:
            collect(compare)
        except _InputsDifferent:
            return False
        if seen != count:
            return False
        if owner.capture_in_transaction(conn, store, expected.source_id) != expected:
            raise owner.SourceChanged('source_changed_during_comparison')
        overlay_index._ready(conn, store.path, index)
        return _EqualAdmittedInputs(
            expected, expected.raw, index, _ADMITTED_COMPARISON,
        )
    finally:
        conn.close()


def admit_identical(store, expected, comparison, *, observed_ns):
    """Refresh readiness after exact complete equality without a new stage."""
    expected = owner._expected(store, expected)
    if type(observed_ns) is not int or not 0 <= observed_ns <= owner.MAX:
        raise ValueError('invalid observation time')
    if (type(comparison) is not _EqualAdmittedInputs
            or comparison.seal is not _ADMITTED_COMPARISON
            or comparison.expected != expected or comparison.raw != expected.raw):
        raise owner.SourceChanged('unproven_admitted_source_equality')
    index = overlay_index._wanted(comparison.index, store.path)
    if index.source != expected.raw:
        raise owner.SourceChanged('foreign_admitted_index')
    with owner._writer(store) as conn:
        current = owner.capture_in_transaction(conn, store, expected.source_id)
        if current != expected or current.writes_pending:
            raise owner.SourceChanged('source_changed_during_ingestion')
        overlay_index._ready(conn, store.path, index)
        # The unchanged fast path does not open a new stage, so explicitly
        # retire candidates left by older interrupted owner revisions.
        staged_source.abandon_superseded(
            conn, store, expected.source_id, expected,
        )
        owner._advance(conn, expected.source_id, current.revision)
        conn.execute(
            "UPDATE local_sources SET ready_incarnation=?,ready_generation=?,"
            "ready_cursor=?,last_success_ns=?,failures=0,error='' WHERE source=?",
            (current.raw.incarnation, current.raw.generation, current.raw.cursor,
             observed_ns, expected.source_id),
        )
        result = owner.capture_in_transaction(conn, store, expected.source_id)
        overlay_index._ready(conn, store.path, index)
        if not result.ready or result.raw != current.raw:
            raise owner.SourceChanged('unchanged_admission_failed')
    return result, index


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
