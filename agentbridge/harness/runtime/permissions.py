"""Secure room-bound permission ask and decision lane."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from ...core.timekit import new_id
from .authority import AuthorityError, authority
from .envelope import EnvelopeError, open_envelope, open_pairwise, seal
from .models import RuntimeContractError, canonical_json_bytes


class PermissionRecordError(RuntimeContractError):
    """A permission record is invalid or no longer authoritative."""


def ask_path(chat_id: str, agent: str, ask_id: str) -> str:
    return f"chats/{chat_id}/runtime/owner-control/{agent}/asks/{ask_id}.json"


def decision_prefix(chat_id: str, agent: str, ask_id: str) -> str:
    return f"chats/{chat_id}/runtime/owner-control/{agent}/decisions/{ask_id}"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _record(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise PermissionRecordError(f"invalid {name} fields")
    return value


ASK_FIELDS = {
    "v", "id", "call_id", "ns", "run_id", "chat_id", "agent", "owner",
    "kind", "tool", "input_digest", "detail", "label", "options", "scope",
    "expires_ns", "key_epoch", "membership_epoch", "ownership_epoch",
    "policy_revision",
}
DECISION_FIELDS = {
    "v", "id", "ns", "ask_id", "ask_digest", "run_id", "call_id",
    "chat_id", "agent", "owner", "verdict", "text", "one_use", "expires_ns",
    "key_epoch", "membership_epoch", "ownership_epoch", "policy_revision",
}


def _validate_authority(mesh, record: dict) -> None:
    current = authority(mesh, record["agent"], record["chat_id"])
    if current["owner"] != record["owner"]:
        raise PermissionRecordError("responsible member changed")
    for name in ("key_epoch", "membership_epoch", "ownership_epoch", "policy_revision"):
        if int(current[name]) != record[name]:
            raise PermissionRecordError(f"stale {name}")
    if time.time_ns() >= record["expires_ns"]:
        raise PermissionRecordError("record expired")


def _match_header(header: dict, record: dict) -> None:
    for name in ("id", "ns", "chat_id", "agent", "expires_ns", "key_epoch",
                 "membership_epoch", "ownership_epoch", "policy_revision"):
        if header[name] != record[name]:
            raise PermissionRecordError(f"envelope header {name} mismatch")


@dataclass(frozen=True, slots=True)
class PermissionAsk:
    record: dict
    envelope: dict

    @property
    def id(self) -> str:
        return self.record["id"]


class PermissionLane:
    def __init__(self, mesh, agent: str) -> None:
        self.mesh = mesh
        self.agent = agent

    def publish_ask(self, *, chat_id: str, kind: str, tool: str, detail: str,
                    input_digest: str, timeout_s: float, run_id: str,
                    call_id: str, label: str = "", options: list | None = None,
                    scope: str = "") -> PermissionAsk:
        auth = authority(self.mesh, self.agent, chat_id)
        ns = time.time_ns()
        ask_id = new_id("ask")
        record = {
            "v": 1, "id": ask_id, "call_id": call_id or new_id("call"),
            "ns": ns, "run_id": run_id or new_id("run"), "chat_id": chat_id,
            "agent": self.agent, "owner": auth["owner"], "kind": kind,
            "tool": tool, "input_digest": input_digest, "detail": detail,
            "label": label, "options": list(options or []), "scope": scope,
            "expires_ns": ns + max(1, int(timeout_s * 1e9)),
            "key_epoch": auth["key_epoch"],
            "membership_epoch": auth["membership_epoch"],
            "ownership_epoch": auth["ownership_epoch"],
            "policy_revision": auth["policy_revision"],
        }
        _validate_ask(record)
        envelope = seal(
            self.mesh, kind="permission_ask", record_id=ask_id, ns=ns,
            sender=self.agent, recipient=str(auth["owner"]), agent=self.agent,
            chat_id=chat_id, expires_ns=record["expires_ns"],
            key_epoch=record["key_epoch"],
            membership_epoch=record["membership_epoch"],
            ownership_epoch=record["ownership_epoch"],
            policy_revision=record["policy_revision"], payload=record,
        )
        self.mesh.tx.create_doc(ask_path(chat_id, self.agent, ask_id), envelope)
        return PermissionAsk(record, envelope)

    def read_decision(
        self,
        ask: PermissionAsk,
        *,
        claim: Callable[[dict, dict, dict, dict], bool] | None = None,
    ) -> dict | None:
        valid: list[tuple[dict, dict]] = []
        owner = ask.record["owner"]
        prefix = decision_prefix(ask.record["chat_id"], self.agent, ask.id)
        for path in self.mesh.tx.list_docs(prefix):
            try:
                raw = self.mesh.tx.get_doc(path)
                opened = open_envelope(
                    self.mesh, raw,
                    expected_kind="permission_decision", expected_sender=owner,
                    expected_recipient=self.agent, expected_agent=self.agent,
                    expected_chat=ask.record["chat_id"],
                )
                decision = _validate_decision(opened.payload)
                _match_header(opened.header, decision)
                _validate_authority(self.mesh, decision)
                if decision["ask_id"] != ask.id or decision["ask_digest"] != digest(ask.record):
                    raise PermissionRecordError("decision does not bind this ask")
                for name in ("run_id", "call_id", "chat_id", "agent", "owner"):
                    if decision[name] != ask.record[name]:
                        raise PermissionRecordError(f"decision {name} mismatch")
                valid.append((decision, raw))
            except (EnvelopeError, PermissionRecordError, AuthorityError, OSError):
                continue
        if not valid:
            return None
        semantics = {(d["verdict"], d["text"]) for d, _raw in valid}
        if len(semantics) != 1:
            return {"verdict": "deny", "text": "conflicting owner decisions; denied"}
        decision, decision_envelope = min(
            valid, key=lambda item: (item[0]["ns"], item[0]["id"]))
        claimed = (claim(
                       ask.record, ask.envelope, decision, decision_envelope)
                   if claim is not None
                   and decision["verdict"] in ("allow", "always") else
                   self.mesh.store.claim_once(
                       "permission-decision", ask.id, time.time_ns()))
        if not claimed:
            return {"verdict": "deny", "text": "permission decision was already consumed"}
        return decision

    def withdraw(self, ask: PermissionAsk, *, keep_decision_id: str = "",
                 keep_ask: bool = False) -> None:
        if not keep_ask:
            try:
                self.mesh.tx.delete_doc(
                    ask_path(ask.record["chat_id"], self.agent, ask.id))
            except Exception:
                pass
        prefix = decision_prefix(ask.record["chat_id"], self.agent, ask.id)
        try:
            paths = self.mesh.tx.list_docs(prefix)
        except Exception:
            paths = []
        for path in paths:
            if keep_decision_id and path.endswith(f"/{keep_decision_id}.json"):
                continue
            try:
                self.mesh.tx.delete_doc(path)
            except Exception:
                pass


def open_effect_grant(mesh, *, chat_id: str, agent: str, grant_id: str,
                      ask_raw: dict, decision_raw: dict) -> tuple[dict, dict]:
    """Open retained owner-signed ask+decision evidence for one effect."""
    snap = mesh.snapshot(chat_id)
    if not snap.is_member(mesh.user) or mesh.user not in {
            agent, mesh.directory.owner_of(agent)}:
        raise PermissionRecordError("viewer is outside the effect grant audience")
    header = decision_raw.get("header") if isinstance(decision_raw, dict) else None
    owner = header.get("sender") if isinstance(header, dict) else ""
    header_fields = {
        "v", "kind", "id", "ns", "sender", "recipient", "agent",
        "chat_id", "expires_ns", "membership_epoch", "ownership_epoch",
        "policy_revision", "key_epoch",
    }
    opened = open_pairwise(
        mesh, decision_raw, header_fields=header_fields,
        expected={
            "v": 1, "kind": "permission_decision", "id": grant_id,
            "sender": owner, "recipient": agent, "agent": agent,
            "chat_id": chat_id,
        }, sender=owner, recipient=agent, viewer=mesh.user,
    )
    decision = _validate_decision(opened.payload)
    _match_header(opened.header, decision)
    ask_id = decision["ask_id"]
    ask_opened = open_pairwise(
        mesh, ask_raw, header_fields=header_fields,
        expected={
            "v": 1, "kind": "permission_ask", "id": ask_id,
            "sender": agent, "recipient": owner, "agent": agent,
            "chat_id": chat_id,
        }, sender=agent, recipient=owner, viewer=mesh.user,
    )
    ask = _validate_ask(ask_opened.payload)
    _match_header(ask_opened.header, ask)
    if decision["ask_digest"] != digest(ask):
        raise PermissionRecordError("effect grant does not bind its ask")
    for name in ("run_id", "call_id", "chat_id", "agent", "owner"):
        if decision[name] != ask[name]:
            raise PermissionRecordError(f"effect grant {name} mismatch")
    return ask, decision


def open_ask(mesh, *, chat_id: str, agent: str, ask_id: str) -> PermissionAsk:
    owner = mesh.directory.owner_of(agent)
    if owner != mesh.user:
        raise PermissionRecordError("only the responsible member can open this ask")
    raw = mesh.tx.get_doc(ask_path(chat_id, agent, ask_id))
    opened = open_envelope(
        mesh, raw, expected_kind="permission_ask", expected_sender=agent,
        expected_recipient=owner, expected_agent=agent, expected_chat=chat_id,
    )
    record = _validate_ask(opened.payload)
    _match_header(opened.header, record)
    if opened.header["id"] != record["id"] or record["id"] != ask_id:
        raise PermissionRecordError("ask id mismatch")
    _validate_authority(mesh, record)
    return PermissionAsk(record, raw)


def list_owner_asks(mesh, *, chat_id: str = "") -> list[dict]:
    out: list[dict] = []
    chats = [mesh.snapshot(chat_id)] if chat_id else mesh.membership.chats_for()
    allowed = {
        (snap.id, agent)
        for snap in chats
        for agent in snap.members
        if mesh.directory.owner_of(agent) == mesh.user
    }
    prefix = f"chats/{chat_id}/runtime/owner-control" if chat_id else "chats"
    for path in mesh.tx.list_docs(prefix):
        parts = path.split("/")
        if (len(parts) != 7 or parts[0] != "chats" or parts[2:4] !=
                ["runtime", "owner-control"] or parts[5] != "asks" or
                not parts[6].endswith(".json")):
            continue
        room, agent = parts[1], parts[4]
        if (room, agent) not in allowed:
            continue
        ask_id = parts[6].removesuffix(".json")
        try:
            out.append(open_ask(mesh, chat_id=room, agent=agent,
                                ask_id=ask_id).record)
        except (EnvelopeError, PermissionRecordError, AuthorityError, OSError):
            continue
    return out


def answer(mesh, *, chat_id: str, agent: str, ask_id: str,
           verdict: str, text: str = "") -> dict:
    ask = open_ask(mesh, chat_id=chat_id, agent=agent, ask_id=ask_id)
    a = ask.record
    allowed = {"answer"} if a["kind"] == "question" else {"allow", "always", "deny"}
    if verdict not in allowed:
        raise PermissionRecordError("verdict is not valid for this ask")
    if verdict == "always" and a["scope"]:
        raise PermissionRecordError("this permission can only be allowed once")
    if verdict == "always":
        acc = mesh.directory.get(agent)
        current = list((acc.agent.harness or {}).get("approvals") or []) \
            if acc and acc.agent else []
        rule = {"tool": a["tool"], "chat": a["chat_id"]}
        if rule not in current:
            current.append(rule)
            mesh.set_agent_harness(agent, {"approvals": current})
    current_auth = authority(mesh, agent, chat_id)
    ns = time.time_ns()
    decision_id = new_id("decision") + "-" + secrets.token_hex(4)
    record = {
        "v": 1, "id": decision_id, "ns": ns, "ask_id": ask_id,
        "ask_digest": digest(a), "run_id": a["run_id"], "call_id": a["call_id"],
        "chat_id": chat_id, "agent": agent, "owner": mesh.user,
        "verdict": verdict, "text": str(text or "")[:2000], "one_use": True,
        "expires_ns": a["expires_ns"], "key_epoch": current_auth["key_epoch"],
        "membership_epoch": current_auth["membership_epoch"],
        "ownership_epoch": current_auth["ownership_epoch"],
        "policy_revision": current_auth["policy_revision"],
    }
    _validate_decision(record)
    envelope = seal(
        mesh, kind="permission_decision", record_id=decision_id, ns=ns,
        sender=mesh.user, recipient=agent, agent=agent, chat_id=chat_id,
        expires_ns=record["expires_ns"], key_epoch=record["key_epoch"],
        membership_epoch=record["membership_epoch"],
        ownership_epoch=record["ownership_epoch"],
        policy_revision=record["policy_revision"], payload=record,
    )
    path = f"{decision_prefix(chat_id, agent, ask_id)}/{decision_id}.json"
    mesh.tx.create_doc(path, envelope)
    return a


def _validate_ask(value: object) -> dict:
    r = _record(value, ASK_FIELDS, "permission ask")
    if r["v"] != 1 or r["kind"] not in ("permission", "question"):
        raise PermissionRecordError("unsupported permission ask")
    for name in ("id", "call_id", "run_id", "chat_id", "agent", "owner", "tool",
                 "input_digest"):
        if not isinstance(r[name], str) or not r[name]:
            raise PermissionRecordError(f"invalid ask {name}")
    if len(r["input_digest"]) != 64:
        raise PermissionRecordError("input digest must be full SHA-256")
    if not isinstance(r["options"], list):
        raise PermissionRecordError("options must be an array")
    for name in ("ns", "expires_ns", "key_epoch", "membership_epoch",
                 "ownership_epoch", "policy_revision"):
        if isinstance(r[name], bool) or not isinstance(r[name], int):
            raise PermissionRecordError(f"invalid ask {name}")
    return r


def _validate_decision(value: object) -> dict:
    r = _record(value, DECISION_FIELDS, "permission decision")
    if r["v"] != 1 or r["verdict"] not in ("allow", "always", "deny", "answer"):
        raise PermissionRecordError("unsupported permission decision")
    if r["one_use"] is not True:
        raise PermissionRecordError("decision must be one-use")
    for name in ("id", "ask_id", "ask_digest", "run_id", "call_id", "chat_id",
                 "agent", "owner"):
        if not isinstance(r[name], str) or not r[name]:
            raise PermissionRecordError(f"invalid decision {name}")
    for name in ("ns", "expires_ns", "key_epoch", "membership_epoch",
                 "ownership_epoch", "policy_revision"):
        if isinstance(r[name], bool) or not isinstance(r[name], int):
            raise PermissionRecordError(f"invalid decision {name}")
    return r
