"""Inactive, receipt-bound typed Supabase mutation wire.

The durable operation journal owns admission, persistence, retries, and replay.
This module only prepares exact requests and makes one RPC per method call.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .mutation_protocol import _DIGEST, _root, _uuid
from .mutation_receipts import (
    MutationReceiptExpectation, ProviderMutationReceipt, decode_mutation_receipt,
)
from .scoped_sources import validate_source_cursor, validate_source_paths
from .recovery_sources import RecoveryCut


# Each tuple is (public RPC name, ordered SQL argument names, result target names).
_FAMILIES = {
    "doc_put": ("ab_mutation_doc_put", ("p_path", "p_data"), ("path",)),
    "doc_compare_put": ("ab_mutation_doc_compare_put",
                        ("p_path", "p_data", "p_expected_source_epoch",
                         "p_expected_seq", "p_expected_deleted"), ("path",)),
    "reaction_pair": ("ab_mutation_reaction_pair",
                      ("p_chat", "p_user", "p_state_data", "p_reaction_data",
                       "p_expected_source_epoch", "p_expected_state_seq",
                       "p_expected_state_deleted", "p_expected_reaction_seq",
                       "p_expected_reaction_deleted"), ("chat_id", "username")),
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


def _jsonb_size(value: object) -> tuple[int, int]:
    """Bound jsonb::text bytes and its quote/backslash escaping overhead.

    PostgreSQL prints a space after JSON separators and expands numeric
    exponents using decimal arithmetic. Decimal reads the exact JSON number
    token sent by Python, so this never estimates from binary-float precision.
    Object ordering does not affect either count. Negative zero's optional
    sign only makes this bound conservative.
    """
    if type(value) is dict:
        parts = []
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("typed mutation JSON object keys must be strings")
            parts.extend((_jsonb_size(key), _jsonb_size(item)))
        return (2 + max(0, len(value) - 1) * 2 + len(value) * 2
                + sum(size for size, _ in parts),
                sum(escaped for _, escaped in parts))
    if type(value) in (list, tuple):
        parts = [_jsonb_size(item) for item in value]
        return (2 + max(0, len(value) - 1) * 2
                + sum(size for size, _ in parts),
                sum(escaped for _, escaped in parts))
    if type(value) is float:
        text = format(Decimal(json.dumps(value, allow_nan=False)), "f")
    elif value is None or type(value) in (str, int, bool):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    else:
        raise ValueError("invalid typed mutation JSON scalar")
    # JSONB uses PostgreSQL numeric: at most 131072 integral digits. Python
    # normally has a stricter conversion limit, but callers can disable it.
    if type(value) is int and len(text.lstrip("-")) > 131072:
        raise ValueError("typed mutation integer exceeds provider numeric range")
    if type(value) is str and "\x00" in value:
        raise ValueError("typed mutation JSON cannot contain NUL")
    return len(text.encode("utf-8")), text.count('"') + text.count("\\")


def _bounded_json(value: object, *, object_only: bool = False,
                  log_record: bool = False) -> None:
    if value is None or object_only and type(value) is not dict:
        raise ValueError("invalid typed mutation JSON value")
    # Check the compact request first, including cycles and invalid scalars.
    if len(_canonical(value)) > _MAX_JSON_BYTES:
        raise ValueError("typed mutation JSON exceeds limit")
    try:
        size, escaped = _jsonb_size(value)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid typed mutation JSON") from None
    # Logs store p_record::text, then ab_source_text_bytes JSON-encodes that
    # complete line again. Documents use ab_source_json_bytes directly.
    if log_record:
        size += escaped + 2
    if size > _MAX_JSON_BYTES:
        raise ValueError("typed mutation JSON exceeds provider limit")


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
        elif name in {"p_chat", "p_user"}:
            _chat(value)
        elif name in {"p_data", "p_record"}:
            _bounded_json(value, object_only=name == "p_record" or kind == "effect_transition",
                          log_record=name == "p_record")
        elif name in {"p_state_data", "p_reaction_data"}:
            _bounded_json(value, object_only=True)
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
        elif name == "p_expected_source_epoch":
            _uuid(value, "typed document source epoch")
        elif name in {"p_expected_seq", "p_expected_state_seq", "p_expected_reaction_seq"}:
            validate_source_cursor(value, "typed document sequence")
        elif name in {"p_expected_deleted", "p_expected_state_deleted", "p_expected_reaction_deleted"}:
            if type(value) is not bool:
                raise ValueError("invalid typed document deletion state")
        elif name == "p_sealed_bytes":
            validate_source_cursor(value, "typed mutation sealed bytes")
            if not 1 <= value <= 52_428_800:
                raise ValueError("invalid typed mutation sealed bytes")
        elif name in {"p_sealed_sha256", "p_manifest_digest"}:
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError("invalid typed mutation digest")
    if kind == "doc_compare_put" and arguments["p_expected_seq"] == 0 \
            and arguments["p_expected_deleted"]:
        raise ValueError("absent typed document cannot be a tombstone")
    if kind == "reaction_pair":
        for document in ("state", "reaction"):
            if arguments[f"p_expected_{document}_seq"] == 0 and arguments[f"p_expected_{document}_deleted"]:
                raise ValueError("absent typed document cannot be a tombstone")
        if sum(_jsonb_size(arguments[name])[0] for name in
               ("p_state_data", "p_reaction_data")) > _MAX_JSON_BYTES:
            raise ValueError("typed reaction pair exceeds provider limit")
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
    if kind == "reaction_pair":
        return (("chat_id", arguments["p_chat"]), ("username", arguments["p_user"]))
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


def _provider_endpoint(value: object) -> str | None:
    from ..node.protocol import canonical_endpoint

    try:
        return canonical_endpoint(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DocumentMutationObservation:
    """Detached provider version for an atomic compare-and-replace.

    Sequence zero denotes physical absence. Tombstones retain their positive
    sequence; neither missing data nor a failed read is an absence proof.
    """

    path: str
    source_epoch: str
    seq: int
    deleted: bool
    payload_json: bytes | None

    def __post_init__(self) -> None:
        validate_source_paths((self.path,))
        _uuid(self.source_epoch, "document source epoch")
        validate_source_cursor(self.seq, "document source sequence")
        if (type(self.deleted) is not bool
                or self.seq == 0 and self.deleted
                or (self.seq == 0 or self.deleted) != (self.payload_json is None)):
            raise ValueError("invalid document observation")
        if self.payload_json is not None:
            if type(self.payload_json) is not bytes:
                raise ValueError("invalid document observation payload")
            data = json.loads(self.payload_json)
            _bounded_json(data)
            if _canonical(data) != self.payload_json:
                raise ValueError("noncanonical document observation")

    @property
    def payload(self) -> Any:
        return None if self.payload_json is None else json.loads(self.payload_json)

    def arguments(self, data: Any) -> dict[str, Any]:
        self.__post_init__()
        return {"p_path": self.path, "p_data": data,
                "p_expected_source_epoch": self.source_epoch,
                "p_expected_seq": self.seq,
                "p_expected_deleted": self.deleted}


class TypedMutationRPC:
    """One pinned authenticated provider client; no fallback or retry path."""

    def __init__(self, provider: Any, *, actor_id: str, root: str,
                 endpoint: str) -> None:
        _uuid(actor_id, "typed mutation actor")
        _root(root)
        from ..node.protocol import canonical_endpoint

        endpoint = canonical_endpoint(endpoint)
        revision = provider.prepare_node_identity()
        client = provider._sb()
        if (type(revision) is not int or provider.node_identity_revision() != revision
                or provider._client is not client or provider.root != root
                or _provider_endpoint(provider._env.get("SUPABASE_URL")) != endpoint
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
            if (_provider_endpoint(str(self._postgrest_base_url()))
                    != endpoint + "/rest/v1"
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
                or _provider_endpoint(p._env.get("SUPABASE_URL")) != self.endpoint
                or _provider_endpoint(str(self._postgrest_base_url()))
                != self.endpoint + "/rest/v1"
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

    def read_document_for_update(self, path: str) -> DocumentMutationObservation:
        """Read one bounded document with this client's exact authenticated JWT.

        The two recovery cuts fence source-epoch replacement, not intervening
        writes. The provider compares the returned row sequence at mutation
        time. This read never publishes a local source or establishes authority.
        """
        validate_source_paths((path,))

        def fence() -> RecoveryCut:
            raw = self._call("ab_node_recovery_fence", {"p_root": self.root})
            try:
                if type(raw) is not dict:
                    raise ValueError("invalid document source fence")
                cut = RecoveryCut(**raw)
                if cut.account_id != self.actor_id:
                    raise ValueError("document source principal changed")
                return cut
            except (TypeError, ValueError):
                raise TypedMutationRPCError("invalid document source fence") from None

        before = fence()
        raw = self._call("ab_node_docs_exact", {
            "p_root": self.root, "p_paths": [path], "p_max_bytes": 8_388_608,
        })
        after = fence()
        try:
            if (before.source_epoch != after.source_epoch
                    or after.cursor < before.cursor
                    or after.minimum_cursor < before.minimum_cursor):
                raise ValueError("document source epoch changed")
            if type(raw) is not list or len(raw) > 1:
                raise ValueError("invalid document response")
            if not raw:
                return DocumentMutationObservation(path, before.source_epoch, 0, False, None)
            row = raw[0]
            if (type(row) is not dict
                    or set(row) != {"path", "seq", "data", "deleted", "batch_overflow"}
                    or row["batch_overflow"] is not False or row["path"] != path
                    or type(row["seq"]) is not int or row["seq"] < 1
                    or type(row["deleted"]) is not bool
                    or row["deleted"] and row["data"] is not None):
                raise ValueError("invalid document response")
            payload = None if row["deleted"] else _canonical(row["data"])
            return DocumentMutationObservation(
                path, before.source_epoch, row["seq"], row["deleted"], payload)
        except (TypeError, ValueError, UnicodeError):
            raise TypedMutationRPCError("invalid document observation") from None

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


__all__ = ["DocumentMutationObservation", "PreparedMutation", "TypedMutationRPC", "TypedMutationRPCError",
           "prepare_mutation"]
