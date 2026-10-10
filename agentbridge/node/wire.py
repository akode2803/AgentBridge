"""Strict JSON wire mapping for bounded local-node captures and changes."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .admission import (
    NodeCapture, NodeCaptureRequest, NodeChangePage, NodeInputError,
    NodeLogRequest,
)
from .protocol import PROTOCOL_VERSION, ProtocolError

MAX_WIRE_REQUEST_BYTES = 64 * 1024
MAX_WIRE_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_WIRE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CURSOR_BYTES = 8 * 1024


class CursorReset(ProtocolError):
    """An opaque continuation belongs to another/replaced local replica."""


def _token(kind: str, **fields: Any) -> str:
    raw = json.dumps(
        {"v": PROTOCOL_VERSION, "kind": kind, **fields},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _untoken(value: Any, kind: str, fields: set[str]) -> dict[str, Any]:
    if type(value) is not str or not value:
        raise ProtocolError("invalid continuation")
    try:
        if len(value.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise ProtocolError("invalid continuation")
        raw = value.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("invalid continuation") from None
    if (type(decoded) is not dict or set(decoded) != {"v", "kind", *fields}
            or type(decoded["v"]) is not int
            or decoded["v"] != PROTOCOL_VERSION or decoded["kind"] != kind):
        raise ProtocolError("invalid continuation")
    return decoded


def _log_selection(chat_id: str, log_name: str) -> str:
    try:
        raw = json.dumps(
            (chat_id, log_name), ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid log request") from None
    return hashlib.sha256(raw).hexdigest()


def _cursor_text(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(
        b"=",
    ).decode("ascii")


def _parse_cursor_text(value: Any) -> str:
    if type(value) is not str:
        raise ProtocolError("invalid visibility continuation")
    try:
        raw = value.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        return base64.b64decode(raw, altchars=b"-_", validate=True).decode("utf-8")
    except (UnicodeError, ValueError):
        raise ProtocolError("invalid visibility continuation") from None


def _incarnation(value: Any, expected: str) -> None:
    if type(value) is not str or value != expected:
        raise CursorReset("continuation database changed")


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ProtocolError(f"invalid {name}")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ProtocolError(f"invalid {name}")
    return tuple(value)


_CAPTURE_FIELDS = {
    "protocol_version", "expected_capture", "exact_document_paths",
    "document_prefixes", "logs", "include_visibility", "visibility_cursor",
    "visibility_limit", "frontier_names", "max_documents", "max_log_rows",
    "max_bytes",
}
_LOG_FIELDS = {"chat_id", "log_name", "cursor", "limit"}


def parse_capture_request(value: Any, *, database_incarnation: str) -> NodeCaptureRequest:
    if type(value) is not dict or set(value) != _CAPTURE_FIELDS:
        raise ProtocolError("invalid capture request")
    if (type(value["protocol_version"]) is not int
            or value["protocol_version"] != PROTOCOL_VERSION):
        raise ProtocolError("unsupported node protocol")
    expected = value["expected_capture"]
    expected_generation = None
    if expected is not None:
        token = _untoken(expected, "capture", {"db", "generation"})
        _incarnation(token["db"], database_incarnation)
        expected_generation = _integer(token["generation"], "capture generation")

    def bind_cursor_generation(token: dict[str, Any]) -> None:
        nonlocal expected_generation
        generation = _integer(token["generation"], "capture generation")
        if expected_generation is None:
            expected_generation = generation
        elif expected_generation != generation:
            raise ProtocolError("continuation capture changed")

    raw_logs = value["logs"]
    if type(raw_logs) is not list:
        raise ProtocolError("invalid log requests")
    logs = []
    for raw in raw_logs:
        if type(raw) is not dict or set(raw) != _LOG_FIELDS:
            raise ProtocolError("invalid log request")
        chat_id, log_name = raw["chat_id"], raw["log_name"]
        after = 0
        if raw["cursor"] is not None:
            token = _untoken(
                raw["cursor"], "log",
                {"db", "generation", "selection", "after"})
            _incarnation(token["db"], database_incarnation)
            if token["selection"] != _log_selection(chat_id, log_name):
                raise ProtocolError("continuation selection changed")
            bind_cursor_generation(token)
            after = _integer(token["after"], "log continuation")
        logs.append(NodeLogRequest(chat_id, log_name, after, raw["limit"]))
    visibility_after = ""
    if value["visibility_cursor"] is not None:
        token = _untoken(
            value["visibility_cursor"], "visibility",
            {"db", "generation", "after"})
        _incarnation(token["db"], database_incarnation)
        bind_cursor_generation(token)
        visibility_after = _parse_cursor_text(token["after"])
    max_bytes = _integer(value["max_bytes"], "capture byte limit")
    if max_bytes > MAX_WIRE_CAPTURE_BYTES:
        raise ProtocolError("capture byte limit exceeds wire maximum")
    try:
        return NodeCaptureRequest(
            exact_document_paths=_strings(
                value["exact_document_paths"], "exact document paths"),
            document_prefixes=_strings(value["document_prefixes"], "document prefixes"),
            log_requests=tuple(logs),
            include_visibility=value["include_visibility"],
            visibility_after=visibility_after,
            visibility_limit=value["visibility_limit"],
            frontier_names=_strings(value["frontier_names"], "frontier names"),
            expected_generation=expected_generation,
            max_documents=value["max_documents"],
            max_log_rows=value["max_log_rows"],
            max_bytes=max_bytes,
        )
    except NodeInputError as exc:
        raise ProtocolError(str(exc)) from None


def _blob(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def capture_response(value: NodeCapture) -> dict[str, Any]:
    if type(value) is not NodeCapture:
        raise ProtocolError("invalid node capture")
    db = value.database_incarnation
    return {
        "protocol_version": PROTOCOL_VERSION,
        "capture": _token("capture", db=db, generation=value.generation),
        "generation": value.generation,
        "health": value.health,
        "last_success_ns": value.last_success_ns,
        "captured_bytes": value.captured_bytes,
        "documents": [
            {"path": row.path, "seq": row.seq, "deleted": row.deleted,
             "payload": _blob(row.payload)} for row in value.documents
        ],
        "logs": [
            {"chat_id": page.request.chat_id, "log_name": page.request.log_name,
             "cursor": _token(
                 "log", db=db, generation=value.generation,
                 selection=_log_selection(
                     page.request.chat_id, page.request.log_name,
                 ), after=page.cursor),
             "has_more": page.has_more,
             "rows": [{"id": row.id, "payload": _blob(row.payload)}
                      for row in page.rows]}
            for page in value.log_pages
        ],
        "visibility": list(value.visibility),
        "visibility_cursor": _token(
            "visibility", db=db, generation=value.generation,
            after=_cursor_text(value.visibility_cursor)),
        "visibility_has_more": value.visibility_has_more,
        "frontiers": [
            {"name": row.name, "epoch": row.epoch, "cursor": row.cursor,
             "minimum_cursor": row.minimum_cursor} for row in value.frontiers
        ],
        "scope_positions": [
            {"scope_kind": row.scope_kind, "scope_id": row.scope_id,
             "generation": row.generation, "pending_reason": row.pending_reason}
            for row in value.scope_positions
        ],
    }


_CHANGE_FIELDS = {"protocol_version", "cursor", "limit"}


def parse_change_request(value: Any, *, database_incarnation: str) -> tuple[str, int, int]:
    if type(value) is not dict or set(value) != _CHANGE_FIELDS:
        raise ProtocolError("invalid change request")
    if (type(value["protocol_version"]) is not int
            or value["protocol_version"] != PROTOCOL_VERSION):
        raise ProtocolError("unsupported node protocol")
    cursor = value["cursor"]
    if cursor is None:
        after = 0
    else:
        token = _untoken(cursor, "change", {"db", "after"})
        _incarnation(token["db"], database_incarnation)
        after = _integer(token["after"], "change continuation")
    limit = _integer(value["limit"], "change page limit")
    return database_incarnation, after, limit


def change_response(value: NodeChangePage) -> dict[str, Any]:
    if type(value) is not NodeChangePage:
        raise ProtocolError("invalid change page")
    db = value.database_incarnation
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": value.status,
        "minimum_cursor": _token("change", db=db, after=value.minimum_cursor),
        "current_cursor": _token("change", db=db, after=value.current_cursor),
        "cursor": _token("change", db=db, after=value.after_cursor),
        "has_more": value.has_more,
        "changes": [
            {"generation": row.generation, "scope_kind": row.scope_kind,
             "scope_id": row.scope_id, "kind": row.kind}
            for row in value.changes
        ],
    }

