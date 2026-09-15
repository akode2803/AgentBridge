"""Disposable-fixture comparison for captured and canonical membership folds."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..core.jsonkit import canonical_json_bytes
from ..core.models import Account, ChatSnapshot, UserKind
from ..mesh import events
from ..mesh.lifecycle_evaluation import (
    AccountAuthorityFact, CapturedLifecycleInputs, LifecycleInputsIncomplete,
    SubjectEvidence, evaluate_lifecycle,
)
from ..mesh.lifecycle import LifecycleUnavailable
from ..mesh.local_membership_capture import (
    CapturedMembershipAuthority, LocalAuthorityUnavailable,
)
from ..mesh.pins import evaluate_pin
from ..store import shadow_slot


@dataclass(frozen=True)
class DisposableFixtureRegistration:
    mesh_identity: int
    pins_identity: int
    store_identity: int
    database_path: str
    pin_path: str


@dataclass(frozen=True)
class MembershipComparison:
    equal: bool | None
    captured_digest: str | None
    canonical_digest: str | None
    differing_fields: tuple[str, ...]
    unavailable_reason: str | None
    dependencies: tuple[tuple[str, str], ...]


def register_disposable_fixture(mesh) -> DisposableFixtureRegistration:
    """Record a harness assertion; this is not external-writer proof."""
    return DisposableFixtureRegistration(
        id(mesh), id(mesh.key_pins), id(mesh.store), str(mesh.store.path),
        str(mesh.key_pins.path),
    )


def compare_fixture(
    mesh, capture: CapturedMembershipAuthority,
    registration: DisposableFixtureRegistration,
) -> MembershipComparison:
    _registration(mesh, capture, registration)
    try:
        snapshot = _shadow(capture)
        records = dict(snapshot.records)
        meta_raw = records.get(f"chats/{capture.chat_id}/meta.json")
        if meta_raw is None:
            raise LocalAuthorityUnavailable("missing_materialized_snapshot")
        meta = json.loads(meta_raw)
        boundary = meta.get("materialized_ns", 0) if type(meta) is dict else None
        if type(meta) is not dict or type(boundary) is not int or boundary < 0:
            raise ValueError("invalid materialized boundary")
        materialized = ChatSnapshot.from_dict(meta)
        newer = []
        for ns, sender, event_id, kind, payload_json in capture.raw.events:
            payload = json.loads(payload_json)
            if type(payload) is not dict or type(payload.get("kind")) is not str \
                    or type(payload.get("ns")) is not int \
                    or type(payload.get("from")) is not str \
                    or type(payload.get("id")) is not str \
                    or payload["kind"] != kind or payload["ns"] != ns \
                    or payload["from"] != sender or payload["id"] != event_id:
                raise ValueError("event metadata mismatch")
            event = payload.get("event")
            if isinstance(event, dict) and event.get("type") == events.EV_REACTION:
                continue
            if ns > materialized.materialized_ns:
                newer.append(payload)
        # Preserve Messaging.snapshot's no-newer fast path exactly.
        captured_snapshot = materialized if not newer else events.advance(
            materialized, newer, _resolver(capture, records),
        )
        captured_value = captured_snapshot.to_dict()
        captured_bytes = canonical_json_bytes(captured_value)
        captured_reason = None
    except LocalAuthorityUnavailable as exc:
        captured_value = None
        captured_bytes = None
        captured_reason = exc.reason
    except LifecycleInputsIncomplete:
        captured_value = None
        captured_bytes = None
        captured_reason = "lifecycle_inputs_incomplete"
    except LifecycleUnavailable:
        captured_value = None
        captured_bytes = None
        captured_reason = "lifecycle_unavailable"
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError,
            MemoryError, AttributeError, OverflowError, json.JSONDecodeError):
        captured_value = None
        captured_bytes = None
        captured_reason = "invalid_captured_membership"
    try:
        canonical_value = mesh.messaging.snapshot(capture.chat_id).to_dict()
        canonical_bytes = canonical_json_bytes(canonical_value)
    except Exception:  # diagnostic fixture reports canonical failure
        return MembershipComparison(
            None, None if captured_bytes is None else hashlib.sha256(captured_bytes).hexdigest(),
            None, (), "canonical_unavailable", _dependencies(capture),
        )
    if captured_bytes is None:
        return MembershipComparison(
            None, None, hashlib.sha256(canonical_bytes).hexdigest(), (),
            captured_reason, _dependencies(capture),
        )
    fields = tuple(sorted(
        key for key in set(captured_value) | set(canonical_value)
        if captured_value.get(key) != canonical_value.get(key)
    ))[:128]
    return MembershipComparison(
        captured_bytes == canonical_bytes,
        hashlib.sha256(captured_bytes).hexdigest(),
        hashlib.sha256(canonical_bytes).hexdigest(), fields, None,
        _dependencies(capture),
    )


def _resolver(capture, records):
    try:
        pins = json.loads(capture.pins_json)
    except (TypeError, ValueError, RecursionError) as exc:
        raise LocalAuthorityUnavailable("invalid_captured_pins") from exc
    accounts = {}
    facts = []
    for name in capture.known_accounts:
        raw = records.get(f"users/{name}.json")
        if raw is None:
            raise LifecycleInputsIncomplete(f"fixture account @{name} is missing")
        value = json.loads(raw)
        _account_shape(name, value)
        account = Account.from_dict(value)
        pin = pins.get("pins", {}).get(name)
        sign, agree = account.keys.sign_pub, account.keys.agree_pub
        if evaluate_pin(name, pin, sign, agree).action != "keep":
            raise LocalAuthorityUnavailable("pin_action_required")
        accounts[name] = account
        facts.append((name, AccountAuthorityFact(account.kind.value, sign, account.active)))
    for name in capture.known_absent_accounts:
        if f"users/{name}.json" in records:
            raise LocalAuthorityUnavailable("fixture_absence_mismatch")
        facts.append((name, None))
    heads = {name: payload for name, _generation, payload in capture.raw.heads}
    subjects = []
    for subject in capture.complete_lifecycle_subjects:
        prefix = f"lifecycle/{subject}/"
        envelopes = tuple((path, raw) for path, raw in snapshot_items(records)
                          if path.startswith(prefix))
        subjects.append((subject, SubjectEvidence(True, envelopes, heads.get(subject))))
    for subject in set(capture.known_accounts) - set(capture.complete_lifecycle_subjects):
        subjects.append((subject, SubjectEvidence(False, (), heads.get(subject))))
    inputs = CapturedLifecycleInputs(capture.now_ns, tuple(facts), tuple(subjects))
    states = {}

    def state(name):
        if name not in states:
            result = evaluate_lifecycle(inputs, name)
            if result.proposals:
                raise LocalAuthorityUnavailable("lifecycle_publication_required")
            states[name] = None if result.effective_json is None \
                else json.loads(result.effective_json)
        return states[name]

    class Resolver:
        def _account(self, name):
            if name in accounts:
                state(name)
                return accounts[name]
            if name in capture.known_absent_accounts:
                return None
            raise LifecycleInputsIncomplete(f"fixture account @{name} is unasserted")
        def kind(self, name):
            account = self._account(name)
            return account.kind if account else None
        def owner_of(self, name):
            account = self._account(name)
            if account is None:
                return None
            current = state(name)
            if current is not None:
                return current.get("owner") or None
            return account.agent.owner if account and account.agent else None
        def sign_pub(self, name):
            account = self._account(name)
            return (account.keys.sign_pub or None) if account else None
    return Resolver()


def snapshot_items(records):
    return tuple(sorted(records.items()))


def _shadow(capture):
    raw = capture.raw.shadow
    if raw is None:
        raise LocalAuthorityUnavailable("missing_shadow")
    revision, cursor, provenance, chats, records = raw
    snapshot = shadow_slot.ShadowSnapshot(
        capture.raw.position.source, revision, cursor, provenance,
        shadow_slot._decode_chat_ids(chats), records,
    )
    return shadow_slot._prepare(
        snapshot, shadow_slot.MAX_RECORDS, shadow_slot.MAX_RECORDS,
        shadow_slot.MAX_BYTES,
    )[0]


def _registration(mesh, capture, value):
    expected = DisposableFixtureRegistration(
        id(mesh), id(mesh.key_pins), id(mesh.store), str(mesh.store.path),
        str(mesh.key_pins.path),
    )
    if type(value) is not DisposableFixtureRegistration or value != expected \
            or capture.process_identity != f"{id(mesh)}:{id(mesh.key_pins)}:{id(mesh.store)}" \
            or capture.pin_path != str(mesh.key_pins.path) \
            or capture.raw.position.database_path != str(mesh.store.path):
        raise LocalAuthorityUnavailable("fixture_registration_mismatch")


def _account_shape(name, value):
    if type(value) is not dict or value.get("name") != name \
            or type(value.get("name")) is not str \
            or type(value.get("kind")) is not str \
            or value["kind"] not in {UserKind.HUMAN.value, UserKind.AGENT.value} \
            or type(value.get("active", True)) is not bool:
        raise LocalAuthorityUnavailable("invalid_captured_account")
    keys = value.get("keys", {})
    if type(keys) is not dict or type(keys.get("sign_pub", "")) is not str \
            or type(keys.get("agree_pub", "")) is not str:
        raise LocalAuthorityUnavailable("invalid_captured_account")
    agent = value.get("agent")
    if agent is not None and (
        type(agent) is not dict or type(agent.get("owner", "")) is not str
        or type(agent.get("machine", "")) is not str
    ):
        raise LocalAuthorityUnavailable("invalid_captured_account")
    if value["kind"] == UserKind.AGENT.value and type(agent) is not dict:
        raise LocalAuthorityUnavailable("invalid_captured_account")


def _dependencies(capture):
    position = capture.raw.position
    return (
        ("capture_process", capture.process_identity),
        ("database", position.database_path),
        ("shadow_generation", str(position.generation)),
        ("shadow_incarnation", position.incarnation),
        ("pin_sha256", hashlib.sha256(capture.pins_json.encode("utf-8")).hexdigest()),
        ("now_ns", str(capture.now_ns)),
    )


__all__ = [
    "DisposableFixtureRegistration", "MembershipComparison", "compare_fixture",
    "register_disposable_fixture",
]
