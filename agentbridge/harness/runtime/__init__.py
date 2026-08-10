"""Versioned contracts and secure runtime-control primitives.

C1.0 freezes the generic record contracts. C1.1 adds a production, room-bound
permission lane with pairwise encryption, signatures, epoch validation, and
durable one-use decisions. C1.4 adds canonical room-encrypted run events while
preserving the old run feed as a compatibility projection. C3.1 adds canonical
same-room child-task offers and destination-authored handoff decisions without
claiming execution routing. C3.2 adds one manager-retained, depth-one,
zero-capability text-only child execution path with durable causal recovery;
true execution handoff and broader capability records remain dormant until
their complete vertical slices land.
"""

from .models import (
    ContinuationRecord,
    ContinuationState,
    ControlRecord,
    ControlState,
    ControlType,
    EffectRecord,
    EffectState,
    HandoffRecord,
    HandoffState,
    HandoffType,
    RecordKind,
    RecordMeta,
    RunRecord,
    RunState,
    RuntimeContractError,
    RuntimeEnvelope,
    TaskRecord,
    TaskState,
    canonical_json_bytes,
    record_from_dict,
)
from .runs import RunLedger, RunLedgerError
from .tasks import TaskLedger, TaskLedgerError
from .handoffs import HandoffLedger, HandoffLedgerError, HandoffView

__all__ = [
    "ContinuationRecord", "ContinuationState", "ControlRecord", "ControlState",
    "ControlType", "EffectRecord", "EffectState", "HandoffRecord", "HandoffState",
    "HandoffType", "RecordKind", "RecordMeta", "RunRecord", "RunState",
    "RuntimeContractError", "RuntimeEnvelope", "TaskRecord", "TaskState",
    "RunLedger", "RunLedgerError", "canonical_json_bytes", "record_from_dict",
    "TaskLedger", "TaskLedgerError", "HandoffLedger", "HandoffLedgerError",
    "HandoffView",
]
