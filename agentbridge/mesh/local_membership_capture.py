"""Compose one bounded historical cut of local membership authority inputs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..store import local_membership_inputs, shadow_slot
from .pin_storage import PinStoreUnavailable


class LocalAuthorityUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MembershipCaptureLimits:
    max_documents: int = shadow_slot.MAX_RECORDS
    max_events: int = local_membership_inputs.MAX_EVENTS
    max_heads: int = local_membership_inputs.MAX_HEADS
    max_bytes: int = local_membership_inputs.MAX_BYTES


@dataclass(frozen=True)
class CapturedMembershipAuthority:
    chat_id: str
    now_ns: int
    process_identity: str
    pin_path: str
    pins_present: bool
    pins_json: str
    raw: local_membership_inputs.RawMembershipInputs
    known_accounts: tuple[str, ...]
    known_absent_accounts: tuple[str, ...]
    complete_lifecycle_subjects: tuple[str, ...]


def capture(
    mesh,
    expected_shadow: shadow_slot.ShadowPosition,
    chat_id: str,
    *,
    known_accounts: tuple[str, ...] = (),
    known_absent_accounts: tuple[str, ...] = (),
    complete_lifecycle_subjects: tuple[str, ...] = (),
    now_ns: int,
    limits: MembershipCaptureLimits = MembershipCaptureLimits(),
) -> CapturedMembershipAuthority:
    """Capture durable pins and SQLite rows; perform no trust side effects."""
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("now_ns must be a nonnegative exact integer")
    if type(limits) is not MembershipCaptureLimits:
        raise TypeError("expected MembershipCaptureLimits")
    bounded = MembershipCaptureLimits(
        limits.max_documents, limits.max_events, limits.max_heads,
        limits.max_bytes,
    )
    wanted = shadow_slot.validate_shadow_position(
        expected_shadow, mesh.store.path, owned=True,
    )
    for value, ceiling in (
        (bounded.max_documents, shadow_slot.MAX_RECORDS),
        (bounded.max_events, local_membership_inputs.MAX_EVENTS),
        (bounded.max_heads, local_membership_inputs.MAX_HEADS),
        (bounded.max_bytes, local_membership_inputs.MAX_BYTES),
    ):
        if type(value) is not int or not 0 <= value <= ceiling:
            raise ValueError("membership capture budget exceeds supported range")
    domains = tuple(_names(value) for value in (
        known_accounts, known_absent_accounts, complete_lifecycle_subjects,
    ))
    if set(domains[0]) & set(domains[1]):
        raise ValueError("account fixture domains overlap")
    if type(chat_id) is not str or not chat_id:
        raise ValueError("chat_id must be a nonempty string")
    process_identity = f"{id(mesh)}:{id(mesh.key_pins)}:{id(mesh.store)}"
    fixed = _fixed_charge(
        chat_id, process_identity, str(mesh.key_pins.path), wanted, domains,
    )
    if fixed > bounded.max_bytes:
        raise LocalAuthorityUnavailable("capture_too_large")
    pins = mesh.key_pins
    storage = pins._storage
    try:
        with pins._lock, storage.locked() as (pin_doc, pin_present):
            if storage.pending:
                raise LocalAuthorityUnavailable("pending_pins")
            pins_json = json.dumps(
                pin_doc, ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            )
            used = fixed + len(pins_json.encode("utf-8"))
            if used > bounded.max_bytes:
                raise LocalAuthorityUnavailable("capture_too_large")
            conn = sqlite3.connect(
                mesh.store.path.as_uri() + "?mode=ro", uri=True, timeout=1.0,
            )
            try:
                conn.execute("BEGIN")
                raw = local_membership_inputs.capture_raw(
                    conn, mesh.store.path, wanted, chat_id,
                    max_documents=bounded.max_documents,
                    max_events=bounded.max_events, max_heads=bounded.max_heads,
                    max_bytes=bounded.max_bytes - used,
                )
            finally:
                conn.close()
    except LocalAuthorityUnavailable:
        raise
    except (PinStoreUnavailable, shadow_slot.ShadowConflict, OverflowError,
            sqlite3.Error, MemoryError, RecursionError, UnicodeError,
            ValueError, TypeError) as exc:
        raise LocalAuthorityUnavailable("capture_unavailable") from exc
    result = CapturedMembershipAuthority(
        chat_id, now_ns, process_identity, str(pins.path), pin_present,
        pins_json, raw, *domains,
    )
    try:
        charged = _byte_charge(result)
    except (TypeError, ValueError, UnicodeError, MemoryError) as exc:
        raise LocalAuthorityUnavailable("capture_unavailable") from exc
    if charged > bounded.max_bytes:
        raise LocalAuthorityUnavailable("capture_too_large")
    return result


def _names(value) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > 1_024 \
            or any(type(name) is not str or not name for name in value) \
            or len(set(value)) != len(value):
        raise ValueError("fixture names must be unique bounded exact strings")
    return tuple(value)


def _byte_charge(value: CapturedMembershipAuthority) -> int:
    position = value.raw.position
    domains = (value.known_accounts, value.known_absent_accounts,
               value.complete_lifecycle_subjects)
    used = _fixed_charge(
        value.chat_id, value.process_identity, value.pin_path, position, domains,
    ) + len(value.pins_json.encode("utf-8"))

    if value.raw.shadow is not None:
        revision, cursor, provenance, chats, records = value.raw.shadow
        used += 16 + len(provenance.encode("utf-8")) + len(chats.encode("utf-8"))
        used += sum(8 + len(path.encode("utf-8")) + len(payload.encode("utf-8"))
                    for path, payload in records)
    used += sum(16 + len(sender.encode("utf-8")) + len(event_id.encode("utf-8"))
                + len(kind.encode("utf-8")) + len(payload.encode("utf-8"))
                for _ns, sender, event_id, kind, payload in value.raw.events)
    used += sum(17 + len(subject.encode("utf-8"))
                + (0 if payload is None else len(payload.encode("utf-8")))
                for subject, _generation, payload in value.raw.heads)
    return used


def _fixed_charge(chat_id, process_identity, pin_path, position, domains) -> int:
    strings = [
        chat_id, process_identity, pin_path, position.database_path,
        position.incarnation, *(domains[0] + domains[1] + domains[2]),
    ]
    if position.publisher_nonce is not None:
        strings.append(position.publisher_nonce)
    if position.source is not None:
        strings.extend((position.source.root, position.source.cache,
                        position.source.mirror_nonce))
    return sum(len(item.encode("utf-8")) for item in strings) \
        + 8 * 3 + 2 + 8 * sum(len(domain) for domain in domains)


__all__ = [
    "CapturedMembershipAuthority", "LocalAuthorityUnavailable",
    "MembershipCaptureLimits", "capture",
]
