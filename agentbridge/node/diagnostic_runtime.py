"""Disabled-by-default composition for private local-node proof runs.

This module does not integrate with the GUI or harness and never admits a
candidate.  One call advances at most one recovery or reference data page.  All
continuations remain Store-owned, so replacing this coordinator instance does
not restart accepted work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..transport.base import Transport
from .admission import NodeGenerationChanged, NodeInputError
from .equivalence import RecoveryEquivalenceRecorder
from .recovery import InactiveRecoveryExecutor, RecoveryStepResult, _provider_cut
from .reference import InactiveReferenceExecutor, ReferenceStepResult
from .store import NodeStore

LOCAL_NODE_DIAGNOSTIC_ENV = "AGENTBRIDGE_LOCAL_NODE_DIAGNOSTIC"
DIAGNOSTIC_REFERENCE_LABEL = "local-node-independent-reference-v1"


def diagnostic_runtime_enabled(environment: Mapping[str, str]) -> bool:
    """Require one exact opt-in value; absence and malformed values stay off."""
    if not isinstance(environment, Mapping):
        raise TypeError("expected diagnostic environment mapping")
    return environment.get(LOCAL_NODE_DIAGNOSTIC_ENV) == "1"


@dataclass(frozen=True)
class NodeDiagnosticStepResult:
    state: str
    phase: str
    action: str
    recovery_id: str | None = None
    reference_id: str | None = None
    recovery_generation: int | None = None
    reference_generation: int | None = None
    source_cursor: int | None = None
    outcome: str | None = None
    evidence_path: str | None = None

    def __post_init__(self) -> None:
        if self.state not in ("disabled", "running", "completed"):
            raise ValueError("invalid diagnostic runtime state")
        if self.phase not in ("disabled", "recovery", "reference", "equivalence"):
            raise ValueError("invalid diagnostic runtime phase")


class LocalNodeDiagnosticRuntime:
    """Advance one private recovery/reference comparison when explicitly enabled."""

    def __init__(
        self, store: NodeStore, transport: Transport, evidence_directory: Path,
        *, enabled: bool = False,
    ) -> None:
        if (type(store) is not NodeStore or not isinstance(transport, Transport)
                or not isinstance(evidence_directory, Path)):
            raise TypeError("expected node Store, transport and evidence path")
        if type(enabled) is not bool:
            raise TypeError("expected diagnostic feature gate")
        self.store = store
        self.transport = transport
        self.evidence_directory = evidence_directory
        self.enabled = enabled

    @staticmethod
    def _attempted_ns(value: object) -> int:
        if type(value) is not int or not 0 < value <= 2**63 - 1:
            raise NodeInputError("invalid diagnostic attempt time")
        return value

    def _advance_recovery(self, attempted: int) -> RecoveryStepResult:
        executor = InactiveRecoveryExecutor(self.store, self.transport)
        result = executor.step(None, attempted_ns=attempted)
        if result.action == "resumed":
            result = executor.step(result.recovery_id, attempted_ns=attempted)
        return result

    def _advance_reference(self, attempted: int) -> ReferenceStepResult:
        executor = InactiveReferenceExecutor(self.store, self.transport)
        result = executor.step(None, attempted_ns=attempted)
        if result.action == "resumed":
            result = executor.step(result.reference_id, attempted_ns=attempted)
        return result

    def _abandon(self, *generations: int | None) -> None:
        for generation in generations:
            if generation is not None:
                self.store.abandon_candidate(generation)

    def step(self, *, attempted_ns: int) -> NodeDiagnosticStepResult:
        """Advance bounded private work; completed evidence never affects reads."""
        attempted = self._attempted_ns(attempted_ns)
        if not self.enabled:
            return NodeDiagnosticStepResult("disabled", "disabled", "disabled")

        try:
            recovery = self._advance_recovery(attempted)
        except NodeGenerationChanged:
            return NodeDiagnosticStepResult(
                "running", "recovery", "restarted")
        if recovery.state != "sealed":
            return NodeDiagnosticStepResult(
                "running", "recovery", recovery.action,
                recovery_id=recovery.recovery_id,
                recovery_generation=recovery.generation,
                source_cursor=recovery.target_cursor,
            )

        try:
            reference = self._advance_reference(attempted)
        except NodeGenerationChanged:
            self._abandon(recovery.generation)
            return NodeDiagnosticStepResult(
                "running", "reference", "restarted")
        if reference.source_cursor != recovery.target_cursor:
            self._abandon(recovery.generation, reference.generation)
            return NodeDiagnosticStepResult(
                "running", "reference", "restarted")
        if reference.state != "sealed":
            return NodeDiagnosticStepResult(
                "running", "reference", reference.action,
                recovery_id=recovery.recovery_id,
                reference_id=reference.reference_id,
                recovery_generation=recovery.generation,
                reference_generation=reference.generation,
                source_cursor=reference.source_cursor,
            )

        current = _provider_cut(self.transport.recovery_fence())
        try:
            compared = RecoveryEquivalenceRecorder(
                self.store, self.evidence_directory,
            ).compare_candidates(
                recovery.recovery_id, reference.reference_id, current,
                observed_ns=attempted, label=DIAGNOSTIC_REFERENCE_LABEL,
            )
        except NodeGenerationChanged:
            self._abandon(recovery.generation, reference.generation)
            return NodeDiagnosticStepResult(
                "running", "equivalence", "restarted")
        return NodeDiagnosticStepResult(
            "completed", "equivalence", "recorded",
            recovery_id=compared.recovery_id,
            reference_id=compared.reference_id,
            recovery_generation=compared.recovery_generation,
            reference_generation=compared.reference_generation,
            source_cursor=compared.target_cursor,
            outcome=compared.outcome,
            evidence_path=compared.evidence_path,
        )


__all__ = [
    "DIAGNOSTIC_REFERENCE_LABEL", "LOCAL_NODE_DIAGNOSTIC_ENV",
    "LocalNodeDiagnosticRuntime", "NodeDiagnosticStepResult",
    "diagnostic_runtime_enabled",
]
