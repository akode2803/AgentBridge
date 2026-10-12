"""Sync engine — pulls new envelope records from the transport into the local
cache, incrementally (stored byte offsets) and in parallel across chats.

Serves requirement "mesh fetches only what it needs": the engine takes an
``is_member`` gate and never even reads logs of chats the identity isn't in.
The run loop follows FORMAT2 tenet 6: the transport watcher only SHORTENS the
wait; the rescan is what finds changes.

Two scan strategies (R30):
- **Change feed** (``tx.has_change_feed``, the cloud drivers): ONE
  "what changed since cursor?" round-trip per tick, then read exactly the
  logs it names — idle cost is one empty query no matter how many chats
  exist. The cursor persists in the local store, so a restart resumes where
  it left off. A chat that becomes newly VISIBLE to this identity (join) is
  caught by a membership diff and fully scanned once — its history may sit
  below the cursor.
- **Per-chat scan** (folder and any driver without a feed): list every
  member chat's logs and read the ones whose size moved — unchanged from R3.
"""

from __future__ import annotations

import threading
import time
import math
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

from ..store.db import Store
from ..core.latency import clock_id, sink_for_store
from ..core import delivery_trace
from ..transport.base import Transport

__all__ = ["SyncEngine", "SyncRetirement"]

# where the change-feed cursor persists (the store's local doc cache)
CURSOR_DOC = "sync/log_cursor"


@dataclass(frozen=True)
class SyncRetirement:
    """Opaque proof that one legacy sync owner can no longer write its Store."""

    _engine: "SyncEngine"
    _token: object
    _owner: object | None = None


class SyncEngine:
    def __init__(
        self,
        tx: Transport,
        store: Store,
        *,
        is_member: Callable[[str], bool] = lambda chat_id: True,
        workers: int = 4,
        on_records: Callable[[str, list[dict]], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        on_chat_progress: Callable[[str, int], None] | None = None,
    ) -> None:
        self.tx = tx
        self.store = store
        self.is_member = is_member
        self.workers = workers
        self.on_records = on_records  # fed ONLY actually-new records (R10 bus)
        self.on_progress = on_progress  # per-log wake after local insertion
        self.on_chat_progress = on_chat_progress
        self.latency = sink_for_store(store)
        self._stop = threading.Event()
        self._ownership = threading.Condition()
        self._ownership_local = threading.local()
        self._active_cycles = 0
        self._retired = False
        self._retirement_token = object()
        self._transfer_owner = None
        # membership as of the last feed tick — a chat that APPEARS here gets
        # one full scan (its history may predate the cursor). Deliberately
        # in-memory: a fresh process full-scans once, which also heals any
        # miss while it was down.
        self._known: set[str] = set()

    @contextmanager
    def _write_cycle(self):
        """Hold the legacy writer lease for one possibly nested sync call."""
        depth = getattr(self._ownership_local, "depth", 0)
        if depth:
            self._ownership_local.depth = depth + 1
            try:
                yield
            finally:
                self._ownership_local.depth -= 1
            return
        with self._ownership:
            if self._retired:
                raise RuntimeError("legacy sync owner retired")
            self._active_cycles += 1
            self._ownership_local.depth = 1
        try:
            yield
        finally:
            self._ownership_local.depth = 0
            with self._ownership:
                self._active_cycles -= 1
                if self._active_cycles == 0:
                    self._ownership.notify_all()

    def retire(self, *, timeout_s: float = 10.0) -> SyncRetirement:
        """Permanently reject new writes and join every in-flight write cycle.

        A timeout leaves the owner retired.  The caller may retry later to
        obtain proof after the outstanding cycle releases its lease.
        """
        if (type(timeout_s) not in (int, float) or isinstance(timeout_s, bool)
                or not math.isfinite(timeout_s) or timeout_s < 0):
            raise ValueError("invalid sync retirement timeout")
        if getattr(self._ownership_local, "depth", 0):
            raise RuntimeError("cannot retire sync owner from its write cycle")
        deadline = time.monotonic() + float(timeout_s)
        self._stop.set()
        with self._ownership:
            self._retired = True
            while self._active_cycles:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._ownership.wait(remaining):
                    if self._active_cycles:
                        raise TimeoutError("legacy sync owner has active writes")
            return SyncRetirement(
                self, self._retirement_token, self._transfer_owner,
            )

    def bind_transfer_owner(self, owner: object) -> None:
        """Bind the sole local-input owner allowed to consume retirement."""
        if owner is None:
            raise TypeError("invalid legacy sync transfer owner")
        with self._ownership:
            if self._retired:
                raise RuntimeError("legacy sync owner already retired")
            if self._transfer_owner is None:
                self._transfer_owner = owner
            elif self._transfer_owner is not owner:
                raise ValueError("foreign legacy sync transfer owner")

    def validate_retirement(
        self, proof: SyncRetirement, tx, store, *, owner: object | None = None,
    ) -> None:
        """Validate an exact owner/transport/Store retirement receipt."""
        with self._ownership:
            if (type(proof) is not SyncRetirement
                    or proof._engine is not self
                    or proof._token is not self._retirement_token
                    or proof._owner is not self._transfer_owner
                    or (owner is not None and owner is not self._transfer_owner)
                    or tx is not self.tx or store is not self.store
                    or not self._retired or self._active_cycles):
                raise ValueError("invalid legacy sync retirement")

    # -------------------------------------------------------------- one log
    @delivery_trace.observed('ingestion', chat_arg=1)
    def _sync_log(self, chat_id: str, log_name: str, *, lane: str) -> int:
        """Read one log from its stored offset. Returns how many were new."""
        with self._write_cycle():
            position = self.store.capture_log_position(chat_id, log_name)
            offset = position.offset
            started = delivery_trace.queue_clock()
            read_failed = False
            try:
                records, new_offset = self.tx.read_log(chat_id, log_name, offset)
            except BaseException:
                read_failed = True
                raise
            finally:
                if started is not None:
                    delivery_trace.emit('transport_read', chat=chat_id, status='error' if read_failed else 'ok',
                                        duration_ms=delivery_trace.elapsed_ms(started))
            if records:
                # R13.5 ingestion sanity: a per-device log is single-writer, so
                # every record's `from` must be that log's owner. Drop any that
                # claim another identity (a buggy/hostile client can't smuggle
                # records attributed to someone else through its own log).
                owner = log_name.split("@", 1)[0]
                records = [r for r in records if r.get("from") == owner]
            observed = time.time_ns()
            observed_mono = time.perf_counter_ns()
            inserted = self.store.ingest_log(
                chat_id, log_name, position, new_offset, records,
                observed_ns=observed, observed_mono=observed_mono,
                observed_clock=clock_id())
            new = len(inserted)
            if inserted:
                if inserted and self.on_chat_progress is not None:
                    try:
                        self.on_chat_progress(chat_id, len(inserted))
                    except Exception:  # noqa: BLE001 — a wake can't break sync
                        pass
                if inserted and self.on_progress is not None:
                    try:
                        self.on_progress(len(inserted))
                    except Exception:  # noqa: BLE001 — a wake can't break sync
                        pass
                for record in inserted:
                    self.latency.observe(
                        "sync_observed", str(record.get("id") or ""),
                        lane=lane, at_ns=observed)
                if inserted and self.on_records is not None:
                    try:
                        self.on_records(chat_id, inserted)
                    except Exception:  # noqa: BLE001 — pump can't break sync
                        pass
            return new

    # ------------------------------------------------------------- one chat
    def sync_chat(self, chat_id: str, *, lane: str = "startup") -> int:
        """Pull new records for one chat. Returns how many were new."""
        with self._write_cycle():
            new = 0
            for log_name, size in self.tx.list_logs(chat_id):
                offset = self.store.get_offset(chat_id, log_name)
                if size == offset:
                    continue  # unchanged (size is the cheap change indicator)
                new += self._sync_log(chat_id, log_name, lane=lane)
            return new

    # ------------------------------------------------------------ all chats
    def my_chat_ids(self) -> list[str]:
        return [c for c in self.tx.list_chat_ids() if self.is_member(c)]

    def _scan(self, ids: list[str], *, lane: str) -> int:
        """Per-chat catch-up, parallel across chats."""
        with self._write_cycle():
            if not ids:
                return 0
            if len(ids) == 1:
                return self.sync_chat(ids[0], lane=lane)
            with ThreadPoolExecutor(max_workers=min(self.workers, len(ids))) as pool:
                return sum(pool.map(lambda cid: self.sync_chat(cid, lane=lane), ids))

    def sync_once(self, chat_ids: Iterable[str] | None = None, *,
                  lane: str = "startup") -> int:
        """One catch-up pass (startup after downtime, poll tick). An explicit
        ``chat_ids`` always takes the per-chat path; otherwise the change
        feed serves the tick when the transport offers one."""
        with self._write_cycle():
            if chat_ids is not None:
                return self._scan(list(chat_ids), lane=lane)
            ids = self.my_chat_ids()
            if not self.tx.has_change_feed:
                self._known = set(ids)
                return self._scan(ids, lane=lane)
            return self._feed_tick(ids, lane=lane)

    def _feed_tick(self, ids: list[str], *, lane: str) -> int:
        with self._write_cycle():
            new = 0
            # newly-visible chats (join, first run of this process) get one full
            # scan — their history may sit BELOW the feed cursor
            joined = [c for c in ids if c not in self._known]
            if joined:
                new += self._scan(joined, lane=lane)
            self._known = set(ids)
            member = self._known
            doc = self.store.cached_doc(CURSOR_DOC, default=None)
            cursor = int(doc["cursor"]) if isinstance(doc, dict) and "cursor" in doc \
                else 0
            try:
                pairs, new_cursor = self.tx.changed_logs(cursor)
            except Exception:  # noqa: BLE001 — network blip: same cursor next tick
                return new
            # NOTE: chats in `joined` stay in todo even though they were just
            # scanned — a record can land between that scan and the feed query,
            # and skipping them here would advance the cursor past it. The
            # re-read is idempotent (offsets) and only happens on a join/boot tick.
            todo = [(c, log) for (c, log) in pairs if c in member]
            ok = True
            for chat_id, log_name in todo:
                try:
                    found = self._sync_log(chat_id, log_name, lane=lane)
                    new += found
                except Exception:  # noqa: BLE001 — keep cursor, retry next tick
                    ok = False
            if ok and new_cursor != cursor:
                # only advance once every named log was read (idempotent by
                # offsets — re-reading a log we already caught costs nothing)
                self.store.cache_doc(CURSOR_DOC, {"cursor": new_cursor})
            return new

    # ------------------------------------------------------------- run loop
    def run(
        self,
        *,
        poll_s: float = 5.0,
        on_new: Callable[[int], None] | None = None,
    ) -> None:
        """Blocking loop: watcher hint OR poll timeout -> rescan. Call
        ``stop()`` from another thread to exit. A failing pass never kills
        the loop (a cloud transport can throw after its retries — the next
        tick heals).

        ``poll_s`` is the caller's cadence on a FREE transport; a metered
        one answers ``suggest_poll_s`` with its profile's slow safety poll
        (R76) — pokes keep message latency instant either way."""
        watcher = self.tx.watch()
        last_hinted: bool | None = None
        try:
            while not self._stop.is_set():
                try:
                    lane_for = getattr(self.tx, "latency_lane", None)
                    lane = (lane_for(last_hinted) if callable(lane_for) else
                            "startup" if last_hinted is None else
                            "hint" if last_hinted else "poll")
                    new = self.sync_once(lane=lane)
                except Exception:  # noqa: BLE001 — transient: next tick retries
                    new = 0
                if last_hinted is not None:
                    self.tx.note_log_poll(changed=bool(new), hinted=last_hinted)
                if new and on_new:
                    on_new(new)
                if self._stop.is_set():
                    break
                last_hinted = watcher.wait(self.tx.suggest_poll_s(poll_s))
        finally:
            watcher.close()

    def stop(self) -> None:
        self._stop.set()
