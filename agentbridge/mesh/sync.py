"""Sync engine — pulls new envelope records from the transport into the local
cache, incrementally (stored byte offsets) and in parallel across chats.

Serves requirement "mesh fetches only what it needs": the engine takes an
``is_member`` gate and never even reads logs of chats the identity isn't in.
The run loop follows FORMAT2 tenet 6: the transport watcher only SHORTENS the
wait; the rescan is what finds changes.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from ..store.db import Store
from ..transport.base import Transport

__all__ = ["SyncEngine"]


class SyncEngine:
    def __init__(
        self,
        tx: Transport,
        store: Store,
        *,
        is_member: Callable[[str], bool] = lambda chat_id: True,
        workers: int = 4,
    ) -> None:
        self.tx = tx
        self.store = store
        self.is_member = is_member
        self.workers = workers
        self._stop = threading.Event()

    # ------------------------------------------------------------- one chat
    def sync_chat(self, chat_id: str) -> int:
        """Pull new records for one chat. Returns how many were new."""
        new = 0
        for log_name, size in self.tx.list_logs(chat_id):
            offset = self.store.get_offset(chat_id, log_name)
            if size == offset:
                continue  # unchanged (size is the cheap change indicator)
            records, new_offset = self.tx.read_log(chat_id, log_name, offset)
            if records:
                new += self.store.upsert_messages(chat_id, records)
            if new_offset != offset:
                self.store.set_offset(chat_id, log_name, new_offset)
        return new

    # ------------------------------------------------------------ all chats
    def my_chat_ids(self) -> list[str]:
        return [c for c in self.tx.list_chat_ids() if self.is_member(c)]

    def sync_once(self, chat_ids: Iterable[str] | None = None) -> int:
        """Parallel catch-up across chats (startup after downtime, poll tick)."""
        ids = list(chat_ids) if chat_ids is not None else self.my_chat_ids()
        if not ids:
            return 0
        if len(ids) == 1:
            return self.sync_chat(ids[0])
        with ThreadPoolExecutor(max_workers=min(self.workers, len(ids))) as pool:
            return sum(pool.map(self.sync_chat, ids))

    # ------------------------------------------------------------- run loop
    def run(
        self,
        *,
        poll_s: float = 5.0,
        on_new: Callable[[int], None] | None = None,
    ) -> None:
        """Blocking loop: watcher hint OR poll timeout -> rescan. Call
        ``stop()`` from another thread to exit."""
        watcher = self.tx.watch()
        try:
            while not self._stop.is_set():
                new = self.sync_once()
                if new and on_new:
                    on_new(new)
                watcher.wait(poll_s)
        finally:
            watcher.close()

    def stop(self) -> None:
        self._stop.set()
