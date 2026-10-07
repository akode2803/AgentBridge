"""Session-scoped positioning for bounded sidebar reconciliation.

This queue contains chat identifiers and progress only.  It is not an
authority cache: every claimed room is rebuilt by the canonical page
operation before its display presentation may be published.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .context import SessionReadToken

MAX_ROOMS = 128
MAX_RUNNING = 2
RETRY_MIN_S = 0.35
RETRY_MAX_S = 30.0


def _binding(token: SessionReadToken) -> tuple[str, int, object]:
    if type(token) is not SessionReadToken:
        raise ValueError("invalid sidebar session")
    return token.app_identity, token.generation, token.mesh


class SidebarRefreshQueue:
    """Bounded, in-memory work positioning for one captured GUI session."""

    def __init__(self, *, clock=time.monotonic) -> None:
        if not callable(clock):
            raise TypeError("sidebar clock must be callable")
        self._lock = threading.Lock()
        self._clock = clock
        self._binding: tuple[str, int, object] | None = None
        self._inventory: tuple[str, ...] = ()
        self._pending: OrderedDict[str, None] = OrderedDict()
        self._running: set[str] = set()
        self._removed: OrderedDict[str, None] = OrderedDict()
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._woken: set[str] = set()

    def clear(self) -> None:
        with self._lock:
            self._binding = None
            self._inventory = ()
            self._pending.clear()
            self._running.clear()
            self._removed.clear()
            self._failures.clear()
            self._retry_at.clear()
            self._woken.clear()

    def request_inventory(self, token: SessionReadToken,
                          chat_ids: list[str]) -> None:
        if (type(chat_ids) is not list or len(chat_ids) > MAX_ROOMS
                or any(type(chat) is not str or not chat for chat in chat_ids)
                or len(set(chat_ids)) != len(chat_ids)):
            raise ValueError("invalid sidebar inventory")
        binding = _binding(token)
        inventory = tuple(chat_ids)
        allowed = frozenset(inventory)
        with self._lock:
            if binding != self._binding:
                self._binding = binding
                self._inventory = inventory
                self._pending = OrderedDict((chat, None) for chat in inventory)
                self._running.clear()
                self._removed.clear()
                self._failures.clear()
                self._retry_at.clear()
                self._woken.clear()
                return
            if inventory == self._inventory:
                return
            for chat in self._inventory:
                if chat not in allowed:
                    self._removed[chat] = None
                    self._removed.move_to_end(chat)
            while len(self._removed) > MAX_ROOMS:
                self._removed.popitem(last=False)
            self._inventory = inventory
            self._pending = OrderedDict(
                (chat, None) for chat in self._pending if chat in allowed)
            self._failures = {chat: value for chat, value in self._failures.items()
                              if chat in allowed}
            self._retry_at = {chat: value for chat, value in self._retry_at.items()
                              if chat in allowed}
            self._woken.intersection_update(allowed)
            for chat in inventory:
                if chat not in self._running:
                    self._pending.setdefault(chat, None)

    def request_chat(self, token: SessionReadToken, chat: str) -> bool:
        binding = _binding(token)
        if type(chat) is not str or not chat:
            raise ValueError("invalid sidebar chat")
        with self._lock:
            if binding != self._binding or chat not in self._inventory:
                return False
            self._failures.pop(chat, None)
            self._retry_at.pop(chat, None)
            if chat in self._running:
                self._woken.add(chat)
            else:
                self._pending[chat] = None
                self._pending.move_to_end(chat, last=False)
            return True

    def request_all(self, token: SessionReadToken) -> bool:
        """Queue the captured inventory after a content-free global hint."""
        binding = _binding(token)
        with self._lock:
            if binding != self._binding:
                return False
            for chat in self._inventory:
                if chat not in self._running:
                    self._pending[chat] = None
            return True

    def record_presentation(self, token: SessionReadToken, chat: str, *, visible: bool) -> bool:
        """Retain bounded removal evidence until a complete list replaces it."""
        binding = _binding(token)
        with self._lock:
            if binding != self._binding:
                return False
            before = chat in self._removed
            if visible:
                self._removed.pop(chat, None)
            else:
                self._removed[chat] = None
                self._removed.move_to_end(chat)
                while len(self._removed) > MAX_ROOMS:
                    self._removed.popitem(last=False)
            return before != (chat in self._removed)

    def claim(self, token: SessionReadToken, *, preferred: str = "") -> str | None:
        binding = _binding(token)
        now = self._clock()
        with self._lock:
            if binding != self._binding or len(self._running) >= MAX_RUNNING:
                return None
            chat = None
            if preferred:
                if (preferred not in self._pending
                        or self._retry_at.get(preferred, 0.0) > now):
                    return None
                chat = preferred
                self._pending.pop(chat)
            else:
                for candidate in self._pending:
                    if self._retry_at.get(candidate, 0.0) <= now:
                        chat = candidate
                        break
                if chat is not None:
                    self._pending.pop(chat)
            if chat is not None:
                self._running.add(chat)
            return chat

    def finish(self, token: SessionReadToken, chat: str, *, resolved: bool) -> None:
        binding = _binding(token)
        now = self._clock()
        with self._lock:
            if binding != self._binding:
                return
            self._running.discard(chat)
            if resolved:
                self._failures.pop(chat, None)
                self._retry_at.pop(chat, None)
                self._woken.discard(chat)
            elif chat in self._inventory:
                failures = 1 if chat in self._woken else min(
                    8, self._failures.get(chat, 0) + 1)
                self._woken.discard(chat)
                self._failures[chat] = failures
                self._retry_at[chat] = now + min(
                    RETRY_MAX_S, RETRY_MIN_S * 2 ** (failures - 1))
                self._pending[chat] = None

    def status(self, token: SessionReadToken) -> dict:
        binding = _binding(token)
        now = self._clock()
        with self._lock:
            if binding != self._binding:
                return {"pending": 0, "running": 0, "complete": False,
                        "active": False, "deferred": 0,
                        "removed": (), "retry_after_ms": 100}
            pending, running = len(self._pending), len(self._running)
            ready = sum(self._retry_at.get(chat, 0.0) <= now
                        for chat in self._pending)
            waits = [max(0.0, self._retry_at.get(chat, 0.0) - now)
                     for chat in self._pending]
            retry_ms = max(100, min(30_000, round(min(waits) * 1000))) if waits else 100
            return {"pending": pending, "running": running,
                    "complete": pending == 0 and running == 0,
                    "active": bool(ready or running),
                    "deferred": pending - ready,
                    "removed": tuple(sorted(self._removed)),
                    "retry_after_ms": retry_ms}
