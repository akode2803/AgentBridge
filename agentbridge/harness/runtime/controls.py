"""Authenticated runtime mutation controls outside the permission broker."""

from __future__ import annotations

import hashlib
import time
from typing import Literal

from ... import crypto
from ...core.models import UserKind
from ...core.timekit import new_id, next_ns
from .authority import AuthorityError, responsible_authority
from .envelope import EnvelopeError, open_pairwise, seal_pairwise
from .models import RuntimeContractError, canonical_json_bytes


class ControlError(RuntimeContractError):
    """A runtime control is malformed, forged, stale, or unauthorized."""


OWNER_FIELDS_V1 = {
    "v", "id", "ns", "target", "owner", "action", "chat_id", "timer_id",
    "timer_at_ns", "expires_ns", "ownership_epoch", "policy_revision",
}
OWNER_FIELDS = OWNER_FIELDS_V1 | {"run_id"}
OWNER_HEADER_FIELDS_V1 = {
    "v", "kind", "id", "ns", "sender", "recipient", "target", "action",
    "expires_ns", "ownership_epoch", "policy_revision",
}
OWNER_HEADER_FIELDS = OWNER_HEADER_FIELDS_V1 | {"run_id"}
PAUSE_FIELDS = {
    "v", "id", "ns", "scope", "actor", "chat_id", "paused",
    "actor_epoch",
}
_FUTURE_SKEW_NS = int(5 * 60 * 1e9)
_PAUSE_READ_LIMIT = 10_000


def owner_command_prefix(target: str) -> str:
    return f"runtime/owner-control/{target}/commands"


def owner_command_path(target: str, command_id: str) -> str:
    return f"{owner_command_prefix(target)}/{command_id}.json"


def pause_prefix(chat_id: str = "") -> str:
    if chat_id:
        return f"chats/{chat_id}/runtime/member-control/pause"
    return "runtime/member-control/global/pause"


def _strict(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ControlError(f"invalid {label} fields")
    return value


def _owner_header(record: dict) -> dict:
    header = {
        "v": record["v"], "kind": "owner_command", "id": record["id"],
        "ns": record["ns"], "sender": record["owner"],
        "recipient": record["target"], "target": record["target"],
        "action": record["action"], "expires_ns": record["expires_ns"],
        "ownership_epoch": record["ownership_epoch"],
        "policy_revision": record["policy_revision"],
    }
    if record["v"] >= 2:
        header["run_id"] = record["run_id"]
    return header


def _validate_owner(record: object) -> dict:
    if not isinstance(record, dict):
        raise ControlError("invalid owner command fields")
    version = record.get("v")
    expected = OWNER_FIELDS if version == 2 else OWNER_FIELDS_V1
    r = _strict(record, expected, "owner command")
    if version not in {1, 2} or r["action"] not in {"stop", "timer_cancel"}:
        raise ControlError("unsupported owner command")
    for name in ("id", "target", "owner", "action"):
        if not isinstance(r[name], str) or not r[name]:
            raise ControlError(f"invalid owner command {name}")
    for name in ("chat_id", "timer_id"):
        if not isinstance(r[name], str):
            raise ControlError(f"invalid owner command {name}")
    run_id = r.get("run_id", "")
    if not isinstance(run_id, str):
        raise ControlError("invalid owner command run_id")
    for name in ("ns", "timer_at_ns", "expires_ns", "ownership_epoch",
                 "policy_revision"):
        if isinstance(r[name], bool) or not isinstance(r[name], int):
            raise ControlError(f"invalid owner command {name}")
    if r["action"] == "stop":
        if version != 2 or not run_id:
            raise ControlError("stop command must bind an active run")
        if r["timer_id"] or r["timer_at_ns"]:
            raise ControlError("stop command cannot carry timer fields")
    if r["action"] == "timer_cancel" and (run_id or not r["timer_id"]
                                             or r["timer_at_ns"] <= 0):
        raise ControlError("timer cancel must bind a live timer")
    return r


def _current_owner_authority(mesh, record: dict) -> None:
    current = responsible_authority(mesh, record["target"])
    if current["owner"] != record["owner"]:
        raise ControlError("responsible member changed")
    for name in ("ownership_epoch", "policy_revision"):
        if int(current[name]) != record[name]:
            raise ControlError(f"stale {name}")
    if time.time_ns() >= record["expires_ns"]:
        raise ControlError("owner command expired")


def publish_owner_command(mesh, *, target: str, action: str,
                          chat_id: str = "", timer_id: str = "",
                          timer_at_ns: int = 0, run_id: str = "",
                          timeout_s: float = 30.0) -> dict:
    """Create one immutable owner-to-agent command from server-owned fields."""
    auth = responsible_authority(mesh, target)
    if auth["owner"] != mesh.user:
        raise ControlError("only the responsible member can control this agent")
    ns = next_ns()
    version = 2 if action == "stop" else 1
    record = {
        "v": version, "id": new_id("control", ns), "ns": ns,
        "target": target,
        "owner": mesh.user, "action": action, "chat_id": str(chat_id),
        "timer_id": str(timer_id), "timer_at_ns": int(timer_at_ns),
        "expires_ns": ns + max(1, int(timeout_s * 1e9)),
        "ownership_epoch": int(auth["ownership_epoch"]),
        "policy_revision": int(auth["policy_revision"]),
    }
    if version >= 2:
        record["run_id"] = str(run_id)
    _validate_owner(record)
    envelope = seal_pairwise(
        mesh, header=_owner_header(record), sender=mesh.user,
        recipient=target, payload=record,
    )
    mesh.tx.create_doc(owner_command_path(target, record["id"]), envelope)
    return record


def _open_owner_command(mesh, path: str, target: str) -> dict:
    owner = mesh.directory.owner_of(target)
    raw = mesh.tx.get_doc(path)
    header = raw.get("header") if isinstance(raw, dict) else None
    version = header.get("v") if isinstance(header, dict) else None
    header_fields = OWNER_HEADER_FIELDS if version == 2 else OWNER_HEADER_FIELDS_V1
    opened = open_pairwise(
        mesh, raw, header_fields=header_fields,
        expected={"v": version, "kind": "owner_command", "sender": owner,
                  "recipient": target, "target": target},
        sender=owner, recipient=target, viewer=target,
    )
    record = _validate_owner(opened.payload)
    header = opened.header
    for name in ("id", "ns", "target", "action", "expires_ns",
                 "ownership_epoch", "policy_revision"):
        if header[name] != record[name]:
            raise ControlError(f"owner command header {name} mismatch")
    if record.get("run_id", "") != header.get("run_id", ""):
        raise ControlError("owner command header run_id mismatch")
    if path != owner_command_path(target, record["id"]):
        raise ControlError("owner command path mismatch")
    _current_owner_authority(mesh, record)
    return record


def owner_commands(mesh, *, target: str, action: str = "") -> list[dict]:
    """Return currently valid commands without consuming mismatched actions."""
    out = []
    for path in mesh.tx.list_docs(owner_command_prefix(target)):
        try:
            record = _open_owner_command(mesh, path, target)
            if not action or record["action"] == action:
                out.append(record)
        except (AuthorityError, ControlError, EnvelopeError, OSError):
            continue
    return sorted(out, key=lambda r: (r["ns"], r["id"]))


def consume_owner_command(mesh, *, target: str, action: str,
                          chat_id: str | None = None,
                          run_id: str | None = None) -> dict | None:
    """Durably claim the first matching valid command and remove it best-effort."""
    if action == "stop" and not run_id:
        raise ControlError("stop consumption requires an exact run id")
    for record in owner_commands(mesh, target=target, action=action):
        if chat_id is not None and record["chat_id"] not in ("", chat_id):
            continue
        if run_id is not None and record.get("run_id", "") != run_id:
            continue
        if not mesh.store.claim_once("runtime-control", record["id"],
                                     time.time_ns()):
            continue
        try:
            mesh.tx.delete_doc(owner_command_path(target, record["id"]))
        except Exception:
            pass
        return record
    return None


def _actor_epoch(directory, actor: str) -> int:
    acc = directory.get(actor)
    if (not acc or not acc.active or acc.kind is not UserKind.HUMAN
            or not acc.keys.sign_pub):
        raise ControlError("pause actor is not an active signed-in member")
    value = {
        "actor": actor, "active": bool(acc.active), "kind": acc.kind.value,
        "sign_pub": acc.keys.sign_pub, "agree_pub": acc.keys.agree_pub,
    }
    raw = hashlib.sha256(canonical_json_bytes(value)).digest()[:8]
    return int.from_bytes(raw, "big") & ((1 << 63) - 1)


def _validate_pause(value: object) -> dict:
    r = _strict(value, PAUSE_FIELDS, "pause control")
    if r["v"] != 1 or r["scope"] not in {"global", "room"}:
        raise ControlError("unsupported pause control")
    if not isinstance(r["paused"], bool):
        raise ControlError("pause state must be boolean")
    for name in ("id", "scope", "actor"):
        if not isinstance(r[name], str) or not r[name]:
            raise ControlError(f"invalid pause control {name}")
    if not isinstance(r["chat_id"], str):
        raise ControlError("invalid pause control chat_id")
    for name in ("ns", "actor_epoch"):
        if isinstance(r[name], bool) or not isinstance(r[name], int):
            raise ControlError(f"invalid pause control {name}")
    if (r["scope"] == "room") != bool(r["chat_id"]):
        raise ControlError("pause scope and chat binding disagree")
    if r["ns"] > time.time_ns() + _FUTURE_SKEW_NS:
        raise ControlError("pause control is too far in the future")
    return r


def publish_pause(mesh, *, paused: bool, chat_id: str = "") -> dict:
    """Append one signed visible pause/resume state from a current human."""
    if not chat_id:
        raise ControlError(
            "Mesh-wide pause was retired; pause one chat or stand down your own agents")
    if not mesh.snapshot(chat_id).is_member(mesh.user):
        raise ControlError("pause actor is not a member of this chat")
    ns = next_ns()
    record = {
        "v": 1, "id": new_id("pause", ns), "ns": ns,
        "scope": "room", "actor": mesh.user,
        "chat_id": chat_id, "paused": bool(paused),
        "actor_epoch": _actor_epoch(mesh.directory, mesh.user),
    }
    _validate_pause(record)
    bundle = mesh.keystore.load(mesh.user)
    if not bundle:
        raise ControlError("pause actor identity key is unavailable")
    signed = canonical_json_bytes(record)
    doc = {"record": record, "sig": crypto.sign(bundle, signed)}
    mesh.tx.create_doc(f"{pause_prefix(chat_id)}/{record['id']}.json", doc)
    return record


def read_pause(directory, tx, *, chat_id: str = "", snapshot=None,
               source: Literal["fresh", "cached"] = "fresh") -> bool:
    """Fold latest valid signed state; malformed and legacy controls are inert.

    ``fresh`` remains the enforcement default and may merge a live runtime
    listing. ``cached`` is presentation-only: it reads one bounded synchronized
    mirror snapshot while applying the identical signature/membership rules.
    """
    if source == "fresh":
        paths = tx.list_docs(pause_prefix(chat_id))
    elif source == "cached":
        paths = tx.list_cached_docs_bounded(
            pause_prefix(chat_id), _PAUSE_READ_LIMIT)
    else:
        raise ValueError("pause source must be fresh or cached")
    valid = []
    for path in paths:
        try:
            doc = _strict(tx.get_doc(path), {"record", "sig"}, "pause envelope")
            record = _validate_pause(doc["record"])
            if record["chat_id"] != chat_id:
                raise ControlError("pause chat mismatch")
            if path != f"{pause_prefix(chat_id)}/{record['id']}.json":
                raise ControlError("pause path mismatch")
            if record["actor_epoch"] != _actor_epoch(directory, record["actor"]):
                raise ControlError("stale pause actor identity")
            if chat_id and (snapshot is None
                            or not snapshot.is_member(record["actor"])):
                raise ControlError("pause actor is no longer a room member")
            pub = directory.sign_pub(record["actor"])
            if not pub or not crypto.verify(pub, str(doc["sig"]),
                                             canonical_json_bytes(record)):
                raise ControlError("invalid pause signature")
            valid.append(record)
        except (ControlError, TypeError, ValueError):
            continue
    if not valid:
        return False
    latest = max(valid, key=lambda r: (r["ns"], r["id"]))
    return bool(latest["paused"])
