"""Short-lived collection positions, never cached messages or authority verdicts."""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..store import membership_input_position, overlay_index, page_inputs
from .page_cursors import MAX_ENTRIES, MAX_POSITION_BYTES, TTL_SECONDS, _TOKEN, _session_parts


@dataclass(frozen=True)
class CollectionPosition:
    before: page_inputs.MessageKey
    position: page_inputs.PageInputPosition
    version: str
    inclusive: bool
    offset: int
    occurrence: int


@dataclass(frozen=True)
class _Entry:
    app_identity: str
    generation: int
    mesh: object
    viewer: str
    chat: str
    kind: str
    query: str
    value: CollectionPosition
    expires_at: float


class CollectionCursorRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def issue(self, session, chat, kind, query, selection, version, *,
              before, inclusive=False, offset=0, occurrence=0):
        app, generation, mesh, viewer = _session_parts(session)
        overlay_index._chat(chat)
        key = page_inputs._key(before)
        raw = selection.position
        messages = membership_input_position._copy_expected(raw.messages, str(mesh.store.path))
        overlays = overlay_index._wanted(raw.overlays, Path(messages.database_path))
        if messages.chat_id != chat or overlays.chat_id != chat:
            raise ValueError('foreign collection position')
        if (type(version) is not str or _TOKEN.fullmatch(version) is None
                or type(inclusive) is not bool or type(offset) is not int or offset < 0
                or type(occurrence) is not int or occurrence < 0):
            raise ValueError('invalid collection position')
        retained = (key.sender, key.id, messages.database_path, messages.incarnation,
                    messages.namespace_epoch, messages.chat_id,
                    overlays.source.database_path, overlays.source.incarnation,
                    overlays.source.source_id, overlays.chat_id, overlays.build)
        if sum(len(v.encode()) for v in retained) > MAX_POSITION_BYTES:
            raise ValueError('collection position byte budget')
        value = CollectionPosition(key, page_inputs.PageInputPosition(messages, overlays),
                                   version, inclusive, offset, occurrence)
        with self._lock:
            now = self._clock()
            self._expire(now)
            token = secrets.token_hex(32)
            while token in self._entries:
                token = secrets.token_hex(32)
            self._entries[token] = _Entry(app, generation, mesh, viewer, chat, kind,
                                          query, value, now + TTL_SECONDS)
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)
            return token

    def resolve(self, token, session, chat, kind, query):
        if type(token) is not str or _TOKEN.fullmatch(token) is None:
            return None
        try:
            app, generation, mesh, viewer = _session_parts(session)
            overlay_index._chat(chat)
        except (ValueError, TypeError, AttributeError):
            return None
        with self._lock:
            self._expire(self._clock())
            entry = self._entries.get(token)
            if (entry is None or entry.app_identity != app or entry.generation != generation
                    or entry.mesh is not mesh or entry.viewer != viewer or entry.chat != chat
                    or entry.kind != kind or entry.query != query):
                return None
            self._entries.move_to_end(token)
            return entry.value

    def _expire(self, now):
        for token, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[token]

    def clear(self):
        with self._lock:
            self._entries.clear()
