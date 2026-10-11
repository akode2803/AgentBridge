"""Shadow local admitted-input node foundation.

The package is deliberately not imported by the GUI or harness.  Its store can
atomically admit detached provider observations, but it performs no provider I/O
and is never a canonical authority cache.
"""

from .admission import (
    NodeCapture,
    NodeCaptureRequest,
    NodeCoordinatorRegistration,
    NodeRecoveryClaim,
    NodeChangePage,
    NodeCandidateCapacityError,
    NodeDocument,
    NodeFrontier,
    NodeFinalizationPosition,
    NodeGenerationChanged,
    NodeInputBatch,
    NodeInputError,
    NodeLogRequest,
    NodeLogRow,
    NodeVisibility,
)
from .protocol import PROTOCOL_VERSION, NodeStatus, ReplicaIdentity
from .equivalence import (
    RecoveryEquivalenceRecorder, RecoveryEquivalenceReference,
    RecoveryEquivalenceResult,
)
from .diagnostic_runtime import (
    LocalNodeDiagnosticRuntime, NodeDiagnosticStepResult,
    diagnostic_runtime_enabled,
)
from .recovery import ClaimedRecoveryExecutor, InactiveRecoveryExecutor, RecoveryStepResult
from .recovery_runtime import LocalNodeRecoveryRuntime, NodeRecoveryRuntimeResult
from .server import LocalNodeServer
from .store import NodeStore

__all__ = [
    "PROTOCOL_VERSION",
    "LocalNodeServer",
    "LocalNodeDiagnosticRuntime",
    "LocalNodeRecoveryRuntime",
    "ClaimedRecoveryExecutor",
    "InactiveRecoveryExecutor",
    "NodeCapture",
    "NodeCaptureRequest",
    "NodeCoordinatorRegistration",
    "NodeRecoveryClaim",
    "NodeCandidateCapacityError",
    "NodeChangePage",
    "NodeDocument",
    "NodeFrontier",
    "NodeFinalizationPosition",
    "NodeGenerationChanged",
    "NodeInputBatch",
    "NodeInputError",
    "NodeLogRequest",
    "NodeLogRow",
    "NodeStatus",
    "NodeDiagnosticStepResult",
    "NodeRecoveryRuntimeResult",
    "NodeStore",
    "NodeVisibility",
    "ReplicaIdentity",
    "RecoveryEquivalenceRecorder",
    "RecoveryEquivalenceReference",
    "RecoveryEquivalenceResult",
    "RecoveryStepResult",
    "diagnostic_runtime_enabled",
]
