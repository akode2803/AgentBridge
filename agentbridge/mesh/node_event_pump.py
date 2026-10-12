"""Bounded consumption of node-admitted Store work.

The durable journal contains identities and delivery high-waters, never an
authority result. Claims therefore emit only content-free canonical-refetch
invalidations. They are at-most-once like the process-local EventBus; an
explicit global read-model invalidation heals every known gap.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from ..store import node_event_journal
from . import eventbus
from .eventbus import Event, EventBus


DELIVERY_RETRY_S = 4.0
IDLE_RECHECK_S = 0.35


@dataclass(frozen=True)
class NodeEventPumpStep:
    operation: str
    records: int = 0
    gap: bool = False
    chat_id: str = ""
    up_to_ns: int = 0
    delivered: bool | None = None

    def __post_init__(self) -> None:
        if (self.operation not in {"none", "events", "delivery"}
                or type(self.records) is not int or self.records < 0
                or type(self.gap) is not bool
                or type(self.chat_id) is not str
                or type(self.up_to_ns) is not int or self.up_to_ns < 0
                or self.delivered not in (None, False, True)):
            raise ValueError("invalid node event pump result")
        if self.operation == "none" and (
                self.records or self.gap or self.chat_id
                or self.up_to_ns or self.delivered is not None):
            raise ValueError("invalid empty node event pump result")
        if self.operation == "events" and (
                not (self.records or self.gap) or self.chat_id
                or self.up_to_ns or self.delivered is not None):
            raise ValueError("invalid node event claim result")
        if self.operation == "delivery" and (
                self.records or self.gap or not self.chat_id
                or not self.up_to_ns or self.delivered is None):
            raise ValueError("invalid node delivery result")


class NodeEventPump:
    """Consume one bounded journal claim or delivered-receipt attempt."""

    def __init__(
        self, store, bus: EventBus, messaging, user: str, *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (type(bus) is not EventBus or not callable(clock)
                or type(user) is not str or not user
                or not callable(getattr(messaging, "mark_delivered", None))):
            raise TypeError("invalid node event pump owner")
        self.store = store
        self.bus = bus
        self.messaging = messaging
        self.user = user
        self.clock = clock
        self._next_due = 0.0
        self._delivery_cursor = ""
        self._delivery_retry = OrderedDict()
        self._prefer_delivery = False
        node_event_journal.initialize(store)

    @classmethod
    def for_mesh(
        cls, mesh, *, clock: Callable[[], float] = time.monotonic,
    ) -> "NodeEventPump":
        from .service import Mesh

        if (type(mesh) is not Mesh or mesh.messaging.store is not mesh.store
                or mesh.messaging.user != mesh.user):
            raise TypeError("invalid node event mesh owner")
        return cls(mesh.store, mesh.bus, mesh.messaging, mesh.user, clock=clock)

    def wake(self) -> None:
        self._next_due = 0.0

    def wait_s(self, maximum: float = IDLE_RECHECK_S) -> float:
        if (type(maximum) not in (int, float) or isinstance(maximum, bool)
                or not math.isfinite(maximum) or not 0 <= maximum <= 300):
            raise ValueError("invalid node event wait")
        return min(float(maximum), max(0.0, self._next_due - self.clock()))

    def run_due(self) -> NodeEventPumpStep:
        now = self.clock()
        if now < self._next_due:
            return NodeEventPumpStep("none")
        delivery = None
        if self._prefer_delivery:
            delivery = self._run_delivery(now)
            if delivery.operation != 'none':
                self._prefer_delivery = False
                return delivery
        claim = node_event_journal.claim_events(self.store)
        if claim is not None:
            gap = claim.gap
            if gap:
                self._global_invalidation()
            for chat_id in dict.fromkeys(item.chat_id for item in claim.records):
                try:
                    self.bus.publish(Event(
                        eventbus.READ_MODEL, chat_id,
                        {"scope": "chat"}, ns=time.time_ns(),
                    ))
                except Exception:  # transient events are healed by refetch
                    gap = True
                    self._global_invalidation()
            self._next_due = 0.0
            self._prefer_delivery = True
            return NodeEventPumpStep(
                "events", records=len(claim.records), gap=gap,
            )
        return delivery if delivery is not None else self._run_delivery(now)

    def _run_delivery(self, now: float) -> NodeEventPumpStep:
        page = node_event_journal.delivery_page(
            self.store, after_chat=self._delivery_cursor,
        )
        if not page.deliveries:
            self._next_due = now + IDLE_RECHECK_S
            return NodeEventPumpStep("none")
        pending = next((item for item in page.deliveries
                        if self._delivery_retry.get(item.chat_id, 0.0) <= now), None)
        if pending is None:
            self._delivery_cursor = page.deliveries[-1].chat_id
            self._next_due = min(
                self._delivery_retry.values(), default=now + IDLE_RECHECK_S,
            )
            return NodeEventPumpStep("none")
        self._delivery_cursor = pending.chat_id
        try:
            self.messaging.mark_delivered(
                pending.chat_id, up_to_ns=pending.max_ns,
            )
            node_event_journal.complete_delivery(self.store, pending)
        except Exception:  # receipt failure never breaks admitted event delivery
            overflow = (pending.chat_id not in self._delivery_retry
                        and len(self._delivery_retry) >= 128)
            self._delivery_retry[pending.chat_id] = now + DELIVERY_RETRY_S
            self._delivery_retry.move_to_end(pending.chat_id)
            while len(self._delivery_retry) > 128:
                self._delivery_retry.popitem(last=False)
            self._next_due = now + DELIVERY_RETRY_S if overflow else 0.0
            return NodeEventPumpStep(
                "delivery", chat_id=pending.chat_id,
                up_to_ns=pending.max_ns, delivered=False,
            )
        self._delivery_retry.pop(pending.chat_id, None)
        self._next_due = 0.0
        return NodeEventPumpStep(
            "delivery", chat_id=pending.chat_id,
            up_to_ns=pending.max_ns, delivered=True,
        )

    def _global_invalidation(self) -> None:
        try:
            self.bus.publish(Event(
                eventbus.READ_MODEL, data={"scope": "global"},
                ns=time.time_ns(),
            ))
        except Exception:
            pass  # EventBus is best effort; canonical reads remain authoritative.


__all__ = ["NodeEventPump", "NodeEventPumpStep"]
