"""Shadow local admitted-input node foundation.

The package is deliberately not imported by the GUI or harness.  Its store can
atomically admit detached provider observations, but it performs no provider I/O
and is never a canonical authority cache.
"""

from .admission import (
    NodeCapture,
    NodeCaptureRequest,
    NodeChangePage,
    NodeDocument,
    NodeFrontier,
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
from .recovery import InactiveRecoveryExecutor, RecoveryStepResult
from .server import LocalNodeServer
from .store import NodeStore

__all__ = [
    "PROTOCOL_VERSION",
    "LocalNodeServer",
    "LocalNodeDiagnosticRuntime",
    "InactiveRecoveryExecutor",
    "NodeCapture",
    "NodeCaptureRequest",
    "NodeChangePage",
    "NodeDocument",
    "NodeFrontier",
    "NodeGenerationChanged",
    "NodeInputBatch",
    "NodeInputError",
    "NodeLogRequest",
    "NodeLogRow",
    "NodeStatus",
    "NodeDiagnosticStepResult",
    "NodeStore",
    "NodeVisibility",
    "ReplicaIdentity",
    "RecoveryEquivalenceRecorder",
    "RecoveryEquivalenceReference",
    "RecoveryEquivalenceResult",
    "RecoveryStepResult",
    "diagnostic_runtime_enabled",
]
