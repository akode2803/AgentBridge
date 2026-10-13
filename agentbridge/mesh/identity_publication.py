"""Inactive, immutable private preparation for a new agent or human identity.

This artifact must commit with its admission owner before installing keys or
publishing either document. It is neither authority nor publication evidence.
Decoding never regenerates keys, timestamps, IDs, wraps, or signatures. There is
no transport, compensation, key deletion, or AccountsService activation here.

A human artifact also holds the account's one-time recovery code in plaintext,
so a sign-up interrupted after publication can still show it. It lives only in
this machine's private owner payload, beside the unlocked key it can recover,
until publication settles; a created account's code then waits in the
keystore's holder until the user confirms saving it.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field

from .. import crypto
from ..core.jsonkit import canonical_json_bytes
from ..core.timekit import new_id, next_ns, utcnow_iso
from ..transport.mutation_protocol import _uuid
from ..transport.typed_mutations import _root
from .lifecycle import _validate_envelope_structure
from .paths import P

OWNER_KIND = "agent_identity_create"
HUMAN_OWNER_KIND = "human_identity_create"
MAX_PRIVATE_BYTES = 256 * 1024
MAX_HARNESS_BYTES = 64 * 1024
_FIELDS = {"schema_version", "owner_id", "root", "actor_id", "owner",
           "owner_sign_pub", "bundle", "account", "bootstrap"}
_ACCOUNT_FIELDS = {"name", "kind", "display", "about", "created", "active",
                   "keys", "agent"}
_HUMAN_FIELDS = {"schema_version", "owner_id", "root", "actor_id", "bundle",
                 "account", "bootstrap", "recovery_code"}
_HUMAN_ACCOUNT_FIELDS = {"name", "kind", "display", "created", "active",
                         "auth", "keys"}
_RECOVERY_CODE = re.compile(r"[0-9a-f]{5}(?:-[0-9a-f]{5}){3}\Z")


def _text(value, maximum=4096):
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("invalid identity text")
    return value


def _name(value):
    from .accounts import valid_name

    if type(value) is not str or not valid_name(value):
        raise ValueError("invalid identity name")
    return value


def _bundle(value):
    if type(value) is not bytes or len(value) != 64:
        raise ValueError("invalid identity bundle")
    return value


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PRIVATE_BYTES:
        raise ValueError("invalid private identity artifact size")
    try:
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != _FIELDS
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or canonical_json_bytes(value) != raw):
            raise ValueError()
        _uuid(value["owner_id"], "owner_id")
        _uuid(value["actor_id"], "actor_id")
        _root(value["root"])
        owner = _name(value["owner"])
        owner_pub = _text(value["owner_sign_pub"], 128)
        bundle = _bundle(base64.b64decode(value["bundle"], validate=True))
        if base64.b64encode(bundle).decode("ascii") != value["bundle"]:
            raise ValueError()
        account = value["account"]
        if type(account) is not dict or set(account) != _ACCOUNT_FIELDS:
            raise ValueError()
        name = _name(account["name"])
        if name == owner or account["kind"] != "agent" or account["active"] is not True:
            raise ValueError()
        for key in ("display", "about", "created"):
            _text(account[key])
        sign_pub, agree_pub = crypto.identity_pubs(bundle)
        if account["keys"] != {"sign_pub": sign_pub, "agree_pub": agree_pub}:
            raise ValueError()
        agent = account["agent"]
        if type(agent) is not dict or set(agent) != {"owner", "machine", "harness"}:
            raise ValueError()
        if agent["owner"] != owner:
            raise ValueError()
        _text(agent["machine"], 1024)
        if (type(agent["harness"]) is not dict
                or len(canonical_json_bytes(agent["harness"])) > MAX_HARNESS_BYTES):
            raise ValueError()
        envelope = value["bootstrap"]
        record = envelope["record"]
        path = f"lifecycle/{name}/{record['id']}.json"
        _validate_envelope_structure(path, envelope)
        if (record["v"] != 1 or type(record["v"]) is not int
                or record["subject"] != name or record["kind"] != "agent"
                or record["actor"] != owner or record["action"] != "bootstrap"
                or record["owner"] != owner or record["machine"] != agent["machine"]
                or record["active"] is not True or record["deactivated"] != ""
                or record["previous_id"] != "" or record["ns"] <= 0
                or not re.fullmatch(rf"life-{record['ns']}-[0-9a-f]{{8}}", record["id"])):
            raise ValueError()
        signed = canonical_json_bytes(record)
        if not crypto.verify(owner_pub, envelope["sig"], signed):
            raise ValueError()
        if not crypto.verify(sign_pub, envelope["subject_sig"], signed):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid private identity artifact") from exc


@dataclass(frozen=True)
class PreparedAgentIdentity:
    """Private bytes suitable for the root-owned admission payload, never logs."""

    private_payload: bytes = field(repr=False)

    def __post_init__(self):
        _decode(self.private_payload)

    @classmethod
    def from_bytes(cls, private_payload: bytes) -> PreparedAgentIdentity:
        return cls(private_payload)

    @property
    def owner_id(self) -> str:
        return _decode(self.private_payload)["owner_id"]

    @property
    def root(self) -> str:
        return _decode(self.private_payload)["root"]

    @property
    def actor_id(self) -> str:
        return _decode(self.private_payload)["actor_id"]

    @property
    def bundle(self) -> bytes:
        return base64.b64decode(_decode(self.private_payload)["bundle"], validate=True)

    @property
    def account(self) -> dict:
        return _decode(self.private_payload)["account"]

    @property
    def bootstrap(self) -> dict:
        return _decode(self.private_payload)["bootstrap"]

    def documents(self) -> tuple[tuple[str, dict], tuple[str, dict]]:
        value = _decode(self.private_payload)
        name = value["account"]["name"]
        record_id = value["bootstrap"]["record"]["id"]
        return ((P.user(name), value["account"]),
                (f"lifecycle/{name}/{record_id}.json", value["bootstrap"]))


def prepare_agent_identity(*, owner_id: str, root: str, actor_id: str,
                           name: str, owner: str, machine: str,
                           owner_bundle: bytes, owner_sign_pub: str,
                           display: str = "", owner_display: str = "",
                           harness: dict | None = None) -> PreparedAgentIdentity:
    """Build once, then freeze durably before any local or provider side effect.

    The caller must establish fresh owner authority separately, and recover an
    existing durable owner before calling this for a retry. The owner's signing
    key is checked against the supplied public key and is never retained.
    """
    _uuid(owner_id, "owner_id")
    _uuid(actor_id, "actor_id")
    _root(root)
    _name(name)
    _name(owner)
    if name == owner:
        raise ValueError("agent and owner must differ")
    _text(machine, 1024)
    if crypto.identity_pubs(_bundle(owner_bundle))[0] != owner_sign_pub:
        raise ValueError("owner signing key mismatch")
    if type(display) is not str or type(owner_display) is not str:
        raise ValueError("invalid identity display")
    display = display or name.title()
    _text(display)
    _text(owner_display or owner)
    if harness is None:
        harness = {}
    if type(harness) is not dict:
        raise ValueError("invalid agent harness")
    frozen_harness = canonical_json_bytes(harness)
    if len(frozen_harness) > MAX_HARNESS_BYTES:
        raise ValueError("agent harness exceeds bound")
    bundle = crypto.generate_identity()
    sign_pub, agree_pub = crypto.identity_pubs(bundle)
    account = {"name": name, "kind": "agent", "display": display,
               "about": f"{owner_display or owner}'s {display} on {machine}",
               "created": utcnow_iso(), "active": True,
               "keys": {"sign_pub": sign_pub, "agree_pub": agree_pub},
               "agent": {"owner": owner, "machine": machine,
                         "harness": json.loads(frozen_harness)}}
    ns = next_ns()
    record = {"v": 1, "id": new_id("life", ns), "ns": ns, "subject": name,
              "kind": "agent", "actor": owner, "action": "bootstrap",
              "owner": owner, "machine": machine, "active": True,
              "deactivated": "", "previous_id": ""}
    signed = canonical_json_bytes(record)
    envelope = {"record": record, "sig": crypto.sign(owner_bundle, signed),
                "subject_sig": crypto.sign(bundle, signed)}
    return PreparedAgentIdentity(canonical_json_bytes({
        "schema_version": 1, "owner_id": owner_id, "root": root,
        "actor_id": actor_id, "owner": owner, "owner_sign_pub": owner_sign_pub,
        "bundle": base64.b64encode(bundle).decode("ascii"),
        "account": account, "bootstrap": envelope,
    }))


def _plan_requests(artifact, protocol_epoch: str, source_epoch: str):
    from uuid import UUID, uuid5

    from ..transport.typed_mutations import prepare_mutation

    return tuple(
        prepare_mutation(
            artifact.root, protocol_epoch,
            str(uuid5(UUID(artifact.owner_id), label)), artifact.actor_id,
            "doc_compare_put", {"p_path": path, "p_data": data,
                                "p_expected_source_epoch": source_epoch,
                                "p_expected_seq": 0, "p_expected_deleted": False},
        )
        for label, (path, data) in zip(("account", "bootstrap"), artifact.documents())
    )


def _freeze(owners, artifact, owner_kind: str, *, protocol_epoch: str,
            source_epoch: str):
    if (artifact.root, artifact.actor_id) != (owners.journal.root, owners.journal.actor_id):
        raise ValueError("identity admission owner mismatch")
    _uuid(source_epoch, "identity source epoch")
    return owners.prepare(artifact.owner_id,
                          _plan_requests(artifact, protocol_epoch, source_epoch),
                          owner_kind=owner_kind,
                          private_payload=artifact.private_payload)


def _recover(owners, owner_id: str, owner_kind: str, artifact_type):
    from uuid import UUID, uuid5

    record = owners.get(owner_id)
    if record is None:
        return None
    if record.owner_kind != owner_kind:
        raise ValueError("identity admission kind mismatch")
    artifact = artifact_type.from_bytes(record.private_payload)
    if (artifact.owner_id, artifact.root, artifact.actor_id) != (
        record.plan.owner_id, record.plan.root, record.plan.actor_id,
    ) or len(record.requests) != 2:
        raise ValueError("identity admission owner mismatch")
    for label, (path, data), request in zip(
        ("account", "bootstrap"), artifact.documents(), record.requests,
    ):
        expected = request.expectation
        args = json.loads(request.arguments_json)
        if (expected.kind != "doc_compare_put"
                or expected.operation_id != str(uuid5(UUID(owner_id), label))
                or args["p_path"] != path or args["p_expected_seq"] != 0
                or args["p_expected_deleted"] is not False
                or canonical_json_bytes(args["p_data"]) != canonical_json_bytes(data)):
            raise ValueError("identity admission request mismatch")
    return artifact


def freeze_agent_identity(owners, artifact: PreparedAgentIdentity, *,
                          protocol_epoch: str, source_epoch: str):
    """Atomically retain the private artifact and both exact absent-CAS legs.

    This does not admit, install, or dispatch. A future dispatcher must settle
    the account CAS as applied before dispatching bootstrap, and retain the
    private bundle through any ambiguous outcome or canonical-head conflict.
    """
    if type(artifact) is not PreparedAgentIdentity:
        raise ValueError("invalid prepared agent identity")
    return _freeze(owners, artifact, OWNER_KIND, protocol_epoch=protocol_epoch,
                   source_epoch=source_epoch)


def recover_agent_identity(owners, owner_id: str) -> PreparedAgentIdentity | None:
    """Recover before minting anything; check the private-to-public plan binding."""
    return _recover(owners, owner_id, OWNER_KIND, PreparedAgentIdentity)


def _decode_human(raw, *, verify_recovery: bool = False):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PRIVATE_BYTES:
        raise ValueError("invalid private identity artifact size")
    try:
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != _HUMAN_FIELDS
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or canonical_json_bytes(value) != raw):
            raise ValueError()
        _uuid(value["owner_id"], "owner_id")
        _uuid(value["actor_id"], "actor_id")
        _root(value["root"])
        code = value["recovery_code"]
        if type(code) is not str or _RECOVERY_CODE.fullmatch(code) is None:
            raise ValueError()
        bundle = _bundle(base64.b64decode(value["bundle"], validate=True))
        if base64.b64encode(bundle).decode("ascii") != value["bundle"]:
            raise ValueError()
        account = value["account"]
        if type(account) is not dict or set(account) != _HUMAN_ACCOUNT_FIELDS:
            raise ValueError()
        name = _name(account["name"])
        if account["kind"] != "human" or account["active"] is not True:
            raise ValueError()
        for key in ("display", "created"):
            _text(account[key])
        auth = account["auth"]
        if (type(auth) is not dict or set(auth) != {"algo", "salt", "hash"}
                or auth["algo"] != "scrypt"
                or len(base64.b64decode(_text(auth["salt"], 64), validate=True)) != 16
                or len(base64.b64decode(_text(auth["hash"], 64), validate=True)) != 32):
            raise ValueError()
        keys = account["keys"]
        sign_pub, agree_pub = crypto.identity_pubs(bundle)
        if (type(keys) is not dict
                or set(keys) != {"sign_pub", "agree_pub", "wrapped_priv", "recovery"}
                or (keys["sign_pub"], keys["agree_pub"]) != (sign_pub, agree_pub)):
            raise ValueError()
        for wrap in (keys["wrapped_priv"], keys["recovery"]):
            if (type(wrap) is not dict or set(wrap) != {"salt", "nonce", "ct"}
                    or not all(type(part) is str for part in wrap.values())):
                raise ValueError()
        if verify_recovery and crypto.unwrap_bundle(keys["recovery"], code) != bundle:
            raise ValueError()
        envelope = value["bootstrap"]
        record = envelope["record"]
        path = f"lifecycle/{name}/{record['id']}.json"
        _validate_envelope_structure(path, envelope)
        if (record["v"] != 1 or type(record["v"]) is not int
                or record["subject"] != name or record["kind"] != "human"
                or record["actor"] != name or record["action"] != "bootstrap"
                or record["owner"] != "" or record["machine"] != ""
                or record["active"] is not True or record["deactivated"] != ""
                or record["previous_id"] != "" or record["ns"] <= 0
                or envelope["subject_sig"] != ""
                or not re.fullmatch(rf"life-{record['ns']}-[0-9a-f]{{8}}", record["id"])):
            raise ValueError()
        if not crypto.verify(sign_pub, envelope["sig"], canonical_json_bytes(record)):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, UnicodeError, crypto.CryptoFail) as exc:
        raise ValueError("invalid private identity artifact") from exc


@dataclass(frozen=True)
class PreparedHumanIdentity:
    """Private bytes for the root-owned admission payload, never logs.

    Construction proves the recovery wrap opens with the retained code, so a
    published account can never carry a code that does not recover it.
    """

    private_payload: bytes = field(repr=False)

    def __post_init__(self):
        _decode_human(self.private_payload, verify_recovery=True)

    @classmethod
    def from_bytes(cls, private_payload: bytes) -> PreparedHumanIdentity:
        return cls(private_payload)

    def _value(self):
        return _decode_human(self.private_payload)

    @property
    def owner_id(self) -> str:
        return self._value()["owner_id"]

    @property
    def root(self) -> str:
        return self._value()["root"]

    @property
    def actor_id(self) -> str:
        return self._value()["actor_id"]

    @property
    def bundle(self) -> bytes:
        return base64.b64decode(self._value()["bundle"], validate=True)

    @property
    def account(self) -> dict:
        return self._value()["account"]

    @property
    def bootstrap(self) -> dict:
        return self._value()["bootstrap"]

    @property
    def recovery_code(self) -> str:
        return self._value()["recovery_code"]

    def documents(self) -> tuple[tuple[str, dict], tuple[str, dict]]:
        value = self._value()
        name = value["account"]["name"]
        record_id = value["bootstrap"]["record"]["id"]
        return ((P.user(name), value["account"]),
                (f"lifecycle/{name}/{record_id}.json", value["bootstrap"]))


def prepare_human_identity(*, owner_id: str, root: str, actor_id: str,
                           name: str, password: str,
                           display: str = "") -> PreparedHumanIdentity:
    """Mint keys, password and recovery wraps, and a self-signed bootstrap once.

    Freeze the result durably before any local or provider side effect; a
    retry recovers this artifact and never mints again. The password itself is
    never retained, only its scrypt record and wrap.
    """
    from .accounts import _scrypt

    _uuid(owner_id, "owner_id")
    _uuid(actor_id, "actor_id")
    _root(root)
    _name(name)
    if type(password) is not str or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    if type(display) is not str:
        raise ValueError("invalid identity display")
    display = display or name.title()
    _text(display)
    salt = os.urandom(16)
    bundle = crypto.generate_identity()
    sign_pub, agree_pub = crypto.identity_pubs(bundle)
    code = crypto.new_recovery_code()
    account = {
        "name": name, "kind": "human", "display": display,
        "created": utcnow_iso(), "active": True,
        "auth": {"algo": "scrypt", "salt": base64.b64encode(salt).decode(),
                 "hash": base64.b64encode(_scrypt(password, salt)).decode()},
        "keys": {"sign_pub": sign_pub, "agree_pub": agree_pub,
                 "wrapped_priv": crypto.wrap_bundle(bundle, password),
                 "recovery": crypto.wrap_bundle(bundle, code)},
    }
    ns = next_ns()
    record = {"v": 1, "id": new_id("life", ns), "ns": ns, "subject": name,
              "kind": "human", "actor": name, "action": "bootstrap",
              "owner": "", "machine": "", "active": True,
              "deactivated": "", "previous_id": ""}
    envelope = {"record": record,
                "sig": crypto.sign(bundle, canonical_json_bytes(record)),
                "subject_sig": ""}
    return PreparedHumanIdentity(canonical_json_bytes({
        "schema_version": 1, "owner_id": owner_id, "root": root,
        "actor_id": actor_id, "bundle": base64.b64encode(bundle).decode("ascii"),
        "account": account, "bootstrap": envelope, "recovery_code": code,
    }))


def freeze_human_identity(owners, artifact: PreparedHumanIdentity, *,
                          protocol_epoch: str, source_epoch: str):
    """Atomically retain the private artifact and both exact absent-CAS legs."""
    if type(artifact) is not PreparedHumanIdentity:
        raise ValueError("invalid prepared human identity")
    return _freeze(owners, artifact, HUMAN_OWNER_KIND,
                   protocol_epoch=protocol_epoch, source_epoch=source_epoch)


def recover_human_identity(owners, owner_id: str) -> PreparedHumanIdentity | None:
    """Recover before minting anything; check the private-to-public plan binding."""
    return _recover(owners, owner_id, HUMAN_OWNER_KIND, PreparedHumanIdentity)
