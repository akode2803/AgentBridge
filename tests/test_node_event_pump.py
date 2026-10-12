"""Bounded node journal to process-event and receipt translation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentbridge.mesh import eventbus
from agentbridge.mesh.eventbus import EventBus
from agentbridge.mesh.notify import Notifier
from agentbridge.mesh.node_event_pump import (
    DELIVERY_RETRY_S, NodeEventPump,
)
from agentbridge.store import node_event_journal
from agentbridge.store.db import Store


CHAT = "room"


class MessagingStub:
    def __init__(self, store, *, user="bob"):
        self.store = store
        self.user = user
        self.calls = []
        self.error = None
        self.result = True

    def mark_delivered(self, chat_id, *, up_to_ns):
        self.calls.append((chat_id, up_to_ns))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    node_event_journal.initialize(value)
    try:
        yield value
    finally:
        value.close()


def _record(message_id="m1", ns=7, **extra):
    return {
        "id": message_id, "ns": ns, "from": "alice",
        "kind": "message", "text": "hello", **extra,
    }


def _pending_record(store, record, *, chat=CHAT):
    store.upsert_messages(chat, (record,), observed_ns=1)
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (chat, record["id"], record["ns"], record.get("from", ""),
         record.get("kind", "message")),
    )
    conn.commit()


def test_event_claim_emits_only_content_free_canonical_refetch(store):
    record = _record(text="raw-secret-that-must-not-leave-the-store")
    _pending_record(store, record)
    bus = EventBus()
    subscription = bus.subscribe()
    pump = NodeEventPump(store, bus, MessagingStub(store), "bob")

    step = pump.run_due()

    assert step.operation == "events" and step.records == 1 and not step.gap
    received = list(subscription.drain())
    assert [(item.type, item.chat_id, item.data) for item in received] == [
        (eventbus.READ_MODEL, CHAT, {"scope": "chat"}),
    ]
    cli_payload = [{"type": item.type, "chat": item.chat_id,
                    "ns": item.ns, "data": item.data} for item in received]
    assert "raw-secret" not in str(cli_payload)
    notifier = Notifier(bus, SimpleNamespace(), SimpleNamespace(), "bob")
    assert notifier.consider(received[0]) is None
    assert pump.run_due().operation == "none"
    assert list(subscription.drain()) == []


def test_missing_claim_emits_one_content_free_global_invalidation(store):
    conn = store._conn()
    conn.execute(
        "INSERT INTO node_event_journal_items"
        "(chat,message_id,ns,sender,kind) VALUES(?,?,?,?,?)",
        (CHAT, "missing", 1, "alice", "message"),
    )
    conn.commit()
    bus = EventBus()
    subscription = bus.subscribe()
    pump = NodeEventPump(store, bus, MessagingStub(store), "bob")

    step = pump.run_due()

    assert step.operation == "events" and step.records == 0 and step.gap
    received = list(subscription.drain())
    assert len(received) == 1
    assert received[0].type == eventbus.READ_MODEL
    assert received[0].chat_id == "" and received[0].data == {"scope": "global"}


def test_delivery_failure_retries_after_backoff_and_then_completes(store):
    conn = store._conn()
    conn.execute("INSERT INTO node_event_delivery VALUES(?,?)", (CHAT, 7))
    conn.commit()
    clock = [10.0]
    messaging = MessagingStub(store)
    messaging.error = OSError("offline")
    pump = NodeEventPump(
        store, EventBus(), messaging, "bob", clock=lambda: clock[0],
    )

    failed = pump.run_due()
    assert failed.operation == "delivery" and failed.delivered is False
    assert node_event_journal.pending_deliveries(store)
    assert pump.run_due().operation == "none"

    messaging.error = None
    clock[0] += DELIVERY_RETRY_S
    completed = pump.run_due()
    assert completed.operation == "delivery" and completed.delivered is True
    assert messaging.calls == [(CHAT, 7), (CHAT, 7)]
    assert node_event_journal.pending_deliveries(store) == ()


def test_failed_first_delivery_does_not_block_later_chat(store):
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_delivery VALUES(?,?)",
        (("a-fails", 7), ("b-succeeds", 8)),
    )
    conn.commit()
    messaging = MessagingStub(store)

    def deliver(chat_id, *, up_to_ns):
        messaging.calls.append((chat_id, up_to_ns))
        if chat_id == "a-fails":
            raise OSError("offline")
        return True

    messaging.mark_delivered = deliver
    pump = NodeEventPump(store, EventBus(), messaging, "bob", clock=lambda: 10.0)

    first = pump.run_due()
    second = pump.run_due()

    assert first.chat_id == "a-fails" and first.delivered is False
    assert second.chat_id == "b-succeeds" and second.delivered is True
    assert node_event_journal.pending_deliveries(store) == (
        node_event_journal.NodeDelivery("a-fails", 7),
    )


def test_full_failed_delivery_cycle_waits_instead_of_hot_spinning(store):
    conn = store._conn()
    conn.executemany(
        "INSERT INTO node_event_delivery VALUES(?,?)",
        ((f"chat-{index:02}", index + 1) for index in range(33)),
    )
    conn.commit()
    messaging = MessagingStub(store)
    messaging.error = OSError("provider offline")
    pump = NodeEventPump(store, EventBus(), messaging, "bob", clock=lambda: 10.0)

    attempts = [pump.run_due() for _ in range(33)]
    idle = pump.run_due()

    assert all(item.operation == "delivery" and item.delivered is False
               for item in attempts)
    assert len({item.chat_id for item in attempts}) == 33
    assert idle.operation == "none"
    assert pump.wait_s() == pytest.approx(0.35)


def test_receipt_noop_is_success_and_stale_completion_keeps_new_highwater(store):
    conn = store._conn()
    conn.execute("INSERT INTO node_event_delivery VALUES(?,?)", (CHAT, 7))
    conn.commit()
    messaging = MessagingStub(store)
    messaging.result = False

    def advance(chat_id, *, up_to_ns):
        result = MessagingStub.mark_delivered(
            messaging, chat_id, up_to_ns=up_to_ns,
        )
        store._conn().execute(
            "UPDATE node_event_delivery SET max_ns=8 WHERE chat=?",
            (CHAT,),
        )
        store._conn().commit()
        return result

    messaging.mark_delivered = advance
    pump = NodeEventPump(store, EventBus(), messaging, "bob")

    handled = pump.run_due()

    assert handled.operation == "delivery" and handled.delivered is True
    assert node_event_journal.pending_deliveries(store) == (
        node_event_journal.NodeDelivery(CHAT, 8),
    )


def test_record_publication_failure_is_consumed_as_a_gap(store, monkeypatch):
    _pending_record(store, _record())
    bus = EventBus()
    pump = NodeEventPump(store, bus, MessagingStub(store), "bob")
    monkeypatch.setattr(bus, "publish", lambda _event: (_ for _ in ()).throw(
        RuntimeError("subscriber failed"),
    ))

    step = pump.run_due()

    assert step.operation == "events" and step.records == 1 and step.gap
    assert node_event_journal.claim_events(store) is None


def test_for_mesh_rejects_incoherent_message_owner(store):
    from agentbridge.mesh.service import Mesh

    mesh = object.__new__(Mesh)
    mesh.store = store
    mesh.bus = EventBus()
    mesh.user = "bob"
    mesh.messaging = SimpleNamespace(
        store=Store(store.path.parent / "other.sqlite"),
        user="bob", mark_delivered=lambda *_args, **_kwargs: True,
    )
    try:
        with pytest.raises(TypeError, match="invalid node event mesh owner"):
            NodeEventPump.for_mesh(mesh)
    finally:
        mesh.messaging.store.close()
