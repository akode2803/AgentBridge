"""Versioned contracts for the future signed runtime data plane.

These models are deliberately not wired into the current ``status/`` control
paths. C1.0 freezes validation and canonical spelling; later C1 releases add
encryption, signatures, transport, replay protection, and compatibility reads.
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
