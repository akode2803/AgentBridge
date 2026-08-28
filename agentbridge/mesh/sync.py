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
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from ..store.db import Store
from ..core.latency import clock_id, sink_for_store
from ..transport.base import Transport

__all__ = ["SyncEngine"]

# where the change-feed cursor persists (the store's local doc cache)
CURSOR_DOC = "sync/log_cursor"


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
    ) -> None:
        self.tx = tx
        self.store = store
        self.is_member = is_member
        self.workers = workers
        self.on_records = on_records  # fed ONLY actually-new records (R10 bus)
        self.on_progress = on_progress  # per-log wake after local insertion
        self.latency = sink_for_store(store)
        self._stop = threading.Event()
        # membership as of the last feed tick — a chat that APPEARS here gets
        # one full scan (its history may predate the cursor). Deliberately
        # in-memory: a fresh process full-scans once, which also heals any
        # miss while it was down.
        self._known: set[str] = set()

    # -------------------------------------------------------------- one log
    def _sync_log(self, chat_id: str, log_name: str, *, lane: str) -> int:
        """Read one log from its stored offset. Returns how many were new."""
        offset = self.store.get_offset(chat_id, log_name)
        records, new_offset = self.tx.read_log(chat_id, log_name, offset)
        if records:
            # R13.5 ingestion sanity: a per-device log is single-writer, so
            # every record's `from` must be that log's owner. Drop any that
            # claim another identity (a buggy/hostile client can't smuggle
            # records attributed to someone else through its own log).
            owner = log_name.split("@", 1)[0]
            records = [r for r in records if r.get("from") == owner]
        new = 0
        if records:
            observed = time.time_ns()
            observed_mono = time.perf_counter_ns()
            inserted = self.store.upsert_messages(
                chat_id, records, observed_ns=observed,
                observed_mono=observed_mono,
                observed_clock=clock_id())
            new = len(inserted)
            for record in inserted:
                self.latency.observe(
                    "sync_observed", str(record.get("id") or ""),
                    lane=lane, at_ns=observed)
            if inserted and self.on_records is not None:
                try:
                    self.on_records(chat_id, inserted)
                except Exception:  # noqa: BLE001 — pump can't break sync
                    pass
            if inserted and self.on_progress is not None:
                try:
                    self.on_progress(len(inserted))
                except Exception:  # noqa: BLE001 — a wake can't break sync
                    pass
        if new_offset != offset:
            self.store.set_offset(chat_id, log_name, new_offset)
        return new

    # ------------------------------------------------------------- one chat
    def sync_chat(self, chat_id: str, *, lane: str = "startup") -> int:
        """Pull new records for one chat. Returns how many were new."""
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
        if chat_ids is not None:
            return self._scan(list(chat_ids), lane=lane)
        ids = self.my_chat_ids()
        if not self.tx.has_change_feed:
            self._known = set(ids)
            return self._scan(ids, lane=lane)
        return self._feed_tick(ids, lane=lane)

    def _feed_tick(self, ids: list[str], *, lane: str) -> int:
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
            except Exception:  # noqa: BLE001 — keep the cursor, retry next tick
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
