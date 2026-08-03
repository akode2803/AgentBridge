"""Signed append-only authority for mutable account lifecycle fields."""

from __future__ import annotations

import time

from .. import crypto
from ..core.models import UserKind
from ..core.jsonkit import canonical_json_bytes
from ..core.timekit import new_id, next_ns

__all__ = [
    "LifecycleError", "ensure_bootstrap", "lifecycle_prefix",
    "publish_change", "resolve_lifecycle",
]


class LifecycleError(ValueError):
    """Lifecycle evidence is malformed, forged, stale, or unauthorized."""


FIELDS = {
    "v", "id", "ns", "subject", "kind", "actor", "action", "owner",
    "machine", "active", "deactivated", "previous_id",
}
_ACTIONS = {"bootstrap", "state", "host", "transfer", "deactivate"}
_FUTURE_SKEW_NS = int(5 * 60 * 1e9)


def lifecycle_prefix(subject: str) -> str:
    return f"lifecycle/{subject}"


def _path(subject: str, record_id: str) -> str:
    return f"{lifecycle_prefix(subject)}/{record_id}.json"


def _validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise LifecycleError("invalid lifecycle fields")
    r = value
    if r["v"] != 1 or r["action"] not in _ACTIONS:
        raise LifecycleError("unsupported lifecycle record")
    for name in ("id", "subject", "kind", "actor", "action", "owner",
                 "machine", "deactivated", "previous_id"):
        if not isinstance(r[name], str):
            raise LifecycleError(f"invalid lifecycle {name}")
    if not r["id"] or not r["subject"] or not r["actor"]:
        raise LifecycleError("lifecycle identity fields are required")
    if r["kind"] not in {UserKind.HUMAN.value, UserKind.AGENT.value}:
        raise LifecycleError("invalid lifecycle kind")
    if isinstance(r["ns"], bool) or not isinstance(r["ns"], int):
        raise LifecycleError("invalid lifecycle ns")
    if not isinstance(r["active"], bool):
        raise LifecycleError("invalid lifecycle active state")
    if r["ns"] > time.time_ns() + _FUTURE_SKEW_NS:
        raise LifecycleError("lifecycle record is too far in the future")
    if r["kind"] == UserKind.HUMAN.value and (r["owner"] or r["machine"]):
        raise LifecycleError("human lifecycle cannot carry agent authority")
    if r["kind"] == UserKind.AGENT.value and (not r["owner"] or not r["machine"]):
        raise LifecycleError("agent lifecycle requires owner and machine")
    if r["deactivated"] and r["active"]:
        raise LifecycleError("deactivated lifecycle cannot be active")
    return r


def _raw(directory, name: str):
    getter = getattr(directory, "_raw_get", None)
    return getter(name) if callable(getter) else None


def _pub(directory, name: str) -> str:
    acc = _raw(directory, name)
    return acc.keys.sign_pub if acc else ""


def _verify_doc(directory, path: str, value: object) -> tuple[dict, str]:
    if not isinstance(value, dict) or set(value) != {"record", "sig", "subject_sig"}:
        raise LifecycleError("invalid lifecycle envelope")
    record = _validate(value["record"])
    if path != _path(record["subject"], record["id"]):
        raise LifecycleError("lifecycle path mismatch")
    signed = canonical_json_bytes(record)
    actor_pub = _pub(directory, record["actor"])
    if not actor_pub or not crypto.verify(actor_pub, str(value["sig"]), signed):
        raise LifecycleError("invalid lifecycle actor signature")
    subject_sig = str(value["subject_sig"])
    if subject_sig:
        subject_pub = _pub(directory, record["subject"])
        if not subject_pub or not crypto.verify(subject_pub, subject_sig, signed):
            raise LifecycleError("invalid lifecycle subject signature")
    return record, subject_sig


def _active_human(directory, name: str) -> bool:
    acc = _raw(directory, name)
    if not acc or acc.kind is not UserKind.HUMAN or not acc.keys.sign_pub:
        return False
    state = resolve_lifecycle(directory, name, store=directory.store)
    return bool(state["active"] if state is not None else acc.active)


def _authorized(directory, record: dict, subject_sig: str,
                current: dict | None) -> None:
    kind = record["kind"]
    action = record["action"]
    actor = record["actor"]
    subject = record["subject"]
    if current is None:
        if action != "bootstrap" or record["previous_id"]:
            raise LifecycleError("lifecycle chain must begin with bootstrap")
        raw = _raw(directory, subject)
        if not raw or raw.kind.value != kind:
            raise LifecycleError("lifecycle subject does not exist")
        if kind == UserKind.HUMAN.value:
            if actor != subject:
                raise LifecycleError("human bootstrap must be self-signed")
        elif actor == subject:
            if not subject_sig:
                raise LifecycleError("agent bootstrap requires subject proof")
        elif actor == record["owner"]:
            if not _active_human(directory, actor) or not subject_sig:
                raise LifecycleError("agent bootstrap requires owner and subject proof")
        else:
            raise LifecycleError("invalid agent bootstrap authority")
        return

    if record["previous_id"] != current["id"]:
        raise LifecycleError("lifecycle record does not extend current state")
    if kind != current["kind"] or subject != current["subject"]:
        raise LifecycleError("lifecycle identity changed")
    if current["deactivated"]:
        raise LifecycleError("deactivation is terminal")
    if action == "state":
        expected = subject if kind == UserKind.HUMAN.value else current["owner"]
        if actor != expected or record["owner"] != current["owner"] \
                or record["machine"] != current["machine"] \
                or record["deactivated"]:
            raise LifecycleError("invalid lifecycle state authority")
    elif action == "host":
        if (kind != UserKind.AGENT.value or actor != current["owner"]
                or record["owner"] != current["owner"] or not subject_sig):
            raise LifecycleError("host change requires owner and agent proof")
    elif action == "transfer":
        if (kind != UserKind.AGENT.value or actor != record["owner"]
                or not _active_human(directory, actor) or not subject_sig):
            raise LifecycleError("transfer requires new owner and agent proof")
    elif action == "deactivate":
        expected = subject if kind == UserKind.HUMAN.value else current["owner"]
        if actor != expected or record["active"] or not record["deactivated"]:
            raise LifecycleError("invalid deactivation authority")
        if record["owner"] != current["owner"] or record["machine"] != current["machine"]:
            raise LifecycleError("deactivation cannot move authority")
    else:
        raise LifecycleError("bootstrap cannot extend a lifecycle chain")


def _cached(store, subject: str) -> dict | None:
    if store is None:
        return None
    value = store.cached_doc(f"lifecycle/head/{subject}", default={})
    return value if isinstance(value, dict) and value.get("id") else None


def resolve_lifecycle(directory, subject: str, *, store=None) -> dict | None:
    """Fold valid evidence and retain a newer local head against rollback."""
    local = _cached(store, subject)
    records = []
    try:
        paths = directory.tx.list_docs(lifecycle_prefix(subject))
    except OSError:
        if local is not None:
            return local
        raise
    for path in paths:
        try:
            record, subject_sig = _verify_doc(
                directory, path, directory.tx.get_doc(path),
            )
            if record["subject"] != subject:
                raise LifecycleError("lifecycle subject mismatch")
            records.append((record["ns"], record["id"], record, subject_sig))
        except OSError:
            if local is not None:
                return local
            raise
        except (LifecycleError, TypeError, ValueError):
            continue

    current = None
    accepted: set[str] = set()
    for _ns, _record_id, record, subject_sig in sorted(records):
        try:
            _authorized(directory, record, subject_sig, current)
            current = record
            accepted.add(record["id"])
        except (LifecycleError, TypeError, ValueError):
            continue
    if local is not None and (current is None or local["id"] not in accepted):
        return local
    if current is not None and store is not None:
        store.cache_doc(f"lifecycle/head/{subject}", current)
    return current


def _write(directory, keystore, record: dict, *, subject_proof: bool) -> dict:
    actor_bundle = keystore.load(record["actor"])
    if not actor_bundle:
        raise LifecycleError(f"identity key for @{record['actor']} is unavailable")
    signed = canonical_json_bytes(record)
    subject_sig = ""
    if subject_proof:
        bundle = keystore.load(record["subject"])
        if not bundle:
            raise LifecycleError(
                f"identity key for @{record['subject']} is unavailable")
        subject_sig = crypto.sign(bundle, signed)
    doc = {"record": record, "sig": crypto.sign(actor_bundle, signed),
           "subject_sig": subject_sig}
    directory.tx.create_doc(_path(record["subject"], record["id"]), doc)
    return record


def ensure_bootstrap(directory, keystore, subject: str, *, actor: str) -> dict:
    current = resolve_lifecycle(directory, subject, store=directory.store)
    if current is not None:
        return current
    raw = _raw(directory, subject)
    if raw is None:
        raise LifecycleError(f"unknown lifecycle subject @{subject}")
    owner = raw.agent.owner if raw.agent else ""
    machine = raw.agent.machine if raw.agent else ""
    ns = next_ns()
    record = {
        "v": 1, "id": new_id("life", ns), "ns": ns, "subject": subject,
        "kind": raw.kind.value, "actor": actor, "action": "bootstrap",
        "owner": owner, "machine": machine, "active": bool(raw.active),
        "deactivated": str(raw.deactivated or ""), "previous_id": "",
    }
    subject_proof = raw.kind is UserKind.AGENT
    _authorized(directory, record, "proof" if subject_proof else "", None)
    return _write(directory, keystore, record, subject_proof=subject_proof)


def publish_change(directory, keystore, subject: str, *, actor: str,
                   action: str, active: bool | None = None,
                   owner: str | None = None, machine: str | None = None,
                   deactivated: str | None = None) -> dict:
    current = resolve_lifecycle(directory, subject, store=directory.store)
    if current is None:
        current = ensure_bootstrap(directory, keystore, subject, actor=actor)
    try:
        remote, _proof = _verify_doc(
            directory, _path(subject, current["id"]),
            directory.tx.get_doc(_path(subject, current["id"])),
        )
    except (LifecycleError, OSError, TypeError, ValueError) as exc:
        raise LifecycleError(
            "cannot mutate lifecycle while its verified head is unavailable"
        ) from exc
    if remote != current:
        raise LifecycleError("lifecycle head changed before mutation")
    ns = next_ns()
    record = {
        **current, "id": new_id("life", ns), "ns": ns, "actor": actor,
        "action": action, "previous_id": current["id"],
        "active": current["active"] if active is None else bool(active),
        "owner": current["owner"] if owner is None else str(owner),
        "machine": current["machine"] if machine is None else str(machine),
        "deactivated": (current["deactivated"] if deactivated is None
                        else str(deactivated)),
    }
    subject_proof = action in {"host", "transfer"}
    _authorized(directory, record, "proof" if subject_proof else "", current)
    written = _write(directory, keystore, record, subject_proof=subject_proof)
    resolved = resolve_lifecycle(directory, subject, store=directory.store)
    if resolved is None or resolved["id"] != written["id"]:
        raise LifecycleError("concurrent lifecycle change won; retry")
    return written
