"""Signed chatless evidence for a peer session's owner decision."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from ...core.timekit import new_id
from .authority import AuthorityError, responsible_authority
from .envelope import EnvelopeError, open_pairwise, seal_pairwise
from .models import RuntimeContractError, canonical_json_bytes


class PeerControlError(RuntimeContractError):
    """A peer owner-control record is invalid or no longer authoritative."""


ASK_FIELDS = {
    "v", "id", "ns", "target", "owner", "requester", "command", "repair",
    "request_digest", "expires_ns", "ownership_epoch", "policy_revision",
}
DECISION_FIELDS = {
    "v", "id", "ns", "ask_id", "ask_digest", "target", "owner",
    "requester", "command", "repair", "verdict", "one_use", "expires_ns",
    "ownership_epoch", "policy_revision",
}
HEADER_FIELDS = {
    "v", "kind", "id", "ns", "sender", "recipient", "target",
    "expires_ns", "ownership_epoch", "policy_revision",
}


def ask_path(target: str, ask_id: str) -> str:
    return f"runtime/owner-control/{target}/peer/asks/{ask_id}.json"


def decision_prefix(target: str, ask_id: str) -> str:
    return f"runtime/owner-control/{target}/peer/decisions/{ask_id}"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def peer_request_digest(env: dict) -> str:
    signed = {k: env.get(k) for k in (
        "to", "from", "id", "kind", "command", "req_id", "ns", "payload")
    }
    return digest(signed)


def _strict(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise PeerControlError(f"invalid {name} fields")
    return value


def _authority(mesh, record: dict) -> None:
    current = responsible_authority(mesh, record["target"])
    if current["owner"] != record["owner"]:
        raise PeerControlError("responsible member changed")
    for name in ("ownership_epoch", "policy_revision"):
        if int(current[name]) != record[name]:
            raise PeerControlError(f"stale {name}")
    requester = mesh.directory.get(record["requester"])
    if not requester or not requester.active or not requester.keys.sign_pub:
        raise PeerControlError("peer requester is no longer active")
    if time.time_ns() >= record["expires_ns"]:
        raise PeerControlError("record expired")


def _header(kind: str, record: dict, sender: str, recipient: str) -> dict:
    return {
        "v": 1, "kind": kind, "id": record["id"], "ns": record["ns"],
        "sender": sender, "recipient": recipient, "target": record["target"],
        "expires_ns": record["expires_ns"],
        "ownership_epoch": record["ownership_epoch"],
        "policy_revision": record["policy_revision"],
    }


def _match_header(header: dict, record: dict) -> None:
    for name in ("id", "ns", "target", "expires_ns", "ownership_epoch",
                 "policy_revision"):
        if header[name] != record[name]:
            raise PeerControlError(f"envelope header {name} mismatch")


@dataclass(frozen=True, slots=True)
class PeerAsk:
    record: dict

    @property
    def id(self) -> str:
        return self.record["id"]


class PeerControlLane:
    def __init__(self, mesh, target: str) -> None:
        self.mesh = mesh
        self.target = target

    def publish(self, request: dict, *, repair: bool,
                timeout_s: float) -> PeerAsk:
        if (request.get("to") != self.target or request.get("kind") != "request"
                or not request.get("from")):
            raise PeerControlError("peer request is not bound to this target")
        auth = responsible_authority(self.mesh, self.target)
        ns = time.time_ns()
        record = {
            "v": 1, "id": str(request.get("id") or ""), "ns": ns,
            "target": self.target, "owner": auth["owner"],
            "requester": str(request.get("from") or ""),
            "command": str(request.get("command") or ""),
            "repair": bool(repair), "request_digest": peer_request_digest(request),
            "expires_ns": ns + max(1, int(timeout_s * 1e9)),
            "ownership_epoch": auth["ownership_epoch"],
            "policy_revision": auth["policy_revision"],
        }
        _validate_ask(record)
        envelope = seal_pairwise(
            self.mesh, header=_header("peer_ask", record, self.target,
                                      str(auth["owner"])),
            sender=self.target, recipient=str(auth["owner"]), payload=record,
        )
        self.mesh.tx.put_doc(ask_path(self.target, record["id"]), envelope)
        return PeerAsk(record)

    def read_decision(self, ask: PeerAsk) -> dict | None:
        a = ask.record
        valid: list[dict] = []
        for path in self.mesh.tx.list_docs(decision_prefix(self.target, ask.id)):
            try:
                opened = open_pairwise(
                    self.mesh, self.mesh.tx.get_doc(path),
                    header_fields=HEADER_FIELDS,
                    expected={"v": 1, "kind": "peer_decision",
                              "sender": a["owner"], "recipient": self.target,
                              "target": self.target},
                    sender=a["owner"], recipient=self.target, viewer=self.target,
                )
                decision = _validate_decision(opened.payload)
                _match_header(opened.header, decision)
                _authority(self.mesh, decision)
                if decision["ask_id"] != ask.id or decision["ask_digest"] != digest(a):
                    raise PeerControlError("decision does not bind this peer ask")
                for name in ("target", "owner", "requester", "command", "repair"):
                    if decision[name] != a[name]:
                        raise PeerControlError(f"decision {name} mismatch")
                valid.append(decision)
            except (EnvelopeError, PeerControlError, AuthorityError, OSError):
                continue
        if not valid:
            return None
        semantics = {d["verdict"] for d in valid}
        if len(semantics) != 1:
            if not self.mesh.store.claim_once(
                    "peer-decision", ask.id, time.time_ns()):
                return {"verdict": "deny", "replayed": True}
            return {"verdict": "deny", "conflict": True}
        decision = min(valid, key=lambda d: (d["ns"], d["id"]))
        if not self.mesh.store.claim_once("peer-decision", ask.id, time.time_ns()):
            return {"verdict": "deny", "replayed": True}
        return decision

    def withdraw(self, ask: PeerAsk) -> None:
        paths = [ask_path(self.target, ask.id)]
        try:
            paths.extend(self.mesh.tx.list_docs(decision_prefix(self.target, ask.id)))
        except Exception:
            pass
        for path in paths:
            try:
                self.mesh.tx.delete_doc(path)
            except Exception:
                pass


def open_owner_ask(mesh, *, target: str, ask_id: str) -> PeerAsk:
    owner = mesh.directory.owner_of(target)
    if owner != mesh.user:
        raise PeerControlError("only the responsible member can open this peer ask")
    opened = open_pairwise(
        mesh, mesh.tx.get_doc(ask_path(target, ask_id)),
        header_fields=HEADER_FIELDS,
        expected={"v": 1, "kind": "peer_ask", "sender": target,
                  "recipient": owner, "target": target},
        sender=target, recipient=owner, viewer=owner,
    )
    record = _validate_ask(opened.payload)
    _match_header(opened.header, record)
    if record["id"] != ask_id:
        raise PeerControlError("peer ask id mismatch")
    _authority(mesh, record)
    return PeerAsk(record)


def list_owner_asks(mesh) -> list[dict]:
    owned = {name for name in mesh.directory.names()
             if mesh.directory.owner_of(name) == mesh.user}
    out = []
    for path in mesh.tx.list_docs("runtime/owner-control"):
        parts = path.split("/")
        if (len(parts) != 6 or parts[:2] != ["runtime", "owner-control"]
                or parts[3:5] != ["peer", "asks"]
                or not parts[5].endswith(".json") or parts[2] not in owned):
            continue
        try:
            out.append(open_owner_ask(
                mesh, target=parts[2], ask_id=parts[5].removesuffix(".json")
            ).record)
        except (EnvelopeError, PeerControlError, AuthorityError, OSError):
            continue
    return out


def answer(mesh, *, target: str, ask_id: str, verdict: str) -> dict:
    ask = open_owner_ask(mesh, target=target, ask_id=ask_id)
    a = ask.record
    if verdict not in ("allow", "always", "deny"):
        raise PeerControlError("invalid peer verdict")
    if verdict == "always" and a["repair"]:
        raise PeerControlError("repair access can only be allowed once")
    acc = mesh.directory.get(target)
    original_auto = list((acc.agent.harness or {}).get("peer_auto") or []) \
        if acc and acc.agent else []
    expected_auto = list(original_auto)
    persisted = False
    try:
        if verdict == "always" and a["requester"] not in expected_auto:
            expected_auto.append(a["requester"])
            mesh.set_agent_harness(target, {"peer_auto": expected_auto})
            persisted = True
        auth = responsible_authority(mesh, target)
        ns = time.time_ns()
        decision_id = new_id("peer-decision") + "-" + secrets.token_hex(4)
        record = {
            "v": 1, "id": decision_id, "ns": ns, "ask_id": ask_id,
            "ask_digest": digest(a), "target": target, "owner": mesh.user,
            "requester": a["requester"], "command": a["command"],
            "repair": a["repair"], "verdict": verdict, "one_use": True,
            "expires_ns": a["expires_ns"],
            "ownership_epoch": auth["ownership_epoch"],
            "policy_revision": auth["policy_revision"],
        }
        _validate_decision(record)
        envelope = seal_pairwise(
            mesh, header=_header("peer_decision", record, mesh.user, target),
            sender=mesh.user, recipient=target, payload=record,
        )
        path = f"{decision_prefix(target, ask_id)}/{decision_id}.json"
        mesh.tx.create_doc(path, envelope)
    except Exception:
        if persisted:
            live = mesh.directory.get(target)
            live_auto = list((live.agent.harness or {}).get("peer_auto") or []) \
                if live and live.agent else []
            if live_auto == expected_auto:
                mesh.set_agent_harness(target, {"peer_auto": original_auto})
        raise
    return a


def _validate_ask(value: object) -> dict:
    r = _strict(value, ASK_FIELDS, "peer ask")
    if r["v"] != 1 or not isinstance(r["repair"], bool):
        raise PeerControlError("unsupported peer ask")
    for name in ("id", "target", "owner", "requester", "command", "request_digest"):
        if not isinstance(r[name], str) or not r[name]:
            raise PeerControlError(f"invalid peer ask {name}")
    if len(r["request_digest"]) != 64:
        raise PeerControlError("request digest must be full SHA-256")
    for name in ("ns", "expires_ns", "ownership_epoch", "policy_revision"):
        if isinstance(r[name], bool) or not isinstance(r[name], int):
            raise PeerControlError(f"invalid peer ask {name}")
    return r


def _validate_decision(value: object) -> dict:
    r = _strict(value, DECISION_FIELDS, "peer decision")
    if r["v"] != 1 or r["verdict"] not in ("allow", "always", "deny"):
        raise PeerControlError("unsupported peer decision")
    if r["one_use"] is not True or not isinstance(r["repair"], bool):
        raise PeerControlError("peer decision must be one-use")
    for name in ("id", "ask_id", "ask_digest", "target", "owner", "requester",
                 "command"):
        if not isinstance(r[name], str) or not r[name]:
            raise PeerControlError(f"invalid peer decision {name}")
    for name in ("ns", "expires_ns", "ownership_epoch", "policy_revision"):
        if isinstance(r[name], bool) or not isinstance(r[name], int):
            raise PeerControlError(f"invalid peer decision {name}")
    return r
