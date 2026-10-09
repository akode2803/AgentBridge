from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.transport.change_ledger import (
    ChangeLedgerCapability,
    ChangeLedgerEpoch,
    ChangeLedgerEvent,
    ChangeLedgerPage,
    MAX_LEDGER_PAGE_SIZE,
)


EPOCH = "12345678-1234-5678-9234-567812345678"


def _doc(event_id=1, *, stream_kind="chat", stream_id="room", head=7):
    return ChangeLedgerEvent(
        event_id, stream_kind, stream_id, "docs", doc_head=head,
    )


def test_ledger_contract_is_immutable_and_cursor_is_the_last_examined_event():
    events = (_doc(4), ChangeLedgerEvent(9, "chat", "room", "logs", log_head=12))
    page = ChangeLedgerPage(2, events, False)
    assert page.cursor == 9
    assert ChangeLedgerPage(9, (), False).cursor == 9
    with pytest.raises(Exception):
        page.has_more = True


@pytest.mark.parametrize("event", [
    ChangeLedgerEvent(1, "root", "", "docs", doc_head=0),
    ChangeLedgerEvent(2, "root", "", "visibility"),
    ChangeLedgerEvent(3, "chat", "room", "docs", doc_head=4),
    ChangeLedgerEvent(4, "chat", "room", "logs", log_head=8),
    ChangeLedgerEvent(5, "chat", "room", "visibility"),
])
def test_valid_event_shapes(event):
    assert event.event_id > 0


@pytest.mark.parametrize("kwargs", [
    {"event_id": 0, "stream_kind": "chat", "stream_id": "room",
     "domain": "docs", "doc_head": 1},
    {"event_id": 1, "stream_kind": "root", "stream_id": "room",
     "domain": "visibility"},
    {"event_id": 1, "stream_kind": "chat", "stream_id": "",
     "domain": "visibility"},
    {"event_id": 1, "stream_kind": "chat", "stream_id": "room",
     "domain": "logs", "doc_head": 1},
    {"event_id": 1, "stream_kind": "root", "stream_id": "",
     "domain": "logs", "log_head": 1},
    {"event_id": 1, "stream_kind": "root", "stream_id": "",
     "domain": "visibility", "doc_head": 1},
    {"event_id": 1, "stream_kind": [], "stream_id": "",
     "domain": "visibility"},
    {"event_id": 1, "stream_kind": "root", "stream_id": "",
     "domain": []},
])
def test_invalid_event_shapes_fail_closed(kwargs):
    with pytest.raises(ValueError):
        ChangeLedgerEvent(**kwargs)


def test_epoch_and_capability_bounds_are_explicit():
    epoch = ChangeLedgerEpoch(EPOCH, minimum_cursor=40, schema_version=1)
    capability = ChangeLedgerCapability(1, max_page_size=250)
    assert epoch.minimum_cursor == 40
    assert capability.max_page_size == 250
    with pytest.raises(ValueError):
        replace(epoch, epoch="not-an-epoch")
    with pytest.raises(ValueError):
        ChangeLedgerCapability(1, MAX_LEDGER_PAGE_SIZE + 1)


def test_page_rejects_reordering_duplicates_and_unbounded_rows():
    with pytest.raises(ValueError, match="strictly ordered"):
        ChangeLedgerPage(3, (_doc(5), _doc(4)), False)
    with pytest.raises(ValueError, match="strictly ordered"):
        ChangeLedgerPage(3, (_doc(4), _doc(4)), False)
    with pytest.raises(ValueError, match="cannot promise"):
        ChangeLedgerPage(3, (), True)
    with pytest.raises(ValueError, match="safety limit"):
        ChangeLedgerPage(
            0, tuple(_doc(index) for index in range(1, MAX_LEDGER_PAGE_SIZE + 2)), False,
        )


class _DeterministicLedger:
    """Test provider whose notifications can be lost without losing replay."""

    def __init__(self):
        self.events = []
        self.notifications = []

    def append(self, event):
        self.events.append(event)
        self.notifications.append(event.event_id)

    def notify(self, *, drop=(), reverse=False, duplicate=False):
        values = [value for value in self.notifications if value not in drop]
        if reverse:
            values.reverse()
        if duplicate and values:
            values.append(values[-1])
        return tuple(values)

    def page(self, cursor, limit):
        rows = tuple(event for event in self.events if event.event_id > cursor)[:limit]
        remaining = any(event.event_id > (rows[-1].event_id if rows else cursor)
                        for event in self.events)
        return ChangeLedgerPage(cursor, rows, remaining)


def test_dropped_duplicate_and_reordered_wakes_do_not_change_durable_replay():
    fake = _DeterministicLedger()
    for event_id in (1, 4, 9):
        fake.append(_doc(event_id, head=event_id))
    assert fake.notify(drop=(4,), reverse=True, duplicate=True) == (9, 1, 1)
    first = fake.page(0, 2)
    second = fake.page(first.cursor, 2)
    assert first.has_more is True
    assert [event.event_id for event in (*first.events, *second.events)] == [1, 4, 9]
