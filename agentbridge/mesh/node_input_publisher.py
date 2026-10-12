"""Caller-driven publication of coherent local-node chat inputs.

This owner performs no foreground repair and creates no thread. Requests are
latency hints only; each due job either observes an already-published node
generation or attempts one complete coupled publication. Canonical readers keep
using the last admitted Store snapshot while background acquisition fails.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..node.capture_inputs import DetachedInputError, DetachedInputOverflow
from ..node.client import (
    LocalNodeClient, NodeClientGenerationChanged, NodeClientReset,
    NodeClientUnavailable,
)
from ..node.protocol import NodeStatus, ProtocolError
from ..store import (
    coupled_admission, coupled_publication, local_source,
    node_publication_receipt, source_selectors,
)
from ..store.mutation_coordinator import MutationCoordinator
from ..store.source_publication import SourcePublisher
from .local_page_source import LocalPageSource
from .source_schedule import SourceSchedule


@dataclass(frozen=True)
class NodeInputPublishResult:
    chat_id: str
    state: str
    changed: bool
    node_generation: int
    source: local_source.SourcePosition | None

    def __post_init__(self) -> None:
        if self.state not in {
                "published", "unchanged", "pending", "catching_up",
                "degraded", "unavailable", "superseded", "budget",
                "invalid", "source_mutation_pending"}:
            raise ValueError("invalid node input publication state")
        if (type(self.changed) is not bool
                or type(self.node_generation) is not int
                or self.node_generation < 0
                or (self.source is not None
                    and type(self.source) is not local_source.SourcePosition)):
            raise ValueError("invalid node input publication result")


class NodeInputPublisher:
    """Prioritize selected chats while publishing only complete node cuts."""

    def __init__(
        self, coordinator: MutationCoordinator, store, client: LocalNodeClient,
        *, background_s: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (type(coordinator) is not MutationCoordinator
                or type(client) is not LocalNodeClient or not callable(clock)):
            raise TypeError("invalid node input publisher owner")
        self.coordinator = coordinator
        self.store = store
        self.client = client
        self.clock = clock
        local_source.initialize(store)
        source_selectors.initialize(store)
        coupled_admission.initialize(store)
        node_publication_receipt.initialize(store)
        coordinator.register_store(store)
        self.schedule = SourceSchedule(background_s=background_s)

    def reader(self, chat_id: str) -> LocalPageSource:
        return LocalPageSource(self.coordinator, self.store, chat_id)

    def request(
        self, chat_id: str, *, selected: bool = False,
        activity: bool = False, settled: bool = False,
    ) -> bool:
        chat = self.reader(chat_id).chat
        return self.schedule.request(
            chat, now=self.clock(), selected=selected,
            activity=activity, settled=settled,
        )

    def discover(self, chat_id: str) -> bool:
        chat = self.reader(chat_id).chat
        return self.schedule.discover(chat, now=self.clock())

    def clear_selection(self) -> None:
        self.schedule.clear_selection()

    def wait_s(self, maximum: float = 0.35) -> float:
        return self.schedule.wait_s(now=self.clock(), maximum=maximum)

    def _published_node_receipt(
        self, reader: LocalPageSource, source: local_source.SourcePosition,
        status: NodeStatus,
    ):
        if not source.ready or source.writes_pending:
            return None
        try:
            receipt = reader.capture()
        except node_publication_receipt.NodePublicationReceiptChanged:
            # A complete republish can repair stale/missing coupled provenance;
            # pages remain unavailable until that atomic replacement commits.
            return None
        if receipt.source != source or receipt.node_position is None:
            return None
        position = receipt.node_position
        identity = self.client.identity
        database_path = str(
            (self.client.directory / "replica.sqlite3").resolve()
        )
        matches = (
            position.database_path == database_path
            and position.database_incarnation == status.database_incarnation
            and position.identity_digest == status.identity_digest
            and position.provider_endpoint == identity.provider_endpoint
            and position.root == identity.root
            and position.admitted_generation == status.admitted_generation
        )
        return receipt if matches else None

    def _failure(
        self, chat: str, source: local_source.SourcePosition | None,
        state: str, reason: str, generation: int = 0,
    ) -> NodeInputPublishResult:
        if source is not None:
            try:
                local_source.record_refresh_failure(
                    self.store, source, reason=reason,
                )
            except (local_source.SourceChanged, OSError):
                pass
        return NodeInputPublishResult(
            chat, state, False, generation, source,
        )

    def publish(self, chat_id: str) -> NodeInputPublishResult:
        """Attempt one bounded publication without hiding the previous cut."""
        reader = self.reader(chat_id)
        chat = reader.chat
        source = None
        status = None
        try:
            captured = SourcePublisher(
                self.coordinator, self.store, reader.definition,
            ).capture()
            source = captured.source
            status = self.client.status()
            generation = status.admitted_generation
            if generation < 1:
                return self._failure(
                    chat, source, "pending", "incomplete", generation,
                )
            receipt = self._published_node_receipt(reader, source, status)
            if receipt is not None:
                state = (
                    "unchanged" if status.health == "ready"
                    else status.health if status.health in {
                        "pending", "catching_up", "degraded", "unavailable"
                    } else "pending"
                )
                if not reader.record_refresh_observation(
                        receipt, health=status.health,
                        last_success_ns=status.last_success_ns):
                    return NodeInputPublishResult(
                        chat, "superseded", False, generation, source,
                    )
                return NodeInputPublishResult(
                    chat, state, False, generation, source,
                )
            if status.pending_mutations:
                return NodeInputPublishResult(
                    chat, "pending", False, generation, source,
                )
            expected = coupled_admission.capture(
                self.store, source, chat,
            )
            result = coupled_publication.publish(
                self.coordinator, self.store, expected, self.client,
                reader.definition, observed_ns=time.time_ns(),
            )
            return NodeInputPublishResult(
                chat, "published", True,
                result.node_position.admitted_generation, result.source,
            )
        except local_source.SourceChanged as exc:
            state = (
                "source_mutation_pending"
                if exc.args == ("source_mutation_pending",)
                else "superseded"
            )
            return NodeInputPublishResult(
                chat, state, False,
                0 if status is None else status.admitted_generation, source,
            )
        except (NodeClientGenerationChanged, NodeClientReset,
                coupled_admission.CoupledAdmissionChanged):
            return NodeInputPublishResult(
                chat, "superseded", False,
                0 if status is None else status.admitted_generation, source,
            )
        except (DetachedInputOverflow, OverflowError):
            return self._failure(
                chat, source, "budget", "budget",
                0 if status is None else status.admitted_generation,
            )
        except NodeClientUnavailable:
            return self._failure(
                chat, source, "unavailable", "unavailable",
                0 if status is None else status.admitted_generation,
            )
        except (DetachedInputError, ProtocolError, ValueError):
            return self._failure(
                chat, source, "invalid", "conflict",
                0 if status is None else status.admitted_generation,
            )
        except OSError:
            return self._failure(
                chat, source, "unavailable", "io",
                0 if status is None else status.admitted_generation,
            )

    def run_due(self) -> bool:
        job = self.schedule.take_due(now=self.clock())
        if job is None:
            return False
        result = None
        try:
            result = self.publish(job.chat_id)
        except Exception:
            # A programming/storage failure must not strand the scheduler's
            # only in-flight lease. Direct callers may still use publish() when
            # they need the exact exception boundary.
            pass
        if result is None:
            self.schedule.finish(
                job, now=self.clock(), changed=False, success=False,
            )
        else:
            success = result.state in {
                "published", "unchanged", "pending", "catching_up",
            }
            blocked = result.state == "source_mutation_pending"
            self.schedule.finish(
                job, now=self.clock(), changed=result.changed,
                success=success and not blocked, blocked=blocked,
            )
        return True


__all__ = ["NodeInputPublishResult", "NodeInputPublisher"]
