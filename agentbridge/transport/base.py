"""Transport interface — the ONLY layer that touches bytes-at-rest.

A transport moves the logical records of docs/FORMAT2.md over some shared
storage. Drivers: ``folder`` (OneDrive/Drive/SharePoint synced folder — files)
today, ``supabase`` (tables + storage + realtime) in R23. Everything above
this layer is storage-agnostic.

Contract highlights (the parts that make a sync transport reliable):
- ``put_doc`` is ATOMIC and retries transient locks; readers never see half a
  document. ``get_doc`` tolerates missing/corrupt (returns default).
- ``append_log`` appends exactly one record; ``read_log`` is INCREMENTAL by
  opaque offset and only advances past COMPLETE records (a half-synced line is
  left for a later pass; a shrunken file resets the offset — callers dedup by
  record id).
- ``watch()`` returns a HINT-only watcher: it may wake early on changes but
  the caller's timed rescan remains the source of truth (FORMAT2 tenet 6).
- Paths are POSIX-style, RELATIVE, and validated — a transport must refuse
  any path that escapes its root.

Adding a connector = subclass Transport, implement the abstract methods, and
register the scheme in ``make_transport``. The REQUIRED surface is enough for
full correctness; two OPTIONAL fast paths make a high-RTT (cloud) driver feel
local, and both degrade gracefully when absent:
- ``get_docs(prefix)`` — bulk-read every doc in one round-trip. The default
  loops ``list_docs``+``get_doc`` (fine locally, slow over a network); a cloud
  driver should override it with one query. The mirror cache (cache.py) warms
  and refreshes from this.
- ``changed_logs(cursor)`` + ``has_change_feed = True`` — a global,
  monotonic change feed over the message logs ("which (chat, log) have rows
  newer than this opaque cursor?"). Lets the sync engine poll ALL chats in
  one round-trip instead of listing logs per chat. Leave ``has_change_feed``
  False (the default) and the sync engine sticks to the per-chat scan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Transport", "TransportProfile", "Watcher"]


@dataclass(frozen=True)
class TransportProfile:
    """A connector's declared ECONOMICS (R76 — docs/SCALING.md §4). Every
    cadence in the app reads from here; no caller may hard-code a poll rate.
    The synced-folder defaults keep today's behaviour: polling a local folder
    is free, so nothing slows down. A metered (cloud API) driver declares
    itself and the mirror/sync/presence layers adapt: hint-woken pulls with
    slow safety polls instead of fast fixed loops."""

    metered: bool = False          # polls cost real quota (egress/requests)?
    supports_doc_delta: bool = False  # get_docs_delta(cursor) implemented?
    idle_poll_s: float = 4.0       # safety poll while hints look healthy
    fallback_poll_s: float = 4.0   # poll while hints are absent/suspect
    reconcile_s: float = 0.0       # full-snapshot healing interval (0 = never)
    presence_beat_s: float = 12.0  # presence heartbeat write cadence
    presence_stale_s: float = 40.0  # >= beat + worst poll + margin
    # doc classes whose writers DELIBERATELY never poke (heartbeats): the
    # mirror's hint watchdog must not read their silent arrival as "hints
    # lost" — without this, presence beats trip the fallback cadence forever
    silent_prefixes: tuple[str, ...] = ()


class Watcher:
    """Best-effort change hint. ``wait`` blocks up to ``timeout`` seconds and
    returns True if a change was hinted (clearing the hint). The default
    implementation never hints — pure polling."""

    def wait(self, timeout: float) -> bool:
        import time

        time.sleep(timeout)
        return False

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class Transport(ABC):
    scheme: str = "abstract"

    # Ceiling is a property of the TRANSPORT, not the app: a synced folder
    # pushes every attachment to each member's machine; an API store has its
    # own service limits. The GUI names this limit in the too-large dialog.
    max_upload_bytes: int = 512 * 1024 * 1024

    # the declared economics (R76) — see TransportProfile above. The default
    # is the free-local-poll profile; a metered driver MUST override this.
    profile: TransportProfile = TransportProfile()

    def suggest_poll_s(self, default: float) -> float:
        """The safety-poll cadence a hint-woken loop should use right now.
        Free transports keep the caller's default; a metered driver's mirror
        wrapper answers from its profile + live hint health (cache.py)."""
        return default

    # ------------------------------------------------------------------ docs
    @abstractmethod
    def get_doc(self, path: str, default: Any = None) -> Any:
        """JSON document at ``path``; missing/corrupt -> ``default``."""

    @abstractmethod
    def put_doc(self, path: str, data: Any) -> None:
        """Atomically replace the document (creating parents)."""

    @abstractmethod
    def delete_doc(self, path: str) -> None:
        """Remove a document; missing is not an error."""

    @abstractmethod
    def list_docs(self, prefix: str) -> list[str]:
        """Paths of ``.json`` documents under ``prefix`` (recursive)."""

    def get_docs(self, prefix: str = "") -> dict[str, Any]:
        """OPTIONAL fast path: every doc under ``prefix`` at once. This
        default loops the required methods (fine on a local driver); a cloud
        driver should override it with ONE bulk query — the mirror cache
        warms from it. Unlike ``get_doc`` this may RAISE on failure, so the
        caller can tell "store is empty" apart from "network is down"."""
        _absent = object()
        out: dict[str, Any] = {}
        for path in self.list_docs(prefix):
            value = self.get_doc(path, _absent)
            if value is not _absent:
                out[path] = value
        return out

    # OPTIONAL fast path (R76): an incremental doc feed, the docs twin of
    # ``changed_logs``. A driver that can answer "which docs changed since
    # cursor X?" sets ``profile.supports_doc_delta`` and implements both of
    # these; the mirror cache then refreshes by delta instead of re-pulling
    # the full snapshot (docs/SCALING.md).
    def snapshot_docs(self) -> tuple[dict[str, Any], int]:
        """Every live doc plus the delta cursor the snapshot is current AT.
        May RAISE on failure (same contract as ``get_docs``)."""
        return self.get_docs(""), 0

    def get_docs_delta(self, cursor: int) -> tuple[dict[str, Any], set[str], int]:
        """``(changed, deleted_paths, new_cursor)`` for docs newer than the
        opaque ``cursor``. ``changed`` maps path -> new value; ``deleted``
        names docs removed since. May RAISE on failure — the caller retries
        with the same cursor (idempotent by construction)."""
        raise NotImplementedError(f"{type(self).__name__} has no doc delta feed")

    # ----------------------------------------------------------- chats / logs
    @abstractmethod
    def list_chat_ids(self) -> list[str]: ...

    # OPTIONAL fast path: a driver with a global, monotonic change feed over
    # its logs sets this True and overrides changed_logs — the sync engine
    # then polls every chat in ONE round-trip instead of listing logs per chat
    has_change_feed: bool = False

    def changed_logs(self, cursor: int) -> tuple[list[tuple[str, str]], int]:
        """``(chat_id, log_name)`` pairs holding records newer than the opaque
        ``cursor``, plus the new cursor (pass 0 for "everything"). Only called
        when ``has_change_feed`` is True. May RAISE on failure — the caller
        retries with the same cursor next tick."""
        raise NotImplementedError(f"{type(self).__name__} has no change feed")

    @abstractmethod
    def list_logs(self, chat_id: str) -> list[tuple[str, int]]:
        """``(log_name, size)`` for every message log of the chat. ``size``
        is an opaque change indicator (file bytes / row high-water)."""

    @abstractmethod
    def append_log(self, chat_id: str, log_name: str, record: dict) -> None:
        """Append ONE record to the (single-writer, per-device) log."""

    @abstractmethod
    def read_log(
        self, chat_id: str, log_name: str, offset: int = 0
    ) -> tuple[list[dict], int]:
        """Records after ``offset`` plus the new offset. Only complete,
        parseable records are returned; the offset never lands mid-record."""

    @abstractmethod
    def delete_chat(self, chat_id: str) -> None:
        """Remove a chat subtree (admin-gated far above this layer)."""

    # ----------------------------------------------------------------- blobs
    @abstractmethod
    def put_blob(self, path: str, data: bytes) -> None: ...

    @abstractmethod
    def put_blob_from(self, local_src: Path, path: str) -> None:
        """Copy a LOCAL file into the store (attachments inbound)."""

    @abstractmethod
    def get_blob(self, path: str) -> bytes | None: ...

    @abstractmethod
    def blob_size(self, path: str) -> int | None: ...

    def delete_blob(self, path: str) -> None:
        """Remove ONE stored blob (the storage janitor, V63). Default: not
        supported — the janitor then reclaims nothing on this transport."""

    def local_path(self, path: str) -> Path | None:
        """Real filesystem path for folder-backed stores, else None — the seam
        where open-with-OS / preview features degrade for API backends."""
        return None

    # ---------------------------------------------------------------- events
    def watch(self) -> Watcher:
        """A change-hint watcher (default: pure polling)."""
        return Watcher()
