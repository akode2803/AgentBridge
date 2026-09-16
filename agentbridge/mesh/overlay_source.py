"""Publication fences for local overlay inputs, not canonical serving authority.

Background capture may inspect whole signed documents. Foreground selection is
exact-path and budgeted. Neither operation verifies signatures or authorizes a
viewer. A receipt is usable only against its originating live mirror AND its
current SQLite generation; persisted initialized state alone is insufficient.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..store.db import DocumentObservation, DocumentObservationConflict, DocumentPosition, Store
from ..transport.base import Transport
from ..transport.cache import CachingTransport
from ..transport.mirror_observation import (
    MirrorCaptureUnavailable, MirrorExpectedPosition, MirrorPositionValidation, MirrorSelection,
    MirrorSelectionRequest,
)

_KINDS = ("edits", "redactions", "reactions", "pins", "state")


class OverlaySourceUnavailable(RuntimeError):
    """The source is cold, unsupported, stale, pending or over its input budget."""


@dataclass(frozen=True)
class OverlaySourceReceipt:
    chat_id: str
    position: DocumentPosition
    mirror: MirrorExpectedPosition


def _chat(value: str) -> str:
    if (type(value) is not str or not value or len(value.encode("utf-8")) > 256
            or any(c in value for c in ("/", "\\", "\x00", ":")) or value in (".", "..")):
        raise ValueError("invalid overlay chat identity")
    return value


def _source(chat_id: str, mirror: MirrorExpectedPosition) -> str:
    # Stable durable namespace; live instance identity remains in the receipt.
    # A new publisher invalidates old receipts by advancing the Store generation.
    payload = [chat_id, mirror.root_identity, mirror.cache_identity]
    return "overlay-v1:" + hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _match(transport: Transport, expected: MirrorExpectedPosition) -> None:
    result = transport.validate_mirror_position(expected)
    if type(result) is not MirrorPositionValidation:
        raise OverlaySourceUnavailable("invalid_mirror_validation")
    if result.status != "matched":
        raise OverlaySourceUnavailable(result.reason or "mirror_changed")


def publish_overlay_source(
    transport: Transport, store: Store, chat_id: str, *,
    max_documents: int = 20_000, max_bytes: int = 16 * 1024 * 1024,
    max_examined_paths: int = 100_000,
) -> OverlaySourceReceipt:
    """One bounded background rebuild; never called from the chat read path.

    All five overlay classes come from one mirror cut. Full publication retires
    removed rows; exact selection represents absence explicitly. Failure after invalidation leaves a pending source;
    callers schedule a new bounded attempt rather than retrying in this function.
    Bare folder transport is explicitly unsupported by this mirror-current gate.
    """
    if type(transport) is not CachingTransport:
        raise OverlaySourceUnavailable("unsupported")
    chat_id = _chat(chat_id)
    selection = transport.capture_mirror_selection(MirrorSelectionRequest(
        expected=None, exact_paths=(),
        complete_prefixes=tuple(f"chats/{chat_id}/overlays/{kind}/" for kind in _KINDS),
        max_records=max_documents, max_bytes=max_bytes,
        max_examined_paths=max_examined_paths,
    ))
    if isinstance(selection, MirrorCaptureUnavailable):
        raise OverlaySourceUnavailable(selection.reason)
    if type(selection) is not MirrorSelection:
        raise TypeError("transport returned an invalid overlay capture")
    if selection.provenance != "provider_observed":
        raise OverlaySourceUnavailable("bootstrap_unverified")
    source_id = _source(chat_id, selection.position)
    expected = store.capture_document_position(source_id)
    pending = store.invalidate_document_observation(expected)
    _match(transport, selection.position)
    documents = {
        row.path: row.decoded()
        for prefix in selection.complete_prefixes for row in prefix.records
    }
    position = store.publish_document_batch(
        pending, documents, cursor=pending.cursor, full=True, retain_tombstones=False,
        max_documents=max_documents, max_bytes=max_bytes,
    )
    # A late completion may leave historical rows in SQLite. No receipt can use
    # them after a mirror change, even if initialized is still true on disk.
    _match(transport, selection.position)
    return OverlaySourceReceipt(chat_id, position, selection.position)


def capture_overlay_inputs(
    transport: Transport, store: Store, receipt: OverlaySourceReceipt,
    document_paths: tuple[str, ...], *, max_documents: int = 256,
    max_bytes: int = 1024 * 1024,
) -> DocumentObservation:
    """Capture bounded raw inputs at a common local mirror/SQLite point.

    The caller must still enforce session, membership, history-on-join, owner,
    key/trust and signature semantics. No API route consumes this primitive yet.
    Changes after the capture linearization point require page invalidation by
    the future caller; this function is not a continuing access lease.
    """
    if type(transport) is not CachingTransport:
        raise OverlaySourceUnavailable("unsupported")
    if type(receipt) is not OverlaySourceReceipt:
        raise TypeError("expected an OverlaySourceReceipt")
    chat_id = _chat(receipt.chat_id)
    if (type(receipt.position) is not DocumentPosition
            or type(receipt.mirror) is not MirrorExpectedPosition
            or receipt.position.source_id != _source(chat_id, receipt.mirror)):
        raise ValueError("overlay receipt has inconsistent source binding")
    if type(document_paths) is not tuple:
        raise ValueError("document_paths must be a tuple")
    if type(max_documents) is not int or not 0 <= max_documents <= 256:
        raise ValueError("max_documents must be between zero and 256")
    if type(max_bytes) is not int or not 0 <= max_bytes <= 1024 * 1024:
        raise ValueError("max_bytes must be between zero and 1 MiB")
    if len(document_paths) > max_documents:
        raise OverflowError("overlay selection exceeds row budget")
    for path in document_paths:
        if type(path) is not str or len(path.encode("utf-8")) > 4096:
            raise ValueError("invalid overlay path")
        parts = path.split("/")
        if (len(parts) != 5 or parts[:3] != ["chats", chat_id, "overlays"]
                or parts[3] not in _KINDS or not parts[4].endswith(".json")
                or parts[4] == ".json"):
            raise ValueError("only exact overlay paths in the bound chat are permitted")
    _match(transport, receipt.mirror)
    captured = store.capture_selected_documents(
        receipt.position, document_paths,
        max_documents=max_documents, max_bytes=max_bytes,
    )
    # Bracket the final Store position read with matching monotonic mirror
    # observations. A Store mutation during copying invalidates the result.
    if store.capture_document_position(receipt.position.source_id) != receipt.position:
        raise DocumentObservationConflict("overlay source changed during capture")
    _match(transport, receipt.mirror)
    return captured
