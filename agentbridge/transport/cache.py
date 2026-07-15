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
  (``snapshot_docs``) plus the chat-id list.
- ``get_doc`` / ``list_docs`` / ``list_chat_ids`` are then served from memory
  — ZERO network on the hot read paths, folder-grade latency.
- A background daemon keeps the mirror fresh. R76 (docs/SCALING.md): on a
  transport with a doc delta feed it pulls ONLY "rows changed since cursor"
  — woken by realtime pokes, with a slow SAFETY poll while idle and a rare
  full-snapshot reconcile — instead of the flat full-snapshot-every-4s loop
  that burned 21 GB/day on the metered free tier. A transport without the
  feed keeps full snapshots at the profile's (slow, when metered) cadence.
  A FAILED refresh keeps the last good snapshot: stale beats gone.
- A hint WATCHDOG guards the slow cadence: when a safety poll finds changes
  no poke announced, polls drop to the profile's fallback rate for a while
  (realtime silently dead ≠ latency regression).
- Writes stay write-through and update the mirror synchronously, so a writer
  always sees its own writes immediately; a refresh never clobbers a doc
  written locally after the pull began (the recent-write guard).
- Returned docs are deep copies — callers patch documents in place
  (read-merge-write), and a shared mirror object must never alias.
- Logs and blobs are deliberately NOT mirrored: message-delivery latency must
  not lag (the SyncEngine already mirrors messages into the local SQLite
  store), and blobs are large + fetched on demand.

Staleness is bounded by poke latency (sub-second writer-coalesced) or, with
realtime down, one safety-poll interval — within the mesh's existing
eventual-consistency tolerance (meta.json is a rebuildable last-writer-wins
snapshot; a OneDrive folder's sync lag is far larger).
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

# base cadence for an UNMETERED wrapped transport (tests; folder stays bare).
# Metered drivers get their cadence from their TransportProfile instead.
CLOUD_REFRESH_S = 4.0
# how long a local write shadows a refresh (a pull in flight while we wrote
# must not resurrect the older value; cycles converge fast)
_WRITE_GUARD_S = 60.0
# a failing refresh backs off up to this, still serving the last snapshot
_MAX_BACKOFF_S = 60.0
# how long the hint watchdog distrusts pokes after a silent change (R76)
_SUSPECT_S = 600.0
# read-through miss sentinel: tells "doc absent/unreachable" apart from a
# stored None (inner.get_doc reports both as its default)
_MISS = object()


class CachingTransport(Transport):
    def __init__(self, inner: Transport, refresh_s: float | None = None,
                 *, auto_refresh: bool = True) -> None:
        self.inner = inner
        prof = inner.profile
        # explicit refresh_s (tests) wins; else the profile decides (R76)
        self.refresh_s = float(refresh_s) if refresh_s is not None else (
            prof.idle_poll_s if prof.metered else CLOUD_REFRESH_S)
        self.auto_refresh = auto_refresh
        self.scheme = inner.scheme
        self.max_upload_bytes = inner.max_upload_bytes
        self.profile = prof
        self._lock = threading.Lock()
        self._docs: dict[str, Any] = {}        # the mirror
        self._chat_ids: list[str] = []
        # R66: confirmed inner misses for read-through paths, so unknown
        # names/epochs don't hammer the cloud; cleared by every refresh
        self._neg: set[str] = set()
        self._warm = False
        self._last_refresh = 0.0               # wall clock of last good pull
        self._cursor = 0                       # doc delta cursor (R76)
        self._last_full = 0.0                  # monotonic of last full pull
        self._suspect_until = 0.0              # hint watchdog (monotonic)
        self._silent_strikes = 0               # consecutive unannounced polls
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
        """One synchronous snapshot pull (tests; the loop calls this too).
        Also ensures the background refresher is running — a caller who
        warms MANUALLY first must not end up with a mirror frozen on its
        boot snapshot (auto_refresh=False keeps this a no-op)."""
        self._refresh_once()
        self._start_thread()

    def mirror_status(self) -> dict:
        """Mirror health for the GUI Connection panel: ``warm`` = the bulk
        snapshot is loaded (hot reads are memory-served); ``age_s`` = seconds
        since the last successful refresh (None before the first). A warm
        mirror with a large age means the refresher is failing and the app is
        serving the last good snapshot. R76 adds the sync mode (``delta`` vs
        ``full``), hint health, and the driver's transfer stats."""
        with self._lock:
            warm = self._warm
            last = self._last_refresh
            cursor = self._cursor
        stats = getattr(self.inner, "transfer_stats", None)
        return {"warm": warm,
                "age_s": (time.time() - last) if last else None,
                "refresh_s": self.suggest_poll_s(self.refresh_s),
                "mode": ("delta" if cursor and self.profile.supports_doc_delta
                         else "full"),
                "hints_suspect": time.monotonic() < self._suspect_until,
                "transfer": stats() if callable(stats) else None}

    def suggest_poll_s(self, default: float) -> float:
        """The safety-poll cadence a hint-woken loop should use now (base
        contract). Metered transports idle slowly while pokes look healthy
        and speed up while the watchdog distrusts them; free transports keep
        the caller's cadence."""
        if not self.profile.metered:
            return default
        if time.monotonic() < self._suspect_until:
            return self.profile.fallback_poll_s
        return self.profile.idle_poll_s

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
                # retry on the FAST leash — a slow metered idle cadence is
                # for healthy steady-state, not for getting warm after boot
                self._next_warm_try = time.monotonic() + (
                    self.profile.fallback_poll_s if self.profile.metered
                    else self.refresh_s)
                return False
            self._start_thread()
            return True

    def _refresh_once(self) -> None:
        t0 = time.monotonic()
        # snapshot_docs = full pull + the delta cursor it is current at
        # (base default wraps get_docs with cursor 0 for feed-less drivers)
        docs, cursor = self.inner.snapshot_docs()
        docs = dict(docs)
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
            self._cursor = cursor
            self._last_refresh = time.time()
            self._last_full = time.monotonic()
            self._prune_guards_locked()

    def _prune_guards_locked(self) -> None:
        floor = time.monotonic() - _WRITE_GUARD_S
        self._doc_writes = {p: w for p, w in self._doc_writes.items()
                            if w > floor}
        self._chat_writes = {c: w for c, w in self._chat_writes.items()
                             if w > floor}

    def _refresh_delta(self) -> bool:
        """One incremental pull (R76). Returns whether anything a poke
        SHOULD have announced changed — the hint watchdog's signal. Classes
        whose writers deliberately never poke (profile.silent_prefixes:
        presence beats) apply to the mirror but don't count, or every
        heartbeat caught by a safety poll would trip the fallback cadence
        forever. Raises NotImplementedError when the driver has no live
        feed (the caller falls back to a full pull) and network errors for
        backoff."""
        t0 = time.monotonic()
        changed, deleted, cursor = self.inner.get_docs_delta(self._cursor)
        silent = self.profile.silent_prefixes
        with self._lock:
            for path, val in changed.items():
                wrote = self._doc_writes.get(path)
                if wrote is not None and wrote >= t0:
                    continue           # our newer local write wins this cycle
                self._docs[path] = val
            for path in deleted:
                wrote = self._doc_writes.get(path)
                if wrote is not None and wrote >= t0:
                    continue
                self._docs.pop(path, None)
                if path.startswith("chats/") and path.endswith("/meta.json"):
                    # a tombstoned meta = the chat is gone; stop listing it
                    cid = path.split("/")[1]
                    self._chat_ids = [c for c in self._chat_ids if c != cid]
            if changed or deleted:
                self._neg.clear()      # the world moved: re-answer misses
            self._cursor = max(self._cursor, cursor)
            self._last_refresh = time.time()
            self._prune_guards_locked()
        return any(not p.startswith(silent)
                   for p in (*changed, *deleted)) if silent \
            else bool(changed or deleted)

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
        prof = self.profile
        backoff = 0.0
        try:
            while not self._stop.is_set():
                wait = backoff or self.suggest_poll_s(self.refresh_s)
                if watcher is not None:
                    hinted = watcher.wait(wait)  # a poke wakes the pull early
                else:
                    hinted = False
                    self._stop.wait(wait)
                if self._stop.is_set():
                    break
                try:
                    changed = self._refresh_tick()
                    backoff = 0.0
                except Exception:  # noqa: BLE001 — keep serving the last good
                    backoff = min(max(backoff, self.refresh_s) * 2,
                                  _MAX_BACKOFF_S)
                    continue
                if prof.metered:
                    self._watchdog(changed, hinted)
        finally:
            if watcher is not None:
                watcher.close()

    def _watchdog(self, changed: bool, hinted: bool) -> None:
        """Hint health (R76): pokes are being LOST when safety polls keep
        finding changes nobody announced — then polls drop to the fallback
        rate for a while. It takes TWO consecutive silent polls to trip:
        a short-lived writer (CLI one-shot, a booting process) legitimately
        drops its first pokes while its socket subscribes, and one isolated
        silent poll must not put the whole process on the fast leash
        (v0.24.154 — the live fleet sat suspect forever on probe writes).
        A real outage with steady activity still trips within two poll
        windows; silent classes (presence) never count (see _refresh_delta)."""
        if changed and not hinted:
            self._silent_strikes += 1
            if self._silent_strikes >= 2:
                self._suspect_until = time.monotonic() + _SUSPECT_S
        else:
            self._silent_strikes = 0

    def _refresh_tick(self) -> bool:
        """Delta when possible; full when due (reconcile), forced (no feed /
        legacy schema), or the mirror is cold. Returns whether docs moved."""
        prof = self.profile
        reconcile_due = (prof.reconcile_s > 0 and
                         time.monotonic() - self._last_full >= prof.reconcile_s)
        if self._warm and prof.supports_doc_delta and not reconcile_due:
            try:
                return self._refresh_delta()
            except NotImplementedError:
                pass                   # legacy schema right now: full pull
        # full-pull path: on a metered transport a poke burst must not turn
        # into a full snapshot per poke (legacy-schema mode) — floor the
        # cadence at the fallback rate; reconciles ride the same floor
        if (prof.metered and self._warm and
                time.monotonic() - self._last_full < prof.fallback_poll_s):
            return False
        self._refresh_once()
        return False   # a full pull can't tell "changed" — never trip the watchdog

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
