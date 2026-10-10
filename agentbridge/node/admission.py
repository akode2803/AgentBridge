"""Bounded raw-input values for the shadow local node.

These values carry provider observations, never canonical authority decisions.
They are detached from provider/client objects before SQLite staging so callers
cannot execute code while the node transaction is open.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

MAX_BATCH_DOCUMENTS = 100_000
MAX_BATCH_LOG_ROWS = 100_000
MAX_BATCH_VISIBILITY = 100_000
MAX_BATCH_FRONTIERS = 128
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_STAGED_DOCUMENTS = 1_000_000
MAX_STAGED_LOG_ROWS = 10_000_000
MAX_STAGED_VISIBILITY = 200_000
MAX_STAGED_CHUNKS = 4_096
MAX_RECOVERY_SCOPES = 20_000
MAX_RECOVERY_WORK = 4_096
MAX_RECOVERY_MANIFEST_PAGE = 256
MAX_RECOVERY_STREAMS = 200_000
MAX_RECOVERY_OPAQUE_BYTES = 8_192
MAX_RECOVERY_METADATA_BYTES = 32 * 1024 * 1024
MAX_STAGED_BYTES = 512 * 1024 * 1024
MAX_CAPTURE_DOCUMENTS = 20_000
MAX_CAPTURE_LOG_REQUESTS = 64
MAX_CAPTURE_LOG_ROWS = 20_000
MAX_CAPTURE_CHAT_IDS = 20_000
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_CHANGE_PAGE = 1_000
MAX_CHANGE_ROWS = 4_096
MAX_ACTIVE_CANDIDATES = 4
MAX_GENERATION_HISTORY = 1_024
MAX_PATH_BYTES = 4_096
MAX_ID_BYTES = 1_024
MAX_LOG_NAME_BYTES = 4_096
MAX_INTEGER = 2**63 - 1
_MODES = frozenset({"delta", "replace"})


class NodeInputError(ValueError):
    """A detached provider batch or capture request is invalid."""


class NodeGenerationChanged(RuntimeError):
    """A candidate/capture no longer refers to the admitted generation."""


class NodeCaptureOverflow(OverflowError):
    """A bounded capture could not fit inside the declared response budget."""


def _integer(value: object, name: str, *, minimum: int = 0,
             maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise NodeInputError(f"invalid {name}")
    return value


def _text(value: object, name: str, *, max_bytes: int = MAX_ID_BYTES,
          allow_empty: bool = False) -> str:
    if (type(value) is not str or (not value and not allow_empty)
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise NodeInputError(f"invalid {name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise NodeInputError(f"invalid {name}") from None
    if len(encoded) > max_bytes:
        raise NodeInputError(f"invalid {name}")
    return value


def _raw_text(value: object, name: str, *, max_bytes: int = MAX_ID_BYTES,
              allow_empty: bool = False) -> str:
    if type(value) is not str or (not value and not allow_empty) or "\x00" in value:
        raise NodeInputError(f"invalid {name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise NodeInputError(f"invalid {name}") from None
    if len(encoded) > max_bytes:
        raise NodeInputError(f"invalid {name}")
    return value


def _path(value: object, name: str = "document path", *, prefix: bool = False) -> str:
    path = _raw_text(value, name, max_bytes=MAX_PATH_BYTES, allow_empty=prefix)
    if not path and prefix:
        return path
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        raise NodeInputError(f"invalid {name}")
    pieces = path.split("/")
    if len(pieces) > 32 or any(not piece or piece in (".", "..") for piece in pieces):
        raise NodeInputError(f"invalid {name}")
    return path


def _payload(value: object, name: str, *, allow_none: bool = False) -> bytes | None:
    if value is None and allow_none:
        return None
    if type(value) is not bytes:
        raise NodeInputError(f"invalid {name}")
    return bytes(value)


@dataclass(frozen=True)
class NodeDocument:
    path: str
    seq: int
    deleted: bool
    payload: bytes | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path))
        _integer(self.seq, "document sequence")
        if type(self.deleted) is not bool:
            raise NodeInputError("invalid document deletion flag")
        payload = _payload(self.payload, "document payload", allow_none=True)
        if self.deleted != (payload is None):
            raise NodeInputError("deleted documents require an absent payload")
        object.__setattr__(self, "payload", payload)


@dataclass(frozen=True)
class NodeLogRow:
    id: int
    chat_id: str
    log_name: str
    payload: bytes

    def __post_init__(self) -> None:
        _integer(self.id, "log row id", minimum=1)
        object.__setattr__(self, "chat_id", _raw_text(self.chat_id, "chat id"))
        object.__setattr__(self, "log_name", _raw_text(
            self.log_name, "log name", max_bytes=MAX_LOG_NAME_BYTES,
        ))
        object.__setattr__(self, "payload", _payload(self.payload, "log payload"))


@dataclass(frozen=True)
class NodeVisibility:
    chat_id: str
    visible: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_id", _raw_text(self.chat_id, "chat id"))
        if type(self.visible) is not bool:
            raise NodeInputError("invalid visibility flag")


@dataclass(frozen=True)
class NodeFrontier:
    name: str
    epoch: str | None
    cursor: int | None
    minimum_cursor: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "frontier name"))
        if self.epoch is not None:
            object.__setattr__(self, "epoch", _text(self.epoch, "frontier epoch"))
        for label, value in (("frontier cursor", self.cursor),
                             ("frontier minimum", self.minimum_cursor)):
            if value is not None:
                _integer(value, label)
        if (self.cursor is not None and self.minimum_cursor is not None
                and self.minimum_cursor > self.cursor):
            raise NodeInputError("frontier minimum exceeds cursor")


@dataclass(frozen=True)
class NodeInputBatch:
    documents: tuple[NodeDocument, ...] = ()
    log_rows: tuple[NodeLogRow, ...] = ()
    visibility: tuple[NodeVisibility, ...] = ()
    frontiers: tuple[NodeFrontier, ...] = ()
    documents_mode: str = "delta"
    logs_mode: str = "delta"
    visibility_mode: str = "delta"
    frontiers_mode: str = "delta"

    def __post_init__(self) -> None:
        families = (
            (self.documents, NodeDocument, MAX_BATCH_DOCUMENTS, "documents"),
            (self.log_rows, NodeLogRow, MAX_BATCH_LOG_ROWS, "log rows"),
            (self.visibility, NodeVisibility, MAX_BATCH_VISIBILITY, "visibility rows"),
            (self.frontiers, NodeFrontier, MAX_BATCH_FRONTIERS, "frontiers"),
        )
        for values, expected, maximum, name in families:
            if type(values) is not tuple or len(values) > maximum or any(
                    type(value) is not expected for value in values):
                raise NodeInputError(f"invalid {name}")
        for mode in (
            self.documents_mode, self.logs_mode, self.visibility_mode,
            self.frontiers_mode,
        ):
            if type(mode) is not str or mode not in _MODES:
                raise NodeInputError("invalid batch replacement mode")
        if len({row.path for row in self.documents}) != len(self.documents):
            raise NodeInputError("duplicate document path")
        if len({row.id for row in self.log_rows}) != len(self.log_rows):
            raise NodeInputError("duplicate log row id")
        if len({row.chat_id for row in self.visibility}) != len(self.visibility):
            raise NodeInputError("duplicate visibility row")
        if len({row.name for row in self.frontiers}) != len(self.frontiers):
            raise NodeInputError("duplicate frontier")
        if self.byte_size > MAX_BATCH_BYTES:
            raise NodeInputError("provider batch exceeds byte budget")

    @property
    def byte_size(self) -> int:
        total = 0
        for row in self.documents:
            total += len(row.path.encode("utf-8")) + 24
            total += 0 if row.payload is None else len(row.payload)
        for row in self.log_rows:
            total += (len(row.chat_id.encode("utf-8"))
                      + len(row.log_name.encode("utf-8")) + len(row.payload) + 24)
        for row in self.visibility:
            total += len(row.chat_id.encode("utf-8")) + 8
        for row in self.frontiers:
            total += len(row.name.encode("utf-8")) + 32
            total += 0 if row.epoch is None else len(row.epoch.encode("utf-8"))
        return total


@dataclass(frozen=True)
class NodeLogRequest:
    chat_id: str
    log_name: str
    after_id: int = 0
    limit: int = 1_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_id", _raw_text(self.chat_id, "chat id"))
        object.__setattr__(self, "log_name", _raw_text(
            self.log_name, "log name", max_bytes=MAX_LOG_NAME_BYTES,
        ))
        _integer(self.after_id, "log cursor")
        _integer(self.limit, "log limit", minimum=1, maximum=MAX_CAPTURE_LOG_ROWS)


@dataclass(frozen=True)
class NodeCaptureRequest:
    exact_document_paths: tuple[str, ...] = ()
    document_prefixes: tuple[str, ...] = ()
    log_requests: tuple[NodeLogRequest, ...] = ()
    include_visibility: bool = False
    visibility_after: str = ""
    visibility_limit: int = 1_000
    frontier_names: tuple[str, ...] = ()
    expected_generation: int | None = None
    max_documents: int = 1_000
    max_log_rows: int = 5_000
    max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.exact_document_paths) is not tuple or len(
                self.exact_document_paths) > 128:
            raise NodeInputError("invalid exact document paths")
        if type(self.document_prefixes) is not tuple or len(self.document_prefixes) > 8:
            raise NodeInputError("invalid document prefixes")
        paths = tuple(_path(value) for value in self.exact_document_paths)
        prefixes = tuple(_path(value, "document prefix", prefix=True)
                         for value in self.document_prefixes)
        if len(set(paths)) != len(paths) or len(set(prefixes)) != len(prefixes):
            raise NodeInputError("duplicate document selector")
        if any(path == prefix or (prefix and path.startswith(prefix + "/"))
               for path in paths for prefix in prefixes):
            raise NodeInputError("overlapping document selector")
        ordered = sorted(prefixes)
        if any(not left or right.startswith(left + "/")
               for left, right in zip(ordered, ordered[1:])):
            raise NodeInputError("overlapping document prefix")
        object.__setattr__(self, "exact_document_paths", paths)
        object.__setattr__(self, "document_prefixes", prefixes)
        if type(self.log_requests) is not tuple or len(
                self.log_requests) > MAX_CAPTURE_LOG_REQUESTS or any(
                    type(value) is not NodeLogRequest for value in self.log_requests):
            raise NodeInputError("invalid log requests")
        if len({(value.chat_id, value.log_name) for value in self.log_requests}) != len(
                self.log_requests):
            raise NodeInputError("duplicate log request")
        if type(self.include_visibility) is not bool:
            raise NodeInputError("invalid visibility selection")
        object.__setattr__(self, "visibility_after", _raw_text(
            self.visibility_after, "visibility cursor", allow_empty=True))
        _integer(self.visibility_limit, "visibility limit", minimum=1,
                 maximum=MAX_CAPTURE_CHAT_IDS)
        if type(self.frontier_names) is not tuple or len(self.frontier_names) > 128:
            raise NodeInputError("invalid frontier names")
        names = tuple(_text(value, "frontier name") for value in self.frontier_names)
        if len(set(names)) != len(names):
            raise NodeInputError("duplicate frontier name")
        object.__setattr__(self, "frontier_names", names)
        if self.expected_generation is not None:
            _integer(self.expected_generation, "expected generation")
        _integer(self.max_documents, "document limit", maximum=MAX_CAPTURE_DOCUMENTS)
        _integer(self.max_log_rows, "log row limit", maximum=MAX_CAPTURE_LOG_ROWS)
        _integer(self.max_bytes, "capture byte limit", maximum=MAX_CAPTURE_BYTES)
        if sum(value.limit for value in self.log_requests) > self.max_log_rows:
            raise NodeInputError("log requests exceed aggregate row limit")


@dataclass(frozen=True)
class NodeScopePosition:
    scope_kind: str
    scope_id: str
    generation: int
    pending_reason: str | None


@dataclass(frozen=True)
class NodeProviderCut:
    schema_version: int
    index_epoch: str
    source_epoch: str
    account_id: str
    role: str
    minimum_cursor: int
    cursor: int

    def __post_init__(self) -> None:
        _integer(self.schema_version, "provider schema version", minimum=1)
        _text(self.index_epoch, "provider index epoch")
        _text(self.source_epoch, "provider source epoch")
        _text(self.account_id, "provider account id")
        _text(self.role, "provider role")
        _integer(self.minimum_cursor, "provider minimum cursor")
        _integer(self.cursor, "provider cursor")
        if self.minimum_cursor > self.cursor:
            raise NodeInputError("provider minimum exceeds cursor")


@dataclass(frozen=True)
class NodeRecoveryScope:
    scope_kind: str
    scope_id: str
    generation: int

    def __post_init__(self) -> None:
        if self.scope_kind not in ("root", "chat") or type(self.scope_kind) is not str:
            raise NodeInputError("invalid recovery scope")
        if self.scope_kind == "root" and self.scope_id != "":
            raise NodeInputError("invalid recovery root")
        if self.scope_kind == "chat":
            _text(self.scope_id, "recovery chat")
        _integer(self.generation, "recovery scope generation")


@dataclass(frozen=True)
class NodeRecoveryWork:
    work_id: str
    family: str
    scope_kind: str
    scope_id: str
    selection: bytes
    checkpoint: bytes = b""

    def __post_init__(self) -> None:
        _text(self.work_id, "recovery work id", max_bytes=256)
        if type(self.family) is not str or self.family not in (
                "docs", "logs", "visibility", "frontiers"):
            raise NodeInputError("invalid recovery family")
        NodeRecoveryScope(self.scope_kind, self.scope_id, 0)
        for name in ("selection", "checkpoint"):
            value = _payload(getattr(self, name), f"recovery {name}")
            if len(value) > MAX_RECOVERY_OPAQUE_BYTES:
                raise NodeInputError(f"recovery {name} exceeds budget")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class NodeRecoveryPlan:
    database_incarnation: str
    identity_digest: str
    base_generation: int
    replay_cursor: int
    provider_cut: NodeProviderCut
    scopes: tuple[NodeRecoveryScope, ...]
    work: tuple[NodeRecoveryWork, ...]

    def __post_init__(self) -> None:
        _text(self.database_incarnation, "database incarnation")
        _text(self.identity_digest, "identity digest")
        _integer(self.base_generation, "recovery base generation")
        _integer(self.replay_cursor, "recovery replay cursor")
        if type(self.provider_cut) is not NodeProviderCut:
            raise NodeInputError("invalid recovery provider cut")
        if not self.provider_cut.minimum_cursor <= self.replay_cursor <= self.provider_cut.cursor:
            raise NodeInputError("recovery replay outside provider cut")
        if (type(self.scopes) is not tuple or len(self.scopes) > MAX_RECOVERY_SCOPES
                or any(type(scope) is not NodeRecoveryScope for scope in self.scopes)):
            raise NodeInputError("invalid recovery scopes")
        keys = {(s.scope_kind, s.scope_id) for s in self.scopes}
        if len(keys) != len(self.scopes):
            raise NodeInputError("duplicate recovery scope")
        if ("root", "") not in keys:
            raise NodeInputError("recovery requires whole-root scope")
        if (type(self.work) is not tuple or len(self.work) > MAX_RECOVERY_WORK
                or any(type(item) is not NodeRecoveryWork for item in self.work)):
            raise NodeInputError("invalid recovery work")
        if len({item.work_id for item in self.work}) != len(self.work):
            raise NodeInputError("duplicate recovery work id")
        if any((item.scope_kind, item.scope_id) not in keys for item in self.work):
            raise NodeInputError("recovery work outside target scopes")
        if not any(item.family == "frontiers" for item in self.work):
            raise NodeInputError("recovery requires provider frontier work")
        size = sum(64 + len(s.scope_id.encode("utf-8")) for s in self.scopes)
        size += sum(128 + len(w.work_id.encode("utf-8")) + len(w.scope_id.encode(
            "utf-8")) + len(w.selection) + 2 * len(w.checkpoint) for w in self.work)
        if size > MAX_RECOVERY_METADATA_BYTES:
            raise NodeInputError("recovery metadata exceeds budget")


@dataclass(frozen=True)
class NodeRecoveryWorkState:
    work_id: str
    family: str
    scope_kind: str
    scope_id: str
    selection: bytes
    checkpoint: bytes
    outcome: str
    page_count: int
    row_count: int


@dataclass(frozen=True)
class NodeRecoveryStreamHead:
    chat_id: str
    log_name: str
    head: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_id", _raw_text(self.chat_id, "chat id"))
        object.__setattr__(self, "log_name", _raw_text(
            self.log_name, "log name", max_bytes=MAX_LOG_NAME_BYTES,
        ))
        _integer(self.head, "recovery stream head", minimum=1)


@dataclass(frozen=True)
class NodeRecoveryManifestState:
    family: str
    checkpoint: bytes
    outcome: str
    page_count: int
    row_count: int


@dataclass(frozen=True)
class NodeRecoveryStreamState:
    chat_id: str
    log_name: str
    head: int
    cursor: int
    outcome: str
    page_count: int
    row_count: int


@dataclass(frozen=True)
class NodeRecoveryState:
    recovery_id: str
    generation: int
    plan: NodeRecoveryPlan
    health_owner_token: int
    replay_cursor: int
    target_cursor: int
    state: str
    work: tuple[NodeRecoveryWorkState, ...]
    proof_revision: int = 0
    manifests: tuple[NodeRecoveryManifestState, ...] = ()
    stream_count: int = 0
    pending_stream_count: int = 0


@dataclass(frozen=True)
class NodeLogPage:
    request: NodeLogRequest
    rows: tuple[NodeLogRow, ...]
    cursor: int
    has_more: bool


@dataclass(frozen=True)
class NodeCapture:
    database_incarnation: str
    generation: int
    health: str
    last_success_ns: int
    documents: tuple[NodeDocument, ...]
    log_pages: tuple[NodeLogPage, ...]
    visibility: tuple[str, ...]
    visibility_cursor: str
    visibility_has_more: bool
    frontiers: tuple[NodeFrontier, ...]
    scope_positions: tuple[NodeScopePosition, ...]
    captured_bytes: int


@dataclass(frozen=True)
class NodeChange:
    seq: int
    generation: int
    scope_kind: str
    scope_id: str
    kind: str


@dataclass(frozen=True)
class NodeChangePage:
    status: str
    database_incarnation: str
    minimum_cursor: int
    current_cursor: int
    after_cursor: int
    changes: tuple[NodeChange, ...]
    has_more: bool


def detached_batch(value: NodeInputBatch) -> NodeInputBatch:
    """Copy and revalidate a batch at the SQLite ownership boundary."""
    if type(value) is not NodeInputBatch:
        raise NodeInputError("expected detached node input batch")
    families = (
        (value.documents, NodeDocument),
        (value.log_rows, NodeLogRow),
        (value.visibility, NodeVisibility),
        (value.frontiers, NodeFrontier),
    )
    if any(type(rows) is not tuple or any(type(row) is not expected for row in rows)
           for rows, expected in families):
        raise NodeInputError("invalid detached node input batch")
    return NodeInputBatch(
        documents=tuple(NodeDocument(
            row.path, row.seq, row.deleted, row.payload) for row in value.documents),
        log_rows=tuple(NodeLogRow(
            row.id, row.chat_id, row.log_name, row.payload) for row in value.log_rows),
        visibility=tuple(NodeVisibility(
            row.chat_id, row.visible) for row in value.visibility),
        frontiers=tuple(NodeFrontier(
            row.name, row.epoch, row.cursor, row.minimum_cursor)
            for row in value.frontiers),
        documents_mode=value.documents_mode,
        logs_mode=value.logs_mode,
        visibility_mode=value.visibility_mode,
        frontiers_mode=value.frontiers_mode,
    )


def detached_capture_request(value: NodeCaptureRequest) -> NodeCaptureRequest:
    """Copy and revalidate a capture request before opening a read cut."""
    if type(value) is not NodeCaptureRequest:
        raise NodeInputError("expected node capture request")
    if type(value.log_requests) is not tuple or any(
            type(row) is not NodeLogRequest for row in value.log_requests):
        raise NodeInputError("invalid log requests")
    logs = tuple(NodeLogRequest(
        row.chat_id, row.log_name, row.after_id, row.limit)
        for row in value.log_requests)
    return NodeCaptureRequest(
        exact_document_paths=value.exact_document_paths,
        document_prefixes=value.document_prefixes,
        log_requests=logs,
        include_visibility=value.include_visibility,
        visibility_after=value.visibility_after,
        visibility_limit=value.visibility_limit,
        frontier_names=value.frontier_names,
        expected_generation=value.expected_generation,
        max_documents=value.max_documents,
        max_log_rows=value.max_log_rows,
        max_bytes=value.max_bytes,
    )

