"""Pure, bounded conversion of one node generation into detached raw inputs.

The result is not a visibility or membership verdict.  It is a complete,
generation-bound collection of the raw dependencies declared by one canonical
page source.  Publication into the GUI Store and canonical interpretation are
separate operations with their own finalization fences.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from ..store import source_selectors
from .admission import MAX_CAPTURE_LOG_REQUESTS, NodeScopePosition
from .client import (
    ClientCapture, ClientCaptureRequest, ClientLogRequest, ClientLogStreamPage,
    LocalNodeClient, NodeBinding,
)
from .protocol import ProtocolError

MAX_INPUT_DOCUMENTS = 20_000
MAX_INPUT_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_INPUT_STREAMS = 4_096
MAX_INPUT_LOG_ROWS = 100_000
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_INPUT_PAGES = 20_000
MAX_JSON_DEPTH = 128
MAX_JSON_NODES = 2_000_000
LOG_PAGE_ROWS = 256
LOG_STREAM_PAGE_ROWS = 1_000
LOG_STREAM_PAGE_BYTES = 256 * 1024
WIRE_CAPTURE_BYTES = 8 * 1024 * 1024


class DetachedInputError(ValueError):
    """The selected node inputs cannot form one complete detached snapshot."""


class DetachedInputOverflow(OverflowError):
    """The selected node inputs exceed an explicit local conversion budget."""


@dataclass(frozen=True)
class FrozenJSONObject:
    items: tuple[tuple[str, "FrozenJSON"], ...]


@dataclass(frozen=True)
class FrozenJSONArray:
    items: tuple["FrozenJSON", ...]


FrozenJSON = (
    type(None) | bool | int | float | str | FrozenJSONObject | FrozenJSONArray
)


@dataclass(frozen=True)
class DetachedDocument:
    path: str
    seq: int
    deleted: bool
    payload: bytes | None
    value: FrozenJSON | None


@dataclass(frozen=True)
class DetachedLogRow:
    provider_id: int
    chat_id: str
    log_name: str
    payload: bytes
    value: FrozenJSONObject | None
    accepted: bool
    rejection_reason: str | None
    message_id: str | None
    ns: int | None
    sender: str | None
    kind: str | None


@dataclass(frozen=True)
class DetachedLogStream:
    log_name: str
    head: int
    rows: tuple[DetachedLogRow, ...]


@dataclass(frozen=True)
class DetachedNodeInputs:
    definition: source_selectors.Definition
    chat_id: str
    binding: NodeBinding
    capture: str
    generation: int
    health: str
    last_success_ns: int
    scope_positions: tuple[NodeScopePosition, ...]
    documents: tuple[DetachedDocument, ...]
    missing_exact_paths: tuple[str, ...]
    streams: tuple[DetachedLogStream, ...]
    raw_log_rows: int
    accepted_log_rows: int
    rejected_log_rows: int
    input_bytes: int
    request_count: int


@dataclass(frozen=True)
class CaptureLimits:
    max_documents: int = MAX_INPUT_DOCUMENTS
    max_document_bytes: int = MAX_INPUT_DOCUMENT_BYTES
    max_streams: int = MAX_INPUT_STREAMS
    max_log_rows: int = MAX_INPUT_LOG_ROWS
    max_input_bytes: int = MAX_INPUT_BYTES
    max_pages: int = MAX_INPUT_PAGES
    max_json_depth: int = MAX_JSON_DEPTH
    max_json_nodes: int = MAX_JSON_NODES

    def __post_init__(self) -> None:
        values = (
            self.max_documents, self.max_document_bytes, self.max_streams,
            self.max_log_rows, self.max_input_bytes, self.max_pages,
            self.max_json_depth, self.max_json_nodes,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("capture limits must be positive integers")
        if (self.max_documents > MAX_INPUT_DOCUMENTS
                or self.max_document_bytes > MAX_INPUT_DOCUMENT_BYTES
                or self.max_streams > MAX_INPUT_STREAMS
                or self.max_log_rows > MAX_INPUT_LOG_ROWS
                or self.max_input_bytes > MAX_INPUT_BYTES
                or self.max_pages > MAX_INPUT_PAGES
                or self.max_json_depth > MAX_JSON_DEPTH
                or self.max_json_nodes > MAX_JSON_NODES):
            raise ValueError("capture limit exceeds local maximum")


@dataclass(frozen=True)
class _Cut:
    binding: NodeBinding
    capture: str
    generation: int
    scope_positions: tuple[NodeScopePosition, ...]


class _JSONBudget:
    def __init__(self, limits: CaptureLimits) -> None:
        self.limits = limits
        self.nodes = 0

    def freeze(self, value: Any, *, depth: int = 0) -> FrozenJSON:
        if depth > self.limits.max_json_depth:
            raise DetachedInputOverflow("JSON depth exceeds capture budget")
        self.nodes += 1
        if self.nodes > self.limits.max_json_nodes:
            raise DetachedInputOverflow("JSON nodes exceed capture budget")
        if value is None or type(value) in (bool, int, str):
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise DetachedInputError("non-finite JSON number")
            return value
        if type(value) is list:
            return FrozenJSONArray(tuple(
                self.freeze(item, depth=depth + 1) for item in value
            ))
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise DetachedInputError("invalid JSON object key")
            return FrozenJSONObject(tuple(
                (key, self.freeze(item, depth=depth + 1))
                for key, item in value.items()
            ))
        raise DetachedInputError("invalid JSON value")


def thaw_json(value: FrozenJSON) -> Any:
    """Return a new mutable JSON value; never expose captured mutable state."""
    if type(value) is FrozenJSONObject:
        return {key: thaw_json(item) for key, item in value.items}
    if type(value) is FrozenJSONArray:
        return [thaw_json(item) for item in value.items]
    return value


def _decode(payload: bytes, budget: _JSONBudget) -> FrozenJSON:
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                DetachedInputError("non-finite JSON number")
            ),
        )
    except DetachedInputError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DetachedInputError("invalid UTF-8 JSON input") from None
    return budget.freeze(value)


def _definition(value: source_selectors.Definition, client: LocalNodeClient) -> tuple[
        source_selectors.Definition, str, tuple[str, ...], tuple[str, ...]]:
    try:
        definition = source_selectors._definition(value)
        serialized = json.loads(definition.serialized)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise DetachedInputError("invalid canonical source definition") from None
    logs = tuple(row.value for row in definition.selectors if row.kind == "log_chat")
    exact = tuple(row.value for row in definition.selectors if row.kind == "doc_exact")
    prefixes = tuple(row.value for row in definition.selectors if row.kind == "doc_prefix")
    root_identity = serialized[0]
    try:
        root = json.loads(root_identity)
    except (TypeError, ValueError):
        raise DetachedInputError("invalid canonical source root") from None
    identity = client.identity
    if (type(root) is not list or len(root) != 4
            or root[:2] != ["local-root-v1", "supabase"]
            or type(root[2]) is not str or type(root[3]) is not str
            or root[2] != identity.provider_endpoint
            or "supabase://" + root[3] != identity.root):
        raise DetachedInputError("source belongs to another node root")
    if (len(logs) != 1 or serialized[1] != "local-canonical-page-v1"
            or any(row.kind not in {"doc_exact", "doc_prefix", "log_chat"}
                   for row in definition.selectors)):
        raise DetachedInputError("source is not one canonical page selection")
    chat = logs[0]
    expected = source_selectors.definition(root_identity, (
        source_selectors.Selector("doc_prefix", "users"),
        source_selectors.Selector("doc_prefix", "lifecycle"),
        source_selectors.Selector("doc_exact", f"chats/{chat}/meta.json"),
        *(source_selectors.Selector(
            "doc_prefix", f"chats/{chat}/overlays/{kind}")
          for kind in ("edits", "redactions", "reactions", "pins", "state")),
        source_selectors.Selector("doc_prefix", f"chats/{chat}/keys"),
        source_selectors.Selector("log_chat", chat),
    ), build="local-canonical-page-v1")
    if definition != expected:
        raise DetachedInputError("source selection differs from canonical page inputs")
    return definition, chat, exact, prefixes


def _ready_scopes(
    scopes: tuple[NodeScopePosition, ...], chat_id: str,
) -> tuple[NodeScopePosition, ...]:
    expected = (("chat", chat_id), ("root", ""))
    if tuple((row.scope_kind, row.scope_id) for row in scopes) != expected:
        raise DetachedInputError("node returned incomplete source scopes")
    if any(row.pending_reason is not None for row in scopes):
        raise DetachedInputError("node source scope is pending")
    return scopes


def _cut(value: ClientCapture | ClientLogStreamPage, chat_id: str) -> _Cut:
    if value.generation < 1:
        raise DetachedInputError("local node has no admitted snapshot")
    return _Cut(
        value.binding, value.capture, value.generation,
        _ready_scopes(value.scope_positions, chat_id),
    )


def _same_cut(value: ClientCapture | ClientLogStreamPage, expected: _Cut,
              chat_id: str) -> None:
    if _cut(value, chat_id) != expected:
        raise ProtocolError("local node capture changed during input conversion")


def _input_charge(path_or_name: str, payload: bytes | None) -> int:
    return len(path_or_name.encode("utf-8")) + 32 + (
        0 if payload is None else len(payload)
    )


def _envelope(row, budget: _JSONBudget) -> DetachedLogRow:
    raw = bytes(row.payload)
    try:
        value = _decode(raw, budget)
        if type(value) is not FrozenJSONObject:
            raise DetachedInputError("log envelope is not an object")
        plain = dict(value.items)
        owner = row.log_name.split("@", 1)[0]
        message_id, ns, sender = plain.get("id"), plain.get("ns"), plain.get("from")
        kind = plain.get("kind", "message")
        if type(message_id) is not str or not message_id:
            reason = "invalid_message_id"
        elif type(ns) is not int:
            reason = "invalid_namespace"
        elif type(sender) is not str or sender != owner:
            reason = "wrong_log_owner"
        elif type(kind) is not str or not kind:
            reason = "invalid_message_kind"
        else:
            return DetachedLogRow(
                row.id, row.chat_id, row.log_name, raw, value, True, None,
                message_id, ns, sender, kind,
            )
    except DetachedInputOverflow:
        raise
    except DetachedInputError as exc:
        value = None
        reason = str(exc).replace(" ", "_")
    return DetachedLogRow(
        row.id, row.chat_id, row.log_name, raw, value, False, reason,
        None, None, None, None,
    )


def _capture_documents(client: LocalNodeClient, exact: tuple[str, ...],
                       prefixes: tuple[str, ...], limits: CaptureLimits):
    try:
        return client.capture(ClientCaptureRequest(
            exact_document_paths=exact,
            document_prefixes=prefixes,
            max_documents=limits.max_documents,
            max_log_rows=1,
            max_bytes=limits.max_document_bytes,
        ))
    except OverflowError:
        raise DetachedInputOverflow("document selection exceeds capture budget") from None


def capture_detached_inputs(
    client: LocalNodeClient,
    definition: source_selectors.Definition,
    *,
    limits: CaptureLimits = CaptureLimits(),
) -> DetachedNodeInputs:
    """Capture all declared documents and full exact-chat logs at one cut.

    Every log is replayed from row zero.  A previous high-water mark is never
    used as completeness evidence because lower provider row IDs may be
    admitted late.  Malformed envelopes remain represented as rejected raw
    rows so publication can advance past them without repeatedly rescanning.
    """
    if not hasattr(client, "capture") or not hasattr(client, "log_streams"):
        raise TypeError("expected local node client")
    if type(limits) is not CaptureLimits:
        raise TypeError("expected capture limits")
    definition, chat_id, exact, prefixes = _definition(definition, client)
    budget = _JSONBudget(limits)
    first = _capture_documents(client, exact, prefixes, limits)
    cut = _cut(first, chat_id)
    requests = 1
    input_bytes = 0
    documents = []
    for row in first.documents:
        payload = None if row.payload is None else bytes(row.payload)
        input_bytes += _input_charge(row.path, payload)
        if input_bytes > limits.max_input_bytes:
            raise DetachedInputOverflow("raw inputs exceed capture byte budget")
        if row.deleted:
            value = None
        else:
            try:
                value = _decode(payload or b"", budget)
            except DetachedInputError as exc:
                raise DetachedInputError(
                    "invalid document JSON"
                ) from exc
        documents.append(DetachedDocument(
            row.path, row.seq, row.deleted, payload, value,
        ))
    observed = {row.path for row in first.documents}
    missing_exact = tuple(path for path in exact if path not in observed)

    inventory = []
    inventory_cursor = None
    while True:
        if requests >= limits.max_pages:
            raise DetachedInputOverflow("node page count exceeds capture budget")
        remaining = limits.max_streams - len(inventory)
        if remaining < 1:
            raise DetachedInputOverflow("log stream count exceeds capture budget")
        requests += 1
        page = client.log_streams(
            chat_id, expected_capture=cut.capture, cursor=inventory_cursor,
            limit=min(LOG_STREAM_PAGE_ROWS, remaining),
            max_bytes=LOG_STREAM_PAGE_BYTES,
        )
        _same_cut(page, cut, chat_id)
        input_bytes += sum(
            len(row.log_name.encode("utf-8")) + 32 for row in page.streams
        )
        if input_bytes > limits.max_input_bytes:
            raise DetachedInputOverflow("raw inputs exceed capture byte budget")
        inventory.extend(page.streams)
        if not page.has_more:
            break
        inventory_cursor = page.cursor

    stream_rows: dict[str, list[DetachedLogRow]] = {
        row.log_name: [] for row in inventory
    }
    active = [
        {"name": row.log_name, "head": row.head, "cursor": None}
        for row in inventory
    ]
    raw_rows = accepted = rejected = 0
    while active:
        remaining_rows = limits.max_log_rows - raw_rows
        if remaining_rows < 1:
            raise DetachedInputOverflow("log rows exceed capture budget")
        chunk_size = min(MAX_CAPTURE_LOG_REQUESTS, len(active))
        per_stream = min(
            LOG_PAGE_ROWS,
            max(1, remaining_rows // chunk_size),
        )
        while True:
            chunk = active[:chunk_size]
            request = ClientCaptureRequest(
                expected_capture=cut.capture,
                logs=tuple(ClientLogRequest(
                    chat_id, state["name"], state["cursor"], per_stream,
                ) for state in chunk),
                max_documents=1,
                max_log_rows=min(20_000, per_stream * chunk_size),
                max_bytes=WIRE_CAPTURE_BYTES,
            )
            if requests >= limits.max_pages:
                raise DetachedInputOverflow("node page count exceeds capture budget")
            requests += 1
            try:
                page = client.capture(request)
                break
            except OverflowError:
                if chunk_size > 1:
                    chunk_size = max(1, chunk_size // 2)
                elif per_stream > 1:
                    per_stream = max(1, per_stream // 2)
                else:
                    raise DetachedInputOverflow(
                        "one log row exceeds the wire capture budget"
                    ) from None
        _same_cut(page, cut, chat_id)
        finished = set()
        for state, log_page in zip(chunk, page.logs, strict=True):
            if log_page.request.log_name != state["name"]:
                raise ProtocolError("node log response changed stream selection")
            head = state["head"]
            for row in log_page.rows:
                if row.id > head:
                    raise ProtocolError("node log replay crossed captured head")
                input_bytes += _input_charge(row.log_name, row.payload)
                if input_bytes > limits.max_input_bytes:
                    raise DetachedInputOverflow(
                        "raw inputs exceed capture byte budget"
                    )
                raw_rows += 1
                if raw_rows > limits.max_log_rows:
                    raise DetachedInputOverflow("log rows exceed capture budget")
                converted = _envelope(row, budget)
                stream_rows[row.log_name].append(converted)
                if converted.accepted:
                    accepted += 1
                else:
                    rejected += 1
            state["cursor"] = log_page.cursor
            if not log_page.has_more:
                rows = stream_rows[state["name"]]
                if not rows or rows[-1].provider_id != head:
                    raise ProtocolError("node log replay did not reach captured head")
                finished.add(state["name"])
        active = [
            state for state in active
            if state["name"] not in finished
        ]

    streams = tuple(DetachedLogStream(
        row.log_name, row.head, tuple(stream_rows[row.log_name])
    ) for row in inventory)
    return DetachedNodeInputs(
        definition, chat_id, cut.binding, cut.capture, cut.generation,
        first.health, first.last_success_ns, cut.scope_positions,
        tuple(documents), missing_exact, streams, raw_rows, accepted, rejected,
        input_bytes, requests,
    )


__all__ = [
    "CaptureLimits", "DetachedDocument", "DetachedInputError",
    "DetachedInputOverflow", "DetachedLogRow", "DetachedLogStream",
    "DetachedNodeInputs", "FrozenJSONArray", "FrozenJSONObject",
    "capture_detached_inputs", "thaw_json",
]
