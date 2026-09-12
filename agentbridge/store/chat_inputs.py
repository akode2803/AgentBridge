"""Bounded committed SQLite inputs; not a complete projection or access proof."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LocalChatInputs:
    database_path: str
    incarnation: str
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


def capture(path: Path, chat_id: str, *, document_paths: tuple[str, ...] = (),
            max_messages: int = 100_000, max_logs: int = 10_000,
            max_bytes: int = 64 * 1024 * 1024) -> LocalChatInputs:
    """Capture only committed rows on a private read connection.

    The read transaction ends before JSON decoding or caller work. Byte limits
    cover serialized message/doc payloads and log/doc names, not Python overhead.
    ``document_paths`` names exact keys in this Store's local ``docs`` table; it
    is an internal selection API, not an agent authorization boundary or
    allowlist. Transport docs, private-key files, trust files and GUI session
    files are deliberately not included.
    """
    if type(chat_id) is not str or not chat_id:
        raise ValueError("chat_id must be a nonempty string")
    if any(type(n) is not int or n < 0 for n in (max_messages, max_logs, max_bytes)):
        raise ValueError("snapshot budgets must be nonnegative integers")
    if type(document_paths) is not tuple or len(document_paths) > 128:
        raise ValueError("document_paths must be a bounded tuple")
    if any(type(p) is not str or not p for p in document_paths):
        raise ValueError("document paths must be nonempty strings")
    if len(set(document_paths)) != len(document_paths):
        raise ValueError("duplicate document path")
    used = sum(len(p.encode("utf-8")) for p in document_paths)
    if used > max_bytes:
        raise OverflowError("local chat inputs exceed byte budget")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
    try:
        conn.execute("BEGIN")
        identity = conn.execute(
            "SELECT incarnation FROM ingestion_identity WHERE singleton=1"
        ).fetchone()
        if identity is None:
            raise RuntimeError("missing ingestion database identity")
        # Preflight serialized sizes inside the same snapshot, before payloads
        # cross into Python. LIMIT bounds row counting even on oversized rooms.
        for query, limit in (
            ("SELECT length(CAST(payload AS BLOB)) FROM messages "
             "WHERE chat_id=? AND ns>0 LIMIT ?", max_messages),
            ("SELECT length(CAST(log_name AS BLOB)) FROM log_offsets "
             "WHERE chat_id=? LIMIT ?", max_logs),
        ):
            for count, (size,) in enumerate(conn.execute(query, (chat_id, limit + 1)), 1):
                if count > limit:
                    raise OverflowError("local chat inputs exceed row budget")
                used += size
                if used > max_bytes:
                    raise OverflowError("local chat inputs exceed byte budget")
        for name in document_paths:
            row = conn.execute(
                "SELECT length(CAST(payload AS BLOB)) FROM docs WHERE path=?", (name,)
            ).fetchone()
            used += row[0] if row else 0
            if used > max_bytes:
                raise OverflowError("local chat inputs exceed byte budget")
        messages = tuple(row[0] for row in conn.execute(
            "SELECT payload FROM messages WHERE chat_id=? AND ns>0 "
            "ORDER BY ns,sender,id", (chat_id,)
        ))
        offsets = tuple(conn.execute(
            "SELECT log_name,offset FROM log_offsets WHERE chat_id=? ORDER BY log_name",
            (chat_id,),
        ))
        docs = []
        for name in document_paths:
            row = conn.execute("SELECT payload FROM docs WHERE path=?", (name,)).fetchone()
            docs.append((name, row[0] if row else None))
        return LocalChatInputs(str(path), identity[0], chat_id, messages, offsets, tuple(docs))
    finally:
        conn.close()
