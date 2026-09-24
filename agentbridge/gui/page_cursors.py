"""Opaque, process-local page continuations; never a read or authority lease.

The caller must repeat current viewer, chat, source and final-position validation
on every page request. Entries hold only a raw seek key and local input position.
"""
from __future__ import annotations

import re
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..store import membership_input_position, overlay_index, page_inputs

if TYPE_CHECKING:
    from .context import SessionReadToken
    from ..mesh.page_selection import PageSelection


MAX_ENTRIES = 128
TTL_SECONDS = 15 * 60
MAX_POSITION_BYTES = 16 * 1024
_TOKEN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


@dataclass(frozen=True)
class PageContinuation:
    before: page_inputs.MessageKey
    position: page_inputs.PageInputPosition


@dataclass(frozen=True)
class PageWindowAnchor:
    """Position only: refresh this window against new canonical raw inputs."""

    before: page_inputs.MessageKey | None
    database_path: str
    incarnation: str
    namespace_epoch: str
    inclusive: bool = False

    def matches_store(self, position: page_inputs.PageInputPosition) -> bool:
        messages = position.messages
        return (messages.database_path == self.database_path
                and messages.incarnation == self.incarnation
                and messages.namespace_epoch == self.namespace_epoch)


@dataclass(frozen=True)
class _Entry:
    app_identity: str
    generation: int
    mesh: object
    viewer: str
    chat: str
    continuation: PageContinuation | PageWindowAnchor
    expires_at: float


def _session_parts(session: SessionReadToken) -> tuple[str, int, object, str]:
    # Local import permits GuiApp to own this registry without a context cycle.
    from .context import SessionReadToken

    if type(session) is not SessionReadToken:
        raise ValueError('invalid cursor session')
    app, generation, mesh = session.app_identity, session.generation, session.mesh
    if (type(app) is not str or not app or len(app) > 128 or '\x00' in app
            or type(generation) is not int or not 0 <= generation < 2**63
            or mesh is None):
        raise ValueError('invalid cursor session')
    viewer = mesh.user
    overlay_index._text(viewer, 'cursor viewer', 256)
    return app, generation, mesh, viewer


def _continuation(chat: str, selection: PageSelection) -> PageContinuation:
    from ..mesh.page_selection import PageSelection

    if type(selection) is not PageSelection or selection.needs_more_input:
        raise ValueError('page selection is incomplete')
    if type(selection.position) is not page_inputs.PageInputPosition:
        raise ValueError('invalid page input position')
    raw = selection.position
    messages = membership_input_position._copy_expected(
        raw.messages, raw.messages.database_path,
    )
    overlays = overlay_index._wanted(raw.overlays, Path(messages.database_path))
    if messages.chat_id != chat or overlays.chat_id != chat:
        raise ValueError('cursor belongs to another chat')
    before = page_inputs._key(selection.oldest_examined)
    retained = (before.sender, before.id, messages.database_path,
                messages.incarnation, messages.namespace_epoch, messages.chat_id,
                overlays.source.database_path, overlays.source.incarnation,
                overlays.source.source_id, overlays.chat_id, overlays.build)
    if sum(len(value.encode('utf-8')) for value in retained) > MAX_POSITION_BYTES:
        raise ValueError('cursor position exceeds byte budget')
    if type(selection.raw_examined) is not int or selection.raw_examined < 1:
        raise ValueError('cursor has no examined raw row')
    return PageContinuation(before, page_inputs.PageInputPosition(messages, overlays))


class PageCursorRegistry:
    """Thread-safe TTL/LRU registry of 256-bit random, fixed-length handles.

    Lock order is independent of the GUI session gate: no provider or store
    activity occurs here, and callers should only issue after page finalization.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._version_secret = secrets.token_bytes(32)

    def version(self, session: SessionReadToken, chat: str, selection: PageSelection,
                *, local_trust_version: str = '') -> str:
        """Opaque equality hint for raw inputs, never permission to reuse a page.

        Even identical raw positions require new authority/trust/key evaluation.
        Consumers must invalidate visible pages on refresh/session/route changes.
        """
        from ..mesh.page_selection import PageSelection
        app, generation, mesh, viewer = _session_parts(session)
        if type(selection) is not PageSelection or selection.needs_more_input:
            raise ValueError('invalid page selection')
        raw = selection.position
        messages = membership_input_position._copy_expected(raw.messages, str(mesh.store.path))
        overlays = overlay_index._wanted(raw.overlays, mesh.store.path)
        if messages.chat_id != chat or overlays.chat_id != chat:
            raise ValueError('foreign page version')
        if (type(local_trust_version) is not str
                or (local_trust_version and _TOKEN.fullmatch(local_trust_version) is None)):
            raise ValueError('invalid trust input version')
        encoded = json.dumps([app, generation, viewer, chat, asdict(messages), asdict(overlays),
                              local_trust_version],
                             sort_keys=True, separators=(',', ':')).encode()
        if len(encoded) > MAX_POSITION_BYTES:
            raise ValueError('page version byte budget')
        return hmac.new(self._version_secret, encoded, hashlib.sha256).hexdigest()

    def _expire(self, now: float) -> None:
        for token, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[token]

    def issue(self, session: SessionReadToken, chat: str,
              page_selection: PageSelection) -> str | None:
        from ..mesh.page_selection import PageSelection

        app, generation, mesh, viewer = _session_parts(session)
        overlay_index._chat(chat)
        if type(page_selection) is not PageSelection:
            raise ValueError('invalid page selection')
        if not page_selection.has_more or page_selection.oldest_examined is None:
            return None
        continuation = _continuation(chat, page_selection)
        if continuation.position.messages.database_path != str(mesh.store.path):
            raise ValueError('cursor belongs to another store')
        return self._issue(app, generation, mesh, viewer, chat, continuation)

    def issue_anchor(self, session: SessionReadToken, chat: str,
                     page_selection: PageSelection,
                     before: page_inputs.MessageKey | None, *, inclusive: bool = False) -> str:
        """Bind a successful request's upper boundary, including the tail.

        Generation and overlay positions are deliberately not retained. This
        handle cannot bypass fresh membership or canonical selection. A Store
        replacement/namespace reset does invalidate the positioning reference.
        """
        app, generation, mesh, viewer = _session_parts(session)
        overlay_index._chat(chat)
        self.version(session, chat, page_selection)  # Validate completed Store binding.
        if type(inclusive) is not bool or (inclusive and before is None):
            raise ValueError('inclusive anchor needs an examined raw key')
        key = None if before is None else page_inputs._key(before)
        if key is not None and len(key.sender.encode()) + len(key.id.encode()) > MAX_POSITION_BYTES:
            raise ValueError('anchor position exceeds byte budget')
        messages = page_selection.position.messages
        anchor = PageWindowAnchor(key, messages.database_path, messages.incarnation,
                                  messages.namespace_epoch, inclusive)
        return self._issue(app, generation, mesh, viewer, chat, anchor)

    def _issue(self, app, generation, mesh, viewer, chat, continuation):
        with self._lock:
            now = self._clock()
            self._expire(now)
            token = secrets.token_hex(32)
            while token in self._entries:
                token = secrets.token_hex(32)
            self._entries[token] = _Entry(app, generation, mesh, viewer, chat,
                                          continuation, now + TTL_SECONDS)
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)
            return token

    def resolve(self, token: str, session: SessionReadToken,
                chat: str) -> PageContinuation | None:
        value = self._resolve(token, session, chat)
        return value if type(value) is PageContinuation else None

    def resolve_anchor(self, token: str, session: SessionReadToken,
                       chat: str) -> PageWindowAnchor | None:
        value = self._resolve(token, session, chat)
        return value if type(value) is PageWindowAnchor else None

    def _resolve(self, token, session, chat):
        if type(token) is not str or _TOKEN.fullmatch(token) is None:
            return None
        try:
            app, generation, mesh, viewer = _session_parts(session)
            overlay_index._chat(chat)
        except (ValueError, TypeError, AttributeError):
            return None
        with self._lock:
            now = self._clock()
            self._expire(now)
            entry = self._entries.get(token)
            if (entry is None or entry.app_identity != app
                    or entry.generation != generation or entry.mesh is not mesh
                    or entry.viewer != viewer or entry.chat != chat):
                return None
            self._entries.move_to_end(token)
            return entry.continuation

    def clear(self) -> None:
        """Discard all outstanding handles during logout or session reset."""
        with self._lock:
            self._entries.clear()
