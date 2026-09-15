"""Single-use admission of a coordinated local membership snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.jsonkit import canonical_json_bytes
from ..core.models import Account, ChatSnapshot, UserKind
from ..store import membership_read_inputs
from ..store.membership_input_position import (
    MAX_SQLITE_INTEGER, MembershipInputPosition, MembershipInputUnavailable,
)
from ..transport.cache import CachingTransport
from ..transport.mirror_observation import (
    MirrorCaptureUnavailable, MirrorExpectedPosition, MirrorPositionValidation,
    MirrorSelection, MirrorSelectionRequest,
)
from . import events
from .lifecycle import LifecycleUnavailable
from .lifecycle_evaluation import (
    AccountAuthorityFact, CapturedLifecycleInputs, LifecycleInputsIncomplete,
    LifecycleLimits, SubjectEvidence, evaluate_lifecycle,
)
from .pin_storage import PinStoreUnavailable, canonical_json
from .pins import evaluate_pin


@dataclass(frozen=True)
class MembershipAdmissionScope:
    mesh_identity: int
    transport_identity: int
    store_identity: int
    pins_identity: int
    database_path: str
    pin_path: str


@dataclass(frozen=True)
class MembershipReadLimits:
    max_documents: int = 100_000
    max_events: int = membership_read_inputs.MAX_EVENTS
    max_heads: int = membership_read_inputs.MAX_HEADS
    max_bytes: int = membership_read_inputs.MAX_BYTES
    max_dependency_steps: int = 64


@dataclass(frozen=True)
class MembershipAdmission:
    mirror_position: MirrorExpectedPosition
    membership_position: MembershipInputPosition
    pin_present: bool
    pin_sha256: str
    retained_heads_sha256: str
    captured_now_ns: int
    validated_now_ns: int
    next_recheck_ns: int | None
    consumed_accounts: tuple[str, ...]
    consumed_subjects: tuple[str, ...]


@dataclass(frozen=True)
class MembershipSnapshotResult:
    source: str
    snapshot_json: str
    admission: MembershipAdmission | None
    rejection_reason: str | None


class _Reject(RuntimeError):
    pass


def register_membership_admission_scope(mesh) -> MembershipAdmissionScope:
    """Record an explicit cooperating-writer assertion for this exact mesh."""
    return MembershipAdmissionScope(
        id(mesh), id(mesh.tx), id(mesh.store), id(mesh.key_pins),
        str(Path(mesh.store.path).resolve()), str(Path(mesh.key_pins.path).resolve()),
    )


def read_membership_snapshot(
    mesh,
    chat_id: str,
    scope: MembershipAdmissionScope,
    *,
    clock_ns: Callable[[], int] = time.time_ns,
    limits: MembershipReadLimits = MembershipReadLimits(),
) -> MembershipSnapshotResult:
    """Return one admitted local snapshot or continue through canonical resolution."""
    reason = None
    try:
        bounded = _limits(limits)
        _scope(mesh, scope)
        if type(chat_id) is not str or not chat_id or "\x00" in chat_id:
            raise ValueError("chat_id must be a nonempty exact string")
        if len(chat_id.encode("utf-8")) > 4096:
            raise ValueError("chat_id exceeds limit")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        for attempt in (1, 2):
            try:
                captured_now = _clock(clock_ns)
                mirror, pins_json, pins_present, cut = _capture(
                    mesh, chat_id, bounded, captured_now,
                )
                snapshot, consumed_accounts, consumed_subjects, deadline = _derive(
                    mesh.tx, chat_id, mirror, pins_json, cut, captured_now, bounded,
                )
                validated_now = _validate_final(
                    mesh, mirror, pins_json, pins_present, cut, captured_now,
                    deadline, clock_ns, bounded,
                )
                admission = MembershipAdmission(
                    mirror.position, cut.position, pins_present, _digest(pins_json),
                    _heads_digest(cut.heads), captured_now, validated_now, deadline,
                    tuple(sorted(consumed_accounts)), tuple(sorted(consumed_subjects)),
                )
                return MembershipSnapshotResult(
                    "admitted_local", _snapshot_json(snapshot), admission, None,
                )
            except _Reject as exc:
                reason = str(exc)
                if reason == "mirror_changed" and attempt == 1:
                    continue
                break
    except _Reject as exc:
        reason = str(exc)
    except (PinStoreUnavailable, MembershipInputUnavailable,
            LifecycleUnavailable, sqlite3.Error, OSError,
            OverflowError, TypeError, ValueError, KeyError, RecursionError,
            UnicodeError, MemoryError, AttributeError):
        reason = "local_unavailable"
    canonical = mesh.messaging.snapshot(chat_id)
    return MembershipSnapshotResult(
        "canonical", _snapshot_json(canonical), None, reason or "local_unavailable",
    )


def _capture(mesh, chat_id, limits, captured_now):
    del captured_now
    transport = mesh.tx
    if type(transport) is not CachingTransport:
        raise _Reject("unsupported")
    mirror = transport.capture_mirror_selection(MirrorSelectionRequest(
        None, (f"chats/{chat_id}/meta.json",), (),
        min(1, limits.max_documents), limits.max_bytes,
    ))
    if isinstance(mirror, MirrorCaptureUnavailable):
        raise _Reject(f"mirror_{mirror.reason}")
    if type(mirror) is not MirrorSelection:
        raise _Reject("mirror_invalid")
    mirror_used = _mirror_bytes(mirror)
    pins = mesh.key_pins
    try:
        with pins._lock, pins._storage.locked() as (pin_doc, pin_present):
            if pins._storage.pending:
                raise _Reject("pin_pending")
            pins_json = canonical_json(pin_doc)
            used = mirror_used + len(pins_json.encode("utf-8"))
            if used > limits.max_bytes:
                raise _Reject("budget_exceeded")
            conn = _open_reader(mesh.store.path)
            try:
                conn.execute("BEGIN")
                cut = membership_read_inputs.capture_cut(
                    conn, mesh.store.path, chat_id,
                    max_events=limits.max_events, max_heads=limits.max_heads,
                    max_bytes=limits.max_bytes - used,
                )
                validation = transport.validate_mirror_position(mirror.position)
            finally:
                conn.close()
    except PinStoreUnavailable as exc:
        raise _Reject("pin_unavailable") from exc
    if type(validation) is not MirrorPositionValidation:
        raise _Reject("mirror_invalid")
    if validation.status == "matched":
        return mirror, pins_json, pin_present, cut
    if validation.status == "unavailable":
        raise _Reject("mirror_unavailable")
    raise _Reject("mirror_changed")


def _derive(transport, chat_id, mirror, pins_json, cut, now_ns, limits):
    records = dict(mirror.exact_records)
    raw_meta = records.get(f"chats/{chat_id}/meta.json")
    if raw_meta is None:
        raise _Reject("missing_meta")
    meta = json.loads(raw_meta)
    boundary = meta.get("materialized_ns", 0) if type(meta) is dict else None
    if type(meta) is not dict or type(boundary) is not int or boundary < 0:
        raise _Reject("invalid_meta")
    materialized = ChatSnapshot.from_dict(meta)
    newer = []
    for ns, sender, event_id, kind, raw in cut.events:
        value = json.loads(raw)
        if type(value) is not dict or type(value.get("kind")) is not str \
                or type(value.get("ns")) is not int \
                or type(value.get("from")) is not str \
                or type(value.get("id")) is not str \
                or (value["kind"], value["ns"], value["from"], value["id"]) \
                != (kind, ns, sender, event_id):
            raise _Reject("invalid_event")
        event = value.get("event")
        if isinstance(event, dict) and event.get("type") == events.EV_REACTION:
            continue
        if ns > materialized.materialized_ns:
            newer.append(value)
    if not newer:
        return materialized, set(), set(), None
    resolver = _CapturedResolver(
        records, pins_json, cut.heads, now_ns, limits, mirror.position,
        mirror.serialized_bytes + len(pins_json.encode("utf-8")) + cut.serialized_bytes,
    ).bind_transport(transport)
    result = events.advance(materialized, newer, resolver)
    return result, resolver.consumed_accounts, resolver.consumed_subjects, resolver.deadline


class _CapturedResolver:
    def __init__(self, records, pins_json, heads, now_ns, limits, position, used):
        self.records = records
        self.transport = None
        self.pins = json.loads(pins_json).get("pins", {})
        self.heads = {name: payload for name, _generation, payload in heads}
        self.now_ns = now_ns
        self.limits = limits
        self.accounts = {}
        self.states = {}
        self.consumed_accounts = set()
        self.consumed_subjects = set()
        self.deadline = None
        self.dependency_steps = 0
        self.position = position
        self.used = used
        self.selected_records = len(records)
        self.lifecycle_loaded = False

    def bind_transport(self, transport):
        self.transport = transport
        return self

    def _selection(self, exact=(), prefixes=()):
        if self.transport is None:
            raise _Reject("mirror_invalid")
        value = self.transport.capture_mirror_selection(MirrorSelectionRequest(
            self.position, tuple(exact), tuple(prefixes),
            self.limits.max_documents - self.selected_records,
            self.limits.max_bytes - self.used,
        ))
        if isinstance(value, MirrorCaptureUnavailable):
            raise _Reject("mirror_changed" if value.reason == "changed"
                          else f"mirror_{value.reason}")
        if type(value) is not MirrorSelection:
            raise _Reject("mirror_invalid")
        self.used += value.serialized_bytes
        self.selected_records += len(value.exact_records) + sum(
            len(item.records) for item in value.complete_prefixes)
        return value

    def _raw_account(self, name):
        if name in self.accounts:
            return self.accounts[name]
        path = f"users/{name}.json"
        if path not in self.records:
            selected = self._selection(exact=(path,))
            self.records.update(selected.exact_records)
        raw = self.records.get(path)
        if raw is None:
            raise _Reject("account_unknown")
        value = json.loads(raw)
        _account_shape(name, value)
        account = Account.from_dict(value)
        pin = self.pins.get(name)
        if evaluate_pin(name, pin, account.keys.sign_pub, account.keys.agree_pub).action != "keep":
            raise _Reject("pin_action_required")
        self.accounts[name] = account
        self.consumed_accounts.add(name)
        if len(self.consumed_accounts) > 1024:
            raise _Reject("dependency_limit")
        return account

    def _account(self, name):
        account = self._raw_account(name)
        self._state(name)
        return account

    def _state(self, subject):
        if subject in self.states:
            return self.states[subject]
        facts = {}
        subjects = {}
        while self.dependency_steps < self.limits.max_dependency_steps:
            self.dependency_steps += 1
            subjects.setdefault(subject, self._evidence(subject))
            inputs = CapturedLifecycleInputs(
                self.now_ns, tuple(sorted(facts.items())), tuple(sorted(subjects.items())),
            )
            try:
                result = evaluate_lifecycle(inputs, subject, limits=LifecycleLimits(
                    max_accounts=1024, max_subjects=1024, max_envelopes=10_000,
                    max_bytes=self.limits.max_bytes, max_depth=32,
                ))
                if result.proposals:
                    raise _Reject("retained_write_required")
                self.consumed_accounts.update(result.consumed_accounts)
                self.consumed_subjects.update(result.consumed_subjects)
                if len(self.consumed_subjects) > 1024:
                    raise _Reject("dependency_limit")
                if result.next_recheck_ns is not None:
                    self.deadline = result.next_recheck_ns if self.deadline is None \
                        else min(self.deadline, result.next_recheck_ns)
                state = None if result.effective_json is None else json.loads(result.effective_json)
                self.states[subject] = state
                return state
            except LifecycleInputsIncomplete as exc:
                if exc.dependency_kind == "account" and exc.dependency_name is not None:
                    account = self._raw_account(exc.dependency_name)
                    facts[exc.dependency_name] = AccountAuthorityFact(
                        account.kind.value, account.keys.sign_pub, account.active,
                    )
                elif exc.dependency_kind == "subject" and exc.dependency_name is not None:
                    subjects[exc.dependency_name] = self._evidence(exc.dependency_name)
                else:
                    raise _Reject("lifecycle_incomplete") from exc
        raise _Reject("dependency_limit")

    def _evidence(self, subject):
        if not self.lifecycle_loaded:
            selected = self._selection(prefixes=("lifecycle/",))
            for group in selected.complete_prefixes:
                self.records.update((row.path, row.payload_json) for row in group.records)
            self.lifecycle_loaded = True
        prefix = f"lifecycle/{subject}/"
        envelopes = tuple(sorted(
            (path, raw) for path, raw in self.records.items() if path.startswith(prefix)
        ))
        return SubjectEvidence(True, envelopes, self.heads.get(subject))

    def kind(self, name):
        return self._account(name).kind

    def owner_of(self, name):
        account = self._account(name)
        state = self._state(name)
        if state is not None:
            return state.get("owner") or None
        return account.agent.owner if account.agent else None

    def sign_pub(self, name):
        return self._account(name).keys.sign_pub or None


def _validate_final(mesh, mirror, pins_json, pin_present, cut, captured_now,
                    deadline, clock_ns, limits):
    expected_mirror = mirror.position
    pins = mesh.key_pins
    try:
        with pins._lock, pins._storage.locked() as (doc, present):
            if pins._storage.pending:
                raise _Reject("pin_pending")
            if present != pin_present or canonical_json(doc) != pins_json:
                raise _Reject("pin_changed")
            conn = _open_writer(mesh.store.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                first = _clock(clock_ns)
                _matched(mesh.tx, expected_mirror)
                if not membership_read_inputs.matches_cut(
                    conn, mesh.store.path, cut, max_heads=limits.max_heads,
                    max_bytes=limits.max_bytes,
                ):
                    raise _Reject("store_changed")
                _matched(mesh.tx, expected_mirror)
                second = _clock(clock_ns)
                if first < captured_now or second < first:
                    raise _Reject("clock_rollback")
                if deadline is not None and (first >= deadline or second >= deadline):
                    raise _Reject("clock_expired")
                return second
            finally:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                finally:
                    conn.close()
    except PinStoreUnavailable as exc:
        raise _Reject("pin_unavailable") from exc


def _matched(transport, expected):
    value = transport.validate_mirror_position(expected)
    if type(value) is not MirrorPositionValidation or value.status != "matched":
        raise _Reject("mirror_changed" if getattr(value, "status", None) == "changed"
                      else "mirror_unavailable")


def _scope(mesh, value):
    expected = register_membership_admission_scope(mesh)
    if type(value) is not MembershipAdmissionScope or value != expected:
        raise _Reject("scope_mismatch")


def _limits(value):
    if type(value) is not MembershipReadLimits:
        raise TypeError("expected MembershipReadLimits")
    fields = tuple(getattr(value, name) for name in (
        "max_documents", "max_events", "max_heads", "max_bytes",
        "max_dependency_steps",
    ))
    copied = MembershipReadLimits(*fields)
    ceilings = (100_000, membership_read_inputs.MAX_EVENTS,
                membership_read_inputs.MAX_HEADS, membership_read_inputs.MAX_BYTES, 128)
    if any(type(item) is not int or not 0 <= item <= ceiling
           for item, ceiling in zip(copied.__dict__.values(), ceilings)):
        raise ValueError("membership read limits exceed supported range")
    return copied


def _clock(callback):
    try:
        value = callback()
    except Exception as exc:
        raise _Reject("clock_unavailable") from exc
    if type(value) is not int or not 0 <= value <= MAX_SQLITE_INTEGER:
        raise _Reject("clock_invalid")
    return value


def _account_shape(name, value):
    if type(value) is not dict or value.get("name") != name \
            or type(value.get("kind")) is not str \
            or value["kind"] not in {UserKind.HUMAN.value, UserKind.AGENT.value} \
            or type(value.get("active", True)) is not bool:
        raise _Reject("invalid_account")
    keys = value.get("keys", {})
    agent = value.get("agent")
    if type(keys) is not dict or type(keys.get("sign_pub", "")) is not str \
            or type(keys.get("agree_pub", "")) is not str \
            or (agent is not None and (type(agent) is not dict
                or type(agent.get("owner", "")) is not str
                or type(agent.get("machine", "")) is not str)) \
            or (value["kind"] == UserKind.AGENT.value and type(agent) is not dict):
        raise _Reject("invalid_account")


def _open_reader(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)


def _open_writer(path):
    return sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=1.0,
    )


def _mirror_bytes(value):
    return value.serialized_bytes


def _snapshot_json(value):
    return canonical_json_bytes(value.to_dict()).decode("utf-8")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _heads_digest(heads):
    return hashlib.sha256(canonical_json_bytes(list(heads))).hexdigest()


__all__ = [
    "MembershipAdmission", "MembershipAdmissionScope", "MembershipReadLimits",
    "MembershipSnapshotResult", "read_membership_snapshot",
    "register_membership_admission_scope",
]
