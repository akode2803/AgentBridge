"""Versioned contracts and secure runtime-control primitives.

C1.0 freezes the generic record contracts. C1.1 adds a production, room-bound
permission lane with pairwise encryption, signatures, epoch validation, and
durable one-use decisions. Other runtime controls remain on their documented
legacy paths until their complete vertical slices land.
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

__all__ = [
    "ContinuationRecord", "ContinuationState", "ControlRecord", "ControlState",
    "ControlType", "EffectRecord", "EffectState", "HandoffRecord", "HandoffState",
    "HandoffType", "RecordKind", "RecordMeta", "RunRecord", "RunState",
    "RuntimeContractError", "RuntimeEnvelope", "TaskRecord", "TaskState",
    "canonical_json_bytes", "record_from_dict",
]
