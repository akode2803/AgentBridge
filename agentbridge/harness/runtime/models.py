"""Immutable C1 runtime-record contracts with fail-closed parsing.

This module specifies plaintext records and the authenticated envelope that a
later data-plane release will seal. It performs no I/O and is not an
authorization mechanism by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, ClassVar

from ...core.jsonkit import canonical_json_bytes as _canonical_json_bytes


SCHEMA_VERSION = 1


class RuntimeContractError(ValueError):
    """A runtime record does not satisfy the frozen contract."""


class RecordKind(str, Enum):
    RUN = "run"
    TASK = "task"
    HANDOFF = "handoff"
    EFFECT = "effect"
    CONTINUATION = "continuation"
    CONTROL = "control"


class RunState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class TaskState(str, Enum):
    QUEUED = "queued"
    OFFERED = "offered"
    ACTIVE = "active"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    RETURNED = "returned"


class HandoffType(str, Enum):
    AGENT_TOOL = "agent_tool"
    HANDOFF = "handoff"


class HandoffState(str, Enum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    AUTHORIZED = "authorized"
    ACTIVE = "active"
    RETURNED = "returned"
    CONSUMED = "consumed"
    DECLINED = "declined"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class EffectState(str, Enum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ContinuationState(str, Enum):
    PAUSED = "paused"
    READY = "ready"
    RESUMED = "resumed"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"


class ControlType(str, Enum):
    ASK = "ask"
    ANSWER = "answer"
    GRANT = "grant"
    REVOKE = "revoke"
    STOP = "stop"
    TIMER_CANCEL = "timer_cancel"
    PAUSE = "pause"
    RESUME = "resume"
    APPLINK = "applink"


class ControlState(str, Enum):
    REQUESTED = "requested"
    ALLOWED = "allowed"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def canonical_json_bytes(value: Any) -> bytes:
    """Return the sole JSON spelling used for digests, AAD, and signatures."""
    try:
        return _canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"value is not canonical JSON: {exc}") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _texts(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise RuntimeContractError(f"{name} must be a string array")
    result = tuple(_text(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise RuntimeContractError(f"{name} must not contain duplicates")
    return result


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeContractError(f"{name} must be an integer >= {minimum}")
    return value


def _enum(enum_type: type[Enum], value: Any, name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"unknown {name}: {value!r}") from exc


def _enum_instance(value: Any, enum_type: type[Enum], name: str) -> None:
    if not isinstance(value, enum_type):
        raise RuntimeContractError(f"{name} must be {enum_type.__name__}")


def _frozen_texts(value: Any, name: str) -> None:
    if not isinstance(value, tuple):
        raise RuntimeContractError(f"{name} must be an immutable string tuple")
    _texts(value, name)


def _strict_dict(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeContractError(f"{name} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeContractError(f"invalid {name} fields; missing={missing}, extra={extra}")
    return value


def _encoded(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_encoded(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass(frozen=True, slots=True)
class RecordMeta:
    """Identity, lineage, and policy bindings common to every runtime record."""

    schema_version: int
    kind: RecordKind
    id: str
    ns: int
    actor: str
    chat_id: str
    signer: str
    root_run_id: str
    run_id: str | None
    task_id: str | None
    call_id: str | None
    key_epoch: int
    policy_revision: int
    membership_epoch: int
    ownership_epoch: int
    expires_ns: int | None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeContractError(f"unsupported schema_version: {self.schema_version!r}")
        if not isinstance(self.kind, RecordKind):
            raise RuntimeContractError("kind must be a RecordKind")
        for name in ("id", "actor", "chat_id", "signer", "root_run_id"):
            _text(getattr(self, name), name)
        for name in ("run_id", "task_id", "call_id"):
            _optional_text(getattr(self, name), name)
        _integer(self.ns, "ns", minimum=1)
        for name in ("key_epoch", "policy_revision", "membership_epoch", "ownership_epoch"):
            _integer(getattr(self, name), name)
        if self.expires_ns is not None:
            _integer(self.expires_ns, "expires_ns", minimum=1)
            if self.expires_ns <= self.ns:
                raise RuntimeContractError("expires_ns must be greater than ns")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: _encoded(getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_dict(cls, value: Any) -> RecordMeta:
        data = _strict_dict(value, {field.name for field in fields(cls)}, "record meta")
        return cls(
            schema_version=data["schema_version"],
            kind=_enum(RecordKind, data["kind"], "record kind"),
            id=data["id"], ns=data["ns"], actor=data["actor"],
            chat_id=data["chat_id"], signer=data["signer"],
            root_run_id=data["root_run_id"], run_id=data["run_id"],
            task_id=data["task_id"], call_id=data["call_id"],
            key_epoch=data["key_epoch"], policy_revision=data["policy_revision"],
            membership_epoch=data["membership_epoch"],
            ownership_epoch=data["ownership_epoch"], expires_ns=data["expires_ns"],
        )


@dataclass(frozen=True, slots=True)
class _Record:
    meta: RecordMeta
    KIND: ClassVar[RecordKind]

    def __post_init__(self) -> None:
        if not isinstance(self.meta, RecordMeta) or self.meta.kind is not self.KIND:
            raise RuntimeContractError(f"record metadata must bind kind={self.KIND.value}")

    def to_dict(self) -> dict[str, Any]:
        return {field.name: _encoded(getattr(self, field.name)) for field in fields(self)}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def _parse_record(
    record_type: type[_Record], value: Any, *,
    enums: dict[str, type[Enum]] | None = None,
    tuples: set[str] | None = None,
) -> _Record:
    data = _strict_dict(value, {field.name for field in fields(record_type)},
                        f"{record_type.__name__} record")
    parsed = dict(data)
    parsed["meta"] = RecordMeta.from_dict(data["meta"])
    for name, enum_type in (enums or {}).items():
        parsed[name] = _enum(enum_type, data[name], name)
    for name in tuples or set():
        parsed[name] = _texts(data[name], name)
    try:
        return record_type(**parsed)
    except TypeError as exc:
        raise RuntimeContractError(str(exc)) from exc


def _required_bindings(meta: RecordMeta, *names: str) -> None:
    for name in names:
        _text(getattr(meta, name), name)


@dataclass(frozen=True, slots=True)
class RunRecord(_Record):
    KIND: ClassVar[RecordKind] = RecordKind.RUN
    state: RunState
    trigger_id: str
    manager_agent: str
    responsible_member: str
    execution_level: str
    provider: str
    model: str
    capability_ceiling: tuple[str, ...]
    active_task_ids: tuple[str, ...]
    status: str
    outcome: str | None
    native_policy_digest: str = ""
    provider_policy_digest: str = ""
    native_provider_version: str = ""
    native_enabled: tuple[str, ...] = ()
    native_approval_gated: tuple[str, ...] = ()
    native_blocked: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(RunRecord, self).__post_init__()
        _enum_instance(self.state, RunState, "state")
        _required_bindings(self.meta, "run_id")
        if self.meta.root_run_id != self.meta.run_id or self.meta.task_id or self.meta.call_id:
            raise RuntimeContractError("run metadata must bind root_run_id == run_id only")
        for name in ("trigger_id", "manager_agent", "responsible_member", "execution_level",
                     "provider", "model", "status"):
            _text(getattr(self, name), name)
        _optional_text(self.outcome, "outcome")
        for name in ("native_policy_digest", "provider_policy_digest"):
            digest = getattr(self, name)
            if not isinstance(digest, str):
                raise RuntimeContractError(f"{name} must be text")
            if (digest and (len(digest) != 64
                     or any(c not in "0123456789abcdef"
                            for c in digest))):
                raise RuntimeContractError(f"{name} must be sha256 hex")
        _frozen_texts(self.capability_ceiling, "capability_ceiling")
        _frozen_texts(self.active_task_ids, "active_task_ids")
        for name in ("native_enabled", "native_approval_gated", "native_blocked"):
            _frozen_texts(getattr(self, name), name)
        if self.native_policy_digest:
            _text(self.native_provider_version, "native_provider_version")
        elif (self.native_provider_version or self.native_enabled
              or self.native_approval_gated or self.native_blocked):
            raise RuntimeContractError("native authority facts need a policy digest")
        if self.provider_policy_digest and not self.native_policy_digest:
            raise RuntimeContractError("provider policy needs native authority")
        groups = (self.native_enabled, self.native_approval_gated,
                  self.native_blocked)
        if len(set().union(*map(set, groups))) != sum(map(len, groups)):
            raise RuntimeContractError("native capability states overlap")

    @classmethod
    def from_dict(cls, value: Any) -> RunRecord:
        # R135 expands the signed native-policy ceiling. Existing signed
        # records do not gain authority by omission; they parse with an empty
        # ceiling so recovery can terminate them while native calls reject it.
        if isinstance(value, dict):
            defaults = {
                "native_policy_digest": "", "provider_policy_digest": "",
                "native_provider_version": "",
                "native_enabled": [], "native_approval_gated": [],
                "native_blocked": [],
            }
            new_fields = set(defaults) - {"native_policy_digest"}
            if not new_fields.issubset(value):
                # Pre-R135 records may carry R134's digest without the facts
                # needed to interpret it. Preserve recoverability but strip
                # that incomplete authority rather than inventing new power.
                value = {**value, **defaults}
            else:
                value = {**defaults, **value}
        return _parse_record(cls, value, enums={"state": RunState},
                             tuples={"capability_ceiling", "active_task_ids",
                                     "native_enabled", "native_approval_gated",
                                     "native_blocked"})  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class TaskRecord(_Record):
    KIND: ClassVar[RecordKind] = RecordKind.TASK
    state: TaskState
    objective: str
    assigned_agent: str
    assigning_agent: str
    responsible_member: str
    parent_task_id: str | None
    success_criteria: tuple[str, ...]
    context_digest: str
    grant_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    progress: str
    result: str | None
    return_to_agent: str

    def __post_init__(self) -> None:
        super(TaskRecord, self).__post_init__()
        _enum_instance(self.state, TaskState, "state")
        _required_bindings(self.meta, "run_id", "task_id")
        for name in ("objective", "assigned_agent", "assigning_agent", "responsible_member",
                     "context_digest", "progress", "return_to_agent"):
            _text(getattr(self, name), name)
        _optional_text(self.parent_task_id, "parent_task_id")
        _optional_text(self.result, "result")
        for name in ("success_criteria", "grant_ids", "dependency_ids"):
            _frozen_texts(getattr(self, name), name)

    @classmethod
    def from_dict(cls, value: Any) -> TaskRecord:
        return _parse_record(cls, value, enums={"state": TaskState}, tuples={
            "success_criteria", "grant_ids", "dependency_ids",
        })  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class HandoffRecord(_Record):
    KIND: ClassVar[RecordKind] = RecordKind.HANDOFF
    state: HandoffState
    handoff_type: HandoffType
    source_agent: str
    destination_agent: str
    source_owner: str
    destination_owner: str
    initiating_member: str
    reason: str
    context_digest: str
    requested_capabilities: tuple[str, ...]
    transferred_grant_ids: tuple[str, ...]
    return_to_agent: str
    result: str | None

    def __post_init__(self) -> None:
        super(HandoffRecord, self).__post_init__()
        _enum_instance(self.state, HandoffState, "state")
        _enum_instance(self.handoff_type, HandoffType, "handoff_type")
        _required_bindings(self.meta, "run_id", "task_id", "call_id")
        for name in ("source_agent", "destination_agent", "source_owner",
                     "destination_owner", "initiating_member", "reason", "context_digest",
                     "return_to_agent"):
            _text(getattr(self, name), name)
        _optional_text(self.result, "result")
        _frozen_texts(self.requested_capabilities, "requested_capabilities")
        _frozen_texts(self.transferred_grant_ids, "transferred_grant_ids")

    @classmethod
    def from_dict(cls, value: Any) -> HandoffRecord:
        return _parse_record(cls, value, enums={
            "state": HandoffState, "handoff_type": HandoffType,
        }, tuples={"requested_capabilities", "transferred_grant_ids"})  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class EffectRecord(_Record):
    """One effect attempt.

    PREPARED is the exclusive grant claim; EXECUTING is dispatch committed;
    COMMITTED is known success; REJECTED means the callback was provably never
    entered; UNKNOWN means dispatch began but no authoritative outcome exists.
    """

    KIND: ClassVar[RecordKind] = RecordKind.EFFECT
    state: EffectState
    capability_id: str
    argument_digest: str
    idempotency_key: str
    grant_id: str
    lease_owner: str
    state_version: int
    receipt_digest: str | None
    cancellation_state: str

    def __post_init__(self) -> None:
        super(EffectRecord, self).__post_init__()
        _enum_instance(self.state, EffectState, "state")
        _required_bindings(self.meta, "run_id", "task_id", "call_id")
        for name in ("capability_id", "argument_digest", "idempotency_key", "grant_id",
                     "lease_owner", "cancellation_state"):
            _text(getattr(self, name), name)
        _optional_text(self.receipt_digest, "receipt_digest")
        _integer(self.state_version, "state_version", minimum=1)

    @classmethod
    def from_dict(cls, value: Any) -> EffectRecord:
        return _parse_record(cls, value,
                             enums={"state": EffectState})  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ContinuationRecord(_Record):
    KIND: ClassVar[RecordKind] = RecordKind.CONTINUATION
    state: ContinuationState
    parent_task_id: str
    child_task_id: str
    provider_continuation_id: str | None
    sandbox_continuation_id: str | None
    grant_ids: tuple[str, ...]
    context_digest: str
    resume_ns: int | None

    def __post_init__(self) -> None:
        super(ContinuationRecord, self).__post_init__()
        _enum_instance(self.state, ContinuationState, "state")
        _required_bindings(self.meta, "run_id", "task_id", "call_id")
        for name in ("parent_task_id", "child_task_id", "context_digest"):
            _text(getattr(self, name), name)
        _optional_text(self.provider_continuation_id, "provider_continuation_id")
        _optional_text(self.sandbox_continuation_id, "sandbox_continuation_id")
        _frozen_texts(self.grant_ids, "grant_ids")
        if self.resume_ns is not None:
            _integer(self.resume_ns, "resume_ns", minimum=1)

    @classmethod
    def from_dict(cls, value: Any) -> ContinuationRecord:
        return _parse_record(cls, value, enums={"state": ContinuationState},
                             tuples={"grant_ids"})  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ControlRecord(_Record):
    KIND: ClassVar[RecordKind] = RecordKind.CONTROL
    control_type: ControlType
    state: ControlState
    target_agent: str | None
    target_machine: str | None
    input_digest: str
    decision: str | None
    text_digest: str | None
    sequence: int
    one_use: bool

    def __post_init__(self) -> None:
        super(ControlRecord, self).__post_init__()
        _enum_instance(self.control_type, ControlType, "control_type")
        _enum_instance(self.state, ControlState, "state")
        _required_bindings(self.meta, "run_id")
        _optional_text(self.target_agent, "target_agent")
        _optional_text(self.target_machine, "target_machine")
        _text(self.input_digest, "input_digest")
        _optional_text(self.decision, "decision")
        _optional_text(self.text_digest, "text_digest")
        _integer(self.sequence, "sequence", minimum=1)
        if not isinstance(self.one_use, bool):
            raise RuntimeContractError("one_use must be a boolean")
        if self.control_type in {
            ControlType.ASK, ControlType.ANSWER, ControlType.GRANT, ControlType.REVOKE,
        }:
            _required_bindings(self.meta, "task_id", "call_id")
        elif self.control_type is ControlType.TIMER_CANCEL:
            _required_bindings(self.meta, "call_id")
        elif self.control_type is ControlType.APPLINK:
            _required_bindings(self.meta, "call_id")
            _text(self.target_machine, "target_machine")
        elif not self.target_agent and not self.target_machine:
            raise RuntimeContractError("control must bind a target agent or machine")

    @classmethod
    def from_dict(cls, value: Any) -> ControlRecord:
        return _parse_record(cls, value, enums={
            "control_type": ControlType, "state": ControlState,
        })  # type: ignore[return-value]


Record = RunRecord | TaskRecord | HandoffRecord | EffectRecord | ContinuationRecord | ControlRecord
_RECORD_TYPES: dict[RecordKind, type[_Record]] = {
    RecordKind.RUN: RunRecord,
    RecordKind.TASK: TaskRecord,
    RecordKind.HANDOFF: HandoffRecord,
    RecordKind.EFFECT: EffectRecord,
    RecordKind.CONTINUATION: ContinuationRecord,
    RecordKind.CONTROL: ControlRecord,
}


def record_from_dict(value: Any) -> Record:
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        raise RuntimeContractError("runtime record must contain record meta")
    meta = RecordMeta.from_dict(value["meta"])
    record_type = _RECORD_TYPES[meta.kind]
    return record_type.from_dict(value)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class RuntimeEnvelope:
    """Ciphertext container; cryptographic execution lands in a later C1 slice."""

    meta: RecordMeta
    nonce: str
    ciphertext: str
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.meta, RecordMeta):
            raise RuntimeContractError("envelope meta must be RecordMeta")
        for name in ("nonce", "ciphertext", "signature"):
            _text(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta.to_dict(), "nonce": self.nonce,
            "ciphertext": self.ciphertext, "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeEnvelope:
        data = _strict_dict(value, {"meta", "nonce", "ciphertext", "signature"},
                            "runtime envelope")
        return cls(RecordMeta.from_dict(data["meta"]), data["nonce"],
                   data["ciphertext"], data["signature"])

    def aad_bytes(self) -> bytes:
        return canonical_json_bytes(self.meta.to_dict())

    def signing_bytes(self) -> bytes:
        return canonical_json_bytes({
            "meta": self.meta.to_dict(), "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        })
