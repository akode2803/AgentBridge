"""Atomic admission of a complete raw presence source and its derived floors."""
from __future__ import annotations

from . import local_source as owner, presence_index, staged_source


def admit(store, expected, stage, *, observed_ns):
    """Caller holds the registered presence source's root publication gate."""
    expected = owner._expected(store, expected)
    if type(observed_ns) is not int or not 0 <= observed_ns <= owner.MAX:
        raise ValueError('invalid observation time')
    with owner._writer(store) as conn:
        current = owner.capture_in_transaction(conn, store, expected.source_id)
        if current != expected or current.writes_pending or current.ready:
            raise owner.SourceChanged('source_changed_during_ingestion')
        raw = staged_source.verify_raw_sealed(conn, store, stage, expected=expected)
        if stage.logical_source != expected.source_id or raw.incarnation != current.raw.incarnation:
            raise owner.SourceChanged('foreign_staged_source')
        presence_index.capture(conn, store, raw, ())
        conn.execute('INSERT INTO local_input_generations(source,physical) VALUES(?,?) '
                     'ON CONFLICT(source) DO UPDATE SET physical=excluded.physical',
                     (expected.source_id, raw.source_id))
        owner._advance(conn, expected.source_id, current.revision)
        conn.execute("UPDATE local_sources SET ready_incarnation=?,ready_generation=?,"
                     "ready_cursor=?,last_success_ns=?,failures=0,error='' WHERE source=?",
                     (raw.incarnation, raw.generation, raw.cursor, observed_ns, expected.source_id))
        result = owner.capture_in_transaction(conn, store, expected.source_id)
        if not result.ready or result.raw != raw:
            raise owner.SourceChanged('presence_admission_failed')
    return result
