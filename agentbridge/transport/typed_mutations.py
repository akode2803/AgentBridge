"""Inactive, receipt-bound typed Supabase mutation wire.

The durable operation journal owns admission, persistence, retries, and replay.
This module only prepares exact requests and makes one RPC per method call.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .mutation_protocol import _DIGEST, _root, _uuid
from .mutation_receipts import (
    MutationReceiptExpectation, ProviderMutationReceipt, decode_mutation_receipt,
)
from .scoped_sources import validate_source_cursor, validate_source_paths


# Each tuple is (public RPC name, ordered SQL argument names, result target names).
_FAMILIES = {
    "doc_put": ("ab_mutation_doc_put", ("p_path", "p_data"), ("path",)),
    "doc_create": ("ab_mutation_doc_create", ("p_path", "p_data"), ("path",)),
    "doc_delete": ("ab_mutation_doc_delete", ("p_path",), ("path",)),
    "log_append": ("ab_mutation_log_append", ("p_chat", "p_log", "p_record"),
                   ("chat_id", "log_name")),
    "effect_transition": ("ab_mutation_effect_transition",
                          ("p_path", "p_data", "p_grant_ask", "p_grant_decision"),
                          ("path",)),
    "chat_delete": ("ab_mutation_chat_delete", ("p_chat",), ("chat_id",)),
    "member_claim": ("ab_mutation_member_claim", ("p_username",), ("username",)),
    "member_leave": ("ab_mutation_member_leave", (), ()),
    "blob_manifest": ("ab_mutation_blob_manifest",
                      ("p_chat", "p_blob", "p_object_id", "p_sealed_bytes",
                       "p_sealed_sha256", "p_manifest_digest"),
                      ("chat_id", "blob_id", "object_id", "sealed_bytes")),
}
_TARGET_ARGUMENTS = {
    "path": "p_path", "chat_id": "p_chat", "log_name": "p_log",
    "username": "p_username", "blob_id": "p_blob", "object_id": "p_object_id",
    "sealed_bytes": "p_sealed_bytes",
}
_BLOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\Z")
_EFFECT_PATH = re.compile(
    r"chats/[^/]+/runtime/effects/[^/]+/[^/]+/(?:claim|state-[23])\.json\Z"
)
_MAX_JSON_BYTES = 8_355_840


class TypedMutationRPCError(RuntimeError):
    """A single typed RPC failed or its identity/response was unsafe."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid typed mutation JSON") from None


def _bounded_json(value: object, *, object_only: bool = False) -> None:
    if value is None or object_only and type(value) is not dict:
        raise ValueError("invalid typed mutation JSON value")
    if len(_canonical(value)) > _MAX_JSON_BYTES:
        raise ValueError("typed mutation JSON exceeds limit")


def _chat(value: object) -> None:
    validate_source_paths((value,))
    if "/" in value or len(value.encode("utf-8")) > 1024:
        raise ValueError("invalid typed mutation chat")


def _validate_arguments(kind: str, arguments: dict[str, Any]) -> None:
    if type(kind) is not str or kind not in _FAMILIES:
        raise ValueError("unsupported typed mutation kind")
    if type(arguments) is not dict or set(arguments) != set(_FAMILIES[kind][1]):
        raise ValueError("invalid typed mutation arguments")
    for name, value in arguments.items():
        if name in {"p_path", "p_log"}:
            validate_source_paths((value,))
        elif name == "p_chat":
            _chat(value)
        elif name in {"p_data", "p_record"}:
            _bounded_json(value, object_only=name == "p_record" or kind == "effect_transition")
        elif name in {"p_grant_ask", "p_grant_decision"}:
            if value is not None:
                _bounded_json(value, object_only=True)
        elif name == "p_username":
            if type(value) is not str or not value or "\x00" in value:
                raise ValueError("invalid typed mutation username")
            try:
                if len(value.encode("utf-8")) > 1024:
                    raise ValueError("invalid typed mutation username")
            except UnicodeError:
                raise ValueError("invalid typed mutation username") from None
        elif name == "p_blob":
            if type(value) is not str or _BLOB_ID.fullmatch(value) is None:
                raise ValueError("invalid typed mutation blob id")
        elif name == "p_object_id":
            _uuid(value, "typed mutation object id")
        elif name == "p_sealed_bytes":
            validate_source_cursor(value, "typed mutation sealed bytes")
            if not 1 <= value <= 52_428_800:
                raise ValueError("invalid typed mutation sealed bytes")
        elif name in {"p_sealed_sha256", "p_manifest_digest"}:
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError("invalid typed mutation digest")
    if kind == "effect_transition":
        path = arguments["p_path"]
        if _EFFECT_PATH.fullmatch(path) is None:
            raise ValueError("invalid typed effect path")
        claim = path.endswith("/claim.json")
        has_ask = arguments["p_grant_ask"] is not None
        has_decision = arguments["p_grant_decision"] is not None
        if (claim and not (has_ask and has_decision)
                or not claim and (has_ask or has_decision)):
            raise ValueError("invalid typed effect grants")
    # Reject non-JSON values, non-finite floats, surrogates, and oversized wire.
    if len(_canonical(arguments)) > _MAX_JSON_BYTES + 4096:
        raise ValueError("typed mutation arguments exceed limit")


def _targets(kind: str, arguments: dict[str, Any]) -> tuple[tuple[str, str | int], ...]:
    return tuple((target, arguments[_TARGET_ARGUMENTS[target]])
                 for target in _FAMILIES[kind][2])


@dataclass(frozen=True)
class PreparedMutation:
    """Immutable, replayable request identity and canonical SQL arguments."""

    expectation: MutationReceiptExpectation
    arguments_json: bytes

    def __post_init__(self) -> None:
        if type(self.expectation) is not MutationReceiptExpectation or self.expectation.provider_digest is not None:
            raise ValueError("invalid prepared mutation identity")
        if (type(self.arguments_json) is not bytes
                or len(self.arguments_json) > _MAX_JSON_BYTES + 4096):
            raise ValueError("invalid prepared mutation arguments")
        try:
            arguments = json.loads(self.arguments_json)
        except (UnicodeError, ValueError, TypeError):
            raise ValueError("invalid prepared mutation arguments") from None
        _validate_arguments(self.expectation.kind, arguments)
        if self.arguments_json != _canonical(arguments):
            raise ValueError("noncanonical prepared mutation arguments")
        if self.expectation.targets != _targets(self.expectation.kind, arguments):
            raise ValueError("prepared mutation target mismatch")
        if self.expectation.client_digest != hashlib.sha256(
            _canonical([self.expectation.kind, arguments])
        ).hexdigest():
            raise ValueError("prepared mutation digest mismatch")

    @property
    def rpc_name(self) -> str:
        return _FAMILIES[self.expectation.kind][0]

    @property
    def request_json(self) -> bytes:
        """Canonical replay payload; no transport credentials or provider state."""
        e = self.expectation
        return _canonical({"root": e.root, "protocol_epoch": e.protocol_epoch,
                           "operation_id": e.operation_id, "actor_id": e.actor_id,
                           "kind": e.kind, "client_digest": e.client_digest,
                           "arguments": json.loads(self.arguments_json)})

    def rpc_parameters(self) -> dict[str, Any]:
        e = self.expectation
        return {"p_root": e.root, "p_protocol_epoch": e.protocol_epoch,
                "p_operation_id": e.operation_id,
                "p_payload_digest": e.client_digest,
                **json.loads(self.arguments_json)}

    @classmethod
    def from_request_json(cls, raw: bytes) -> PreparedMutation:
        if type(raw) is not bytes or len(raw) > _MAX_JSON_BYTES + 8192:
            raise ValueError("invalid typed mutation replay record")
        try:
            data = json.loads(raw)
        except (UnicodeError, ValueError, TypeError):
            raise ValueError("invalid typed mutation replay record") from None
        if type(data) is not dict or set(data) != {
            "root", "protocol_epoch", "operation_id", "actor_id", "kind",
            "client_digest", "arguments",
        } or raw != _canonical(data):
            raise ValueError("invalid typed mutation replay record")
        arguments = data["arguments"]
        _validate_arguments(data["kind"], arguments)
        expected = MutationReceiptExpectation(
            data["root"], data["protocol_epoch"], data["operation_id"],
            data["actor_id"], data["kind"], data["client_digest"],
            _targets(data["kind"], arguments),
        )
        return cls(expected, _canonical(arguments))


def prepare_mutation(root: str, protocol_epoch: str, operation_id: str,
                     actor_id: str, kind: str,
                     arguments: dict[str, Any]) -> PreparedMutation:
    _root(root)
    for value in (protocol_epoch, operation_id, actor_id):
        _uuid(value, "typed mutation identity")
    _validate_arguments(kind, arguments)
    frozen_arguments = _canonical(arguments)
    # Digest identity is independent of dict insertion order and Python objects.
    digest = hashlib.sha256(_canonical([kind, arguments])).hexdigest()
    expected = MutationReceiptExpectation(
        root, protocol_epoch, operation_id, actor_id, kind, digest,
        _targets(kind, arguments),
    )
    return PreparedMutation(expected, frozen_arguments)


class TypedMutationRPC:
    """One pinned authenticated provider client; no fallback or retry path."""

    def __init__(self, provider: Any, *, actor_id: str, root: str,
                 endpoint: str) -> None:
        _uuid(actor_id, "typed mutation actor")
        _root(root)
        if type(endpoint) is not str or not endpoint:
            raise ValueError("invalid typed mutation endpoint")
        revision = provider.prepare_node_identity()
        client = provider._sb()
        if (type(revision) is not int or provider.node_identity_revision() != revision
                or provider._client is not client or provider.root != root
                or provider._env.get("SUPABASE_URL") != endpoint
                or not provider.auth_mode.startswith("member:")):
            raise TypedMutationRPCError("typed mutation identity is unavailable")
        self._provider = provider
        self._client = client
        self._revision = revision
        self.actor_id = actor_id
        self.root = root
        self.endpoint = endpoint
        self._access_token = self._session_token()
        # Verify the JWT itself; session.user is a local mutable cache.
        try:
            user_response = client.auth.get_user(jwt=self._access_token)
            user = user_response.user
            if user.id != actor_id or user.role != "authenticated":
                raise ValueError("wrong typed mutation principal")
        except Exception:
            raise TypedMutationRPCError("typed mutation principal is unavailable") from None
        # In supabase-py, auth events replace client._postgrest. Capturing the
        # current PostgREST instance pins its copied Authorization header to
        # this JWT even if another thread changes the client's session later.
        try:
            self._postgrest = client.postgrest
            if (str(self._postgrest_base_url()).rstrip("/")
                    != endpoint.rstrip("/") + "/rest/v1"
                    or self._postgrest_headers().get("Authorization")
                    != f"Bearer {self._access_token}"):
                raise ValueError("wrong typed mutation authorization")
        except Exception:
            raise TypedMutationRPCError("typed mutation authorization is unavailable") from None
        self._check_identity()

    def _session_token(self) -> str:
        try:
            session = self._client.auth.get_session()
            user = session.user
            account = user.id
            role = user.role
            token = session.access_token
        except Exception:
            raise TypedMutationRPCError("typed mutation session is unavailable") from None
        if (account != self.actor_id or role != "authenticated"
                or type(token) is not str or not token):
            raise TypedMutationRPCError("typed mutation session identity mismatch")
        return token

    def _postgrest_headers(self) -> Any:
        # supabase>=2.11 admits postgrest 0.19, whose authorization lives on
        # its httpx session. postgrest 2.x keeps a copied client header set.
        headers = getattr(self._postgrest, "headers", None)
        return headers if headers is not None else self._postgrest.session.headers

    def _postgrest_base_url(self) -> Any:
        base_url = getattr(self._postgrest, "base_url", None)
        return base_url if base_url is not None else self._postgrest.session.base_url

    def _check_identity(self) -> None:
        p = self._provider
        if (p.node_identity_revision() != self._revision
                or p._client is not self._client or p.root != self.root
                or p._env.get("SUPABASE_URL") != self.endpoint
                or not p.auth_mode.startswith("member:")
                or self._session_token() != self._access_token
                or self._postgrest_headers().get("Authorization")
                != f"Bearer {self._access_token}"):
            raise TypedMutationRPCError("typed mutation identity changed")

    def _call(self, name: str, parameters: dict[str, Any]) -> Any:
        self._check_identity()
        try:
            # RPC builders copy the pinned PostgREST client's headers before
            # execute; this never asks the mutable Supabase client for a new
            # session or a replacement PostgREST instance.
            builder = self._postgrest.rpc(name, parameters)
            request = getattr(builder, "request", None)
            if request is not None:
                # postgrest 2.x has already copied the pinned header.
                if (request.headers.get("Authorization")
                        != f"Bearer {self._access_token}"):
                    raise TypedMutationRPCError("typed mutation authorization changed")
            else:
                # postgrest 0.19 passes builder.headers explicitly to httpx;
                # pin the token there so mutable session defaults cannot win.
                builder.headers["Authorization"] = f"Bearer {self._access_token}"
            self._check_identity()
            raw = builder.execute().data
        except TypedMutationRPCError:
            raise
        except Exception:
            self._check_identity()
            raise TypedMutationRPCError("typed mutation RPC failed") from None
        self._check_identity()
        return raw

    def _check_expectation(self, expected: MutationReceiptExpectation) -> None:
        if (type(expected) is not MutationReceiptExpectation
                or expected.root != self.root
                or expected.actor_id != self.actor_id):
            raise TypedMutationRPCError("typed mutation request identity mismatch")

    def execute(self, request: PreparedMutation) -> ProviderMutationReceipt:
        if type(request) is not PreparedMutation:
            raise TypeError("invalid prepared mutation")
        # Revalidate even if the frozen object was forged through object.__setattr__.
        request.__post_init__()
        self._check_expectation(request.expectation)
        raw = self._call(request.rpc_name, request.rpc_parameters())
        try:
            return decode_mutation_receipt(raw, request.expectation)
        except (TypeError, ValueError):
            raise TypedMutationRPCError("invalid typed mutation receipt") from None

    def resolve(self, expected: MutationReceiptExpectation) -> ProviderMutationReceipt:
        self._check_expectation(expected)
        if expected.provider_digest is None:
            raise ValueError("provider digest required for receipt resolution")
        raw = self._call("ab_mutation_receipt", self._receipt_parameters(expected))
        try:
            return decode_mutation_receipt(raw, expected)
        except (TypeError, ValueError):
            raise TypedMutationRPCError("invalid typed mutation receipt") from None

    def acknowledge(self, receipt: ProviderMutationReceipt) -> bool:
        if type(receipt) is not ProviderMutationReceipt:
            raise TypeError("invalid typed mutation receipt")
        receipt.__post_init__()
        self._check_expectation(receipt.identity)
        result = self._call("ab_ack_mutation_receipt",
                            self._receipt_parameters(receipt.identity))
        if result is not True:
            raise TypedMutationRPCError("invalid typed mutation acknowledgment")
        return True

    @staticmethod
    def _receipt_parameters(expected: MutationReceiptExpectation) -> dict[str, str]:
        if expected.provider_digest is None:
            raise ValueError("provider digest required")
        return {"p_root": expected.root,
                "p_protocol_epoch": expected.protocol_epoch,
                "p_operation_id": expected.operation_id,
                "p_payload_digest": expected.client_digest,
                "p_provider_digest": expected.provider_digest}


__all__ = ["PreparedMutation", "TypedMutationRPC", "TypedMutationRPCError",
           "prepare_mutation"]
