"""A warm read MIRROR in front of a cloud Transport (R29 — replaces R28's
short-TTL read-through cache).

Why the TTL cache wasn't enough: the GUI refetches ``/api/mesh/state`` on
every SSE event, route change and poll tick — always AFTER the 2s TTL had
lapsed — so every sidebar repaint still paid ~14 sequential cloud round-trips
(~3s measured live). And a transient cloud fault inside ``get_doc`` read as
"doc missing" and was cached for the TTL, so chats/profiles/presence
flickered out of the GUI: the reported instability.

How the mirror works instead:
- ``warm()`` bulk-loads EVERY doc under the root in one paged query
  (``get_docs``, when the inner transport offers it) plus the chat-id list.
- ``get_doc`` / ``list_docs`` / ``list_chat_ids`` are then served from memory
  — ZERO network on the hot read paths, folder-grade latency.
- A background daemon re-pulls the snapshot every ``refresh_s`` seconds, woken
  early by the transport's realtime change hints. A FAILED refresh keeps the
  last good snapshot: slightly stale always beats gone.
- Writes stay write-through and update the mirror synchronously, so a writer
  always sees its own writes immediately; a refresh snapshot never clobbers a
  doc written locally after the snapshot query began (the recent-write guard).
- Returned docs are deep copies — callers patch documents in place
  (read-merge-write), and a shared mirror object must never alias.
- Logs and blobs are deliberately NOT mirrored: message-delivery latency must
  not lag (the SyncEngine already mirrors messages into the local SQLite
  store), and blobs are large + fetched on demand.

Staleness is bounded by the refresh cadence + hint latency, well within the
mesh's existing eventual-consistency tolerance (meta.json is a rebuildable
last-writer-wins snapshot; a OneDrive folder's sync lag is far larger).
Everything not overridden delegates to the inner transport.
"""

from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any

from .base import Transport, Watcher

__all__ = ["CachingTransport"]

# background snapshot cadence — realtime hints wake the refresher early, so
# this is the WORST-case cross-process staleness, not the typical one
CLOUD_REFRESH_S = 4.0
# how long a local write shadows the refresh snapshot (a refresh in flight
# while we wrote must not resurrect the older value; cycles converge fast)
_WRITE_GUARD_S = 60.0
# a failing refresh backs off up to this, still serving the last snapshot
_MAX_BACKOFF_S = 60.0
# read-through miss sentinel: tells "doc absent/unreachable" apart from a
# stored None (inner.get_doc reports both as its default)
_MISS = object()


class CachingTransport(Transport):
    def __init__(self, inner: Transport, refresh_s: float = CLOUD_REFRESH_S,
                 *, auto_refresh: bool = True) -> None:
        self.inner = inner
        self.refresh_s = float(refresh_s)
        self.auto_refresh = auto_refresh
        self.scheme = inner.scheme
        self.max_upload_bytes = inner.max_upload_bytes
        self._lock = threading.Lock()
        self._docs: dict[str, Any] = {}        # the mirror
        self._chat_ids: list[str] = []
        # R66: confirmed inner misses for read-through paths, so unknown
        # names/epochs don't hammer the cloud; cleared by every refresh
        self._neg: set[str] = set()
        self._warm = False
        self._last_refresh = 0.0               # wall clock of last good pull
        self._doc_writes: dict[str, float] = {}   # path -> monotonic of write
        self._chat_writes: dict[str, float] = {}  # chat_id -> monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warm_lock = threading.Lock()
        self._next_warm_try = 0.0

    # delegate unknown attributes (root, cache_key, …) to the inner transport
    def __getattr__(self, name: str) -> Any:
        if name == "inner":  # not yet set during __init__ — never recurse
            raise AttributeError(name)
        return getattr(self.inner, name)

    # ----------------------------------------------------------- mirror core
    def warm_async(self) -> None:
        """Kick the first bulk load in the background (boot-time warmup)."""
        threading.Thread(target=self._ensure_warm, daemon=True,
                         name="ab-mirror-warm").start()

    def refresh(self) -> None:
        """One synchronous snapshot pull (tests; the loop calls this too)."""
        self._refresh_once()

    def mirror_status(self) -> dict:
        """Mirror health for the GUI Connection panel: ``warm`` = the bulk
        snapshot is loaded (hot reads are memory-served); ``age_s`` = seconds
        since the last successful refresh (None before the first). A warm
        mirror with a large age means the refresher is failing and the app is
        serving the last good snapshot."""
        with self._lock:
            warm = self._warm
            last = self._last_refresh
        return {"warm": warm,
                "age_s": (time.time() - last) if last else None,
                "refresh_s": self.refresh_s}

    def _ensure_warm(self) -> bool:
        """Mirror ready? Warm it on first use; if warming FAILS (offline),
        back off for a cycle and let reads fall through to the inner
        transport instead of blocking every caller on retries."""
        if self._warm:
            return True
        with self._warm_lock:
            if self._warm:
                return True
            if time.monotonic() < self._next_warm_try:
                return False
            try:
                self._refresh_once()
            except Exception:  # noqa: BLE001 — cloud unreachable: degrade
                self._next_warm_try = time.monotonic() + self.refresh_s
                return False
            self._start_thread()
            return True

    def _refresh_once(self) -> None:
        t0 = time.monotonic()
        # every Transport has get_docs (base.py default loops per path; cloud
        # drivers override it with one bulk query)
        docs = dict(self.inner.get_docs(""))
        ids = set(self.inner.list_chat_ids())
        with self._lock:
            # local writes newer than the snapshot query win until the next
            # cycle (present = keep ours; absent = we deleted it, keep it gone)
            for path, wrote in self._doc_writes.items():
                if wrote >= t0:
                    if path in self._docs:
                        docs[path] = self._docs[path]
                    else:
                        docs.pop(path, None)
            for chat_id, wrote in self._chat_writes.items():
                if wrote >= t0:
                    ids.add(chat_id)
            self._docs = docs
            self._chat_ids = sorted(ids)
            self._neg.clear()  # a fresh snapshot re-answers every miss
            self._warm = True
            self._last_refresh = time.time()
            floor = time.monotonic() - _WRITE_GUARD_S
            self._doc_writes = {p: w for p, w in self._doc_writes.items()
                                if w > floor}
            self._chat_writes = {c: w for c, w in self._chat_writes.items()
                                 if w > floor}

    def _start_thread(self) -> None:
        if not self.auto_refresh or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True,
                                        name="ab-mirror")
        self._thread.start()

    def _refresh_loop(self) -> None:
        try:
            watcher = self.inner.watch()
        except Exception:  # noqa: BLE001 — no hints: pure cadence
            watcher = None
        wait = self.refresh_s
        try:
            while not self._stop.is_set():
                if watcher is not None:
                    watcher.wait(wait)  # a change hint wakes the pull early
                else:
                    self._stop.wait(wait)
                if self._stop.is_set():
                    break
                try:
                    self._refresh_once()
                    wait = self.refresh_s
                except Exception:  # noqa: BLE001 — keep serving the last good
                    wait = min(max(wait, self.refresh_s) * 2, _MAX_BACKOFF_S)
        finally:
            if watcher is not None:
                watcher.close()

    # ------------------------------------------------------------------ docs
    @staticmethod
    def _reads_through(path: str) -> bool:
        """Correctness-critical, low-volume domains where a warm-miss falls
        through to the cloud instead of waiting for the next refresh (R66):
        chat-key epoch docs (a fresh epoch's first message must unseal on the
        very next harness scan — the silent-lost-trigger bug) and directory
        entries (a brand-new sender's sign key is needed the moment they
        post). Everything else keeps the pure-mirror behaviour that fixed
        the R29 miss-pinning instability."""
        return path.startswith("users/") or (
            path.startswith("chats/") and "/keys/" in path)

    def get_doc(self, path: str, default: Any = None) -> Any:
        if self._ensure_warm():
            with self._lock:
                if path in self._docs:
                    return copy.deepcopy(self._docs[path])
                miss_known = path in self._neg
            if self._reads_through(path) and not miss_known:
                val = self.inner.get_doc(path, _MISS)
                with self._lock:
                    if val is not _MISS:
                        self._docs[path] = copy.deepcopy(val)
                        return val
                    self._neg.add(path)
            return default
        return self.inner.get_doc(path, default)

    def put_doc(self, path: str, data: Any) -> None:
        self.inner.put_doc(path, data)
        with self._lock:
            self._docs[path] = copy.deepcopy(data)
            self._doc_writes[path] = time.monotonic()
            self._neg.discard(path)
            self._neg.discard(f"list:{path.rsplit('/', 1)[0]}")

    def delete_doc(self, path: str) -> None:
        self.inner.delete_doc(path)
        with self._lock:
            self._docs.pop(path, None)
            self._doc_writes[path] = time.monotonic()

    def list_docs(self, prefix: str) -> list[str]:
        if self._ensure_warm():
            with self._lock:
                out = sorted(p for p in self._docs
                             if p.startswith(prefix) and p.endswith(".json"))
                miss_known = f"list:{prefix}" in self._neg
            # R66: an EMPTY keys listing for a chat is how the seal path
            # decides to mint a brand-new epoch — on a genesis race that
            # would fork a duplicate epoch, so verify emptiness with the
            # cloud once per refresh cycle. Established chats always list
            # non-empty and never pay this.
            if not out and not miss_known and self._reads_through(
                    prefix.rstrip("/") + "/x"):
                out = sorted(self.inner.list_docs(prefix))
                if not out:
                    with self._lock:
                        self._neg.add(f"list:{prefix}")
            return out
        return self.inner.list_docs(prefix)

    # ----------------------------------------------------------- chats / logs
    def list_chat_ids(self) -> list[str]:
        if self._ensure_warm():
            with self._lock:
                ids = set(self._chat_ids)
                for p in self._docs:  # a chat we created shows up at once
                    if p.startswith("chats/"):
                        parts = p.split("/", 2)
                        if len(parts) > 2 and parts[1]:
                            ids.add(parts[1])
                return sorted(ids)
        return self.inner.list_chat_ids()

    def list_logs(self, chat_id: str) -> list[tuple[str, int]]:
        return self.inner.list_logs(chat_id)

    # the change feed is log-domain (never cached) — delegate it explicitly:
    # these exist on the Transport base class now, so __getattr__ won't fire
    @property
    def has_change_feed(self) -> bool:  # type: ignore[override]
        return self.inner.has_change_feed

    def changed_logs(self, cursor: int) -> tuple[list[tuple[str, str]], int]:
        return self.inner.changed_logs(cursor)

    def append_log(self, chat_id: str, log_name: str, record: dict) -> None:
        self.inner.append_log(chat_id, log_name, record)
        with self._lock:
            # a first append can create a new chat — visible to us at once
            if chat_id not in self._chat_ids:
                self._chat_ids = sorted({*self._chat_ids, chat_id})
            self._chat_writes[chat_id] = time.monotonic()

    def read_log(
        self, chat_id: str, log_name: str, offset: int = 0
    ) -> tuple[list[dict], int]:
        return self.inner.read_log(chat_id, log_name, offset)

    def delete_chat(self, chat_id: str) -> None:
        self.inner.delete_chat(chat_id)
        now = time.monotonic()
        with self._lock:
            prefix = f"chats/{chat_id}/"
            for p in [p for p in self._docs if p.startswith(prefix)]:
                self._docs.pop(p, None)
                self._doc_writes[p] = now
            self._chat_ids = [c for c in self._chat_ids if c != chat_id]
            self._chat_writes.pop(chat_id, None)

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
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()
