"""Inactive, strictly bound terminal receipt decoding. No RPCs or write activation.

Provider digests are opaque: initial replies supply them; subsequent reads must
match the retained value. A receipt proves an operation outcome, not authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .mutation_protocol import _DIGEST, _root, _uuid
from .scoped_sources import _text, validate_source_cursor, validate_source_paths


_RESULT_FIELDS = {
    "doc_put": ("path", "updated"),
    "doc_compare_put": ("path", "applied"),
    "doc_create": ("path", "created"),
    "doc_delete": ("path", "deleted"),
    "log_append": ("chat_id", "log_name", "row_id"),
    "effect_transition": ("path", "transitioned"),
    "chat_delete": ("chat_id", "logs_deleted", "documents_tombstoned"),
    "member_claim": ("username", "claimed"),
    "member_leave": ("username", "left"),
    "blob_manifest": ("chat_id", "blob_id", "object_id", "sealed_bytes"),
}
_TARGET_FIELDS = {
    kind: tuple(key for key in fields if key in {
        "path", "chat_id", "log_name", "blob_id", "object_id", "sealed_bytes",
        "username",
    }) for kind, fields in _RESULT_FIELDS.items()
}
# Leave takes no username argument; the server returns the locked member row.
_TARGET_FIELDS["member_leave"] = ()
_TRUE_FIELDS = frozenset(("updated", "created", "transitioned", "claimed", "left"))
_FIELDS = frozenset((
    "schema_version", "root", "protocol_epoch", "operation_id", "actor_id",
    "kind", "client_digest", "provider_digest", "result",
))


def _digest(value: object) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("invalid mutation receipt digest")


def _result_value(key: str, value: object, kind: str) -> None:
    if key in _TRUE_FIELDS:
        if value is not True:
            raise ValueError("invalid mutation success flag")
    elif key in {"deleted", "applied"}:
        if type(value) is not bool:
            raise ValueError("invalid mutation deletion flag")
    elif key in {"row_id", "logs_deleted", "documents_tombstoned", "sealed_bytes"}:
        validate_source_cursor(value, "mutation result integer")
        if key != "logs_deleted" and value < 1:
            raise ValueError("invalid mutation result count")
        if key == "sealed_bytes" and value > 52428800:
            raise ValueError("invalid mutation blob size")
    elif key == "object_id":
        _uuid(value, "mutation object id")
    elif key == "blob_id":
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}", value) is None:
            raise ValueError("invalid mutation blob id")
    elif key == "username":
        if kind == "member_leave":
            # Leave echoes an existing row, whose schema predates the claim
            # input limit. Empty/long legacy names remain valid outcomes.
            if type(value) is not str or "\x00" in value:
                raise ValueError("invalid mutation username")
            try:
                if len(value.encode("utf-8")) > 16384:
                    raise ValueError("invalid mutation username")
            except UnicodeError:
                raise ValueError("invalid mutation username") from None
        else:
            _text(value, "mutation username", maximum=1024)
    else:
        validate_source_paths((value,))
        if key == "chat_id":
            _text(value, "mutation stream", maximum=1024)
            if "/" in value:
                raise ValueError("invalid mutation stream")


@dataclass(frozen=True)
class MutationReceiptExpectation:
    """Identity retained by the future journal before sending an operation.

Targets are immutable scalar pairs, with exactly the fields for this family.
An absent provider digest is permitted only for the initial operation response;
the caller must persist the returned digest before subsequent resolution/ack.
"""

    root: str
    protocol_epoch: str
    operation_id: str
    actor_id: str
    kind: str
    client_digest: str
    targets: tuple[tuple[str, str | int], ...]
    provider_digest: str | None = None

    def __post_init__(self) -> None:
        _root(self.root)
        for value in (self.protocol_epoch, self.operation_id, self.actor_id):
            _uuid(value, "mutation receipt identity")
        _digest(self.client_digest)
        if self.provider_digest is not None:
            _digest(self.provider_digest)
        if type(self.kind) is not str or self.kind not in _RESULT_FIELDS:
            raise ValueError("unsupported mutation kind")
        if (type(self.targets) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2
                       or type(pair[0]) is not str for pair in self.targets)):
            raise ValueError("invalid mutation target binding")
        keys = tuple(pair[0] for pair in self.targets)
        if len(keys) != len(set(keys)) or set(keys) != set(_TARGET_FIELDS[self.kind]):
            raise ValueError("incomplete mutation target binding")
        for key, value in self.targets:
            _result_value(key, value, self.kind)


@dataclass(frozen=True)
class ProviderMutationReceipt:
    """Validated immutable outcome; never a continuing authority verdict."""

    identity: MutationReceiptExpectation
    result: tuple[tuple[str, str | int | bool], ...]

    def __post_init__(self) -> None:
        if (type(self.identity) is not MutationReceiptExpectation
                or self.identity.provider_digest is None):
            raise ValueError("invalid terminal receipt identity")
        if (type(self.result) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2
                       or type(pair[0]) is not str for pair in self.result)):
            raise ValueError("invalid mutation result")
        keys = tuple(pair[0] for pair in self.result)
        if len(keys) != len(set(keys)) or set(keys) != set(_RESULT_FIELDS[self.identity.kind]):
            raise ValueError("invalid mutation result fields")
        for key, value in self.result:
            _result_value(key, value, self.identity.kind)
        result = dict(self.result)
        for key, value in self.identity.targets:
            if type(result[key]) is not type(value) or result[key] != value:
                raise ValueError("mutation receipt target mismatch")
        # Validate scalars first, so nested/cyclic or huge unknown objects never
        # reach serialization. Bound the same result envelope as the provider.
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 16384:
            raise ValueError("mutation receipt result exceeds limit")


def decode_mutation_receipt(
    raw: object, expected: MutationReceiptExpectation,
) -> ProviderMutationReceipt:
    """Reject extra fields, weak types, wrong identities and wrong targets."""
    if type(expected) is not MutationReceiptExpectation:
        raise ValueError("invalid mutation receipt expectation")
    if type(raw) is not dict or set(raw) != _FIELDS:
        raise ValueError("invalid mutation receipt fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise ValueError("unsupported mutation receipt version")
    for key in ("root", "protocol_epoch", "operation_id", "actor_id", "kind", "client_digest"):
        if type(raw[key]) is not str or raw[key] != getattr(expected, key):
            raise ValueError("mutation receipt identity mismatch")
    _digest(raw["provider_digest"])
    if expected.provider_digest is not None and raw["provider_digest"] != expected.provider_digest:
        raise ValueError("mutation receipt provider digest mismatch")
    result = raw["result"]
    if type(result) is not dict or set(result) != set(_RESULT_FIELDS[expected.kind]):
        raise ValueError("invalid mutation result fields")
    identity = MutationReceiptExpectation(
        expected.root, expected.protocol_epoch, expected.operation_id,
        expected.actor_id, expected.kind, expected.client_digest,
        expected.targets, raw["provider_digest"],
    )
    return ProviderMutationReceipt(identity, tuple(result.items()))
