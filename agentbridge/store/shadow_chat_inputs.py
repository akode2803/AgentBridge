"""One historical durable SQLite cut of shadow and local chat inputs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import chat_inputs, shadow_slot

_DOCUMENT_PATHS = ("sync/log_cursor",)
MAX_MESSAGES = 100_000
MAX_LOGS = 10_000


@dataclass(frozen=True)
class ShadowChatInputs:
    position: shadow_slot.ShadowPosition
    snapshot: shadow_slot.ShadowSnapshot | None
    chat_id: str
    message_json: tuple[str, ...]
    offsets: tuple[tuple[str, int], ...]
    document_json: tuple[tuple[str, str | None], ...]

    def messages(self) -> list[dict]:
        return [json.loads(payload) for payload in self.message_json]

    def document(self, path: str, default: Any = None) -> Any:
        for name, payload in self.document_json:
            if name == path:
                return default if payload is None else json.loads(payload)
        raise KeyError("document was not captured")


def capture(
    path: Path,
    expected_shadow: shadow_slot.ShadowPosition,
    chat_id: str,
    *,
    max_documents: int = shadow_slot.MAX_RECORDS,
    max_chat_ids: int = shadow_slot.MAX_RECORDS,
    max_messages: int = MAX_MESSAGES,
    max_logs: int = MAX_LOGS,
    max_bytes: int = shadow_slot.MAX_BYTES,
) -> ShadowChatInputs:
    """Capture a paired historical cut; this grants no freshness or authority.

    The aggregate byte ceiling charges database path, incarnation, chat id,
    publisher/source identities, initialized snapshot provenance and stored
    chat-id JSON, every shadow record, every message, every log name, and the
    fixed local-document path plus its present payload. Numeric metadata is not
    charged. All payload decoding and shadow shape validation happen detached.
    """
    if type(chat_id) is not str or not chat_id:
        raise ValueError("chat_id must be a nonempty string")
    shadow_slot._budgets(max_documents, max_chat_ids, max_bytes)
    for value, limit in ((max_messages, MAX_MESSAGES), (max_logs, MAX_LOGS)):
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError("chat input budget exceeds supported range")
    wanted = shadow_slot._expected(expected_shadow, path, owned=True)
    used = sum(len(value.encode("utf-8")) for value in (
        str(path), wanted.incarnation, chat_id, wanted.publisher_nonce,
        wanted.source.root, wanted.source.cache, wanted.source.mirror_nonce,
        *_DOCUMENT_PATHS,
    ))
    if used > max_bytes:
        raise OverflowError("shadow chat inputs exceed byte budget")

    conn = _open_reader(path)
    try:
        conn.execute("BEGIN")
        current = shadow_slot._match(conn, path, wanted)
        shadow_bytes = shadow_slot._capture_preflight(
            conn, current, max_documents=max_documents, max_bytes=max_bytes - used,
        )
        used += shadow_bytes
        local_bytes = chat_inputs._capture_preflight(
            conn, chat_id, _DOCUMENT_PATHS,
            max_messages=max_messages, max_logs=max_logs,
            max_bytes=max_bytes - used,
        )
        used += local_bytes
        if used > max_bytes:
            raise OverflowError("shadow chat inputs exceed byte budget")
        raw_shadow = shadow_slot._capture_rows(conn, current)
        messages, offsets, docs = chat_inputs._capture_rows(
            conn, chat_id, _DOCUMENT_PATHS,
        )
    finally:
        conn.close()

    snapshot = None
    if raw_shadow is not None:
        revision, cursor, provenance, chats, records = raw_shadow
        snapshot = shadow_slot.ShadowSnapshot(
            current.source, revision, cursor, provenance,
            shadow_slot._decode_chat_ids(chats), records,
        )
        snapshot, _, _ = shadow_slot._prepare(
            snapshot, max_documents, max_chat_ids, max_bytes,
        )
        if snapshot.source != current.source:
            raise ValueError("shadow snapshot source differs from position")
    return ShadowChatInputs(current, snapshot, chat_id, messages, offsets, docs)


def _open_reader(path: Path) -> sqlite3.Connection:
    """Open the coordinator-owned private reader with the existing timeout."""
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
