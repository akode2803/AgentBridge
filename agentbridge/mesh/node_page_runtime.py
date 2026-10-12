"""Threadless orchestration of provider-to-node and node-to-Store work.

The app owner calls ``run_due`` and supplies content-free wakes.  One call
performs at most one bounded provider, Store-publication, event-claim, or
delivered-receipt operation. A newly selected chat gets the first opportunity
to use already-admitted local node data before any provider fallback work.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..node.client import LocalNodeClient
from ..node.server import LocalNodeServer
from ..node.sync_scheduler import LocalNodeSyncScheduler, NodeSyncTickResult
from .node_event_pump import NodeEventPump, NodeEventPumpStep
from .node_input_publisher import NodeInputPublisher, NodeInputPublishResult
from .source_schedule import SELECTION_LEASE_S


@dataclass(frozen=True)
class NodePageRuntimeStep:
    operation: str
    next_due_s: float
    node: NodeSyncTickResult | None = None
    publication: NodeInputPublishResult | None = None
    event: NodeEventPumpStep | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"none", "node", "publication", "event"}:
            raise ValueError("invalid node page runtime operation")
        if (type(self.next_due_s) is not float
                or not math.isfinite(self.next_due_s)
                or not 0 <= self.next_due_s <= 300.0
                or (self.operation == "node") != (self.node is not None)
                or (self.publication is not None and (
                    self.operation != "publication"
                    or type(self.publication) is not NodeInputPublishResult
                ))
                or (self.event is not None and (
                    self.operation != "event"
                    or type(self.event) is not NodeEventPumpStep
                    or self.event.operation == "none"
                ))
                or (self.operation == "event") != (self.event is not None)):
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
        self._lane_cursor = 0
        self._lock = threading.RLock()
        self._source_owner = None
        self._events: NodeEventPump | None = None
        self._event_source_owner = None
        self._publication_activation = object()
        self.node = LocalNodeSyncScheduler(
            server, foreground=False,
            realtime_healthy=realtime_healthy, clock_ns=clock_ns,
        )
        self.publisher = NodeInputPublisher(
            server.recovery_runtime.coordinator, store,
            LocalNodeClient(server.home, server.identity), clock=clock,
            activation_token=self._publication_activation,
        )

    def bind_event_owner(self, mesh, source_owner) -> None:
        """Bind the exact Mesh and source-handoff owner before activation."""
        from .local_input_runtime import LocalInputRuntime
        from .sync import SyncEngine

        with self._lock:
            if self._events is not None or self._source_owner is not None:
                raise RuntimeError("node event owner already bound or active")
            if (type(source_owner) is not LocalInputRuntime
                    or source_owner.store is not self.store
                    or source_owner.transport is not self.server.recovery_runtime.transport
                    or mesh.local_inputs is not source_owner
                    or source_owner._page_preparation is None
                    or source_owner._page_preparation.mesh is not mesh
                    or source_owner.read_events is None
                    or source_owner.read_events.bus is not mesh.bus
                    or type(mesh.sync) is not SyncEngine
                    or source_owner._sync_owner is not mesh.sync
                    or mesh.tx is not source_owner.transport):
                raise ValueError("foreign node event source owner")
            pump = NodeEventPump.for_mesh(mesh, clock=self.clock)
            if pump.store is not self.store:
                raise ValueError("foreign node event owner")
            self._events = pump
            self._event_source_owner = source_owner

    def _activate_source(self, owner, retirement) -> None:
        """Accept the exact retired legacy owner and enable Store writes once."""
        from .local_input_runtime import LocalInputRuntime
        from .sync import SyncEngine, SyncRetirement

        if (type(owner) is not LocalInputRuntime
                or type(retirement) is not SyncRetirement):
            raise TypeError("invalid node source activation")
        engine = retirement._engine
        if (type(engine) is not SyncEngine or engine is not owner._sync_owner
                or owner.store is not self.store
                or owner.coordinator is not self.publisher.coordinator
                or self.server.recovery_runtime.transport is not owner.transport):
            raise ValueError("foreign node source activation")
        engine.validate_retirement(
            retirement, owner.transport, owner.store, owner=owner,
        )
        with self._lock:
            if self._source_owner is not None:
                raise RuntimeError("node source already active")
            if self._events is None or self._event_source_owner is not owner:
                raise RuntimeError("node event owner is not bound")
            self.publisher._activate(self._publication_activation)
            self._publication_activation = None
            self._source_owner = owner

    def request(
        self, chat_id: str, *, selected: bool = False,
        activity: bool = False, settled: bool = False,
    ) -> bool:
        now = self.clock()
        with self._lock:
            self._expire_selection(now)
            chat = self.publisher.reader(chat_id).chat
            changed_route = selected and self.selected != chat
            accepted = self.publisher.request(
                chat, selected=selected, activity=activity, settled=settled,
            )
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
        if settled:
            # Settlement proves the durable mutation fence changed. It is a
            # wake for normal recovery checks, never evidence that the remote
            # write succeeded or that any page is fresh.
            self.node.wake("manual", now_ns=self.clock_ns())
        return accepted

    def discover(self, chat_id: str) -> bool:
        return self.publisher.discover(chat_id)

    def clear_selection(self) -> None:
        with self._lock:
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
        with self._lock:
            self._expire_selection(self.clock())
            self.realtime_healthy = healthy
            self.node.set_mode(
                foreground=self.selected is not None,
                realtime_healthy=healthy, now_ns=self.clock_ns(),
            )

    def wake(self, kind: str) -> int:
        with self._lock:
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
        with self._lock:
            self._expire_selection(self.clock())
            page = self.publisher.wait_s(
                maximum, chat_id=self._page_first_chat,
            )
            now_ns = self.clock_ns()
            node = max(0.0, (self.node.next_due_ns - now_ns) / 1_000_000_000)
            if self._page_first_chat is not None:
                return page
            events = (
                maximum if self._events is None
                else self._events.wait_s(maximum)
            )
            return min(maximum, page, node, events)

    def _result(
        self, operation: str, node: NodeSyncTickResult | None = None,
        publication: NodeInputPublishResult | None = None,
        event: NodeEventPumpStep | None = None,
    ) -> NodePageRuntimeStep:
        return NodePageRuntimeStep(
            operation, self.wait_s(300.0), node, publication, event,
        )

    def _run_event(self) -> NodePageRuntimeStep | None:
        with self._lock:
            events = self._events
        if events is None:
            return None
        result = events.run_due()
        if result.operation == "none":
            return None
        return self._result("event", event=result)

    def run_due(self) -> NodePageRuntimeStep:
        """Advance no more than one node or Store operation."""
        with self._lock:
            if self._source_owner is None:
                raise RuntimeError("node page runtime is inactive")
            self._expire_selection(self.clock())
            chat = self._page_first_chat
        if chat is not None:
            attempted, result = self.publisher.run_due_result(chat)
            if attempted:
                with self._lock:
                    if self._page_first_chat == chat:
                        self._page_first_chat = None
                    if (self._events is not None and result is not None
                            and result.changed):
                        self._events.wake()
                    self._lane_cursor = 1
                return self._result("publication", publication=result)
            return self._result("none")
        now_ns = self.clock_ns()
        with self._lock:
            cursor = self._lane_cursor
        lanes = ("publication", "node", "event")
        for offset in range(len(lanes)):
            index = (cursor + offset) % len(lanes)
            lane = lanes[index]
            step = None
            if lane == "publication":
                attempted, result = self.publisher.run_due_result()
                if attempted:
                    with self._lock:
                        if (self._events is not None and result is not None
                                and result.changed):
                            self._events.wake()
                    step = self._result("publication", publication=result)
            elif lane == "node" and now_ns >= self.node.next_due_ns:
                result = self.node.tick(now_ns=now_ns)
                if result.operation != "none":
                    with self._lock:
                        if (self.selected is not None
                                and result.admitted_generation > 0):
                            self.publisher.request(self.selected, activity=True)
                    step = self._result("node", result)
            elif lane == "event":
                step = self._run_event()
            if step is not None:
                with self._lock:
                    self._lane_cursor = (index + 1) % len(lanes)
                return step
        return self._result("none")


__all__ = ["LocalNodePageRuntime", "NodePageRuntimeStep"]
