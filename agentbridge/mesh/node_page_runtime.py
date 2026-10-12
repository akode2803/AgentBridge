"""Threadless orchestration of provider-to-node and node-to-Store work.

The app owner calls ``run_due`` and supplies content-free wakes.  One call
performs at most one bounded provider operation or one Store publication.  A
newly selected chat gets the first opportunity to use already-admitted local
node data before any provider fallback work begins.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from ..node.client import LocalNodeClient
from ..node.server import LocalNodeServer
from ..node.sync_scheduler import LocalNodeSyncScheduler, NodeSyncTickResult
from .node_input_publisher import NodeInputPublisher
from .source_schedule import SELECTION_LEASE_S


@dataclass(frozen=True)
class NodePageRuntimeStep:
    operation: str
    next_due_s: float
    node: NodeSyncTickResult | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"none", "node", "publication"}:
            raise ValueError("invalid node page runtime operation")
        if (type(self.next_due_s) is not float
                or not math.isfinite(self.next_due_s)
                or not 0 <= self.next_due_s <= 300.0
                or (self.operation == "node") != (self.node is not None)):
            raise ValueError("invalid node page runtime result")


class LocalNodePageRuntime:
    """Compose existing bounded owners without creating another thread."""

    def __init__(
        self, server: LocalNodeServer, store, *,
        realtime_healthy: bool = True,
        clock: Callable[[], float] = time.monotonic,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (type(server) is not LocalNodeServer
                or server.store is None or server.recovery_runtime is None
                or type(realtime_healthy) is not bool
                or not callable(clock) or not callable(clock_ns)):
            raise TypeError("expected running recovery-owned local node")
        if server.recovery_runtime.store is not server.store:
            raise ValueError("local node recovery owner changed")
        self.server = server
        self.store = store
        self.clock = clock
        self.clock_ns = clock_ns
        self.realtime_healthy = realtime_healthy
        self.selected: str | None = None
        self._selection_until = 0.0
        self._page_first_chat: str | None = None
        self._last_operation = "none"
        self.node = LocalNodeSyncScheduler(
            server, foreground=False,
            realtime_healthy=realtime_healthy, clock_ns=clock_ns,
        )
        self.publisher = NodeInputPublisher(
            server.recovery_runtime.coordinator, store,
            LocalNodeClient(server.home, server.identity), clock=clock,
        )

    def request(
        self, chat_id: str, *, selected: bool = False,
        activity: bool = False, settled: bool = False,
    ) -> bool:
        now = self.clock()
        self._expire_selection(now)
        chat = self.publisher.reader(chat_id).chat
        changed_route = selected and self.selected != chat
        accepted = self.publisher.request(
            chat, selected=selected, activity=activity, settled=settled,
        )
        if settled:
            # Settlement proves the durable mutation fence changed. It is a
            # wake for normal recovery checks, never evidence that the remote
            # write succeeded or that any page is fresh.
            self.node.wake("manual", now_ns=self.clock_ns())
        if accepted and selected:
            self.selected = chat
            self._selection_until = now + SELECTION_LEASE_S
            if changed_route:
                self._page_first_chat = chat
            self.node.set_mode(
                foreground=True,
                realtime_healthy=self.realtime_healthy,
                now_ns=self.clock_ns(),
            )
        return accepted

    def discover(self, chat_id: str) -> bool:
        return self.publisher.discover(chat_id)

    def clear_selection(self) -> None:
        self.selected = None
        self._selection_until = 0.0
        self._page_first_chat = None
        self.publisher.clear_selection()
        self.node.set_mode(
            foreground=False, realtime_healthy=self.realtime_healthy,
            now_ns=self.clock_ns(),
        )

    def set_realtime_healthy(self, healthy: bool) -> None:
        if type(healthy) is not bool:
            raise TypeError("invalid realtime health")
        self._expire_selection(self.clock())
        self.realtime_healthy = healthy
        self.node.set_mode(
            foreground=self.selected is not None,
            realtime_healthy=healthy, now_ns=self.clock_ns(),
        )

    def wake(self, kind: str) -> int:
        self._expire_selection(self.clock())
        return self.node.wake(kind, now_ns=self.clock_ns())

    def _expire_selection(self, now: float) -> None:
        if self.selected is None or now < self._selection_until:
            return
        self.clear_selection()

    def wait_s(self, maximum: float = 0.35) -> float:
        if (type(maximum) not in (int, float) or isinstance(maximum, bool)
                or not math.isfinite(maximum) or not 0 <= maximum <= 300):
            raise ValueError("invalid node page runtime wait")
        maximum = float(maximum)
        self._expire_selection(self.clock())
        page = self.publisher.wait_s(
            maximum, chat_id=self._page_first_chat,
        )
        now_ns = self.clock_ns()
        node = max(0.0, (self.node.next_due_ns - now_ns) / 1_000_000_000)
        if self._page_first_chat is not None:
            return page
        return min(maximum, page, node)

    def _result(
        self, operation: str, node: NodeSyncTickResult | None = None,
    ) -> NodePageRuntimeStep:
        return NodePageRuntimeStep(operation, self.wait_s(300.0), node)

    def run_due(self) -> NodePageRuntimeStep:
        """Advance no more than one node or Store operation."""
        self._expire_selection(self.clock())
        if self._page_first_chat is not None:
            chat = self._page_first_chat
            if self.publisher.run_due(chat):
                self._page_first_chat = None
                self._last_operation = "publication"
                return self._result("publication")
            return self._result("none")
        now_ns = self.clock_ns()
        node_due = now_ns >= self.node.next_due_ns
        # A bounded room backlog cannot starve provider progress. Alternate
        # planes whenever both remain due; selected page-first is the sole
        # exception and is consumed after one Store attempt above.
        if not (node_due and self._last_operation == "publication") \
                and self.publisher.run_due():
            self._last_operation = "publication"
            return self._result("publication")
        if not node_due:
            return self._result("none")
        result = self.node.tick(now_ns=now_ns)
        if result.operation == "none":
            return self._result("none")
        if (self.selected is not None and result.operation != "none"
                and result.admitted_generation > 0):
            self.publisher.request(self.selected, activity=True)
        self._last_operation = "node"
        return self._result("node", result)


__all__ = ["LocalNodePageRuntime", "NodePageRuntimeStep"]
