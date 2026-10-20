"""Inactive, bounded provider admission and cancellation wire.

Admission is a capacity/identity contract, not permission to execute a write.
Only the durable owner may decide when to invoke this explicit helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Iterable

from .mutation_protocol import _DIGEST, _root, _uuid
from .typed_mutations import PreparedMutation, TypedMutationRPC, _FAMILIES, _canonical

_MAX_LEGS = 1025
_MAX_PLAN_BYTES = 512 * 1024
_MAX_REPLY_BYTES = _MAX_PLAN_BYTES + 4096
_LEG_FIELDS = frozenset(("operation_id", "kind", "client_digest"))
_ADMISSION_FIELDS = frozenset((
    "schema_version", "root", "actor_id", "protocol_epoch", "owner_id",
    "status", "plan",
))
_CANCELLATION_FIELDS = frozenset((
    "schema_version", "root", "actor_id", "protocol_epoch", "owner_id",
    "operation_id", "kind", "client_digest", "status",
))


class ProviderAdmissionError(RuntimeError):
    """An admission reply or its pinned request identity was unsafe."""


@dataclass(frozen=True)
class AdmissionLeg:
    operation_id: str
    kind: str
    client_digest: str

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "admission operation")
        if type(self.kind) is not str or self.kind not in _FAMILIES:
            raise ValueError("unsupported admission mutation kind")
        if type(self.client_digest) is not str or _DIGEST.fullmatch(self.client_digest) is None:
            raise ValueError("invalid admission client digest")

    @classmethod
    def from_prepared(cls, request: PreparedMutation) -> AdmissionLeg:
        if type(request) is not PreparedMutation:
            raise TypeError("invalid prepared admission mutation")
        request.__post_init__()
        e = request.expectation
        return cls(e.operation_id, e.kind, e.client_digest)

    def wire(self) -> dict[str, str]:
        self.__post_init__()
        return {"operation_id": self.operation_id, "kind": self.kind,
                "client_digest": self.client_digest}


@dataclass(frozen=True)
class AdmissionPlan:
    root: str
    actor_id: str
    protocol_epoch: str
    owner_id: str
    legs: tuple[AdmissionLeg, ...]

    def __post_init__(self) -> None:
        _root(self.root)
        _uuid(self.actor_id, "admission actor")
        _uuid(self.protocol_epoch, "admission protocol epoch")
        _uuid(self.owner_id, "admission owner")
        if (type(self.legs) is not tuple or not 1 <= len(self.legs) <= _MAX_LEGS
                or any(type(leg) is not AdmissionLeg for leg in self.legs)):
            raise ValueError("invalid admission legs")
        for leg in self.legs:
            leg.__post_init__()
        if len({leg.operation_id for leg in self.legs}) != len(self.legs):
            raise ValueError("duplicate admission operation")
        if len(self.request_json) > _MAX_PLAN_BYTES:
            raise ValueError("admission plan exceeds limit")

    @classmethod
    def from_prepared(cls, owner_id: str,
                      requests: Iterable[PreparedMutation]) -> AdmissionPlan:
        prepared = tuple(islice(requests, _MAX_LEGS + 1))
        if not 1 <= len(prepared) <= _MAX_LEGS:
            raise ValueError("invalid admission request count")
        first = prepared[0]
        if type(first) is not PreparedMutation:
            raise TypeError("invalid prepared admission mutation")
        identity = first.expectation
        legs = []
        for request in prepared:
            leg = AdmissionLeg.from_prepared(request)
            e = request.expectation
            if (e.root != identity.root or e.actor_id != identity.actor_id
                    or e.protocol_epoch != identity.protocol_epoch):
                raise ValueError("mixed admission request identities")
            legs.append(leg)
        return cls(identity.root, identity.actor_id, identity.protocol_epoch,
                   owner_id, tuple(legs))

    def wire_plan(self) -> list[dict[str, str]]:
        return [leg.wire() for leg in self.legs]

    @property
    def request_json(self) -> bytes:
        """Canonical admission identity, with digests but no mutation payloads."""
        return _canonical({
            "root": self.root, "actor_id": self.actor_id,
            "protocol_epoch": self.protocol_epoch, "owner_id": self.owner_id,
            "plan": self.wire_plan(),
        })

    def rpc_parameters(self) -> dict:
        self.__post_init__()
        return {"p_root": self.root, "p_protocol_epoch": self.protocol_epoch,
                "p_owner_id": self.owner_id, "p_plan": self.wire_plan()}


def prepare_admission_plan(owner_id: str,
                           requests: Iterable[PreparedMutation]) -> AdmissionPlan:
    return AdmissionPlan.from_prepared(owner_id, requests)


@dataclass(frozen=True)
class AdmissionResult:
    status: str  # admitted | full; neither grants write authority


@dataclass(frozen=True)
class CancellationResult:
    status: str  # cancelled | executed; executed requires receipt retrieval


def _bounded_reply(raw: object) -> dict:
    if type(raw) is not dict:
        raise ProviderAdmissionError("invalid provider admission response")
    try:
        if len(_canonical(raw)) > _MAX_REPLY_BYTES:
            raise ValueError("oversized reply")
    except ValueError:
        raise ProviderAdmissionError("invalid provider admission response") from None
    return raw


class ProviderAdmission:
    """Explicit calls through the typed RPC's pinned authenticated client."""

    def __init__(self, rpc: TypedMutationRPC) -> None:
        if type(rpc) is not TypedMutationRPC:
            raise TypeError("invalid typed mutation RPC")
        self._rpc = rpc

    def _check_plan(self, plan: AdmissionPlan) -> None:
        if type(plan) is not AdmissionPlan:
            raise TypeError("invalid admission plan")
        plan.__post_init__()
        if plan.root != self._rpc.root or plan.actor_id != self._rpc.actor_id:
            raise ProviderAdmissionError("admission request identity mismatch")

    def _cancel_parameters(self, plan: AdmissionPlan,
                           leg: AdmissionLeg) -> dict[str, str]:
        self._check_plan(plan)
        if type(leg) is not AdmissionLeg or leg not in plan.legs:
            raise ValueError("cancellation leg is not in admission plan")
        leg.__post_init__()
        return {
            "p_root": plan.root, "p_protocol_epoch": plan.protocol_epoch,
            "p_owner_id": plan.owner_id, "p_operation_id": leg.operation_id,
            "p_kind": leg.kind, "p_client_digest": leg.client_digest,
        }

    def admit(self, plan: AdmissionPlan) -> AdmissionResult:
        self._check_plan(plan)
        raw = self._rpc._call("ab_admit_mutation_owner", plan.rpc_parameters())
        raw = _bounded_reply(raw)
        if (raw.keys() != _ADMISSION_FIELDS or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
                or any(type(raw[key]) is not str for key in
                       ("root", "actor_id", "protocol_epoch", "owner_id", "status"))
                or raw["root"] != plan.root or raw["actor_id"] != plan.actor_id
                or raw["protocol_epoch"] != plan.protocol_epoch
                or raw["owner_id"] != plan.owner_id
                or raw["status"] not in ("admitted", "full")
                or type(raw["plan"]) is not list
                or len(raw["plan"]) != len(plan.legs)):
            raise ProviderAdmissionError("invalid provider admission response")
        for actual, expected in zip(raw["plan"], plan.wire_plan()):
            if (type(actual) is not dict or actual.keys() != _LEG_FIELDS
                    or any(type(actual[key]) is not str for key in _LEG_FIELDS)
                    or actual != expected):
                raise ProviderAdmissionError("invalid provider admission response")
        return AdmissionResult(raw["status"])

    def cancel(self, plan: AdmissionPlan, leg: AdmissionLeg) -> CancellationResult:
        raw = self._rpc._call(
            "ab_cancel_mutation_admission", self._cancel_parameters(plan, leg))
        raw = _bounded_reply(raw)
        if (raw.keys() != _CANCELLATION_FIELDS
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
                or any(type(raw[key]) is not str for key in
                       _CANCELLATION_FIELDS - {"schema_version"})
                or raw["root"] != plan.root or raw["actor_id"] != plan.actor_id
                or raw["protocol_epoch"] != plan.protocol_epoch
                or raw["owner_id"] != plan.owner_id
                or raw["operation_id"] != leg.operation_id
                or raw["kind"] != leg.kind
                or raw["client_digest"] != leg.client_digest
                or raw["status"] not in ("cancelled", "executed")):
            raise ProviderAdmissionError("invalid provider cancellation response")
        return CancellationResult(raw["status"])

    def acknowledge_cancel(self, plan: AdmissionPlan,
                           leg: AdmissionLeg) -> bool:
        """Acknowledge a terminal cancellation after its owner settled it."""
        raw = self._rpc._call(
            "ab_ack_mutation_admission_cancel",
            self._cancel_parameters(plan, leg),
        )
        if raw is not True or type(raw) is not bool:
            raise ProviderAdmissionError("invalid cancellation acknowledgment")
        return True


__all__ = [
    "AdmissionLeg", "AdmissionPlan", "AdmissionResult", "CancellationResult",
    "ProviderAdmission", "ProviderAdmissionError", "prepare_admission_plan",
]
