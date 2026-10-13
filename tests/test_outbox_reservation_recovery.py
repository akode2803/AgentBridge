"""Independent recovery probes for complete-owner capacity reservations."""

import sqlite3
import pytest
import test_operation_journal as cases
import test_outbox_operation_consumers as c
from test_receipt_retirement import _epoch, NEXT
from agentbridge.store import operation_consumers as oc, operation_journal as oj

world = cases.world


def test_exact_reserved_budget_survives_epoch_and_completes(world, monkeypatch):
    store, journal, item, args = c._attachment_pending(world)
    first = c._manifest(journal, store, item, args)
    with sqlite3.connect(journal.coordinator.path) as db:
        n, size = oc.reservation_capacity(db)
        pending = db.execute(
            "select count(*) from typed_operations where receipt is null"
        ).fetchone()[0]
        ceiling = oj._bytes_used(db) + size + (pending + n) * oj.MAX_RECEIPT_BYTES
    monkeypatch.setattr(oj, "MAX_BYTES", ceiling)
    monkeypatch.setattr(oj, "MAX_RECORDS", 2)
    c._terminal(journal, first)
    assert journal._adopt_epoch(_epoch(1, NEXT)) == 0
    journal = cases._reload(world)
    second = c._allocate(
        journal, store, item, {**c._args(item), "p_record": item.payload["envelope"]}
    )
    assert first.protocol_epoch != second.protocol_epoch
    c._terminal(journal, second)
    assert store.outbox_mark_sent(item)
    journal.consume_outbox_leg(store, item.seq, "manifest:f2-reserved", item.owner_digest)
    journal.consume_outbox_leg(store, item.seq, "append", item.owner_digest)
    with sqlite3.connect(journal.coordinator.path) as db:
        assert oc.reservation_capacity(db) == (0, 0)
        assert db.execute("select count(*) from typed_outbox_reservations").fetchone() == (2,)
    assert journal.reap_outbox_consumers() == 0
    journal.finish_outbox(store, item.seq, item.owner_digest)
    assert store.outbox_finalize_sent(store.outbox_sent_items()[0])
    assert journal.reap_outbox_consumers() == 2


def test_failed_second_leg_commit_preserves_reservation(world, monkeypatch):
    store, journal, item, args = c._attachment_pending(world)
    c._terminal(journal, c._manifest(journal, store, item, args))
    with sqlite3.connect(journal.coordinator.path) as db:
        before = oc.reservation_capacity(db)
    with monkeypatch.context() as m:

        def crash(*a):
            raise RuntimeError("crash")

        m.setattr(journal.coordinator, "_advance_root_revision", crash)
        with pytest.raises(RuntimeError, match="crash"):
            c._allocate(
                journal, store, item, {**c._args(item), "p_record": item.payload["envelope"]}
            )
    with sqlite3.connect(journal.coordinator.path) as db:
        assert oc.reservation_capacity(db) == before
        assert db.execute("select count(*) from typed_operations").fetchone() == (1,)
    assert c._allocate(
        journal, store, item, {**c._args(item), "p_record": item.payload["envelope"]}
    )
