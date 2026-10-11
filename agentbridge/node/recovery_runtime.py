"""Explicit, inactive owner for coordinator-bound whole-root recovery.

The runtime performs no recurring work and is not imported by GUI or harness
code.  A caller request is coalesced in process, and each ``step`` advances at
most one bounded provider recovery operation before returning.  Product reads
remain on the existing local-input path.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from ..core.errors import TransportError
from ..store import local_source
from ..transport.local_mutations import LocalMutationTransport
from .admission import (
    NodeCandidateCapacityError, NodeGenerationChanged, NodeInputError,
    NodeRecoveryClaim,
)
from .recovery import ClaimedRecoveryExecutor
from .store import NodeStore


@dataclass(frozen=True)
class NodeRecoveryRuntimeResult:
    state: str
    action: str
    recovery_id: str | None = None
    generation: int | None = None
    target_cursor: int | None = None

    def __post_init__(self) -> None:
        if self.state not in (
                "idle", "busy", "running", "ready", "retry",
                "degraded", "unavailable"):
            raise ValueError("invalid local node recovery runtime state")
        if type(self.action) is not str or not self.action:
            raise ValueError("invalid local node recovery runtime action")


class LocalNodeRecoveryRuntime:
    """Advance explicitly requested recovery through the shared writer fence."""

    def __init__(self, store: NodeStore,
                 transport: LocalMutationTransport) -> None:
        if type(store) is not NodeStore \
                or type(transport) is not LocalMutationTransport:
            raise TypeError("expected node Store and mutation-owned transport")
        self.store = store
        self.transport = transport
        self.coordinator = transport._coordinator
        # Join the shared writer fence before this owner can advance recovery
        # or be composed with a loopback read server.
        self.coordinator.register_node(self.store)
        self._state_lock = threading.Lock()
        self._step_lock = threading.Lock()
        # A clean ready restart does not manufacture whole-root work. Cold,
        # pending, failed and interrupted nodes resume without an external hint.
        needs_recovery = self._durable_recovery_needed()
        self._requested_revision = int(needs_recovery)
        self._completed_revision = 0
        self._admission_retry: tuple[NodeRecoveryClaim, str, int] | None = None

    def request(self) -> int:
        """Coalesce a content-free recovery hint and return its local revision."""
        with self._state_lock:
            self._requested_revision += 1
            return self._requested_revision

    def _durable_recovery_needed(self) -> bool:
        status = self.store.status(node_epoch="", started_ns=0)
        return (self.store.claimed_recovery_id() is not None
                or status.admitted_generation == 0
                or status.pending_mutations > 0
                or (status.health != "ready"
                    and not self.store.has_incremental_source_position()))

    def pending(self) -> bool:
        with self._state_lock:
            process_pending = (self._completed_revision < self._requested_revision
                               or self._admission_retry is not None)
        return process_pending or self._durable_recovery_needed()

    def _finish(self, revision: int) -> None:
        with self._state_lock:
            self._completed_revision = max(self._completed_revision, revision)
            self._admission_retry = None

    def _clear_admission(self) -> None:
        with self._state_lock:
            self._admission_retry = None

    def _retry_admission(self) -> tuple[NodeRecoveryClaim, str, int] | None:
        with self._state_lock:
            return self._admission_retry

    def _remember_admission(self, claim: NodeRecoveryClaim,
                            recovery_id: str, revision: int) -> None:
        with self._state_lock:
            self._admission_retry = (claim, recovery_id, revision)

    def _failure(self, attempted_ns: int) -> NodeRecoveryRuntimeResult:
        status = self.store.status(node_epoch="", started_ns=0)
        health = "degraded" if status.admitted_generation else "unavailable"
        try:
            effective = self.store.note_refresh_state(
                health, attempted_ns=attempted_ns,
                expected_generation=status.admitted_generation,
            )
        except NodeGenerationChanged:
            return NodeRecoveryRuntimeResult("retry", "generation-changed")
        if effective == "pending":
            return NodeRecoveryRuntimeResult("retry", "mutation-pending")
        return NodeRecoveryRuntimeResult(effective, "provider-unavailable")

    def _admit(self, claim: NodeRecoveryClaim, recovery_id: str,
               revision: int, *, action: str,
               target_cursor: int | None = None) -> NodeRecoveryRuntimeResult:
        self._remember_admission(claim, recovery_id, revision)
        try:
            generation = self.coordinator.admit_node_recovery(
                self.store, claim, recovery_id)
        except (NodeGenerationChanged, local_source.SourceChanged):
            # A later mutation or coordinator movement has made this exact
            # claim terminally unusable. Keep the request pending, but do not
            # pin subsequent steps to a dead admission receipt identity.
            self._clear_admission()
            try:
                self.coordinator.abandon_node_recovery(
                    self.store, claim, recovery_id)
            except (NodeGenerationChanged, local_source.SourceChanged):
                # A later exact claim or coordinator owner wins. Never retire
                # it using evidence from this failed admission.
                pass
            return NodeRecoveryRuntimeResult(
                "retry", "cut-changed", recovery_id)
        except (TimeoutError, ConnectionError, OSError):
            # The node transaction may already have committed. Retain the exact
            # claim and recovery ID so the next step resolves only its receipt.
            return NodeRecoveryRuntimeResult(
                "retry", "admission-uncertain", recovery_id)
        self._clear_admission()
        status = self.store.status(node_epoch="", started_ns=0)
        if status.admitted_generation != generation:
            return NodeRecoveryRuntimeResult(
                "retry", "generation-changed", recovery_id, generation,
                target_cursor,
            )
        if status.pending_mutations:
            return NodeRecoveryRuntimeResult(
                "retry", "mutation-pending", recovery_id, generation,
                target_cursor,
            )
        self._finish(revision)
        return NodeRecoveryRuntimeResult(
            "ready", action, recovery_id, generation, target_cursor)

    def step(self, *, attempted_ns: int) -> NodeRecoveryRuntimeResult:
        """Advance one requested unit; never starts a loop or serves inputs."""
        if type(attempted_ns) is not int or not 0 < attempted_ns <= 2**63 - 1:
            raise NodeInputError("invalid recovery runtime attempt time")
        if not self._step_lock.acquire(blocking=False):
            return NodeRecoveryRuntimeResult("busy", "step-in-progress")
        try:
            with self._state_lock:
                revision = self._requested_revision
                pending = (self._completed_revision < revision
                           or self._admission_retry is not None)
            if not pending and self._durable_recovery_needed():
                with self._state_lock:
                    self._requested_revision += 1
                    revision = self._requested_revision
                pending = True
            if not pending:
                return NodeRecoveryRuntimeResult("idle", "no-request")

            # Registration is idempotent and must precede every provider step.
            # A replaced coordinator or node therefore fails before remote I/O.
            self.coordinator.register_node(self.store)

            remembered = self._retry_admission()
            if remembered is not None:
                claim, recovery_id, admission_revision = remembered
                return self._admit(
                    claim, recovery_id, admission_revision,
                    action="admission-confirmed")

            status = self.store.status(node_epoch="", started_ns=0)
            self.store.note_refresh_state(
                "catching_up", attempted_ns=attempted_ns,
                expected_generation=status.admitted_generation,
            )
            result = ClaimedRecoveryExecutor(
                self.store, self.transport, self.coordinator,
            ).step(None, attempted_ns=attempted_ns)
            if result.state != "sealed":
                return NodeRecoveryRuntimeResult(
                    "running", result.action, result.recovery_id,
                    result.generation, result.target_cursor,
                )

            claimed = self.store.claimed_recovery()
            if claimed is None or claimed[1] != result.recovery_id:
                raise sqlite3.DatabaseError(
                    "sealed claimed recovery is not durably bound")
            claim, recovery_id = claimed
            return self._admit(
                claim, recovery_id, revision,
                action="admitted", target_cursor=result.target_cursor)
        except (NodeGenerationChanged, local_source.SourceChanged,
                NodeCandidateCapacityError):
            return NodeRecoveryRuntimeResult("retry", "cut-changed")
        except (TransportError, TimeoutError, ConnectionError, OSError):
            return self._failure(attempted_ns)
        finally:
            self._step_lock.release()


__all__ = ["LocalNodeRecoveryRuntime", "NodeRecoveryRuntimeResult"]
