"""A short-TTL read cache in front of any Transport (the R28 cloud-perf fix).

The hot GUI endpoints (``/api/mesh/state`` especially) read chat/account/
presence METADATA straight from the transport, and they re-read the SAME docs
many times within a single request: ``PrivacyService.visible_profile`` fetches
an account doc ~8× per user (once in ``public_gates`` and once per
``profile_allows`` field), ``presence_of`` re-lists + re-reads every presence
doc once per user, ``chats_for`` re-reads every chat's meta. On a local folder
each read is ~free; over a cloud transport each is a network round-trip, so a
single state build is O(users × chats × fields) RTTs — ~30 s on Supabase vs
~120 ms on the folder.

This wrapper caches the three READ-metadata methods — ``get_doc``,
``list_docs``, ``list_chat_ids`` — for a short TTL and invalidates them on any
write through the SAME transport, so a writer always sees its own writes. It
deliberately does NOT cache ``read_log`` / ``list_logs`` (message-delivery
latency must not lag) or blobs (large, content-addressed). The TTL is short
(seconds) and the whole mesh is already eventually-consistent — meta.json is a
rebuildable last-writer-wins snapshot — so a cross-process change showing up a
couple of seconds late is within the existing tolerance, not a new one.

Everything not overridden here delegates to the inner transport (blobs, logs,
``watch``, ``close``, ``local_path``, and attributes like ``root`` /
``cache_key`` / ``scheme``), so the wrapper is a drop-in.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .base import Transport, Watcher

__all__ = ["CachingTransport"]

# default for cloud transports — short enough that a membership/profile change
# from another machine surfaces within a poll or two, long enough to collapse
# the many same-doc reads inside one request
CLOUD_CACHE_TTL = 2.0

_MISS = object()  # a genuinely-absent doc, cached to avoid re-fetching a miss


class CachingTransport(Transport):
    def __init__(self, inner: Transport, ttl: float = CLOUD_CACHE_TTL) -> None:
        self.inner = inner
        self.ttl = float(ttl)
        self.scheme = inner.scheme
        self.max_upload_bytes = inner.max_upload_bytes
        self._lock = threading.Lock()
        self._docs: dict[str, tuple[float, Any]] = {}      # path -> (exp, value)
        self._lists: dict[str, tuple[float, list]] = {}    # key  -> (exp, list)

    # delegate unknown attributes (root, cache_key, …) to the inner transport
    def __getattr__(self, name: str) -> Any:
        if name == "inner":  # not yet set during __init__ — never recurse
            raise AttributeError(name)
        return getattr(self.inner, name)

    # ------------------------------------------------------------ cache core
    def _get_cached(self, store: dict, key: str):
        ent = store.get(key)
        if ent is None:
            return _MISS
        exp, value = ent
        if exp < time.monotonic():
            store.pop(key, None)
            return _MISS
        return value

    def _put_cached(self, store: dict, key: str, value) -> None:
        store[key] = (time.monotonic() + self.ttl, value)

    def _invalidate_lists(self) -> None:
        self._lists.clear()

    # ------------------------------------------------------------------ docs
    def get_doc(self, path: str, default: Any = None) -> Any:
        with self._lock:
            hit = self._get_cached(self._docs, path)
        if hit is not _MISS:
            # a cached miss is stored as None; hand back the caller's default
            return hit if hit is not None else default
        value = self.inner.get_doc(path, _MISS)
        with self._lock:
            self._put_cached(self._docs, path, None if value is _MISS else value)
        return default if value is _MISS else value

    def put_doc(self, path: str, data: Any) -> None:
        self.inner.put_doc(path, data)
        with self._lock:
            # the writer's own read must reflect the write immediately
            self._put_cached(self._docs, path, data)
            self._invalidate_lists()

    def delete_doc(self, path: str) -> None:
        self.inner.delete_doc(path)
        with self._lock:
            self._docs[path] = (time.monotonic() + self.ttl, None)
            self._invalidate_lists()

    def list_docs(self, prefix: str) -> list[str]:
        key = f"docs:{prefix}"
        with self._lock:
            hit = self._get_cached(self._lists, key)
            if hit is not _MISS:
                return list(hit)
        value = self.inner.list_docs(prefix)
        with self._lock:
            self._put_cached(self._lists, key, list(value))
        return list(value)

    # ----------------------------------------------------------- chats / logs
    def list_chat_ids(self) -> list[str]:
        with self._lock:
            hit = self._get_cached(self._lists, "chat_ids")
            if hit is not _MISS:
                return list(hit)
        value = self.inner.list_chat_ids()
        with self._lock:
            self._put_cached(self._lists, "chat_ids", list(value))
        return list(value)

    def list_logs(self, chat_id: str) -> list[tuple[str, int]]:
        return self.inner.list_logs(chat_id)

    def append_log(self, chat_id: str, log_name: str, record: dict) -> None:
        self.inner.append_log(chat_id, log_name, record)
        with self._lock:
            # a first append can create a new chat — drop the id/list caches
            self._invalidate_lists()

    def read_log(
        self, chat_id: str, log_name: str, offset: int = 0
    ) -> tuple[list[dict], int]:
        return self.inner.read_log(chat_id, log_name, offset)

    def delete_chat(self, chat_id: str) -> None:
        self.inner.delete_chat(chat_id)
        with self._lock:
            self._docs.clear()  # the chat's whole doc subtree is gone
            self._invalidate_lists()

    # ----------------------------------------------------------------- blobs
    def put_blob(self, path: str, data: bytes) -> None:
        self.inner.put_blob(path, data)

    def put_blob_from(self, local_src: Path, path: str) -> None:
        self.inner.put_blob_from(local_src, path)

    def get_blob(self, path: str) -> bytes | None:
        return self.inner.get_blob(path)

    def blob_size(self, path: str) -> int | None:
        return self.inner.blob_size(path)

    def local_path(self, path: str) -> Path | None:
        return self.inner.local_path(path)

    # ---------------------------------------------------------------- events
    def watch(self) -> Watcher:
        return self.inner.watch()

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()
