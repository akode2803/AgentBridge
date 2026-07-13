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
from .base import Transport, Watcher

__all__ = ["SupabaseTransport", "load_supabase_env"]

BUCKET = "ab-mesh"
ENV_FILE = "supabase.env"
_RETRIES = 3
_RETRY_WAIT = 0.4


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
                last = e
                time.sleep(_RETRY_WAIT * (i + 1))
        raise last

    # ------------------------------------------------------------------ docs
    def get_doc(self, path: str, default: Any = None) -> Any:
        path = _check(path)
        try:
            # retried like every other op: without it a single transient fault
            # read as "doc missing" and the read cache pinned that miss — chats
            # and profiles flickered out of the GUI (the R29 instability)
            rows = self._retry(
                lambda: self._sb().table("ab_docs").select("data")
                .eq("root", self.root).eq("path", path).limit(1).execute()
            ).data
        except Exception:  # noqa: BLE001 — unreadable == missing (contract)
            return default
        return rows[0]["data"] if rows else default

    def get_docs(self, prefix: str = "") -> dict[str, Any]:
        """EVERY doc under ``prefix`` in one paged query — the bulk read the
        mirror cache (cache.py) warms and refreshes from. Unlike ``get_doc``
        this RAISES on failure: the mirror must be able to tell 'the store is
        empty' apart from 'the network is down' (stale beats vanished)."""
        prefix = _check(prefix) if prefix else ""
        out: dict[str, Any] = {}
        page = 1000
        start = 0
        while True:
            def fetch(lo: int = start):
                q = self._sb().table("ab_docs").select("path,data") \
                    .eq("root", self.root)
                if prefix:
                    q = q.like("path", f"{prefix}%")
                return q.order("path").range(lo, lo + page - 1).execute()
            rows = self._retry(fetch).data
            for r in rows:
                out[str(r["path"])] = r["data"]
            if len(rows) < page:
                return out
            start += page

    def put_doc(self, path: str, data: Any) -> None:
        path = _check(path)
        self._retry(lambda: self._sb().table("ab_docs").upsert({
            "root": self.root, "path": path, "data": data,
        }).execute())
        self._hint()

    def delete_doc(self, path: str) -> None:
        path = _check(path)
        self._retry(lambda: self._sb().table("ab_docs").delete()
                    .eq("root", self.root).eq("path", path).execute())

    def list_docs(self, prefix: str) -> list[str]:
        prefix = _check(prefix) if prefix else ""
        q = self._sb().table("ab_docs").select("path").eq("root", self.root)
        if prefix:
            q = q.like("path", f"{prefix}%")
        rows = self._retry(lambda: q.execute()).data
        return sorted(r["path"] for r in rows
                      if str(r.get("path", "")).endswith(".json"))

    # ----------------------------------------------------------- chats / logs
    def list_chat_ids(self) -> list[str]:
        rows = self._retry(lambda: self._sb().rpc(
            "ab_chat_ids", {"p_root": self.root}).execute()).data
        return sorted({r["chat_id"] for r in rows if r.get("chat_id")})

    def list_logs(self, chat_id: str) -> list[tuple[str, int]]:
        chat_id = _check(chat_id)
        rows = self._retry(lambda: self._sb().rpc(
            "ab_list_logs", {"p_root": self.root, "p_chat": chat_id})
            .execute()).data
        return sorted((r["log_name"], int(r["head"])) for r in rows)

    def append_log(self, chat_id: str, log_name: str, record: dict) -> None:
        chat_id, log_name = _check(chat_id), _check(log_name)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._retry(lambda: self._sb().table("ab_logs").insert({
            "root": self.root, "chat_id": chat_id,
            "log_name": log_name, "line": line,
        }).execute())
        self._hint()

    def read_log(
        self, chat_id: str, log_name: str, offset: int = 0
    ) -> tuple[list[dict], int]:
        chat_id, log_name = _check(chat_id), _check(log_name)
        rows = self._retry(lambda: self._sb().table("ab_logs")
                           .select("id,line").eq("root", self.root)
                           .eq("chat_id", chat_id).eq("log_name", log_name)
                           .gt("id", int(offset)).order("id").execute()).data
        out: list[dict] = []
        new_offset = int(offset)
        for r in rows:
            try:
                out.append(json.loads(r["line"]))
                new_offset = int(r["id"])
            except (TypeError, ValueError):
                new_offset = int(r["id"])   # a bad row is skipped, not re-read
        return out, new_offset

    def delete_chat(self, chat_id: str) -> None:
        chat_id = _check(chat_id)
        sb = self._sb()
        self._retry(lambda: sb.table("ab_logs").delete()
                    .eq("root", self.root).eq("chat_id", chat_id).execute())
        self._retry(lambda: sb.table("ab_docs").delete()
                    .eq("root", self.root)
                    .like("path", f"chats/{chat_id}/%").execute())
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
            return self._store().download(f"{self.root}/{path}")
        except Exception:  # noqa: BLE001 — missing/unreadable -> None
            return None

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

    # ---------------------------------------------------------------- events
    def watch(self) -> Watcher:
        w = _HintWatcher(self)
        self._watchers.append(w)
        self._ensure_rt()
        return w

    def _hint(self) -> None:
        """Announce a change on the root's broadcast channel (fire & forget —
        the hint is garnish; the poll is truth)."""
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
        if self._rt is not None:
            self._rt.close()
            self._rt = None


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
