"""In-process event bus (R10) — the one place change-events fan out.

Producers: the sync engine (new records pulled from the transport) and local
actions. Consumers: the notifier (below), the GUI connector's SSE stream
(R13), the MCP server's notifications (R12), and the harness queue (R15).

Delivery is best-effort per subscriber (bounded queue, drop-oldest): a slow
consumer can never stall the sync loop — the store remains the source of
truth and a dropped event only delays a repaint until the next poll.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = ["Event", "EventBus", "Subscription"]

# event types
MESSAGE = "message"            # a new message envelope landed
CHAT_UPDATE = "chat_update"    # info event: membership/name/permissions moved
ADDED_TO_CHAT = "added_to_chat"  # this identity was added to a chat


@dataclass
class Event:
    type: str
    chat_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ns: int = 0


class Subscription:
    def __init__(self, bus: "EventBus", maxsize: int = 1000) -> None:
        self._bus = bus
        self._q: queue.Queue[Event] = queue.Queue(maxsize=maxsize)

    def _offer(self, event: Event) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:  # drop-oldest: the poll loop heals any gap
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(event)
            except queue.Full:
                pass

    def get(self, timeout: float | None = None) -> Event | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> Iterator[Event]:
        while True:
            try:
                yield self._q.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._bus.unsubscribe(self)


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []

    def subscribe(self, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub._offer(event)
