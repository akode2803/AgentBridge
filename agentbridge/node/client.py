"""Strict owner-only client for the inactive local node loopback API.

Discovery reads the protected state/token pair, every response is bounded and
validated against the full replica identity, and the pair is rechecked after
I/O.  Returned generations and scope positions are input-stability evidence;
they never carry membership or canonical visibility authority.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .admission import (
    MAX_CHANGE_PAGE, NodeCaptureRequest, NodeDocument, NodeFrontier,
    NodeInputError, NodeLogRow, NodeLogStream, NodeLogStreamRequest,
    NodeScopePosition, NodeVisibility,
)
from .protocol import (
    HEALTH_VALUES, MAX_STATUS_BYTES, PROTOCOL_VERSION, NodeStatus,
    ProtocolError, ReplicaIdentity, encode_json, parse_status,
    request_proof, response_proof, valid_wire_proof,
)
from .security import LocalSecurityError, verify_private_path
from .wire import (
    MAX_WIRE_REQUEST_BYTES, MAX_WIRE_RESPONSE_BYTES,
    CursorReset, _incarnation, _integer, _log_selection, _log_stream_selection,
    _parse_cursor_text, _untoken, parse_capture_request, parse_change_request,
    parse_log_stream_request,
)

CLIENT_TIMEOUT_S = 2.0
MAX_STATE_BYTES = 16 * 1024
MAX_TOKEN_BYTES = 512
_STATE_FIELDS = {
    "pid", "port", "node_epoch", "database_incarnation", "token_digest",
    "started_ns", "protocol_version", "identity_digest",
}


class NodeClientUnavailable(OSError):
    """The local owner is absent, changed, malformed, or unreachable."""


class NodeClientReset(ProtocolError):
    """A continuation belongs to a replaced or compacted local replica."""


class NodeClientGenerationChanged(ProtocolError):
    """The admitted generation changed during a paged capture."""


@dataclass(frozen=True)
class NodeBinding:
    node_epoch: str
    database_incarnation: str
    identity_digest: str
    started_ns: int


@dataclass(frozen=True)
class ClientLogRequest:
    chat_id: str
    log_name: str
    cursor: str | None = None
    limit: int = 1_000


@dataclass(frozen=True)
class ClientCaptureRequest:
    expected_capture: str | None = None
    exact_document_paths: tuple[str, ...] = ()
    document_prefixes: tuple[str, ...] = ()
    logs: tuple[ClientLogRequest, ...] = ()
    include_visibility: bool = False
    visibility_cursor: str | None = None
    visibility_limit: int = 1_000
    frontier_names: tuple[str, ...] = ()
    max_documents: int = 1_000
    max_log_rows: int = 5_000
    max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.logs) is not tuple or any(
                type(value) is not ClientLogRequest for value in self.logs):
            raise NodeInputError("invalid client log requests")

    def payload(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "expected_capture": self.expected_capture,
            "exact_document_paths": list(self.exact_document_paths),
            "document_prefixes": list(self.document_prefixes),
            "logs": [{
                "chat_id": row.chat_id, "log_name": row.log_name,
                "cursor": row.cursor, "limit": row.limit,
            } for row in self.logs],
            "include_visibility": self.include_visibility,
            "visibility_cursor": self.visibility_cursor,
            "visibility_limit": self.visibility_limit,
            "frontier_names": list(self.frontier_names),
            "max_documents": self.max_documents,
            "max_log_rows": self.max_log_rows,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class ClientLogPage:
    request: ClientLogRequest
    cursor: str
    rows: tuple[NodeLogRow, ...]
    has_more: bool


@dataclass(frozen=True)
class ClientCapture:
    binding: NodeBinding
    capture: str
    generation: int
    health: str
    last_success_ns: int
    documents: tuple[NodeDocument, ...]
    logs: tuple[ClientLogPage, ...]
    visibility: tuple[str, ...]
    visibility_cursor: str
    visibility_has_more: bool
    frontiers: tuple[NodeFrontier, ...]
    scope_positions: tuple[NodeScopePosition, ...]
    captured_bytes: int


@dataclass(frozen=True)
class ClientLogStreamPage:
    binding: NodeBinding
    capture: str
    generation: int
    health: str
    last_success_ns: int
    chat_id: str
    streams: tuple[NodeLogStream, ...]
    cursor: str
    has_more: bool
    scope_positions: tuple[NodeScopePosition, ...]
    captured_bytes: int


@dataclass(frozen=True)
class ClientChange:
    generation: int
    scope_kind: str
    scope_id: str
    kind: str


@dataclass(frozen=True)
class ClientChangePage:
    binding: NodeBinding
    status: str
    minimum_cursor: str
    current_cursor: str
    cursor: str
    has_more: bool
    changes: tuple[ClientChange, ...]


@dataclass(frozen=True)
class _Endpoint:
    binding: NodeBinding
    pid: int
    port: int
    token_digest: str
    token: str = field(repr=False)
    state_bytes: bytes = field(repr=False)


def _private_bytes(path: Path, limit: int) -> bytes:
    try:
        verify_private_path(path, directory=False)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise NodeClientUnavailable("invalid local node file")
        if os.name != "nt" and (
                before.st_mode & 0o077 or (
                    hasattr(os, "geteuid") and before.st_uid != os.geteuid())):
            raise NodeClientUnavailable("local node file is not owner-only")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (not stat.S_ISREG(current.st_mode)
                    or (before.st_dev, before.st_ino) != (
                        current.st_dev, current.st_ino)):
                raise NodeClientUnavailable("local node file changed")
            verify_private_path(path, directory=False)
            after = path.lstat()
            if ((after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                    or not stat.S_ISREG(after.st_mode)
                    or stat.S_ISLNK(after.st_mode)):
                raise NodeClientUnavailable("local node file changed")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(limit + 1)
        finally:
            os.close(descriptor)
    except NodeClientUnavailable:
        raise
    except LocalSecurityError as exc:
        raise NodeClientUnavailable(str(exc)) from exc
    except OSError as exc:
        raise NodeClientUnavailable("local node file is unavailable") from exc
    if not raw or len(raw) > limit:
        raise NodeClientUnavailable("invalid local node file")
    return raw


def _private_directory(path: Path) -> None:
    try:
        verify_private_path(path, directory=True)
        value = path.lstat()
    except LocalSecurityError as exc:
        raise NodeClientUnavailable(str(exc)) from exc
    except OSError as exc:
        raise NodeClientUnavailable("local node owner is unavailable") from exc
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise NodeClientUnavailable("invalid local node directory")
    if os.name != "nt" and (
            value.st_mode & 0o077 or (
                hasattr(os, "geteuid") and value.st_uid != os.geteuid())):
        raise NodeClientUnavailable("local node directory is not owner-only")


def _text(value: Any, name: str, *, maximum: int = 128) -> str:
    if type(value) is not str or not value:
        raise NodeClientUnavailable(f"invalid local node {name}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise NodeClientUnavailable(f"invalid local node {name}") from None
    if size > maximum:
        raise NodeClientUnavailable(f"invalid local node {name}")
    return value


def _binding(endpoint: _Endpoint) -> NodeBinding:
    return NodeBinding(**vars(endpoint.binding))


def _decode_blob(value: Any, name: str, *, optional: bool = False) -> bytes | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise ProtocolError(f"invalid {name}")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError):
        raise ProtocolError(f"invalid {name}") from None


def _chat_scope(path: str) -> str | None:
    pieces = path.split("/")
    return pieces[1] if len(pieces) >= 3 and pieces[0] == "chats" else None


def _chat_selector_scope(path: str) -> str | None:
    pieces = path.split("/")
    return pieces[1] if len(pieces) >= 2 and pieces[0] == "chats" else None


def _capture_charge(documents: list[NodeDocument], logs: list[ClientLogPage],
                    visibility: tuple[str, ...],
                    frontiers: tuple[NodeFrontier, ...],
                    scopes: tuple[NodeScopePosition, ...]) -> int:
    used = sum(
        len(row.path.encode("utf-8")) + len(row.payload or b"") + 24
        for row in documents
    )
    used += sum(
        len(row.chat_id.encode("utf-8"))
        + len(row.log_name.encode("utf-8")) + len(row.payload) + 24
        for page in logs for row in page.rows
    )
    used += sum(len(chat.encode("utf-8")) + 8 for chat in visibility)
    used += sum(
        len(row.name.encode("utf-8"))
        + len((row.epoch or "").encode("utf-8")) + 32
        for row in frontiers
    )
    used += sum(
        len(row.scope_kind.encode("utf-8"))
        + len(row.scope_id.encode("utf-8")) + 24
        + len((row.pending_reason or "").encode("utf-8"))
        for row in scopes
    )
    return used


def _log_stream_charge(chat_id: str, streams: tuple[NodeLogStream, ...],
                       scopes: tuple[NodeScopePosition, ...]) -> int:
    used = len(chat_id.encode("utf-8")) + 8
    used += sum(len(row.log_name.encode("utf-8")) + 24 for row in streams)
    used += sum(
        len(row.scope_kind.encode("utf-8"))
        + len(row.scope_id.encode("utf-8")) + 24
        + len((row.pending_reason or "").encode("utf-8"))
        for row in scopes
    )
    return used


class LocalNodeClient:
    """Synchronous bounded client. It creates no thread or provider fallback."""

    def __init__(self, home: Path | str, identity: ReplicaIdentity, *,
                 timeout_s: float = CLIENT_TIMEOUT_S) -> None:
        if type(identity) is not ReplicaIdentity:
            raise TypeError("invalid replica identity")
        if (type(timeout_s) not in (int, float) or isinstance(timeout_s, bool)
                or not 0 < timeout_s <= CLIENT_TIMEOUT_S):
            raise ValueError("invalid local node client timeout")
        self.home = Path(home).resolve()
        self.identity = identity
        self.timeout_s = float(timeout_s)
        self.directory = self.home / "nodes" / identity.digest
        self.state_path = self.directory / "state.json"
        self.token_path = self.directory / "token"

    def _discover(self) -> _Endpoint:
        _private_directory(self.directory)
        state_bytes = _private_bytes(self.state_path, MAX_STATE_BYTES)
        token_bytes = _private_bytes(self.token_path, MAX_TOKEN_BYTES)
        # Atomic replacement prevents partial files; rereading state binds the
        # independently stored credential to one published owner record.
        if _private_bytes(self.state_path, MAX_STATE_BYTES) != state_bytes:
            raise NodeClientUnavailable("local node owner changed")
        try:
            value = json.loads(state_bytes)
            token = token_bytes.decode("ascii")
        except (UnicodeError, ValueError, RecursionError):
            raise NodeClientUnavailable("invalid local node discovery") from None
        if type(value) is not dict or set(value) != _STATE_FIELDS:
            raise NodeClientUnavailable("invalid local node discovery")
        integers = (value["pid"], value["port"], value["started_ns"])
        if (any(type(item) is not int or item <= 0 for item in integers)
                or value["port"] > 65535
                or type(value["protocol_version"]) is not int
                or value["protocol_version"] != PROTOCOL_VERSION):
            raise NodeClientUnavailable("invalid local node discovery")
        node_epoch = _text(value["node_epoch"], "epoch")
        incarnation = _text(value["database_incarnation"], "database")
        identity_digest = _text(value["identity_digest"], "identity", maximum=64)
        token_digest = _text(value["token_digest"], "token digest", maximum=64)
        if (identity_digest != self.identity.digest
                or len(identity_digest) != 64
                or len(token_digest) != 64
                or any(char not in "0123456789abcdef"
                       for char in identity_digest + token_digest)
                or not 20 <= len(token) <= MAX_TOKEN_BYTES
                or any(char.isspace() for char in token)
                or hashlib.sha256(token_bytes).hexdigest() != token_digest):
            raise NodeClientUnavailable("invalid local node discovery")
        return _Endpoint(
            NodeBinding(node_epoch, incarnation, identity_digest,
                        value["started_ns"]),
            value["pid"], value["port"], token_digest, token, state_bytes,
        )

    def _assert_current(self, expected: _Endpoint) -> None:
        current = self._discover()
        if current != expected:
            raise NodeClientUnavailable("local node owner changed")

    def _request(self, endpoint: _Endpoint, method: str, path: str,
                 payload: dict[str, Any] | None, *, max_response: int) -> tuple[int, dict]:
        body = None if payload is None else encode_json(
            payload, max_bytes=MAX_WIRE_REQUEST_BYTES)
        transmitted = body or b""
        body_digest = hashlib.sha256(transmitted).hexdigest()
        nonce = secrets.token_hex(32)
        headers = {
            "Host": f"127.0.0.1:{endpoint.port}",
            "Authorization": "AgentBridge-HMAC " + nonce + "." + request_proof(
                endpoint.token, nonce, method, path, body_digest),
            "X-AgentBridge-Body-SHA256": body_digest,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", endpoint.port, timeout=self.timeout_s)
        deadline = time.monotonic() + self.timeout_s
        expired = threading.Event()
        transport: list[socket.socket | None] = [None]

        def expire() -> None:
            expired.set()
            wire = transport[0] or connection.sock
            if wire is not None:
                try:
                    wire.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    wire.close()
                except OSError:
                    pass

        watchdog = threading.Timer(self.timeout_s, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            connection.request(method, path, body=body, headers=headers)
            transport[0] = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0 or expired.is_set():
                raise NodeClientUnavailable("local node request timed out")
            if transport[0] is not None:
                transport[0].settimeout(remaining)
            response = connection.getresponse()
            content_type = response.headers.get_content_type()
            raw_length = response.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                length = -1
            if (content_type != "application/json" or length < 0
                    or length > max_response):
                raise NodeClientUnavailable("invalid local node response")
            chunks = []
            received = 0
            while received < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or expired.is_set():
                    raise NodeClientUnavailable("local node request timed out")
                if transport[0] is not None:
                    transport[0].settimeout(remaining)
                chunk = response.read(min(64 * 1024, length - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            raw = b"".join(chunks)
            if len(raw) != length:
                raise NodeClientUnavailable("invalid local node response")
            expected_proof = response_proof(
                endpoint.token, nonce, method, path, response.status, raw)
            supplied_proof = response.headers.get("X-AgentBridge-Proof", "")
            if (not valid_wire_proof(supplied_proof)
                    or not secrets.compare_digest(
                        supplied_proof, expected_proof)):
                raise NodeClientUnavailable(
                    "local node response authentication failed")
            try:
                value = json.loads(raw)
            except (UnicodeError, ValueError, RecursionError):
                raise NodeClientUnavailable("invalid local node response") from None
            if type(value) is not dict:
                raise NodeClientUnavailable("invalid local node response")
            if time.monotonic() > deadline or expired.is_set():
                raise NodeClientUnavailable("local node request timed out")
            return response.status, value
        except (NodeClientUnavailable, ProtocolError):
            raise
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            if expired.is_set() or time.monotonic() > deadline:
                raise NodeClientUnavailable(
                    "local node request timed out") from exc
            raise NodeClientUnavailable("local node request failed") from exc
        finally:
            watchdog.cancel()
            connection.close()

    def _call(self, method: str, path: str, payload: dict[str, Any] | None,
              *, max_response: int) -> tuple[_Endpoint, dict]:
        endpoint = self._discover()
        status, value = self._request(
            endpoint, method, path, payload, max_response=max_response)
        self._assert_current(endpoint)
        if status == 200:
            return endpoint, value
        error = value.get("error") if set(value) == {"error"} else None
        if status == 409 and error == "reset_required":
            raise NodeClientReset("local node continuation reset")
        if status == 409 and error == "generation_changed":
            raise NodeClientGenerationChanged("local node generation changed")
        if status == 413 and error == "capture_budget_exceeded":
            raise OverflowError("local node capture exceeds budget")
        if status in (400, 404):
            raise ProtocolError("local node rejected request")
        raise NodeClientUnavailable("local node request unavailable")

    def status(self) -> NodeStatus:
        endpoint, value = self._call(
            "GET", "/v1/status", None, max_response=MAX_STATUS_BYTES)
        result = parse_status(
            value, expected_identity=self.identity,
            expected_incarnation=endpoint.binding.database_incarnation)
        if (result.node_epoch != endpoint.binding.node_epoch
                or result.started_ns != endpoint.binding.started_ns):
            raise NodeClientUnavailable("local node status changed")
        return result

    def discovery_matches(self, binding: NodeBinding) -> bool:
        """Process-discovery continuity only; never freshness or authority."""
        if type(binding) is not NodeBinding:
            raise TypeError("invalid local node binding")
        try:
            return self._discover().binding == binding
        except NodeClientUnavailable:
            return False

    def capture(self, request: ClientCaptureRequest) -> ClientCapture:
        if type(request) is not ClientCaptureRequest:
            raise TypeError("invalid client capture request")
        endpoint = self._discover()
        payload = request.payload()
        try:
            parsed = parse_capture_request(
                payload,
                database_incarnation=endpoint.binding.database_incarnation)
        except CursorReset:
            raise NodeClientReset("local node continuation reset") from None
        status, value = self._request(
            endpoint, "POST", "/v1/capture", payload,
            max_response=MAX_WIRE_RESPONSE_BYTES)
        self._assert_current(endpoint)
        if status != 200:
            return self._capture_error(status, value)
        return self._parse_capture(endpoint, request, parsed, value)

    def _capture_error(self, status: int, value: dict):
        error = value.get("error") if set(value) == {"error"} else None
        if status == 409 and error == "reset_required":
            raise NodeClientReset("local node continuation reset")
        if status == 409 and error == "generation_changed":
            raise NodeClientGenerationChanged("local node generation changed")
        if status == 413 and error == "capture_budget_exceeded":
            raise OverflowError("local node capture exceeds budget")
        if status in (400, 404):
            raise ProtocolError("local node rejected request")
        raise NodeClientUnavailable("local node request unavailable")

    def _parse_capture(self, endpoint: _Endpoint, wanted: ClientCaptureRequest,
                       parsed: NodeCaptureRequest, value: dict) -> ClientCapture:
        fields = {
            "protocol_version", "capture", "generation", "health",
            "last_success_ns", "captured_bytes", "documents", "logs",
            "visibility", "visibility_cursor", "visibility_has_more",
            "frontiers", "scope_positions",
        }
        if type(value) is not dict or set(value) != fields:
            raise ProtocolError("invalid node capture response")
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("unsupported node protocol")
        generation = _integer(value["generation"], "capture generation")
        token = _untoken(value["capture"], "capture", {"db", "generation"})
        _incarnation(token["db"], endpoint.binding.database_incarnation)
        if _integer(token["generation"], "capture generation") != generation:
            raise ProtocolError("capture generation mismatch")
        if (parsed.expected_generation is not None
                and generation != parsed.expected_generation):
            raise NodeClientGenerationChanged("local node generation changed")
        health = value["health"]
        if type(health) is not str or health not in HEALTH_VALUES:
            raise ProtocolError("invalid node capture health")
        success = _integer(value["last_success_ns"], "last success time")
        captured_bytes = _integer(value["captured_bytes"], "captured bytes")
        if captured_bytes > parsed.max_bytes:
            raise ProtocolError("capture byte budget exceeded")

        raw_documents = value["documents"]
        if type(raw_documents) is not list or len(raw_documents) > parsed.max_documents:
            raise ProtocolError("invalid node documents")
        documents = []
        for raw in raw_documents:
            if type(raw) is not dict or set(raw) != {"path", "seq", "deleted", "payload"}:
                raise ProtocolError("invalid node document")
            try:
                row = NodeDocument(
                    raw["path"], raw["seq"], raw["deleted"],
                    _decode_blob(raw["payload"], "document payload", optional=True),
                )
            except (NodeInputError, TypeError, ValueError):
                raise ProtocolError("invalid node document") from None
            selected = (row.path in parsed.exact_document_paths or any(
                not prefix or row.path == prefix or row.path.startswith(prefix + "/")
                for prefix in parsed.document_prefixes))
            if not selected:
                raise ProtocolError("document outside capture selection")
            documents.append(row)
        if tuple(row.path for row in documents) != tuple(sorted(
                {row.path for row in documents})):
            raise ProtocolError("invalid node document order")

        raw_logs = value["logs"]
        if type(raw_logs) is not list or len(raw_logs) != len(parsed.log_requests):
            raise ProtocolError("invalid node log pages")
        logs = []
        total_rows = 0
        for client_request, numeric_request, raw in zip(
                wanted.logs, parsed.log_requests, raw_logs):
            if type(raw) is not dict or set(raw) != {
                    "chat_id", "log_name", "cursor", "has_more", "rows"}:
                raise ProtocolError("invalid node log page")
            if ((raw["chat_id"], raw["log_name"])
                    != (client_request.chat_id, client_request.log_name)
                    or type(raw["has_more"]) is not bool
                    or type(raw["rows"]) is not list
                    or len(raw["rows"]) > numeric_request.limit):
                raise ProtocolError("invalid node log page")
            cursor = _untoken(raw["cursor"], "log", {
                "db", "generation", "selection", "after"})
            _incarnation(cursor["db"], endpoint.binding.database_incarnation)
            if (_integer(cursor["generation"], "capture generation") != generation
                    or cursor["selection"] != _log_selection(
                        client_request.chat_id, client_request.log_name)):
                raise ProtocolError("log continuation changed")
            after = _integer(cursor["after"], "log continuation")
            rows = []
            for item in raw["rows"]:
                if type(item) is not dict or set(item) != {"id", "payload"}:
                    raise ProtocolError("invalid node log row")
                try:
                    rows.append(NodeLogRow(
                        item["id"], client_request.chat_id,
                        client_request.log_name,
                        _decode_blob(item["payload"], "log payload"),
                    ))
                except (NodeInputError, TypeError, ValueError):
                    raise ProtocolError("invalid node log row") from None
            ids = tuple(row.id for row in rows)
            if (ids != tuple(sorted(set(ids)))
                    or any(ident <= numeric_request.after_id or ident > after
                           for ident in ids)
                    or (ids and ids[-1] != after)
                    or (not ids and after != numeric_request.after_id)):
                raise ProtocolError("invalid node log row order")
            total_rows += len(rows)
            logs.append(ClientLogPage(
                client_request, raw["cursor"], tuple(rows), raw["has_more"]))
        if total_rows > parsed.max_log_rows:
            raise ProtocolError("log row budget exceeded")

        raw_visibility = value["visibility"]
        if (type(raw_visibility) is not list
                or len(raw_visibility) > parsed.visibility_limit
                or type(value["visibility_has_more"]) is not bool):
            raise ProtocolError("invalid node visibility page")
        try:
            visibility = tuple(
                NodeVisibility(chat, True).chat_id for chat in raw_visibility)
        except (NodeInputError, TypeError, ValueError):
            raise ProtocolError("invalid node visibility page") from None
        if visibility != tuple(sorted(set(visibility))):
            raise ProtocolError("invalid node visibility order")
        visibility_token = _untoken(value["visibility_cursor"], "visibility", {
            "db", "generation", "after"})
        _incarnation(
            visibility_token["db"], endpoint.binding.database_incarnation)
        visibility_after = _parse_cursor_text(visibility_token["after"])
        if (_integer(visibility_token["generation"], "capture generation")
                != generation
                or any(chat <= parsed.visibility_after for chat in visibility)
                or visibility_after != (
                    visibility[-1] if visibility else parsed.visibility_after)):
            raise ProtocolError("visibility continuation changed")

        frontiers = self._frontiers(value["frontiers"], parsed)
        scopes = self._scopes(value["scope_positions"], parsed, documents, visibility)
        if _capture_charge(
                documents, logs, visibility, frontiers, scopes) != captured_bytes:
            raise ProtocolError("capture byte charge mismatch")
        return ClientCapture(
            _binding(endpoint), value["capture"], generation, health, success,
            tuple(documents), tuple(logs), visibility,
            value["visibility_cursor"], value["visibility_has_more"],
            frontiers, scopes, captured_bytes,
        )

    @staticmethod
    def _frontiers(value: Any, request: NodeCaptureRequest) -> tuple[NodeFrontier, ...]:
        if type(value) is not list or len(value) > 128:
            raise ProtocolError("invalid node frontiers")
        rows = []
        for item in value:
            if type(item) is not dict or set(item) != {
                    "name", "epoch", "cursor", "minimum_cursor"}:
                raise ProtocolError("invalid node frontier")
            try:
                rows.append(NodeFrontier(
                    item["name"], item["epoch"], item["cursor"],
                    item["minimum_cursor"]))
            except (NodeInputError, TypeError, ValueError):
                raise ProtocolError("invalid node frontier") from None
        names = tuple(row.name for row in rows)
        if names != tuple(sorted(set(names))):
            raise ProtocolError("invalid node frontier order")
        if request.frontier_names and not set(names).issubset(request.frontier_names):
            raise ProtocolError("frontier outside capture selection")
        return tuple(rows)

    @staticmethod
    def _scopes(value: Any, request: NodeCaptureRequest,
                documents: list[NodeDocument], visibility: tuple[str, ...],
                ) -> tuple[NodeScopePosition, ...]:
        expected = {("root", "")}
        expected.update(("chat", row.chat_id) for row in request.log_requests)
        expected.update(("chat", chat) for chat in visibility)
        for path in (*request.exact_document_paths, *request.document_prefixes):
            chat = _chat_selector_scope(path)
            if chat:
                expected.add(("chat", chat))
        for row in documents:
            chat = _chat_scope(row.path)
            if chat:
                expected.add(("chat", chat))
        if type(value) is not list or len(value) != len(expected):
            raise ProtocolError("invalid node scope positions")
        rows = []
        for item in value:
            if type(item) is not dict or set(item) != {
                    "scope_kind", "scope_id", "generation", "pending_reason"}:
                raise ProtocolError("invalid node scope position")
            kind, ident = item["scope_kind"], item["scope_id"]
            generation = _integer(item["generation"], "scope generation")
            pending = item["pending_reason"]
            if ((kind, ident) not in expected
                    or (pending is not None and (
                        type(pending) is not str or not pending
                        or len(pending.encode("utf-8")) > 256))):
                raise ProtocolError("invalid node scope position")
            rows.append(NodeScopePosition(kind, ident, generation, pending))
        if tuple((row.scope_kind, row.scope_id) for row in rows) != tuple(
                sorted(expected)):
            raise ProtocolError("invalid node scope position order")
        return tuple(rows)

    def log_streams(
        self, chat_id: str, *, expected_capture: str | None = None,
        cursor: str | None = None, limit: int = 1_000,
        max_bytes: int = 256 * 1024,
    ) -> ClientLogStreamPage:
        """Return one generation-bound page of nonempty streams for a chat."""
        endpoint = self._discover()
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "expected_capture": expected_capture,
            "chat_id": chat_id,
            "cursor": cursor,
            "limit": limit,
            "max_bytes": max_bytes,
        }
        try:
            parsed = parse_log_stream_request(
                payload,
                database_incarnation=endpoint.binding.database_incarnation)
        except CursorReset:
            raise NodeClientReset("local node continuation reset") from None
        status, value = self._request(
            endpoint, "POST", "/v1/log-streams", payload,
            max_response=MAX_WIRE_RESPONSE_BYTES)
        self._assert_current(endpoint)
        if status != 200:
            return self._capture_error(status, value)
        return self._parse_log_streams(endpoint, parsed, value)

    @staticmethod
    def _parse_log_streams(
        endpoint: _Endpoint, wanted: NodeLogStreamRequest, value: dict,
    ) -> ClientLogStreamPage:
        fields = {
            "protocol_version", "capture", "generation", "health",
            "last_success_ns", "captured_bytes", "chat_id", "streams",
            "cursor", "has_more", "scope_positions",
        }
        if type(value) is not dict or set(value) != fields:
            raise ProtocolError("invalid node log stream response")
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("unsupported node protocol")
        generation = _integer(value["generation"], "capture generation")
        capture = _untoken(value["capture"], "capture", {"db", "generation"})
        _incarnation(capture["db"], endpoint.binding.database_incarnation)
        if (_integer(capture["generation"], "capture generation") != generation
                or (wanted.expected_generation is not None
                    and wanted.expected_generation != generation)):
            raise NodeClientGenerationChanged("local node generation changed")
        health = value["health"]
        if type(health) is not str or health not in HEALTH_VALUES:
            raise ProtocolError("invalid node log stream health")
        success = _integer(value["last_success_ns"], "last success time")
        captured_bytes = _integer(value["captured_bytes"], "captured bytes")
        if captured_bytes > wanted.max_bytes or value["chat_id"] != wanted.chat_id:
            raise ProtocolError("invalid node log stream response")
        raw_streams = value["streams"]
        if (type(raw_streams) is not list or len(raw_streams) > wanted.limit
                or type(value["has_more"]) is not bool):
            raise ProtocolError("invalid node log streams")
        streams = []
        for raw in raw_streams:
            if type(raw) is not dict or set(raw) != {"log_name", "head"}:
                raise ProtocolError("invalid node log stream")
            try:
                streams.append(NodeLogStream(raw["log_name"], raw["head"]))
            except (NodeInputError, TypeError, ValueError):
                raise ProtocolError("invalid node log stream") from None
        names = tuple(row.log_name for row in streams)
        if (names != tuple(sorted(set(names)))
                or any(name <= wanted.after_name for name in names)
                or (value["has_more"] and not streams)):
            raise ProtocolError("invalid node log stream order")
        token = _untoken(
            value["cursor"], "log_stream",
            {"db", "generation", "selection", "after"},
        )
        _incarnation(token["db"], endpoint.binding.database_incarnation)
        after = names[-1] if names else wanted.after_name
        if (_integer(token["generation"], "capture generation") != generation
                or token["selection"] != _log_stream_selection(wanted.chat_id)
                or _parse_cursor_text(token["after"], "log stream") != after):
            raise ProtocolError("log stream continuation changed")
        scopes = LocalNodeClient._stream_scopes(
            value["scope_positions"], wanted.chat_id)
        copied = tuple(streams)
        if _log_stream_charge(wanted.chat_id, copied, scopes) != captured_bytes:
            raise ProtocolError("log stream byte charge mismatch")
        return ClientLogStreamPage(
            _binding(endpoint), value["capture"], generation, health, success,
            wanted.chat_id, copied, value["cursor"], value["has_more"],
            scopes, captured_bytes,
        )

    @staticmethod
    def _stream_scopes(
        value: Any, chat_id: str,
    ) -> tuple[NodeScopePosition, ...]:
        expected = (("chat", chat_id), ("root", ""))
        if type(value) is not list or len(value) != len(expected):
            raise ProtocolError("invalid node scope positions")
        rows = []
        for item in value:
            if type(item) is not dict or set(item) != {
                    "scope_kind", "scope_id", "generation", "pending_reason"}:
                raise ProtocolError("invalid node scope position")
            kind, ident = item["scope_kind"], item["scope_id"]
            generation = _integer(item["generation"], "scope generation")
            pending = item["pending_reason"]
            if ((kind, ident) not in expected
                    or (pending is not None and (
                        type(pending) is not str or not pending
                        or len(pending.encode("utf-8")) > 256))):
                raise ProtocolError("invalid node scope position")
            rows.append(NodeScopePosition(kind, ident, generation, pending))
        if tuple((row.scope_kind, row.scope_id) for row in rows) != expected:
            raise ProtocolError("invalid node scope position order")
        return tuple(rows)

    def changes(self, *, cursor: str | None = None,
                limit: int = MAX_CHANGE_PAGE) -> ClientChangePage:
        endpoint = self._discover()
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "cursor": cursor,
            "limit": limit,
        }
        try:
            _incarnation_value, after, validated_limit = parse_change_request(
                payload,
                database_incarnation=endpoint.binding.database_incarnation)
        except CursorReset:
            raise NodeClientReset("local node continuation reset") from None
        status, value = self._request(
            endpoint, "POST", "/v1/changes", payload,
            max_response=MAX_WIRE_RESPONSE_BYTES)
        self._assert_current(endpoint)
        if status != 200:
            if status == 409 and value == {"error": "reset_required"}:
                raise NodeClientReset("local node continuation reset")
            if status in (400, 404):
                raise ProtocolError("local node rejected request")
            raise NodeClientUnavailable("local node request unavailable")
        return self._parse_changes(
            endpoint, value, after=after, limit=validated_limit)

    @staticmethod
    def _parse_changes(endpoint: _Endpoint, value: dict, *, after: int,
                       limit: int) -> ClientChangePage:
        fields = {
            "protocol_version", "status", "minimum_cursor", "current_cursor",
            "cursor", "has_more", "changes",
        }
        if type(value) is not dict or set(value) != fields:
            raise ProtocolError("invalid node change response")
        if (value["protocol_version"] != PROTOCOL_VERSION
                or value["status"] not in ("ok", "reset_required")
                or type(value["has_more"]) is not bool
                or type(value["changes"]) is not list
                or len(value["changes"]) > limit):
            raise ProtocolError("invalid node change response")
        positions = {}
        for name in ("minimum_cursor", "current_cursor", "cursor"):
            token = _untoken(value[name], "change", {"db", "after"})
            _incarnation(token["db"], endpoint.binding.database_incarnation)
            positions[name] = _integer(token["after"], "change continuation")
        changes = []
        for item in value["changes"]:
            if type(item) is not dict or set(item) != {
                    "generation", "scope_kind", "scope_id", "kind"}:
                raise ProtocolError("invalid node change")
            generation = _integer(item["generation"], "change generation")
            kind, ident, change_kind = (
                item["scope_kind"], item["scope_id"], item["kind"])
            valid_scope = kind == "root" and ident == ""
            if kind == "chat":
                try:
                    valid_scope = NodeVisibility(ident, True).chat_id == ident
                except (NodeInputError, TypeError, ValueError):
                    valid_scope = False
            if valid_scope and change_kind == "inputs":
                changes.append(ClientChange(
                    generation, kind, ident, change_kind))
            else:
                raise ProtocolError("invalid node change")
        if value["status"] == "reset_required":
            if changes or value["has_more"] or positions["cursor"] != after:
                raise ProtocolError("invalid reset response")
        elif (positions["minimum_cursor"] > positions["current_cursor"]
              or positions["cursor"] < after
              or positions["cursor"] > positions["current_cursor"]
              or (not changes and positions["cursor"] != after)
              or (changes and positions["cursor"] <= after)
              or (value["has_more"]
                  and positions["cursor"] >= positions["current_cursor"])
              or (not value["has_more"]
                  and positions["cursor"] != positions["current_cursor"])):
            raise ProtocolError("invalid change cursor")
        return ClientChangePage(
            _binding(endpoint), value["status"], value["minimum_cursor"],
            value["current_cursor"], value["cursor"], value["has_more"],
            tuple(changes),
        )


__all__ = [
    "CLIENT_TIMEOUT_S", "ClientCapture", "ClientCaptureRequest",
    "ClientChange", "ClientChangePage", "ClientLogPage", "ClientLogRequest",
    "ClientLogStreamPage",
    "LocalNodeClient", "NodeBinding", "NodeClientGenerationChanged",
    "NodeClientReset", "NodeClientUnavailable",
]
