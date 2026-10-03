"""Bounded opaque acknowledgment positions, never continuing read authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from ..store.page_inputs import MessageKey, PageInputPosition, _key
from .page_cursors import (MAX_POSITION_BYTES, PageCursorRegistry,
                           _continuation, _session_parts, _TOKEN)


@dataclass(frozen=True)
class ReadAckPosition:
    before: MessageKey
    position: PageInputPosition
    limit: int
    trust_version: str
    page_version: str


class PageReadTokens(PageCursorRegistry):
    """Same 128-entry, 15-minute, 256-bit handle bounds as page continuations."""

    def issue_read(self, session, chat, selection, *, limit, trust_version, page_version):
        if not selection.messages or selection.newest_examined is None:
            return None
        # Validate/copy the completed position with the existing byte ceiling.
        position = _continuation(chat, selection).position
        self.version(session, chat, selection, local_trust_version=trust_version)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid read acknowledgment window limit')
        if type(page_version) is not str or _TOKEN.fullmatch(page_version) is None:
            raise ValueError('invalid read acknowledgment page version')
        value = ReadAckPosition(_key(selection.newest_examined), position,
                                limit, trust_version, page_version)
        if len(json.dumps(asdict(value), ensure_ascii=False).encode()) > MAX_POSITION_BYTES:
            raise ValueError('read acknowledgment position byte budget')
        return self._issue(*_session_parts(session), chat, value)

    def resolve_read(self, handle, session, chat, page_version):
        value = self._resolve(handle, session, chat)
        return value if (type(value) is ReadAckPosition
                         and value.page_version == page_version) else None
