"""Pure lifecycle evaluation over explicit serialized observations."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..core.jsonkit import canonical_json_bytes
from ..core.models import UserKind
from .lifecycle import (
    LifecycleError,
    LifecycleUnavailable,
    _FUTURE_SKEW_NS,
    _authorize_with_facts,
    _validate_structure,
    _validate_envelope_structure,
    _verify_with_keys,
)


@dataclass(frozen=True)
class AccountAuthorityFact:
    kind: str
    trusted_sign_pub: str
    active: bool


@dataclass(frozen=True)
class SubjectEvidence:
    available: bool
    envelopes: tuple[tuple[str, str], ...]
    retained_json: str | None


@dataclass(frozen=True)
class CapturedLifecycleInputs:
    now_ns: int
    accounts: tuple[tuple[str, AccountAuthorityFact | None], ...]
    subjects: tuple[tuple[str, SubjectEvidence], ...]


@dataclass(frozen=True)
class LifecycleLimits:
    max_accounts: int = 1024
    max_subjects: int = 1024
    max_envelopes: int = 10_000
    max_bytes: int = 16_777_216
    max_depth: int = 32


@dataclass(frozen=True)
class HeadProposal:
    subject: str
    expected_retained_json: str | None
    proposed_json: str


@dataclass(frozen=True)
class LifecycleEvaluation:
    effective_json: str | None
    proposals: tuple[HeadProposal, ...]
    consumed_accounts: tuple[str, ...]
    consumed_subjects: tuple[str, ...]
    now_ns: int
    next_recheck_ns: int | None = None
    next_proposal: HeadProposal | None = None


@dataclass(frozen=True)
class LifecycleWork:
    complete: bool
    result: LifecycleEvaluation


class _ProposalReady(Exception):
    pass


def evaluate_lifecycle_work(inputs, subject, *, limits=LifecycleLimits()):
    """Stop at the first postorder publication, before evaluating outer work.

    A proposal is raw work requiring the coordinator's full authority fence.
    Its effective state must never be handed out; publish once, then recapture.
    Existing full diagnostic evaluation keeps its original complete-fold API.
    """
    captured, root, bounded = _copy_inputs(inputs, subject, limits)
    evaluator = _Evaluator(captured, bounded)
    evaluator.stop_on_proposal = True
    complete = True
    effective = None
    try:
        effective = evaluator.evaluate(root, depth=0)
    except _ProposalReady:
        complete = False
    result = LifecycleEvaluation(
        _canonical(effective),
        tuple(evaluator.proposals[name] for name in sorted(evaluator.proposals)),
        tuple(sorted(evaluator.consumed_accounts)),
        tuple(sorted(evaluator.consumed_subjects)), captured.now_ns,
        evaluator.next_recheck_ns(), next(iter(evaluator.proposals.values()), None),
    )
    return LifecycleWork(complete, result)


class LifecycleInputsIncomplete(LifecycleUnavailable):
    """A fact required by this evaluation was not present in the capture."""

    def __init__(
        self, message: str, dependency_kind: str | None = None,
        dependency_name: str | None = None,
    ) -> None:
        self.dependency_kind = dependency_kind
        self.dependency_name = dependency_name
        super().__init__(message)


_MAX_LIMITS = LifecycleLimits()


def evaluate_lifecycle(
    inputs: CapturedLifecycleInputs,
    subject: str,
    *,
    limits: LifecycleLimits = LifecycleLimits(),
) -> LifecycleEvaluation:
    """Evaluate one subject without Store, transport, filesystem, or clock reads."""
    captured, root, bounded = _copy_inputs(inputs, subject, limits)
    evaluator = _Evaluator(captured, bounded)
    effective = evaluator.evaluate(root, depth=0)
    next_recheck_ns = evaluator.next_recheck_ns()
    return LifecycleEvaluation(
        _canonical(effective),
        tuple(evaluator.proposals[name] for name in sorted(evaluator.proposals)),
        tuple(sorted(evaluator.consumed_accounts)),
        tuple(sorted(evaluator.consumed_subjects)),
        captured.now_ns,
        next_recheck_ns,
        next(iter(evaluator.proposals.values()), None),
    )


class _Evaluator:
    def __init__(self, inputs: CapturedLifecycleInputs, limits: LifecycleLimits) -> None:
        self.inputs = inputs
        self.limits = limits
        self.accounts = dict(inputs.accounts)
        self.subjects = dict(inputs.subjects)
        self.consumed_accounts: set[str] = set()
        self.consumed_subjects: set[str] = set()
        self.proposals: dict[str, HeadProposal] = {}
        self.stop_on_proposal = False
        self.memo: dict[str, dict | None] = {}
        self.stack: set[str] = set()

    def next_recheck_ns(self) -> int | None:
        boundary = None
        for subject in self.consumed_subjects:
            evidence = self.subjects[subject]
            if not evidence.available:
                continue
            for path, serialized in evidence.envelopes:
                try:
                    value = json.loads(serialized)
                    record = _validate_envelope_structure(path, value)
                    if record["subject"] != subject:
                        continue
                    candidate = record["ns"] - _FUTURE_SKEW_NS
                    if not self.inputs.now_ns < candidate <= 2**63 - 1:
                        continue
                    if boundary is None or candidate < boundary:
                        boundary = candidate
                except (LifecycleError, TypeError, ValueError, RecursionError):
                    continue
        return boundary

    def evaluate(self, subject: str, *, depth: int) -> dict | None:
        self.consumed_subjects.add(subject)
        if depth > self.limits.max_depth:
            raise LifecycleInputsIncomplete("lifecycle owner depth is incomplete")
        if subject in self.stack:
            raise LifecycleInputsIncomplete("lifecycle owner cycle is incomplete")
        if subject in self.memo:
            return self.memo[subject]
        evidence = self.subjects.get(subject, _MISSING)
        if evidence is _MISSING:
            raise LifecycleInputsIncomplete(
                f"lifecycle subject @{subject} is missing from captured inputs",
                "subject", subject,
            )
        local = self._retained(subject, evidence.retained_json)
        if not evidence.available:
            if local is None:
                raise LifecycleInputsIncomplete(
                    f"lifecycle subject @{subject} was not completely enumerated"
                )
            self.memo[subject] = local
            return local

        self.stack.add(subject)
        try:
            result = self._fold(subject, evidence, local, depth)
            self.memo[subject] = result
            return result
        finally:
            self.stack.remove(subject)

    def _fold(
        self,
        subject: str,
        evidence: SubjectEvidence,
        local: dict | None,
        depth: int,
    ) -> dict | None:
        records = []
        for path, serialized in evidence.envelopes:
            try:
                value = json.loads(serialized)
                record, proof = _verify_with_keys(
                    path, value, self._key_of, self.inputs.now_ns,
                )
                if record["subject"] != subject:
                    raise LifecycleError("lifecycle subject mismatch")
                records.append((record["ns"], record["id"], record, proof))
            except LifecycleUnavailable:
                raise
            except (LifecycleError, TypeError, ValueError, RecursionError):
                continue

        current = None
        accepted: set[str] = set()
        for _ns, _record_id, record, proof in sorted(records):
            try:
                _authorize_with_facts(
                    record,
                    proof,
                    current,
                    self._kind_of,
                    lambda name: self._active_human(name, depth + 1),
                )
                current = record
                accepted.add(record["id"])
            except LifecycleUnavailable:
                raise
            except (LifecycleError, TypeError, ValueError):
                continue
        if local is not None and (current is None or local["id"] not in accepted):
            return local
        if current is not None:
            proposed = _canonical(current)
            if proposed != _canonical(local):
                self.proposals[subject] = HeadProposal(
                    subject, evidence.retained_json, proposed,
                )
                if self.stop_on_proposal:
                    raise _ProposalReady
        return current

    def _account(self, name: str) -> AccountAuthorityFact | None:
        self.consumed_accounts.add(name)
        fact = self.accounts.get(name, _MISSING)
        if fact is _MISSING:
            raise LifecycleInputsIncomplete(
                f"account @{name} is missing from captured inputs",
                "account", name,
            )
        return fact

    def _key_of(self, name: str) -> str:
        fact = self._account(name)
        return fact.trusted_sign_pub if fact is not None else ""

    def _kind_of(self, name: str) -> str | None:
        fact = self._account(name)
        return fact.kind if fact is not None else None

    def _active_human(self, name: str, depth: int) -> bool:
        fact = self._account(name)
        if (
            fact is None
            or fact.kind != UserKind.HUMAN.value
            or not fact.trusted_sign_pub
        ):
            return False
        state = self.evaluate(name, depth=depth)
        return state["active"] if state is not None else fact.active

    @staticmethod
    def _retained(subject: str, serialized: str | None) -> dict | None:
        if serialized is None:
            return None
        try:
            record = _validate_structure(json.loads(serialized))
            if record["subject"] != subject:
                raise LifecycleError("retained lifecycle subject mismatch")
            return record
        except LifecycleUnavailable:
            raise
        except Exception as exc:
            raise LifecycleUnavailable(
                "retained lifecycle authority is unavailable"
            ) from exc


_MISSING = object()


def _copy_inputs(
    inputs: CapturedLifecycleInputs,
    subject: str,
    limits: LifecycleLimits,
) -> tuple[CapturedLifecycleInputs, str, LifecycleLimits]:
    if type(inputs) is not CapturedLifecycleInputs:
        raise TypeError("inputs must be exact CapturedLifecycleInputs")
    if type(limits) is not LifecycleLimits:
        raise TypeError("limits must be exact LifecycleLimits")
    copied_limits = _copy_limits(limits)
    root = _name(subject, "root subject")
    now_ns = inputs.now_ns
    account_entries = inputs.accounts
    subject_entries = inputs.subjects
    if type(now_ns) is not int or not 0 <= now_ns <= 2**63 - 1:
        raise ValueError("now_ns must be a nonnegative SQLite integer")
    if type(account_entries) is not tuple or type(subject_entries) is not tuple:
        raise TypeError("captured lifecycle collections must be exact tuples")
    if len(account_entries) > copied_limits.max_accounts:
        raise OverflowError("captured lifecycle accounts exceed limit")
    if len(subject_entries) > copied_limits.max_subjects:
        raise OverflowError("captured lifecycle subjects exceed limit")

    used = _charge(0, root, copied_limits.max_bytes)
    accounts = []
    account_names: set[str] = set()
    for entry in account_entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("account entries must be exact pairs")
        name = _name(entry[0], "account name")
        used = _charge(used, name, copied_limits.max_bytes)
        if name in account_names:
            raise ValueError("duplicate captured lifecycle account")
        account_names.add(name)
        fact = entry[1]
        if fact is not None:
            if type(fact) is not AccountAuthorityFact:
                raise TypeError("account fact must be exact AccountAuthorityFact")
            kind = fact.kind
            trusted_sign_pub = fact.trusted_sign_pub
            active = fact.active
            if type(kind) is not str or kind not in {
                UserKind.HUMAN.value, UserKind.AGENT.value,
            }:
                raise ValueError("invalid account authority kind")
            if type(trusted_sign_pub) is not str:
                raise TypeError("trusted signing key must be an exact string")
            if type(active) is not bool:
                raise TypeError("account active must be an exact bool")
            used = _charge(used, kind, copied_limits.max_bytes)
            used = _charge(used, trusted_sign_pub, copied_limits.max_bytes)
            fact = AccountAuthorityFact(kind, trusted_sign_pub, active)
        accounts.append((name, fact))

    subjects = []
    subject_names: set[str] = set()
    envelope_count = 0
    for entry in subject_entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("subject entries must be exact pairs")
        name = _name(entry[0], "subject name")
        used = _charge(used, name, copied_limits.max_bytes)
        if name in subject_names:
            raise ValueError("duplicate captured lifecycle subject")
        subject_names.add(name)
        evidence = entry[1]
        if type(evidence) is not SubjectEvidence:
            raise TypeError("subject evidence must be exact SubjectEvidence")
        available = evidence.available
        envelope_entries = evidence.envelopes
        retained_json = evidence.retained_json
        if type(available) is not bool:
            raise TypeError("subject availability must be an exact bool")
        if type(envelope_entries) is not tuple:
            raise TypeError("subject envelopes must be an exact tuple")
        if retained_json is not None and type(retained_json) is not str:
            raise TypeError("retained lifecycle value must be serialized text")
        if retained_json is not None:
            used = _charge(used, retained_json, copied_limits.max_bytes)
        envelopes = []
        paths: set[str] = set()
        envelope_count += len(envelope_entries)
        if envelope_count > copied_limits.max_envelopes:
            raise OverflowError("captured lifecycle envelopes exceed limit")
        for envelope in envelope_entries:
            if type(envelope) is not tuple or len(envelope) != 2:
                raise TypeError("lifecycle envelopes must be exact pairs")
            path, serialized = envelope
            if type(path) is not str or type(serialized) is not str:
                raise TypeError("lifecycle envelope fields must be exact strings")
            used = _charge(used, path, copied_limits.max_bytes)
            used = _charge(used, serialized, copied_limits.max_bytes)
            if path in paths:
                raise ValueError("duplicate captured lifecycle envelope path")
            paths.add(path)
            envelopes.append((path, serialized))
        subjects.append((name, SubjectEvidence(
            available, tuple(envelopes), retained_json,
        )))
    return CapturedLifecycleInputs(
        now_ns, tuple(accounts), tuple(subjects),
    ), root, copied_limits


def _copy_limits(value: LifecycleLimits) -> LifecycleLimits:
    fields = (
        "max_accounts", "max_subjects", "max_envelopes", "max_bytes", "max_depth",
    )
    copied = []
    for name in fields:
        item = getattr(value, name)
        maximum = getattr(_MAX_LIMITS, name)
        if type(item) is not int or not 0 <= item <= maximum:
            raise ValueError(f"{name} must be an integer from zero through {maximum}")
        copied.append(item)
    return LifecycleLimits(*copied)


def _name(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty exact string")
    return value


def _charge(used: int, value: str, maximum: int) -> int:
    remaining = maximum - used
    if len(value) > remaining:
        raise OverflowError("captured lifecycle inputs exceed byte limit")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("captured lifecycle strings must encode as UTF-8") from exc
    if size > remaining:
        raise OverflowError("captured lifecycle inputs exceed byte limit")
    return used + size


def _canonical(value: dict | None) -> str | None:
    return None if value is None else canonical_json_bytes(value).decode("utf-8")


__all__ = [
    "AccountAuthorityFact",
    "CapturedLifecycleInputs",
    "HeadProposal",
    "LifecycleEvaluation",
    "LifecycleWork",
    "LifecycleInputsIncomplete",
    "LifecycleLimits",
    "SubjectEvidence",
    "evaluate_lifecycle",
    "evaluate_lifecycle_work",
]
