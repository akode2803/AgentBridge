"""Supabase transport (R23, D2) — the cloud realtime driver behind the same
Transport contract as the synced folder.

Mapping (schema in ``docs/supabase_schema.sql``, pasted once by the owner):
- docs  -> ``ab_docs``  (root, path, jsonb) — put_doc is one atomic upsert;
- logs  -> ``ab_logs``  (one row per record; the row id IS the read offset,
  so ``read_log`` is a WHERE id > cursor — no half-synced-line problem by
  construction);
- blobs -> one private Storage bucket ("ab-mesh"), keys ``<root>/<path>``;
- hints -> a realtime BROADCAST channel per root: every writer announces
  after a write, every watcher wakes early. The channel lives on a daemon
  thread with its own event loop (supabase realtime is async-only — the R1
  note); if the socket is blocked or drops, everything silently degrades to
  pure polling, because the poll stays the source of truth (tenet 6).

Trust model v1: only the SECRET key talks to the project (RLS enabled with
no policies, so the publishable key can do nothing). Per-member Supabase
auth + real RLS policies is a later round. E2EE is unchanged — bodies and
files arrive here already sealed; the server stores ciphertext (D2).

Credentials come from ``~/.agentbridge/supabase.env`` (or the process env);
they are never committed and never live in the mesh root string, which is
just ``supabase://<root-name>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from .base import Transport, TransportProfile, Watcher

__all__ = ["SupabaseTransport", "load_supabase_env"]

BUCKET = "ab-mesh"
ENV_FILE = "supabase.env"
_RETRIES = 3
_RETRY_WAIT = 0.4

# R76 (docs/SCALING.md §3) — writer-side hint coalescing. A hint is a
# content-free wake-up; per-class intervals bound how long a write may wait
# for its poke. None = never poke (safety polls carry it). First match wins.
_HINT_CLASSES: list[tuple[str, float | None]] = [
    ("presence/", None),      # heartbeats never poke; flips use hint_now()
    ("status/asks/", 1.0),    # permission prompts are latency-critical (V85)
    ("status/", 5.0),         # run-feed spinners: progress, not content
]
_HINT_STATE_S = 10.0          # chats/*/state/* (read receipts) may settle lazily
_HINT_DEFAULT_S = 1.0         # meta/roster/overlays/settings: user-visible, rare
_HINT_LOG_S = 0.5             # message appends: latency IS the product
# tells "the schema is missing the R76 columns" apart from a network fault —
# only these flip the driver into legacy full-snapshot mode
_MISSING_COL_MARKS = ("42703", "PGRST204", "does not exist", "Could not find")
_DELTA_REPROBE_S = 60.0       # legacy mode re-probes (a paste upgrades live)


def _is_missing_column(err: Exception) -> bool:
    s = str(err)
    return any(m in s for m in _MISSING_COL_MARKS)


def load_supabase_env(home: Path | None = None) -> dict[str, str]:
    """URL + keys from ``<home>/supabase.env``, overlaid by the process env
    (the env wins, so deployments can inject without a file)."""
    from ..core.config import DEFAULT_HOME

    out: dict[str, str] = {}
    path = (home or DEFAULT_HOME) / ENV_FILE
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_PUBLISHABLE_KEY"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def _check(path: str) -> str:
    """POSIX-relative path discipline, same as the folder driver."""
    p = (path or "").replace("\\", "/").strip("/")
    if not p or ".." in p.split("/"):
        raise ValidationError(f"bad transport path: {path!r}")
    return p


class SupabaseTransport(Transport):
    scheme = "supabase"
    max_upload_bytes = 50 * 1024 * 1024   # storage free-tier per-object cap
    has_change_feed = True                # ab_logs row ids are the feed
    # the declared economics (R76): every request is metered egress, so the
    # mirror/sync/presence layers slow their safety polls and lean on hints
    profile = TransportProfile(
        metered=True, supports_doc_delta=True,
        idle_poll_s=45.0, fallback_poll_s=10.0, reconcile_s=6 * 3600.0,
        presence_beat_s=30.0, presence_stale_s=120.0,
        silent_prefixes=("presence/",),   # mirrors _HINT_CLASSES' None entry
    )

    def __init__(self, root: str, *, env: dict[str, str] | None = None,
                 home: Path | None = None, client=None) -> None:
        self.root = (root or "mesh").strip("/ ") or "mesh"
        self._env = env or load_supabase_env(home)
        # the local-cache identity: unique per (project, root) — two projects
        # sharing a root name must never share a SQLite cache
        self.cache_key = f"supabase:{self._env.get('SUPABASE_URL', '')}:{self.root}"
        self._client = client              # tests inject a fake here
        self._client_lock = threading.Lock()
        self._rt = None                    # the realtime hint thread
        self._watchers: list[_HintWatcher] = []
        self._bucket_ready = False
        # R76: does ab_docs carry the delta columns (seq/deleted)? None =
        # unprobed; False re-probes on a slow leash so pasting the migration
        # upgrades a live fleet without restarts.
        self._delta: bool | None = None
        self._delta_reprobe = 0.0
        self._ret_min: bool | None = None  # library accepts returning="minimal"?
        self._hints = _HintCoalescer(self._send_hint)
        self._stats_lock = threading.Lock()
        self._stats = {"queries": 0, "rx_bytes": 0, "blob_bytes": 0,
                       "since": time.time()}

    @property
    def host(self) -> str:
        """Project host for status displays (``<ref>.supabase.co``) — the
        URL carries no credentials, but only the netloc is surfaced."""
        from urllib.parse import urlsplit

        url = self._env.get("SUPABASE_URL", "")
        return urlsplit(url).netloc or url

    # ------------------------------------------------------------- client
    def _sb(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    from supabase import create_client

                    url = self._env.get("SUPABASE_URL", "")
                    key = self._env.get("SUPABASE_SECRET_KEY", "")
                    if not url or not key:
                        raise ValidationError(
                            "Supabase credentials missing — put SUPABASE_URL "
                            "and SUPABASE_SECRET_KEY in ~/.agentbridge/"
                            "supabase.env")
                    self._client = create_client(url, key)
        return self._client

    def _retry(self, fn):
        last = None
        for i in range(_RETRIES):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 — transient network faults
                if _is_missing_column(e):
                    raise            # deterministic: retrying can't help
                last = e
                time.sleep(_RETRY_WAIT * (i + 1))
        raise last

    def _count(self, rows: Any = None, blob: int = 0) -> None:
        """Approximate transfer bookkeeping for the About panel + soak
        measurements (SCALING.md §4 checklist item 6). ``repr`` length is a
        cheap, good-enough proxy for response bytes."""
        with self._stats_lock:
            self._stats["queries"] += 1
            if rows is not None:
                self._stats["rx_bytes"] += len(repr(rows))
            self._stats["blob_bytes"] += blob

    def transfer_stats(self) -> dict:
        with self._stats_lock:
            out = dict(self._stats)
        out["mode"] = ("delta" if self._delta
                       else "legacy" if self._delta is False else "unprobed")
        return out

    # ------------------------------------------------------------ delta probe
    def _delta_ok(self) -> bool:
        """Is the R76 migration (ab_docs.seq/deleted) live? Probes once; a
        legacy verdict re-probes every ``_DELTA_REPROBE_S`` so pasting the
        migration upgrades the fleet within a minute, no restart. A NETWORK
        fault leaves the verdict unchanged (never downgrades a working
        delta mode to legacy full snapshots)."""
        if self._delta is True:
            return True
        if self._delta is False and time.monotonic() < self._delta_reprobe:
            return False
        try:
            self._sb().table("ab_docs").select("seq").limit(1).execute()
            self._delta = True
        except Exception as e:  # noqa: BLE001
            if _is_missing_column(e):
                self._delta = False
                self._delta_reprobe = time.monotonic() + _DELTA_REPROBE_S
            elif self._delta is None:
                # can't tell yet (offline?) — stay unprobed, decide later
                return False
        return bool(self._delta)

    # ------------------------------------------------------------------ docs
    def get_doc(self, path: str, default: Any = None) -> Any:
        path = _check(path)
        try:
            # retried like every other op: without it a single transient fault
            # read as "doc missing" and the read cache pinned that miss — chats
            # and profiles flickered out of the GUI (the R29 instability)
            def fetch():
                q = self._sb().table("ab_docs").select("data") \
                    .eq("root", self.root).eq("path", path)
                if self._delta_ok():  # a soft-deleted doc reads as missing
                    q = q.eq("deleted", False)
                return q.limit(1).execute()
            rows = self._retry(fetch).data
        except Exception:  # noqa: BLE001 — unreadable == missing (contract)
            return default
        self._count(rows)
        return rows[0]["data"] if rows else default

    def get_docs(self, prefix: str = "") -> dict[str, Any]:
        """EVERY doc under ``prefix`` in one paged query — the legacy bulk
        read (``snapshot_docs`` is the delta-aware variant the mirror uses).
        Unlike ``get_doc`` this RAISES on failure: the mirror must be able to
        tell 'the store is empty' apart from 'the network is down' (stale
        beats vanished)."""
        prefix = _check(prefix) if prefix else ""
        out: dict[str, Any] = {}
        page = 1000
        start = 0
        while True:
            def fetch(lo: int = start):
                q = self._sb().table("ab_docs").select("path,data") \
                    .eq("root", self.root)
                if self._delta_ok():
                    q = q.eq("deleted", False)
                if prefix:
                    q = q.like("path", f"{prefix}%")
                return q.order("path").range(lo, lo + page - 1).execute()
            rows = self._retry(fetch).data
            self._count(rows)
            for r in rows:
                out[str(r["path"])] = r["data"]
            if len(rows) < page:
                return out
            start += page

    # ------------------------------------------------------- delta feed (R76)
    def snapshot_docs(self) -> tuple[dict[str, Any], int]:
        """Full snapshot + the cursor it is current at. The cursor is read
        BEFORE the snapshot: a row updated mid-pull gets a later seq than the
        cursor, so the next delta re-fetches it — never stale, only an
        idempotent overlap (SCALING.md §2)."""
        if not self._delta_ok():
            return self.get_docs(""), 0
        def head():
            return self._sb().table("ab_docs").select("seq") \
                .eq("root", self.root).order("seq", desc=True) \
                .limit(1).execute()
        rows = self._retry(head).data
        cursor = int(rows[0]["seq"]) if rows else 0
        return self.get_docs(""), cursor

    def get_docs_delta(self, cursor: int) -> tuple[dict[str, Any], set[str], int]:
        """Docs whose seq moved past ``cursor``: ``(changed, deleted, new
        cursor)``. Seq-keyed pagination (no offset paging over a moving set);
        rows apply in seq order so the final state of a path wins."""
        if not self._delta_ok():
            # the mirror falls back to a full pull on this signal — the
            # legacy schema has no cursor to serve (base contract)
            raise NotImplementedError("doc delta feed unavailable (legacy schema)")
        changed: dict[str, Any] = {}
        deleted: set[str] = set()
        last = int(cursor)
        page = 1000
        while True:
            def fetch(lo: int = last):
                return self._sb().table("ab_docs") \
                    .select("path,data,seq,deleted").eq("root", self.root) \
                    .gt("seq", lo).order("seq").limit(page).execute()
            rows = self._retry(fetch).data
            self._count(rows)
            for r in rows:
                path = str(r["path"])
                last = max(last, int(r["seq"]))
                if r.get("deleted"):
                    deleted.add(path)
                    changed.pop(path, None)
                else:
                    changed[path] = r["data"]
                    deleted.discard(path)
            if len(rows) < page:
                return changed, deleted, last

    # ----------------------------------------------------------------- writes
    def _write(self, build):
        """Run a write built by ``build(returning_kwargs)`` echo-free when the
        library supports it (``returning="minimal"`` saves the row echo on
        every write — measurable egress at heartbeat volume)."""
        if self._ret_min is not False:
            try:
                return self._retry(build({"returning": "minimal"}))
            except TypeError:        # older postgrest-py: no kwarg
                self._ret_min = False
        return self._retry(build({}))

    def put_doc(self, path: str, data: Any) -> None:
        path = _check(path)
        row = {"root": self.root, "path": path, "data": data}
        if self._delta_ok():
            row["deleted"] = False   # writing a doc revives a tombstone
        def build(kw):
            def run():
                return self._sb().table("ab_docs").upsert(row, **kw).execute()
            return run
        try:
            self._write(build)
        except Exception as e:  # noqa: BLE001
            if not (_is_missing_column(e) and "deleted" in row):
                raise
            # probe said delta but the write says legacy (mid-migration
            # race): flip modes and land the write the old way
            self._delta = False
            self._delta_reprobe = time.monotonic() + _DELTA_REPROBE_S
            row.pop("deleted")
            self._write(build)
        self._count()
        self._hint_for(path)

    def delete_doc(self, path: str) -> None:
        path = _check(path)
        if self._delta_ok():
            # SOFT delete: the tombstone rides the delta feed to every
            # mirror; the janitor purges old tombstones (reconciles heal
            # anything offline longer). UPDATE, not upsert — deleting a doc
            # that never existed must not mint a row.
            def build(kw):
                def run():
                    return self._sb().table("ab_docs") \
                        .update({"deleted": True, "data": {}}, **kw) \
                        .eq("root", self.root).eq("path", path).execute()
                return run
            self._write(build)
        else:
            self._retry(lambda: self._sb().table("ab_docs").delete()
                        .eq("root", self.root).eq("path", path).execute())
        self._count()
        self._hint_for(path)

    def purge_deleted_docs(self, older_than_days: float = 30.0) -> None:
        """Hard-drop tombstones old enough that every live mirror has long
        seen them (the storage janitor calls this on its daily sweep)."""
        if not self._delta_ok():
            return
        cutoff = time.time() - older_than_days * 86400
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
        try:
            self._retry(lambda: self._sb().table("ab_docs").delete()
                        .eq("root", self.root).eq("deleted", True)
                        .lt("updated", iso).execute())
        except Exception:  # noqa: BLE001 — next sweep retries
            pass

    def list_docs(self, prefix: str) -> list[str]:
        prefix = _check(prefix) if prefix else ""
        def fetch():
            q = self._sb().table("ab_docs").select("path").eq("root", self.root)
            if self._delta_ok():
                q = q.eq("deleted", False)
            if prefix:
                q = q.like("path", f"{prefix}%")
            return q.execute()
        rows = self._retry(fetch).data
        self._count(rows)
        return sorted(r["path"] for r in rows
                      if str(r.get("path", "")).endswith(".json"))

    # ----------------------------------------------------------- chats / logs
    def list_chat_ids(self) -> list[str]:
        rows = self._retry(lambda: self._sb().rpc(
            "ab_chat_ids", {"p_root": self.root}).execute()).data
        self._count(rows)
        return sorted({r["chat_id"] for r in rows if r.get("chat_id")})

    def list_logs(self, chat_id: str) -> list[tuple[str, int]]:
        chat_id = _check(chat_id)
        rows = self._retry(lambda: self._sb().rpc(
            "ab_list_logs", {"p_root": self.root, "p_chat": chat_id})
            .execute()).data
        self._count(rows)
        return sorted((r["log_name"], int(r["head"])) for r in rows)

    def append_log(self, chat_id: str, log_name: str, record: dict) -> None:
        chat_id, log_name = _check(chat_id), _check(log_name)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        row = {"root": self.root, "chat_id": chat_id,
               "log_name": log_name, "line": line}
        def build(kw):
            def run():
                return self._sb().table("ab_logs").insert(row, **kw).execute()
            return run
        self._write(build)
        self._count()
        self._hints.request(_HINT_LOG_S)

    def read_log(
        self, chat_id: str, log_name: str, offset: int = 0
    ) -> tuple[list[dict], int]:
        chat_id, log_name = _check(chat_id), _check(log_name)
        rows = self._retry(lambda: self._sb().table("ab_logs")
                           .select("id,line").eq("root", self.root)
                           .eq("chat_id", chat_id).eq("log_name", log_name)
                           .gt("id", int(offset)).order("id").execute()).data
        self._count(rows)
        out: list[dict] = []
        new_offset = int(offset)
        for r in rows:
            try:
                out.append(json.loads(r["line"]))
                new_offset = int(r["id"])
            except (TypeError, ValueError):
                new_offset = int(r["id"])   # a bad row is skipped, not re-read
        return out, new_offset

    def changed_logs(self, cursor: int) -> tuple[list[tuple[str, str]], int]:
        """The R30 sync fast path: ``ab_logs`` row ids are globally monotonic
        (one identity column for the whole table), so "what changed since?"
        is ONE indexed query no matter how many chats exist. Idle ticks cost
        one empty-result round-trip instead of a list_logs RPC per chat."""
        rows = self._retry(
            lambda: self._sb().table("ab_logs").select("id,chat_id,log_name")
            .eq("root", self.root).gt("id", int(cursor)).order("id")
            .execute()
        ).data
        self._count(rows)
        new_cursor = int(cursor)
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for r in rows:
            new_cursor = max(new_cursor, int(r["id"]))
            key = (str(r["chat_id"]), str(r["log_name"]))
            if key not in seen:
                seen.add(key)
                pairs.append(key)
        return pairs, new_cursor

    def delete_chat(self, chat_id: str) -> None:
        chat_id = _check(chat_id)
        sb = self._sb()
        self._retry(lambda: sb.table("ab_logs").delete()
                    .eq("root", self.root).eq("chat_id", chat_id).execute())
        if self._delta_ok():
            # soft-delete the doc subtree so every mirror's delta feed sees
            # the chat vanish (hard-deleted rows are invisible to a cursor)
            def build(kw):
                def run():
                    return sb.table("ab_docs") \
                        .update({"deleted": True, "data": {}}, **kw) \
                        .eq("root", self.root) \
                        .like("path", f"chats/{chat_id}/%").execute()
                return run
            self._write(build)
        else:
            self._retry(lambda: sb.table("ab_docs").delete()
                        .eq("root", self.root)
                        .like("path", f"chats/{chat_id}/%").execute())
        self._hint_for(f"chats/{chat_id}/meta.json")
        try:  # blobs are best-effort cleanup
            store = sb.storage.from_(BUCKET)
            for area in ("files",):
                objs = store.list(f"{self.root}/chats/{chat_id}/{area}")
                names = [f"{self.root}/chats/{chat_id}/{area}/{o['name']}"
                         for o in objs or []]
                if names:
                    store.remove(names)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- blobs
    def _store(self):
        sb = self._sb()
        if not self._bucket_ready:
            try:
                if BUCKET not in [b.name for b in sb.storage.list_buckets()]:
                    sb.storage.create_bucket(BUCKET)
            except Exception:  # noqa: BLE001 — races with another creator
                pass
            self._bucket_ready = True
        return sb.storage.from_(BUCKET)

    def put_blob(self, path: str, data: bytes) -> None:
        path = _check(path)
        if len(data) > self.max_upload_bytes:
            raise ValidationError("file exceeds the storage limit")
        self._retry(lambda: self._store().upload(
            f"{self.root}/{path}", data,
            file_options={"content-type": "application/octet-stream",
                          "upsert": "true"}))

    def put_blob_from(self, local_src: Path, path: str) -> None:
        self.put_blob(path, Path(local_src).read_bytes())

    def get_blob(self, path: str) -> bytes | None:
        path = _check(path)
        try:
            data = self._store().download(f"{self.root}/{path}")
        except Exception:  # noqa: BLE001 — missing/unreadable -> None
            return None
        self._count(blob=len(data) if data else 0)
        return data

    def blob_size(self, path: str) -> int | None:
        path = _check(path)
        full = f"{self.root}/{path}"
        parent, _, name = full.rpartition("/")
        try:
            for o in self._store().list(parent) or []:
                if o.get("name") == name:
                    meta = o.get("metadata") or {}
                    return int(meta.get("size") or 0) or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def delete_blob(self, path: str) -> None:
        path = _check(path)
        try:  # idempotent — a missing object is already the goal (V63)
            self._retry(lambda: self._store().remove([f"{self.root}/{path}"]))
        except Exception:  # noqa: BLE001 — next sweep retries
            pass

    # ---------------------------------------------------------------- events
    def watch(self) -> Watcher:
        w = _HintWatcher(self)
        self._watchers.append(w)
        self._ensure_rt()
        return w

    def _hint_for(self, path: str) -> None:
        """Class-coalesced change poke (R76): latency-critical writes
        announce fast, chatty maintenance classes batch, presence never
        pokes (SCALING.md §3). The hint stays garnish; polls stay truth."""
        for prefix, interval in _HINT_CLASSES:
            if path.startswith(prefix):
                self._hints.request(interval)
                return
        if "/state/" in path:
            self._hints.request(_HINT_STATE_S)
            return
        self._hints.request(_HINT_DEFAULT_S)

    def hint_now(self) -> None:
        """Immediate poke for rare, latency-critical moments outside the
        class table (presence flips on sign-in/out)."""
        self._hints.request(0.0)

    def _send_hint(self) -> None:
        rt = self._ensure_rt()
        if rt is not None:
            rt.send()

    def _on_hint(self) -> None:
        for w in list(self._watchers):
            w.poke()

    def _ensure_rt(self):
        if self._rt is None:
            try:
                self._rt = _RealtimeThread(self._env, self.root, self._on_hint)
            except Exception:  # noqa: BLE001 — no realtime = poll-only
                self._rt = None
        return self._rt

    def close(self) -> None:
        self._hints.close()
        if self._rt is not None:
            self._rt.close()
            self._rt = None


class _HintCoalescer:
    """Trailing-edge poke batcher (R76): ``request(interval)`` guarantees a
    broadcast fires within ``interval`` seconds while a global floor caps the
    send rate. A burst's LAST write always gets announced (the trailing
    edge) — without it the final change of a burst would sit unannounced
    until a safety poll."""

    FLOOR_S = 0.25   # min spacing between sends (hard rate cap)

    def __init__(self, send) -> None:
        self._send = send
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._due: float | None = None   # monotonic deadline of next send
        self._last = 0.0                 # monotonic of last send
        self._thread: threading.Thread | None = None
        self._stop = False

    def request(self, interval: float | None) -> None:
        if interval is None or self._stop:
            return
        with self._lock:
            due = max(time.monotonic() + interval, self._last + self.FLOOR_S)
            if self._due is None or due < self._due:  # only ever pulls EARLIER
                self._due = due
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="ab-hint-coalesce")
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop:
            with self._lock:
                due = self._due
            if due is None:
                self._wake.wait(30.0)    # parked until the next request
                self._wake.clear()
                continue
            delay = due - time.monotonic()
            if delay > 0:
                self._wake.wait(delay)
                self._wake.clear()
                with self._lock:
                    if self._due is not None and time.monotonic() < self._due:
                        continue         # pulled earlier mid-sleep: re-evaluate
            with self._lock:
                self._due = None
                self._last = time.monotonic()
            try:
                self._send()
            except Exception:  # noqa: BLE001 — the hint is garnish
                pass

    def close(self) -> None:
        self._stop = True
        self._wake.set()


class _HintWatcher(Watcher):
    def __init__(self, tx: SupabaseTransport) -> None:
        self._tx = tx
        self._event = threading.Event()

    def poke(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        hit = self._event.wait(timeout)
        self._event.clear()
        return hit

    def close(self) -> None:
        try:
            self._tx._watchers.remove(self)
        except ValueError:
            pass


class _RealtimeThread:
    """One daemon thread owning the async realtime channel for a root.
    Sends and receives change hints; any failure just goes quiet."""

    def __init__(self, env: dict[str, str], root: str, on_hint) -> None:
        self._env = env
        self._root = root
        self._on_hint = on_hint
        self._loop = asyncio.new_event_loop()
        self._channel = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ab-supabase-rt")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001 — hint channel death is silent
            pass

    async def _main(self) -> None:
        from realtime import RealtimeChannelOptions
        from supabase import acreate_client

        sb = await acreate_client(self._env.get("SUPABASE_URL", ""),
                                  self._env.get("SUPABASE_SECRET_KEY", ""))
        self._channel = sb.channel(
            f"ab-{self._root}",
            RealtimeChannelOptions(config={"broadcast": {"self": False}}))
        self._channel.on_broadcast("change", lambda _p: self._on_hint())
        await self._channel.subscribe()
        self._ready.set()
        while True:                       # parked; sends ride this loop
            await asyncio.sleep(3600)

    def send(self) -> None:
        if not self._ready.is_set() or self._channel is None:
            return

        async def _send():
            try:
                await self._channel.send_broadcast("change", {"r": 1})
            except Exception:  # noqa: BLE001
                pass

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass
