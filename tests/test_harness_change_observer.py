from __future__ import annotations

from agentbridge.harness.change_observer import HarnessChangeObserver
from agentbridge.store.db import Store
from agentbridge.transport.change_ledger import (
    ChangeLedgerCapability,
    ChangeLedgerEpoch,
    ChangeLedgerEvent,
    ChangeLedgerPage,
)


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class _Transport:
    def __init__(self, events=()):
        self.epoch = ChangeLedgerEpoch(
            "00000000-0000-0000-0000-000000000001", 0, 1,
        )
        self.events = tuple(events)
        self.calls = []
        self.listener = None
        self.status = "ready"
        self.fail_refresh = False

    def change_ledger_capability(self):
        self.calls.append(("capability",))
        return ChangeLedgerCapability(1)

    def change_ledger_epoch(self):
        self.calls.append(("epoch",))
        return self.epoch

    def subscribe_change_ledger(self, callback):
        self.calls.append(("subscribe",))
        self.listener = callback
        return lambda: self.calls.append(("unsubscribe",))

    def change_ledger_realtime_status(self):
        return self.status

    def change_ledger_events(self, cursor, *, limit):
        self.calls.append(("page", cursor, limit))
        rows = tuple(event for event in self.events if event.event_id > cursor)[:limit]
        return ChangeLedgerPage(cursor, rows, False)

    def refresh(self):
        self.calls.append(("baseline",))

    def refresh_observed_changes(self, *, visibility=False):
        self.calls.append(("refresh", visibility))
        if self.fail_refresh:
            raise OSError("offline")
        return True


def _event(event_id, *, domain="docs"):
    return ChangeLedgerEvent(
        event_id, "root", "", domain,
        doc_head=event_id if domain == "docs" else None,
    )


def test_replay_advances_only_after_mirror_admission(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    tx = _Transport((_event(1), _event(2, domain="visibility")))
    wakes = []
    observer = HarnessChangeObserver(tx, store, lambda: wakes.append(True))
    try:
        assert observer.active is False
        assert observer.tick() is True
        assert observer.active is True
        assert ("baseline",) in tx.calls
        assert ("refresh", True) in tx.calls
        assert store.cached_doc("sync/harness_change_ledger")["cursor"] == 2

        tx.events += (_event(3),)
        tx.listener(3)
        assert wakes
        assert observer.tick() is True
        assert store.cached_doc("sync/harness_change_ledger")["cursor"] == 3
    finally:
        observer.close()
        assert observer.active is False
        store.close()
    assert ("unsubscribe",) in tx.calls


def test_unavailable_ledger_stays_in_legacy_scan_mode(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    tx = _Transport()
    tx.change_ledger_capability = lambda: None
    observer = HarnessChangeObserver(tx, store, lambda: None)
    try:
        assert observer.tick() is False
        assert observer.active is False
    finally:
        observer.close()
        store.close()


def test_failed_mirror_admission_keeps_replay_cursor(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    clock = _Clock()
    tx = _Transport((_event(1),))
    tx.fail_refresh = True
    observer = HarnessChangeObserver(tx, store, lambda: None, clock=clock)
    try:
        assert observer.tick() is False
        assert store.cached_doc("sync/harness_change_ledger")["cursor"] == 0
        tx.fail_refresh = False
        page_calls = len([call for call in tx.calls if call[0] == "page"])
        assert observer.tick() is False
        assert len([call for call in tx.calls if call[0] == "page"]) == page_calls
        clock.now += 46
        assert observer.tick() is True
        assert store.cached_doc("sync/harness_change_ledger")["cursor"] == 1
    finally:
        observer.close()
        store.close()


def test_ready_observer_queries_on_signal_or_bounded_audit(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    clock = _Clock()
    tx = _Transport()
    observer = HarnessChangeObserver(tx, store, lambda: None, clock=clock)
    try:
        assert observer.tick() is False
        pages = len([call for call in tx.calls if call[0] == "page"])
        assert observer.tick() is False
        assert len([call for call in tx.calls if call[0] == "page"]) == pages

        tx.listener(5)
        assert observer.tick() is False
        assert len([call for call in tx.calls if call[0] == "page"]) == pages + 1

        clock.now += 301
        assert observer.tick() is False
        assert len([call for call in tx.calls if call[0] == "page"]) == pages + 2
    finally:
        observer.close()
        store.close()


def test_lower_realtime_id_rechecks_replaced_epoch_before_replay(tmp_path):
    store = Store(tmp_path / "observer.sqlite")
    tx = _Transport((_event(1), _event(2)))
    observer = HarnessChangeObserver(tx, store, lambda: None)
    try:
        assert observer.tick() is True
        assert store.cached_doc("sync/harness_change_ledger")["cursor"] == 2
        baselines = len([call for call in tx.calls if call[0] == "baseline"])

        tx.epoch = ChangeLedgerEpoch(
            "00000000-0000-0000-0000-000000000002", 0, 1,
        )
        tx.events = (_event(1),)
        tx.listener(1)
        assert observer.tick() is True
        state = store.cached_doc("sync/harness_change_ledger")
        assert state["epoch"] == tx.epoch.epoch
        assert state["cursor"] == 1
        assert len([call for call in tx.calls if call[0] == "baseline"]) == baselines + 1
    finally:
        observer.close()
        store.close()
