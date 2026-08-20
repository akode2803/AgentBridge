"""Provider-neutral C2 agent execution contracts.

These immutable values are the boundary between AgentBridge orchestration and
provider adapters.  They are deliberately separate from ``runtime.models``:
the latter is signed room-ledger truth, while this module is a bounded adapter
projection.  In particular, an ``AuthorityBinding`` is evidence pointing back
to exact signed run and task records; it never replaces validation of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Callable

from ...core.jsonkit import canonical_json_bytes


AGENT_CONTRACT_VERSION = 1
MAX_JSON_BYTES = 256 * 1024
MAX_TEXT_CHARS = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AgentContractError(ValueError):
    """A provider-neutral agent contract is malformed or ambiguous."""


def _strict(value: Any, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentContractError(f"{label} must be an object")
    actual = set(value)
    if actual != names:
        raise AgentContractError(
            f"invalid {label} fields; missing={sorted(names - actual)}, "
            f"extra={sorted(actual - names)}"
        )
    return value


def _text(value: Any, name: str, *, maximum: int = MAX_TEXT_CHARS) -> str:
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value) > maximum):
        raise AgentContractError(f"{name} must be bounded non-empty text")
    return value


def _optional_text(value: Any, name: str, *, maximum: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=maximum)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AgentContractError(f"{name} must be an integer >= {minimum}")
    return value


def _texts(value: Any, name: str, *, maximum: int = 128) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise AgentContractError(f"{name} must be a bounded string array")
    result = tuple(_text(item, f"{name}[]", maximum=256) for item in value)
    if len(result) != len(set(result)):
        raise AgentContractError(f"{name} must not contain duplicates")
    return result


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise AgentContractError(f"{name} must be sha256 hex")
    return value


def _version(value: Any) -> int:
    if value != AGENT_CONTRACT_VERSION:
        raise AgentContractError(f"unsupported agent contract version: {value!r}")
    return value


def _enum(enum_type: type[Enum], value: Any, name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise AgentContractError(f"unknown {name}: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class CanonicalValue:
    """Deeply immutable JSON represented by its sole canonical byte spelling."""

    encoded: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.encoded, bytes) or not self.encoded:
            raise AgentContractError("canonical value must contain JSON bytes")
        if len(self.encoded) > MAX_JSON_BYTES:
            raise AgentContractError("canonical value exceeds its byte bound")
        try:
            value = json.loads(self.encoded.decode("utf-8"))
            canonical = canonical_json_bytes(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise AgentContractError(f"canonical value is invalid JSON: {exc}") from exc
        if canonical != self.encoded:
            raise AgentContractError("canonical value does not use canonical JSON")

    @classmethod
    def from_value(cls, value: Any) -> CanonicalValue:
        try:
            encoded = canonical_json_bytes(value)
        except (TypeError, ValueError) as exc:
            raise AgentContractError(f"value is not canonical JSON: {exc}") from exc
        return cls(encoded)

    def to_value(self) -> Any:
        return json.loads(self.encoded.decode("utf-8"))

    def digest(self) -> str:
        return hashlib.sha256(self.encoded).hexdigest()


EMPTY_OBJECT = CanonicalValue.from_value({})


def _extensions(value: CanonicalValue) -> None:
    if not isinstance(value, CanonicalValue):
        raise AgentContractError("extensions must be canonical JSON")
    decoded = value.to_value()
    if not isinstance(decoded, dict) or any(
            not isinstance(name, str) or not name.startswith("x.") or len(name) > 64
            for name in decoded):
        raise AgentContractError("extensions must be an object with bounded x.* keys")


def _json_object(value: CanonicalValue, name: str) -> None:
    if not isinstance(value, CanonicalValue) or not isinstance(value.to_value(), dict):
        raise AgentContractError(f"{name} must be a canonical JSON object")


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """Definition-time prompt references; dynamic code stays in a resolver registry."""

    static_instructions: str | None = None
    template_id: str | None = None
    template_revision: int | None = None
    dynamic_resolver_id: str | None = None

    def __post_init__(self) -> None:
        _optional_text(self.static_instructions, "static_instructions")
        _optional_text(self.template_id, "template_id", maximum=256)
        _optional_text(self.dynamic_resolver_id, "dynamic_resolver_id", maximum=256)
        if self.template_revision is not None:
            _integer(self.template_revision, "template_revision", minimum=1)
            if not self.template_id:
                raise AgentContractError("template_revision requires template_id")
        if not (self.static_instructions or self.template_id or self.dynamic_resolver_id):
            raise AgentContractError("a prompt needs static text, a template, or a resolver")

    def to_dict(self) -> dict[str, Any]:
        return {
            "static_instructions": self.static_instructions,
            "template_id": self.template_id,
            "template_revision": self.template_revision,
            "dynamic_resolver_id": self.dynamic_resolver_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> PromptSpec:
        data = _strict(value, {
            "static_instructions", "template_id", "template_revision",
            "dynamic_resolver_id",
        }, "prompt spec")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Reusable specialist behavior, intentionally containing no run authority."""

    schema_version: int
    definition_id: str
    revision: int
    name: str
    provider: str
    model: str
    prompt: PromptSpec
    model_settings: CanonicalValue = EMPTY_OBJECT
    input_schema: CanonicalValue | None = None
    output_schema: CanonicalValue | None = None
    requested_tool_ids: tuple[str, ...] = ()
    requested_handoff_definition_ids: tuple[str, ...] = ()
    hook_ids: tuple[str, ...] = ()
    approval_policy_id: str = "default"
    max_turns: int = 20
    extensions: CanonicalValue = EMPTY_OBJECT

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("definition_id", "name", "provider", "model", "approval_policy_id"):
            _text(getattr(self, name), name, maximum=256)
        _integer(self.revision, "revision", minimum=1)
        _integer(self.max_turns, "max_turns", minimum=1)
        if self.max_turns > 10_000:
            raise AgentContractError("max_turns exceeds its bound")
        if not isinstance(self.prompt, PromptSpec):
            raise AgentContractError("prompt must be a PromptSpec")
        _json_object(self.model_settings, "model_settings")
        _extensions(self.extensions)
        for name in ("input_schema", "output_schema"):
            value = getattr(self, name)
            if value is not None:
                _json_object(value, name)
        for name in (
            "requested_tool_ids", "requested_handoff_definition_ids", "hook_ids",
        ):
            normalized = _texts(getattr(self, name), name)
            object.__setattr__(self, name, normalized)

    def clone(self, *, definition_id: str, name: str | None = None) -> AgentDefinition:
        """Clone reusable behavior only; authority and continuation never exist here."""
        return AgentDefinition(
            schema_version=self.schema_version,
            definition_id=definition_id,
            revision=1,
            name=name or self.name,
            provider=self.provider,
            model=self.model,
            prompt=self.prompt,
            model_settings=self.model_settings,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            requested_tool_ids=self.requested_tool_ids,
            requested_handoff_definition_ids=self.requested_handoff_definition_ids,
            hook_ids=self.hook_ids,
            approval_policy_id=self.approval_policy_id,
            max_turns=self.max_turns,
            # Extensions are deliberately not cloned: even non-authoritative
            # provider metadata can contain a local reference or stale hint.
            extensions=EMPTY_OBJECT,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "definition_id": self.definition_id,
            "revision": self.revision,
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "prompt": self.prompt.to_dict(),
            "model_settings": self.model_settings.to_value(),
            "input_schema": None if self.input_schema is None else self.input_schema.to_value(),
            "output_schema": None if self.output_schema is None else self.output_schema.to_value(),
            "requested_tool_ids": list(self.requested_tool_ids),
            "requested_handoff_definition_ids": list(
                self.requested_handoff_definition_ids),
            "hook_ids": list(self.hook_ids),
            "approval_policy_id": self.approval_policy_id,
            "max_turns": self.max_turns,
            "extensions": self.extensions.to_value(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgentDefinition:
        names = {
            "schema_version", "definition_id", "revision", "name", "provider",
            "model", "prompt", "model_settings", "input_schema", "output_schema",
            "requested_tool_ids", "requested_handoff_definition_ids", "hook_ids",
            "approval_policy_id", "max_turns", "extensions",
        }
        data = _strict(value, names, "agent definition")
        return cls(
            schema_version=data["schema_version"],
            definition_id=data["definition_id"], revision=data["revision"],
            name=data["name"], provider=data["provider"], model=data["model"],
            prompt=PromptSpec.from_dict(data["prompt"]),
            model_settings=CanonicalValue.from_value(data["model_settings"]),
            input_schema=(None if data["input_schema"] is None
                          else CanonicalValue.from_value(data["input_schema"])),
            output_schema=(None if data["output_schema"] is None
                           else CanonicalValue.from_value(data["output_schema"])),
            requested_tool_ids=_texts(
                data["requested_tool_ids"], "requested_tool_ids"),
            requested_handoff_definition_ids=_texts(
                data["requested_handoff_definition_ids"],
                "requested_handoff_definition_ids"),
            hook_ids=_texts(data["hook_ids"], "hook_ids"),
            approval_policy_id=data["approval_policy_id"],
            max_turns=data["max_turns"],
            extensions=CanonicalValue.from_value(data["extensions"]),
        )


class ContinuationMode(str, Enum):
    NONE = "none"
    LOCAL_REPLAY = "local_replay"
    PROVIDER_SESSION = "provider_session"
    PROVIDER_RESPONSE = "provider_response"
    PAUSED_RUN = "paused_run"


@dataclass(frozen=True, slots=True)
class ContinuationBinding:
    """Authenticated opaque state bound to one exact invocation context."""

    mode: ContinuationMode
    state_ref: str | None = None
    state_digest: str | None = None
    state_auth_tag: str | None = None
    invocation_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    chat_id: str | None = None
    agent: str | None = None
    responsible_member: str | None = None
    definition_digest: str | None = None
    provider: str | None = None
    model: str | None = None
    authority_digest: str | None = None
    invocation_context_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ContinuationMode):
            raise AgentContractError("mode must be a ContinuationMode")
        text_fields = (
            "state_ref", "invocation_id", "run_id", "task_id", "chat_id", "agent",
            "responsible_member", "provider", "model",
        )
        digest_fields = (
            "state_digest", "state_auth_tag", "definition_digest", "authority_digest",
            "invocation_context_digest",
        )
        for name in text_fields:
            _optional_text(getattr(self, name), name, maximum=512)
        for name in digest_fields:
            value = getattr(self, name)
            if value is not None:
                _digest(value, name)
        bound = tuple(getattr(self, name) for name in (*text_fields, *digest_fields))
        if self.mode is ContinuationMode.NONE:
            if any(value is not None for value in bound):
                raise AgentContractError("none continuation cannot carry state")
        elif any(value is None for value in bound):
            raise AgentContractError(
                "continuation state needs authentication and full invocation binding")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "state_ref": self.state_ref,
            "state_digest": self.state_digest,
            "state_auth_tag": self.state_auth_tag,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "agent": self.agent,
            "responsible_member": self.responsible_member,
            "definition_digest": self.definition_digest,
            "provider": self.provider,
            "model": self.model,
            "authority_digest": self.authority_digest,
            "invocation_context_digest": self.invocation_context_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ContinuationBinding:
        data = _strict(value, {
            "mode", "state_ref", "state_digest", "state_auth_tag", "invocation_id",
            "run_id", "task_id", "chat_id", "agent", "responsible_member",
            "definition_digest", "provider", "model", "authority_digest",
            "invocation_context_digest",
        }, "continuation binding")
        return cls(
            mode=_enum(ContinuationMode, data["mode"], "continuation mode"),
            state_ref=data["state_ref"], state_digest=data["state_digest"],
            state_auth_tag=data["state_auth_tag"], invocation_id=data["invocation_id"],
            run_id=data["run_id"], task_id=data["task_id"], chat_id=data["chat_id"],
            agent=data["agent"], responsible_member=data["responsible_member"],
            definition_digest=data["definition_digest"], provider=data["provider"],
            model=data["model"], authority_digest=data["authority_digest"],
            invocation_context_digest=data["invocation_context_digest"],
        )


@dataclass(frozen=True, slots=True)
class AuthorityBinding:
    """A minimized projection that must be revalidated against signed run truth."""

    run_id: str
    task_id: str
    chat_id: str
    agent: str
    responsible_member: str
    run_record_id: str
    run_record_digest: str
    task_record_id: str
    task_record_digest: str
    key_epoch: int
    policy_revision: int
    membership_epoch: int
    ownership_epoch: int
    capability_ids: tuple[str, ...] = ()
    grant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "run_id", "task_id", "chat_id", "agent", "responsible_member",
            "run_record_id", "task_record_id",
        ):
            _text(getattr(self, name), name, maximum=256)
        _digest(self.run_record_digest, "run_record_digest")
        _digest(self.task_record_digest, "task_record_digest")
        for name in (
            "key_epoch", "policy_revision", "membership_epoch", "ownership_epoch",
        ):
            _integer(getattr(self, name), name)
        for name in ("capability_ids", "grant_ids"):
            object.__setattr__(self, name, _texts(getattr(self, name), name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "agent": self.agent,
            "responsible_member": self.responsible_member,
            "run_record_id": self.run_record_id,
            "run_record_digest": self.run_record_digest,
            "task_record_id": self.task_record_id,
            "task_record_digest": self.task_record_digest,
            "key_epoch": self.key_epoch,
            "policy_revision": self.policy_revision,
            "membership_epoch": self.membership_epoch,
            "ownership_epoch": self.ownership_epoch,
            "capability_ids": list(self.capability_ids),
            "grant_ids": list(self.grant_ids),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AuthorityBinding:
        data = _strict(value, {
            "run_id", "task_id", "chat_id", "agent", "responsible_member",
            "run_record_id", "run_record_digest", "task_record_id",
            "task_record_digest", "key_epoch", "policy_revision",
            "membership_epoch", "ownership_epoch", "capability_ids", "grant_ids",
        }, "authority binding")
        return cls(
            run_id=data["run_id"], task_id=data["task_id"], chat_id=data["chat_id"],
            agent=data["agent"],
            responsible_member=data["responsible_member"],
            run_record_id=data["run_record_id"],
            run_record_digest=data["run_record_digest"],
            task_record_id=data["task_record_id"],
            task_record_digest=data["task_record_digest"],
            key_epoch=data["key_epoch"], policy_revision=data["policy_revision"],
            membership_epoch=data["membership_epoch"],
            ownership_epoch=data["ownership_epoch"],
            capability_ids=_texts(data["capability_ids"], "capability_ids"),
            grant_ids=_texts(data["grant_ids"], "grant_ids"),
        )


@dataclass(frozen=True, slots=True)
class AgentInvocationSpec:
    """One resolved adapter call, bound to but not itself granting authority."""

    schema_version: int
    invocation_id: str
    run_id: str
    task_id: str
    chat_id: str
    agent: str
    responsible_member: str
    definition_id: str
    definition_revision: int
    definition_digest: str
    provider: str
    model: str
    input: CanonicalValue
    resolved_instructions: str
    model_settings: CanonicalValue
    max_turns: int
    authority: AuthorityBinding
    continuation: ContinuationBinding
    deadline_ns: int | None = None
    extensions: CanonicalValue = EMPTY_OBJECT

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in (
            "invocation_id", "run_id", "task_id", "chat_id", "agent",
            "responsible_member", "definition_id", "provider", "model",
            "resolved_instructions",
        ):
            _text(getattr(self, name), name)
        _integer(self.definition_revision, "definition_revision", minimum=1)
        _digest(self.definition_digest, "definition_digest")
        _integer(self.max_turns, "max_turns", minimum=1)
        if self.max_turns > 10_000:
            raise AgentContractError("max_turns exceeds its bound")
        if not isinstance(self.input, CanonicalValue):
            raise AgentContractError("input must be canonical JSON")
        _json_object(self.model_settings, "model_settings")
        if not isinstance(self.authority, AuthorityBinding):
            raise AgentContractError("authority must be an AuthorityBinding")
        if not isinstance(self.continuation, ContinuationBinding):
            raise AgentContractError("continuation must be a ContinuationBinding")
        for name in ("run_id", "task_id", "chat_id", "agent", "responsible_member"):
            if getattr(self.authority, name) != getattr(self, name):
                raise AgentContractError(f"authority {name} must bind this invocation")
        if self.continuation.mode is not ContinuationMode.NONE:
            expected = {
                "invocation_id": self.invocation_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "chat_id": self.chat_id,
                "agent": self.agent,
                "responsible_member": self.responsible_member,
                "definition_digest": self.definition_digest,
                "provider": self.provider,
                "model": self.model,
                "authority_digest": contract_digest(self.authority),
                "invocation_context_digest": invocation_context_digest(self),
            }
            for name, value in expected.items():
                if getattr(self.continuation, name) != value:
                    raise AgentContractError(
                        f"continuation {name} must bind this invocation")
        if self.deadline_ns is not None:
            _integer(self.deadline_ns, "deadline_ns", minimum=1)
        _extensions(self.extensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "agent": self.agent,
            "responsible_member": self.responsible_member,
            "definition_id": self.definition_id,
            "definition_revision": self.definition_revision,
            "definition_digest": self.definition_digest,
            "provider": self.provider,
            "model": self.model,
            "input": self.input.to_value(),
            "resolved_instructions": self.resolved_instructions,
            "model_settings": self.model_settings.to_value(),
            "max_turns": self.max_turns,
            "authority": self.authority.to_dict(),
            "continuation": self.continuation.to_dict(),
            "deadline_ns": self.deadline_ns,
            "extensions": self.extensions.to_value(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgentInvocationSpec:
        names = {
            "schema_version", "invocation_id", "run_id", "task_id", "chat_id",
            "agent", "responsible_member", "definition_id", "definition_revision",
            "definition_digest", "provider", "model", "input",
            "resolved_instructions", "model_settings", "max_turns",
            "authority", "continuation", "deadline_ns",
            "extensions",
        }
        data = _strict(value, names, "agent invocation")
        return cls(
            schema_version=data["schema_version"],
            invocation_id=data["invocation_id"], run_id=data["run_id"],
            task_id=data["task_id"], chat_id=data["chat_id"], agent=data["agent"],
            responsible_member=data["responsible_member"],
            definition_id=data["definition_id"],
            definition_revision=data["definition_revision"],
            definition_digest=data["definition_digest"], provider=data["provider"],
            model=data["model"],
            input=CanonicalValue.from_value(data["input"]),
            resolved_instructions=data["resolved_instructions"],
            model_settings=CanonicalValue.from_value(data["model_settings"]),
            max_turns=data["max_turns"],
            authority=AuthorityBinding.from_dict(data["authority"]),
            continuation=ContinuationBinding.from_dict(data["continuation"]),
            deadline_ns=data["deadline_ns"],
            extensions=CanonicalValue.from_value(data["extensions"]),
        )


class StreamEventKind(str, Enum):
    STARTED = "started"
    ACTIVITY = "activity"
    TEXT_DELTA = "text_delta"
    TOOL_REQUESTED = "tool_requested"
    HANDOFF_REQUESTED = "handoff_requested"
    APPROVAL_REQUIRED = "approval_required"
    USAGE = "usage"
    OUTPUT = "output"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


TERMINAL_EVENT_KINDS = frozenset({
    StreamEventKind.COMPLETED,
    StreamEventKind.FAILED,
    StreamEventKind.STOPPED,
    StreamEventKind.INTERRUPTED,
})


@dataclass(frozen=True, slots=True)
class AgentStreamEvent:
    schema_version: int
    invocation_id: str
    sequence: int
    ns: int
    kind: StreamEventKind
    payload: CanonicalValue = EMPTY_OBJECT
    extensions: CanonicalValue = EMPTY_OBJECT

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _text(self.invocation_id, "invocation_id", maximum=256)
        _integer(self.sequence, "sequence", minimum=1)
        _integer(self.ns, "ns", minimum=1)
        if not isinstance(self.kind, StreamEventKind):
            raise AgentContractError("kind must be a StreamEventKind")
        if not isinstance(self.payload, CanonicalValue):
            raise AgentContractError("payload must be canonical JSON")
        _extensions(self.extensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "sequence": self.sequence,
            "ns": self.ns,
            "kind": self.kind.value,
            "payload": self.payload.to_value(),
            "extensions": self.extensions.to_value(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgentStreamEvent:
        data = _strict(value, {
            "schema_version", "invocation_id", "sequence", "ns", "kind", "payload",
            "extensions",
        }, "agent stream event")
        return cls(
            schema_version=data["schema_version"],
            invocation_id=data["invocation_id"], sequence=data["sequence"],
            ns=data["ns"], kind=_enum(StreamEventKind, data["kind"], "stream event kind"),
            payload=CanonicalValue.from_value(data["payload"]),
            extensions=CanonicalValue.from_value(data["extensions"]),
        )


class InterruptionKind(str, Enum):
    APPROVAL = "approval"
    USER_INPUT = "user_input"
    HANDOFF = "handoff"
    PROVIDER_RETRY = "provider_retry"


@dataclass(frozen=True, slots=True)
class AgentInterruption:
    schema_version: int
    interruption_id: str
    invocation_id: str
    kind: InterruptionKind
    request_ids: tuple[str, ...]
    state_ref: str
    state_digest: str
    state_auth_tag: str
    expires_ns: int | None = None
    extensions: CanonicalValue = EMPTY_OBJECT

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("interruption_id", "invocation_id", "state_ref"):
            _text(getattr(self, name), name, maximum=512)
        if not isinstance(self.kind, InterruptionKind):
            raise AgentContractError("kind must be an InterruptionKind")
        object.__setattr__(self, "request_ids", _texts(self.request_ids, "request_ids"))
        if not self.request_ids:
            raise AgentContractError("an interruption needs at least one request")
        _digest(self.state_digest, "state_digest")
        _digest(self.state_auth_tag, "state_auth_tag")
        if self.expires_ns is not None:
            _integer(self.expires_ns, "expires_ns", minimum=1)
        _extensions(self.extensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "interruption_id": self.interruption_id,
            "invocation_id": self.invocation_id,
            "kind": self.kind.value,
            "request_ids": list(self.request_ids),
            "state_ref": self.state_ref,
            "state_digest": self.state_digest,
            "state_auth_tag": self.state_auth_tag,
            "expires_ns": self.expires_ns,
            "extensions": self.extensions.to_value(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgentInterruption:
        data = _strict(value, {
            "schema_version", "interruption_id", "invocation_id", "kind",
            "request_ids", "state_ref", "state_digest", "expires_ns",
            "state_auth_tag", "extensions",
        }, "agent interruption")
        return cls(
            schema_version=data["schema_version"],
            interruption_id=data["interruption_id"],
            invocation_id=data["invocation_id"],
            kind=_enum(InterruptionKind, data["kind"], "interruption kind"),
            request_ids=_texts(data["request_ids"], "request_ids"),
            state_ref=data["state_ref"], state_digest=data["state_digest"],
            state_auth_tag=data["state_auth_tag"],
            expires_ns=data["expires_ns"],
            extensions=CanonicalValue.from_value(data["extensions"]),
        )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    provider_reported_cost_micros: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens",
        ):
            _integer(getattr(self, name), name)
        if self.cached_input_tokens > self.input_tokens:
            raise AgentContractError("cached_input_tokens cannot exceed input_tokens")
        if self.provider_reported_cost_micros is not None:
            _integer(self.provider_reported_cost_micros,
                     "provider_reported_cost_micros")
            _text(self.currency, "currency", maximum=16)
        elif self.currency is not None:
            raise AgentContractError("currency requires provider-reported cost")

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "provider_reported_cost_micros": self.provider_reported_cost_micros,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, value: Any) -> UsageRecord:
        data = _strict(value, {
            "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens",
            "provider_reported_cost_micros", "currency",
        }, "usage record")
        return cls(**data)


class ProviderErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    TOOL = "tool"
    OUTPUT = "output"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


PUBLIC_ERROR_MESSAGES = {
    ProviderErrorCategory.AUTHENTICATION: "Provider authentication failed",
    ProviderErrorCategory.RATE_LIMIT: "Provider rate limit reached",
    ProviderErrorCategory.TIMEOUT: "Provider request timed out",
    ProviderErrorCategory.INVALID_REQUEST: "Provider rejected the request",
    ProviderErrorCategory.UNAVAILABLE: "Provider is unavailable",
    ProviderErrorCategory.TOOL: "Provider tool execution failed",
    ProviderErrorCategory.OUTPUT: "Provider returned invalid output",
    ProviderErrorCategory.CANCELLED: "Provider execution was cancelled",
    ProviderErrorCategory.INTERNAL: "Provider execution failed",
}
PUBLIC_ERROR_CODES = {
    ProviderErrorCategory.AUTHENTICATION: frozenset({"authentication"}),
    ProviderErrorCategory.RATE_LIMIT: frozenset({"rate_limit"}),
    ProviderErrorCategory.TIMEOUT: frozenset({"timeout"}),
    ProviderErrorCategory.INVALID_REQUEST: frozenset({"invalid_request"}),
    ProviderErrorCategory.UNAVAILABLE: frozenset({"unavailable", "offline"}),
    ProviderErrorCategory.TOOL: frozenset({"tool_error"}),
    ProviderErrorCategory.OUTPUT: frozenset({
        "invalid_output", "invalid_structured_output",
    }),
    ProviderErrorCategory.CANCELLED: frozenset({"cancelled"}),
    ProviderErrorCategory.INTERNAL: frozenset({"internal", "fake_fault"}),
}
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ProviderError:
    category: ProviderErrorCategory
    code: str
    public_message: str
    retryable: bool
    evidence_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, ProviderErrorCategory):
            raise AgentContractError("category must be a ProviderErrorCategory")
        if not isinstance(self.code, str) or not _ERROR_CODE_RE.fullmatch(self.code):
            raise AgentContractError("provider error code must be a normalized identifier")
        if self.code not in PUBLIC_ERROR_CODES[self.category]:
            raise AgentContractError("provider error code must be code-owned")
        if self.public_message != PUBLIC_ERROR_MESSAGES[self.category]:
            raise AgentContractError("provider error public message must be code-owned")
        if not isinstance(self.retryable, bool):
            raise AgentContractError("retryable must be boolean")
        if self.evidence_digest is not None:
            _digest(self.evidence_digest, "evidence_digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "public_message": self.public_message,
            "retryable": self.retryable,
            "evidence_digest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ProviderError:
        data = _strict(value, {
            "category", "code", "public_message", "retryable", "evidence_digest",
        }, "provider error")
        return cls(
            category=_enum(ProviderErrorCategory, data["category"],
                           "provider error category"),
            code=data["code"], public_message=data["public_message"],
            retryable=data["retryable"], evidence_digest=data["evidence_digest"],
        )

    @classmethod
    def normalized(
        cls,
        category: ProviderErrorCategory,
        code: str,
        retryable: bool,
        *,
        evidence_digest: str | None = None,
    ) -> ProviderError:
        if not isinstance(category, ProviderErrorCategory):
            raise AgentContractError("category must be a ProviderErrorCategory")
        return cls(
            category=category,
            code=code,
            public_message=PUBLIC_ERROR_MESSAGES[category],
            retryable=retryable,
            evidence_digest=evidence_digest,
        )


class AgentResultStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class AgentResult:
    schema_version: int
    invocation_id: str
    status: AgentResultStatus
    last_event_sequence: int
    final_output: CanonicalValue | None
    final_text: str | None
    usage: UsageRecord
    error: ProviderError | None = None
    interruption: AgentInterruption | None = None
    extensions: CanonicalValue = EMPTY_OBJECT

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _text(self.invocation_id, "invocation_id", maximum=256)
        if not isinstance(self.status, AgentResultStatus):
            raise AgentContractError("status must be an AgentResultStatus")
        _integer(self.last_event_sequence, "last_event_sequence", minimum=1)
        if self.final_output is not None and not isinstance(self.final_output, CanonicalValue):
            raise AgentContractError("final_output must be canonical JSON")
        _optional_text(self.final_text, "final_text")
        if not isinstance(self.usage, UsageRecord):
            raise AgentContractError("usage must be a UsageRecord")
        _extensions(self.extensions)
        if self.status is AgentResultStatus.COMPLETED:
            if self.error or self.interruption:
                raise AgentContractError("completed result cannot carry error or interruption")
            if self.final_output is None and self.final_text is None:
                raise AgentContractError("completed result needs output")
        elif self.status is AgentResultStatus.FAILED:
            if not isinstance(self.error, ProviderError) or self.interruption:
                raise AgentContractError("failed result needs only a provider error")
            if self.final_output is not None or self.final_text is not None:
                raise AgentContractError("failed result cannot claim final output")
        elif self.status is AgentResultStatus.INTERRUPTED:
            if not isinstance(self.interruption, AgentInterruption) or self.error:
                raise AgentContractError("interrupted result needs only an interruption")
            if self.interruption.invocation_id != self.invocation_id:
                raise AgentContractError("interruption must bind this invocation")
            if self.final_output is not None or self.final_text is not None:
                raise AgentContractError("interrupted result cannot claim final output")
        else:
            if self.error or self.interruption or self.final_output is not None \
                    or self.final_text is not None:
                raise AgentContractError("stopped result cannot carry output or provider state")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "status": self.status.value,
            "last_event_sequence": self.last_event_sequence,
            "final_output": None if self.final_output is None else self.final_output.to_value(),
            "final_text": self.final_text,
            "usage": self.usage.to_dict(),
            "error": None if self.error is None else self.error.to_dict(),
            "interruption": (
                None if self.interruption is None else self.interruption.to_dict()),
            "extensions": self.extensions.to_value(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgentResult:
        data = _strict(value, {
            "schema_version", "invocation_id", "status", "last_event_sequence",
            "final_output", "final_text", "usage", "error", "interruption",
            "extensions",
        }, "agent result")
        return cls(
            schema_version=data["schema_version"], invocation_id=data["invocation_id"],
            status=_enum(AgentResultStatus, data["status"], "agent result status"),
            last_event_sequence=data["last_event_sequence"],
            final_output=(None if data["final_output"] is None
                          else CanonicalValue.from_value(data["final_output"])),
            final_text=data["final_text"], usage=UsageRecord.from_dict(data["usage"]),
            error=(None if data["error"] is None
                   else ProviderError.from_dict(data["error"])),
            interruption=(None if data["interruption"] is None
                          else AgentInterruption.from_dict(data["interruption"])),
            extensions=CanonicalValue.from_value(data["extensions"]),
        )


def contract_digest(value: Any) -> str:
    """Digest a contract's public dictionary without accepting arbitrary objects."""
    if not hasattr(value, "to_dict") or not callable(value.to_dict):
        raise AgentContractError("contract value must provide to_dict()")
    return hashlib.sha256(canonical_json_bytes(value.to_dict())).hexdigest()


def invocation_context_digest(invocation: AgentInvocationSpec) -> str:
    """Digest every invocation field except the continuation being authenticated."""
    if not isinstance(invocation, AgentInvocationSpec):
        raise AgentContractError("invocation context needs an AgentInvocationSpec")
    raw = invocation.to_dict()
    raw.pop("continuation")
    return hashlib.sha256(canonical_json_bytes(raw)).hexdigest()


def verify_invocation(
    definition: AgentDefinition,
    invocation: AgentInvocationSpec,
    run_record: Any,
    task_record: Any,
) -> None:
    """Fail closed across definition, adapter input, and signed ledger truth."""
    from .models import RunRecord, RunState, TaskRecord, TaskState

    if not isinstance(definition, AgentDefinition):
        raise AgentContractError("definition is unavailable")
    if not isinstance(invocation, AgentInvocationSpec):
        raise AgentContractError("invocation is unavailable")
    if not isinstance(run_record, RunRecord) or run_record.state is not RunState.RUNNING:
        raise AgentContractError("signed running record is unavailable")
    if not isinstance(task_record, TaskRecord) or task_record.state not in {
        TaskState.QUEUED, TaskState.ACTIVE, TaskState.WAITING,
    }:
        raise AgentContractError("signed active task is unavailable")
    expected_definition = contract_digest(definition)
    if invocation.definition_digest != expected_definition:
        raise AgentContractError("invocation definition digest mismatch")
    if (invocation.definition_id != definition.definition_id
            or invocation.definition_revision != definition.revision
            or invocation.provider != definition.provider
            or invocation.model != definition.model
            or invocation.model_settings != definition.model_settings
            or invocation.max_turns > definition.max_turns):
        raise AgentContractError("invocation does not match its definition")
    run_meta = run_record.meta
    task_meta = task_record.meta
    checks = {
        "run id": run_meta.run_id == invocation.run_id,
        "run chat": run_meta.chat_id == invocation.chat_id,
        "run agent": run_record.manager_agent == invocation.agent,
        "run owner": run_record.responsible_member == invocation.responsible_member,
        "run provider": run_record.provider == invocation.provider,
        "run model": run_record.model == invocation.model,
        "task id": task_meta.task_id == invocation.task_id,
        "task run": task_meta.run_id == invocation.run_id,
        "task root": task_meta.root_run_id == run_meta.root_run_id,
        "task chat": task_meta.chat_id == invocation.chat_id,
        "task agent": task_record.assigned_agent == invocation.agent,
        "task owner": task_record.responsible_member == invocation.responsible_member,
        "task active": invocation.task_id in run_record.active_task_ids,
        "task key epoch": task_meta.key_epoch == run_meta.key_epoch,
        "task policy revision": task_meta.policy_revision == run_meta.policy_revision,
        "task membership epoch": task_meta.membership_epoch == run_meta.membership_epoch,
        "task ownership epoch": task_meta.ownership_epoch == run_meta.ownership_epoch,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise AgentContractError(f"signed invocation mismatch: {', '.join(failed)}")
    authority = invocation.authority
    authority_checks = {
        "record id": authority.run_record_id == run_meta.id,
        "record digest": authority.run_record_digest == contract_digest(run_record),
        "task record id": authority.task_record_id == task_meta.id,
        "task record digest": authority.task_record_digest == contract_digest(task_record),
        "key epoch": authority.key_epoch == run_meta.key_epoch,
        "policy revision": authority.policy_revision == run_meta.policy_revision,
        "membership epoch": authority.membership_epoch == run_meta.membership_epoch,
        "ownership epoch": authority.ownership_epoch == run_meta.ownership_epoch,
        "capabilities": authority.capability_ids == run_record.capability_ceiling,
        "grants": authority.grant_ids == task_record.grant_ids,
    }
    failed = [name for name, ok in authority_checks.items() if not ok]
    if failed:
        raise AgentContractError(f"signed authority mismatch: {', '.join(failed)}")


CancelCheck = Callable[[], bool]
