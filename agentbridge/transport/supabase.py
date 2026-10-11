"""Supabase transport (R23, D2) — the cloud realtime driver behind the same
Transport contract used by the mesh, GUI, CLI and harness.

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

Trust model v2 (R84, docs/SECURITY_RLS.md): each mesh member holds their
OWN Supabase auth credential (``SUPABASE_MEMBER_EMAIL``/``_PASSWORD`` +
the publishable key) and RLS policies scope rows to chat membership — the
chat's own meta doc is the ACL. When member credentials are present the
driver signs in as that member; otherwise it falls back to the v1 SECRET
key (which bypasses RLS), so a mixed fleet keeps working through the
migration. A member sign-in failure also falls back (with a loud mode
flag) rather than bricking the fleet. E2EE is unchanged either way —
bodies and files arrive here already sealed; the server stores
ciphertext (D2).

Credentials come from ``~/.agentbridge/supabase.env`` (or the process env);
they are never committed and never live in the mesh root string, which is
just ``supabase://<root-name>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from ..core.errors import TransportError, ValidationError
from .base import Transport, TransportProfile, Watcher
from .change_ledger import (
    ChangeLedgerCapability,
    ChangeLedgerEpoch,
    ChangeLedgerEvent,
    ChangeLedgerPage,
    MAX_LEDGER_INTEGER,
    MAX_LEDGER_PAGE_SIZE,
)
from .health import classify_transport_error, retry_inline
from .recovery_sources import (
    MAX_RECOVERY_PAGE_ROWS,
    RECOVERY_RESPONSE_OVERHEAD,
    RECOVERY_SCHEMA_VERSION,
    RecoveryChat,
    RecoveryCut,
    RecoveryPage,
    RecoveryStream,
    validate_recovery_after,
)
from .scoped_sources import (
    MAX_SOURCE_BYTES,
    ScopedDocumentBatch,
    ScopedDocumentRow,
    ScopedLogPage,
    ScopedLogRow,
    ScopedSourceOverflow,
    SOURCE_ROW_WIRE_OVERHEAD,
    validate_source_budget,
    validate_source_cursor,
    validate_source_key,
    validate_source_paths,
    validate_source_stream,
)
from .source_ledger import SourceLedgerEvent, SourceLedgerFence, SourceLedgerPage

__all__ = ["SupabaseTransport", "load_supabase_env"]

BUCKET = "ab-mesh"
ENV_FILE = "supabase.env"
_RETRIES = 2
_RETRY_WAIT = 0.4
_POSTGREST_TIMEOUT_S = 6
_STORAGE_TIMEOUT_S = 20
_FUNCTION_TIMEOUT_S = 6
_RT_MAX_BACKOFF_S = 15.0

# R76 (docs/SCALING.md §3) — writer-side hint coalescing. A hint is a
# content-free wake-up; per-class intervals bound how long a write may wait
# for its poke. None = never poke (safety polls carry it). First match wins.
_HINT_CLASSES: list[tuple[str, float | None]] = [
    ("presence/", None),      # heartbeats never poke; flips use hint_now()
    ("status/asks/", 1.0),    # legacy test/old-client lane only
    ("status/", 5.0),         # run-feed spinners: progress, not content
]
_HINT_STATE_S = 0.5           # read receipts are visible delivery feedback
_HINT_DEFAULT_S = 0.25        # rare user-visible meta/roster/overlay changes
_HINT_LOG_S = 0.1             # first idle message pokes promptly; floor caps bursts
# tells "the schema is missing the R76 columns" apart from a network fault —
# only these flip the driver into legacy full-snapshot mode
_MISSING_COL_MARKS = (
    "42703", "PGRST202", "PGRST204", "does not exist", "Could not find",
)
_DELTA_REPROBE_S = 60.0       # legacy mode re-probes (a paste upgrades live)
_LEDGER_REPROBE_S = 60.0
_LEDGER_SCHEMA_VERSION = 1
_SOURCE_LEDGER_SCHEMA_VERSION = 2
_SOURCE_LEDGER_REPROBE_S = 60.0
_SCOPED_SOURCE_SCHEMA_VERSION = 1
_SCOPED_SOURCE_REPROBE_S = 60.0
_RECOVERY_SOURCE_REPROBE_S = 60.0


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
    for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_PUBLISHABLE_KEY",
              "SUPABASE_MEMBER_EMAIL", "SUPABASE_MEMBER_PASSWORD"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


# marks of an expired/invalid auth token — the member session heals itself
_AUTH_EXPIRED_MARKS = ("JWT expired", "PGRST301", "invalid JWT", "jwt expired")


def _is_auth_expired(err: Exception) -> bool:
    s = str(err)
    return any(m.lower() in s.lower() for m in _AUTH_EXPIRED_MARKS)


def _is_rls_denied(err: Exception) -> bool:
    """PostgREST can surface a missing/stale member session as the same
    42501 row-policy denial as a genuine authorization failure. Treat only
    that narrow shape as a re-auth candidate; the retry still has to pass the
    unchanged policy."""
    s = str(err).lower()
    return "42501" in s and "row-level security policy" in s


def _is_unique_violation(err: Exception) -> bool:
    return "23505" in str(err)


def _check(path: str) -> str:
    """Validate POSIX-relative logical record paths."""
    p = (path or "").replace("\\", "/").strip("/")
    if not p or ".." in p.split("/"):
        raise ValidationError(f"bad transport path: {path!r}")
    return p


class SupabaseTransport(Transport):
    supports_exclusive_create = True
    scheme = "supabase"
    max_upload_bytes = 50 * 1024 * 1024   # storage free-tier per-object cap
    has_change_feed = True                # ab_logs row ids are the feed
    # the declared economics (R76): every request is metered egress, so the
    # mirror/sync/presence layers slow their safety polls and lean on hints
    profile = TransportProfile(
        metered=True, supports_doc_delta=True,
        idle_poll_s=45.0, fallback_poll_s=10.0, reconcile_s=6 * 3600.0,
        # Each durable heartbeat is also an API Gateway log entry.  A 45-second
        # beat reduces the idle baseline while remaining inside older clients'
        # 120-second stale window after their own 45-second safety read.  New
        # readers retain more crash-detection jitter with the larger window.
        presence_beat_s=45.0, presence_stale_s=180.0,
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
        self.auth_mode = "injected" if client is not None else ""
        self._client_lock = threading.Lock()
        self._rt = None                    # the realtime hint thread
        self._rt_lock = threading.Lock()
        self._rt_state_lock = threading.Lock()
        self._closed = False
        self._rt_failures = 0
        self._rt_retry_at = 0.0
        self._rt_ready_since = 0.0
        self._watchers: list[_HintWatcher] = []
        self._ledger_listener_lock = threading.Lock()
        self._ledger_listeners: list[Any] = []
        self._bucket_ready = False
        # R76: does ab_docs carry the delta columns (seq/deleted)? None =
        # unprobed; False re-probes on a slow leash so pasting the migration
        # upgrades a live fleet without restarts.
        self._delta: bool | None = None
        self._delta_reprobe = 0.0
        self._ret_min: bool | None = None  # library accepts returning="minimal"?
        self._effects_ready: bool | None = None
        self._effects_reprobe = 0.0
        self._ledger_ready: bool | None = None
        self._ledger_reprobe = 0.0
        self._source_ledger_ready: bool | None = None
        self._source_ledger_reprobe = 0.0
        self._scoped_source_ready: bool | None = None
        self._scoped_source_reprobe = 0.0
        self._recovery_source_ready: bool | None = None
        self._recovery_source_reprobe = 0.0
        self._hints = _HintCoalescer(self._send_hint)
        self._stats_lock = threading.Lock()
        self._stats = {"queries": 0, "rx_bytes": 0, "blob_bytes": 0,
                       "rt_open_attempts": 0, "rt_ready": 0,
                       "rt_disconnects": 0, "rt_socket_closes": 0,
                       "rt_active": 0, "rt_active_peak": 0,
                       "broadcast_sent": 0, "broadcast_failures": 0,
                       "broadcast_skipped": 0,
                       "ledger_events": 0, "ledger_ready": 0,
                       "ledger_invalid_events": 0,
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
                    from supabase.client import ClientOptions

                    url = self._env.get("SUPABASE_URL", "")
                    email = self._env.get("SUPABASE_MEMBER_EMAIL", "")
                    pw = self._env.get("SUPABASE_MEMBER_PASSWORD", "")
                    pub = self._env.get("SUPABASE_PUBLISHABLE_KEY", "")
                    secret = self._env.get("SUPABASE_SECRET_KEY", "")
                    if not url or not (secret or (email and pw and pub)):
                        raise ValidationError(
                            "Supabase credentials missing — put SUPABASE_URL "
                            "and either a member credential (R84) or "
                            "SUPABASE_SECRET_KEY in ~/.agentbridge/"
                            "supabase.env")
                    # The library default is 120 seconds for PostgREST. A weak
                    # connection then makes the local GUI look frozen even
                    # though its server is healthy. Keep every provider call
                    # bounded; the mirror/outbox own retries across calls.
                    options = ClientOptions(
                        postgrest_client_timeout=_POSTGREST_TIMEOUT_S,
                        storage_client_timeout=_STORAGE_TIMEOUT_S,
                        function_client_timeout=_FUNCTION_TIMEOUT_S,
                    )

                    def connect(key: str):
                        try:
                            return create_client(url, key, options=options)
                        except TypeError:
                            # Compatibility with older supabase-py releases
                            # (and small injected test factories) that predate
                            # the options parameter.
                            return create_client(url, key)
                    # R84: member auth first — the RLS trust model. A failed
                    # sign-in FALLS BACK to the service key (never brick the
                    # fleet), and the About panel shows the honest mode.
                    if email and pw and pub:
                        try:
                            client = connect(pub)
                            client.auth.sign_in_with_password(
                                {"email": email, "password": pw})
                            self.auth_mode = ("member:"
                                              + email.split("@", 1)[0])
                            self._client = client
                            return self._client
                        except Exception:  # noqa: BLE001 — fall back below
                            self.auth_mode = ("member-signin-FAILED"
                                              + (":service" if secret else ""))
                            if not secret:
                                raise
                    if self._client is None:
                        self._client = connect(secret)
                        if not self.auth_mode.startswith("member-signin"):
                            self.auth_mode = "service"
        return self._client

    def _refresh_auth(self, *, fresh: bool = False) -> bool:
        """Best-effort member-session refresh after a JWT-expired error —
        the library refreshes on its own; this is the belt for long-lived
        fleet processes whose timer thread died or drifted. ``fresh`` skips
        the refresh token and signs in from the local member credential; RLS
        42501 is ambiguous, so that path gets exactly one clean re-auth before
        the unchanged policy is allowed to fail for real."""
        client, mode = self._client, self.auth_mode
        if client is None or not mode.startswith("member:"):
            return False
        if not fresh:
            try:
                client.auth.refresh_session()
                return True
            except Exception:  # noqa: BLE001 — fresh sign-in is last resort
                pass
        try:
            client.auth.sign_in_with_password({
                "email": self._env.get("SUPABASE_MEMBER_EMAIL", ""),
                "password": self._env.get("SUPABASE_MEMBER_PASSWORD", ""),
            })
            return True
        except Exception:  # noqa: BLE001 — the retry loop reports it
            return False

    def _retry(self, fn):
        last = None
        rls_reauth_attempted = False
        for i in range(_RETRIES):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 — transient network faults
                if _is_missing_column(e) or _is_unique_violation(e):
                    raise            # deterministic: retrying can't help
                healed = False
                if _is_auth_expired(e):
                    healed = self._refresh_auth()
                elif (
                    not rls_reauth_attempted
                    and self.auth_mode.startswith("member:")
                    and _is_rls_denied(e)
                ):
                    # A signed-out/stale client produces the SAME 42501 as a
                    # real policy refusal. One fresh sign-in is safe: the next
                    # attempt still runs under the unchanged RLS policy.
                    rls_reauth_attempted = True
                    healed = self._refresh_auth(fresh=True)
                last = e
                if healed:
                    continue
                # Quota/auth/policy/schema/rate-limit failures are not healed
                # by replaying the same request milliseconds later. Let the
                # mirror's circuit breaker choose a provider-appropriate delay.
                if not retry_inline(classify_transport_error(e)):
                    raise
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
        out["realtime"] = self.realtime_status()
        with self._rt_state_lock:
            retry_at = self._rt_retry_at
        out["rt_retry_in_s"] = max(0.0, retry_at - time.monotonic())
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

    # ----------------------------------------------------- durable change log
    @property
    def supports_change_ledger(self) -> bool:  # type: ignore[override]
        return self.change_ledger_capability() is not None

    def change_ledger_capability(self) -> ChangeLedgerCapability | None:
        """Probe the exact observation-only ledger schema on a slow leash."""
        now = time.monotonic()
        if self._ledger_ready is True:
            return ChangeLedgerCapability(_LEDGER_SCHEMA_VERSION)
        if self._ledger_ready is False and now < self._ledger_reprobe:
            return None
        try:
            ready = self._retry(
                lambda: self._sb().rpc("ab_change_ledger_ready").execute()
            ).data
            self._count(ready)
            self._ledger_ready = ready == _LEDGER_SCHEMA_VERSION
        except Exception as exc:  # noqa: BLE001 - unavailable retains polling
            if _is_missing_column(exc):
                self._ledger_ready = False
            elif self._ledger_ready is None:
                return None
        if not self._ledger_ready:
            self._ledger_reprobe = now + _LEDGER_REPROBE_S
            return None
        return ChangeLedgerCapability(_LEDGER_SCHEMA_VERSION)

    def change_ledger_epoch(self) -> ChangeLedgerEpoch:
        if self.change_ledger_capability() is None:
            raise TransportError("Supabase change ledger is unavailable")
        rows = self._retry(lambda: self._sb().table("ab_change_epochs")
                           .select("epoch,minimum_cursor,schema_version")
                           .eq("root", self.root).limit(1).execute()).data
        self._count(rows)
        if len(rows) != 1:
            self._ledger_ready = False
            raise TransportError("Supabase change ledger epoch is unavailable")
        try:
            epoch = ChangeLedgerEpoch(
                str(rows[0].get("epoch", "")),
                rows[0].get("minimum_cursor"),
                rows[0].get("schema_version"),
            )
        except ValueError as exc:
            raise TransportError("Supabase change ledger epoch is invalid") from exc
        if epoch.schema_version != _LEDGER_SCHEMA_VERSION:
            self._ledger_ready = False
            raise TransportError("Supabase change ledger version is unsupported")
        return epoch

    def change_ledger_events(
        self, after_cursor: int, *, limit: int,
    ) -> ChangeLedgerPage:
        capability = self.change_ledger_capability()
        if capability is None:
            raise TransportError("Supabase change ledger is unavailable")
        if type(after_cursor) is not int \
                or after_cursor < 0 or after_cursor > MAX_LEDGER_INTEGER:
            raise ValueError("after_cursor must be a non-negative integer")
        if type(limit) is not int or limit < 1 or limit > capability.max_page_size:
            raise ValueError("invalid change ledger page limit")
        rows = self._retry(lambda: self._sb().rpc(
            "ab_change_events_page", {
                "p_root": self.root,
                "p_after": after_cursor,
                "p_limit": limit,
            },
        ).execute()).data
        self._count(rows)
        try:
            if len(rows) > limit:
                raise ValueError("provider exceeded the requested page limit")
            events = tuple(ChangeLedgerEvent(
                event_id=row.get("id"),
                stream_kind=row.get("stream_kind"),
                stream_id=row.get("stream_id"),
                domain=row.get("domain"),
                doc_head=row.get("doc_head"),
                log_head=row.get("log_head"),
            ) for row in rows)
            # A full page conservatively promises another bounded probe.  This
            # avoids relying on PostgREST returning limit+1 through a provider
            # response-row cap; the possible final empty probe is intentional.
            return ChangeLedgerPage(after_cursor, events, len(rows) == limit)
        except (AttributeError, TypeError, ValueError) as exc:
            raise TransportError("Supabase returned an invalid change ledger page") from exc

    def subscribe_change_ledger(self, callback):
        """Observe validated event IDs; callers still replay durable pages."""
        if not callable(callback):
            raise TypeError("change ledger callback must be callable")
        if self.change_ledger_capability() is None:
            raise NotImplementedError("Supabase change ledger is unavailable")
        with self._ledger_listener_lock:
            self._ledger_listeners.append(callback)
        self._ensure_rt()

        def unsubscribe() -> None:
            with self._ledger_listener_lock:
                if callback in self._ledger_listeners:
                    self._ledger_listeners.remove(callback)

        return unsubscribe

    def _ledger_observation_requested(self) -> bool:
        with self._ledger_listener_lock:
            return self._ledger_ready is True and bool(self._ledger_listeners)

    def _on_ledger_event(self, event_id: object) -> None:
        if type(event_id) is not int or event_id < 1 or event_id > MAX_LEDGER_INTEGER:
            self._rt_metric("ledger_invalid_events")
            return
        self._rt_metric("ledger_events")
        self._on_hint()
        with self._ledger_listener_lock:
            listeners = list(self._ledger_listeners)
        for callback in listeners:
            try:
                callback(event_id)
            except Exception:  # noqa: BLE001 - observer cannot break Realtime
                pass

    def change_ledger_realtime_status(self) -> str:
        if self._ledger_observation_requested():
            self._ensure_rt()
        rt = self._rt
        if rt is None or not rt.observes_change_ledger():
            return "disconnected" if self._ledger_ready else "unsupported"
        return rt.change_ledger_status()

    # -------------------------------------- local-node exact source ledger
    @property
    def supports_source_ledger(self) -> bool:  # type: ignore[override]
        return self._source_ledger_capability()

    def _source_ledger_capability(self) -> bool:
        now = time.monotonic()
        if self._source_ledger_ready is True:
            return True
        if (self._source_ledger_ready is False
                and now < self._source_ledger_reprobe):
            return False
        try:
            ready = self._retry(
                lambda: self._sb().rpc("ab_node_source_ledger_ready").execute(),
            ).data
            self._count(ready)
            self._source_ledger_ready = (
                type(ready) is int and ready == _SOURCE_LEDGER_SCHEMA_VERSION)
        except Exception as exc:  # noqa: BLE001 - optional capability
            if _is_missing_column(exc):
                self._source_ledger_ready = False
            elif self._source_ledger_ready is None:
                return False
        if not self._source_ledger_ready:
            self._source_ledger_reprobe = now + _SOURCE_LEDGER_REPROBE_S
        return bool(self._source_ledger_ready)

    def _require_source_ledger(self) -> None:
        if not self._source_ledger_capability():
            raise NotImplementedError("Supabase source ledger is unavailable")

    def source_ledger_fence(self) -> SourceLedgerFence:
        self._require_source_ledger()
        raw = self._retry(lambda: self._sb().rpc(
            "ab_node_source_ledger_fence", {"p_root": self.root},
        ).execute()).data
        self._count(raw)
        try:
            if type(raw) is not list or len(raw) != 1 or type(raw[0]) is not dict:
                raise ValueError("invalid source ledger fence row")
            row = raw[0]
            fence = SourceLedgerFence(
                str(row.get("epoch", "")), row.get("minimum_cursor"),
                row.get("cursor"), row.get("schema_version"),
            )
            if fence.schema_version != _SOURCE_LEDGER_SCHEMA_VERSION:
                raise ValueError("unsupported source ledger fence")
            return fence
        except (AttributeError, TypeError, ValueError) as exc:
            raise TransportError("Supabase returned an invalid source ledger fence") from exc

    def source_ledger_events(
        self, after_cursor: int, *, limit: int,
    ) -> SourceLedgerPage:
        self._require_source_ledger()
        if (type(after_cursor) is not int or after_cursor < 0
                or after_cursor > MAX_LEDGER_INTEGER):
            raise ValueError("after_cursor must be a non-negative integer")
        if type(limit) is not int or limit < 1 or limit > MAX_LEDGER_PAGE_SIZE:
            raise ValueError("invalid source ledger page limit")
        raw = self._retry(lambda: self._sb().rpc(
            "ab_node_source_events_page", {
                "p_root": self.root, "p_after": after_cursor, "p_limit": limit,
            },
        ).execute()).data
        self._count(raw)
        try:
            if type(raw) is not list or len(raw) > limit:
                raise ValueError("provider exceeded source ledger page")
            required = {
                "id", "stream_kind", "stream_id", "domain", "source_key",
                "doc_head", "log_head",
            }
            if any(type(row) is not dict or not required.issubset(row) for row in raw):
                raise ValueError("incomplete source ledger event")
            events = tuple(SourceLedgerEvent(
                event_id=row["id"], stream_kind=row["stream_kind"],
                stream_id=row["stream_id"], domain=row["domain"],
                source_key=row["source_key"], doc_head=row["doc_head"],
                log_head=row["log_head"],
            ) for row in raw)
            return SourceLedgerPage(after_cursor, events, len(raw) == limit)
        except (AttributeError, TypeError, ValueError) as exc:
            raise TransportError("Supabase returned an invalid source ledger page") from exc

    # -------------------------------------- local-node scoped source reads
    @property
    def supports_recovery_source(self) -> bool:  # type: ignore[override]
        return self._recovery_source_capability()

    def _recovery_source_capability(self) -> bool:
        now = time.monotonic()
        if (self._recovery_source_ready is not None
                and now < self._recovery_source_reprobe):
            return self._recovery_source_ready
        try:
            ready = self._retry(
                lambda: self._sb().rpc("ab_node_recovery_ready").execute(),
            ).data
            self._count(ready)
            self._recovery_source_ready = (
                type(ready) is int and ready == RECOVERY_SCHEMA_VERSION)
        except Exception as exc:  # noqa: BLE001 - optional capability
            if _is_missing_column(exc):
                self._recovery_source_ready = False
            elif self._recovery_source_ready is None:
                return False
        self._recovery_source_reprobe = now + _RECOVERY_SOURCE_REPROBE_S
        return bool(self._recovery_source_ready)

    def _require_recovery_source(self) -> None:
        if not self._recovery_source_capability():
            raise NotImplementedError("Supabase recovery source is unavailable")

    @staticmethod
    def _recovery_cut(raw: object) -> RecoveryCut:
        if type(raw) is not dict:
            raise ValueError("invalid recovery cut")
        required = {"schema_version", "index_contract", "source_schema_version",
                    "source_epoch", "account_id", "role", "minimum_cursor", "cursor"}
        if set(raw) != required:
            raise ValueError("incomplete recovery cut")
        return RecoveryCut(**{key: raw[key] for key in required})

    def recovery_fence(self) -> RecoveryCut:
        self._require_recovery_source()
        raw = self._retry(lambda: self._sb().rpc(
            "ab_node_recovery_fence", {"p_root": self.root},
        ).execute()).data
        self._count(raw)
        try:
            return self._recovery_cut(raw)
        except (AttributeError, TypeError, ValueError) as exc:
            raise TransportError("Supabase returned an invalid recovery fence") from exc

    def recovery_page(self, family: str, after: str | tuple[str, str] | int,
                      *, limit: int, max_bytes: int = MAX_SOURCE_BYTES) -> RecoveryPage:
        self._require_recovery_source()
        after = validate_recovery_after(after, family)
        if type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_PAGE_ROWS:
            raise ValueError("invalid recovery page limit")
        budget = validate_source_budget(max_bytes)
        request_continuation = (list(after) if family == "streams" else after)
        continuation_bytes = len(json.dumps(
            request_continuation, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        if budget < RECOVERY_RESPONSE_OVERHEAD + continuation_bytes:
            raise ValueError("recovery byte budget is too small")
        params = {"p_root": self.root, "p_family": family,
                  "p_after": after if type(after) is str else
                  after[0] if type(after) is tuple else "",
                  "p_after_log": after[1] if type(after) is tuple else "",
                  "p_after_id": after if type(after) is int else 0,
                  "p_limit": limit, "p_max_bytes": budget}
        raw = self._retry(lambda: self._sb().rpc(
            "ab_node_recovery_page", params,
        ).execute()).data
        self._count(raw)
        try:
            response_keys = {
                    "cut", "rows", "has_more", "empty_terminal", "overflow",
                    "outcome", "continuation",
            }
            if type(raw) is not dict or set(raw) != response_keys:
                raise ValueError("invalid recovery page response")
            cut = self._recovery_cut(raw["cut"])
            rows = raw["rows"]
            more = raw["has_more"]
            empty = raw["empty_terminal"]
            overflow = raw["overflow"]
            if (type(rows) is not list or len(rows) > limit
                    or any(type(value) is not bool for value in
                           (more, empty, overflow))
                    or empty != (not rows and not more and not overflow)):
                raise ValueError("invalid recovery page metadata")
            outcome = ("overflow" if overflow else "empty_terminal" if empty
                       else "page")
            if type(raw["outcome"]) is not str or raw["outcome"] != outcome:
                raise ValueError("invalid recovery outcome")
            if overflow:
                overflow_cursor = list(after) if family == "streams" else after
                if (rows or not more or
                        type(raw["continuation"]) is not type(overflow_cursor)
                        or raw["continuation"] != overflow_cursor):
                    raise ValueError("invalid recovery overflow")
                raise ScopedSourceOverflow("Supabase recovery page exceeds byte budget")
            if family == "documents":
                converted = []
                for row in rows:
                    if type(row) is not dict or set(row) != {
                            "path", "seq", "deleted", "data"}:
                        raise ValueError("invalid recovery document")
                    if type(row["deleted"]) is not bool or (
                            row["deleted"] and row["data"] is not None):
                        raise ValueError("invalid recovery tombstone")
                    payload = None if row["deleted"] else json.dumps(
                        row["data"], ensure_ascii=False, allow_nan=False,
                        sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                    converted.append(ScopedDocumentRow(
                        row["path"], row["seq"], row["deleted"], payload))
            elif family == "chats":
                converted = [RecoveryChat(row) for row in rows]
            elif family == "streams":
                if any(type(row) is not dict or set(row) != {
                        "chat_id", "log_name", "head"} for row in rows):
                    raise ValueError("invalid recovery stream")
                converted = [RecoveryStream(**row) for row in rows]
            else:
                required = {"id", "stream_kind", "stream_id", "domain",
                            "source_key", "doc_head", "log_head"}
                if any(type(row) is not dict or set(row) != required for row in rows):
                    raise ValueError("invalid recovery event")
                converted = [SourceLedgerEvent(
                    row["id"], row["stream_kind"], row["stream_id"],
                    row["domain"], row["source_key"], row["doc_head"],
                    row["log_head"],
                ) for row in rows]
            page = RecoveryPage(family, after, cut, tuple(converted), more)
            continuation = (list(page.cursor) if family == "streams"
                            else page.cursor)
            if (type(raw["continuation"]) is not type(continuation)
                    or raw["continuation"] != continuation):
                raise ValueError("invalid recovery continuation")
            if len(json.dumps(raw, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")) > budget:
                raise ValueError("recovery wire response exceeded byte budget")
            return page
        except (AttributeError, TypeError, ValueError, UnicodeError,
                RecursionError) as exc:
            raise TransportError("Supabase returned an invalid recovery page") from exc

    # -------------------------------------- local-node scoped source reads
    @property
    def supports_scoped_source_reads(self) -> bool:  # type: ignore[override]
        return self._scoped_source_capability()

    def _scoped_source_capability(self) -> bool:
        now = time.monotonic()
        if self._scoped_source_ready is True:
            return True
        if (self._scoped_source_ready is False
                and now < self._scoped_source_reprobe):
            return False
        try:
            ready = self._retry(
                lambda: self._sb().rpc("ab_node_scoped_source_ready").execute(),
            ).data
            self._count(ready)
            self._scoped_source_ready = (
                type(ready) is int and ready == _SCOPED_SOURCE_SCHEMA_VERSION)
        except Exception as exc:  # noqa: BLE001 - optional capability
            if _is_missing_column(exc):
                self._scoped_source_ready = False
            elif self._scoped_source_ready is None:
                return False
        if not self._scoped_source_ready:
            self._scoped_source_reprobe = now + _SCOPED_SOURCE_REPROBE_S
        return bool(self._scoped_source_ready)

    def _require_scoped_source(self) -> None:
        if not self._scoped_source_capability():
            raise NotImplementedError("Supabase scoped source reads are unavailable")

    def source_documents(
        self, paths: tuple[str, ...], *, max_bytes: int = MAX_SOURCE_BYTES,
    ) -> ScopedDocumentBatch:
        self._require_scoped_source()
        requested = validate_source_paths(paths)
        budget = validate_source_budget(max_bytes)
        raw = self._retry(lambda: self._sb().rpc(
            "ab_node_docs_exact", {
                "p_root": self.root, "p_paths": list(requested),
                "p_max_bytes": budget,
            },
        ).execute()).data
        self._count(raw)
        required = {"path", "seq", "data", "deleted", "batch_overflow"}
        try:
            if (type(raw) is not list or len(raw) > len(requested)
                    or any(type(row) is not dict or not required.issubset(row)
                           for row in raw)):
                raise ValueError("invalid exact document response")
            overflow = tuple(row["batch_overflow"] for row in raw)
            if any(type(value) is not bool for value in overflow):
                raise ValueError("invalid exact document metadata")
            if any(overflow):
                if (len(raw) != 1 or overflow != (True,)
                        or any(raw[0][key] is not None
                               for key in ("path", "seq", "data", "deleted"))):
                    raise ValueError("invalid exact document overflow")
                raise ScopedSourceOverflow(
                    "Supabase document source batch exceeds byte budget",
                )
            rows = []
            total = 0
            for row in raw:
                deleted = row["deleted"]
                if type(deleted) is not bool or (deleted and row["data"] is not None):
                    raise ValueError("invalid exact document tombstone")
                payload = None if deleted else json.dumps(
                    row["data"], ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
                value = ScopedDocumentRow(
                    row["path"], row["seq"], deleted, payload,
                )
                total += len(json.dumps(
                    value.path, ensure_ascii=False,
                ).encode("utf-8")) + SOURCE_ROW_WIRE_OVERHEAD
                total += 0 if payload is None else len(payload)
                rows.append(value)
            if total > budget:
                raise ValueError("exact document response exceeded byte budget")
            return ScopedDocumentBatch(requested, tuple(rows))
        except (AttributeError, TypeError, ValueError, RecursionError,
                UnicodeError) as exc:
            raise TransportError(
                "Supabase returned an invalid exact document batch",
            ) from exc

    def source_log_page(
        self, chat_id: str, log_name: str, *, after_cursor: int,
        through_cursor: int, limit: int, max_bytes: int = MAX_SOURCE_BYTES,
    ) -> ScopedLogPage:
        self._require_scoped_source()
        chat = validate_source_stream(chat_id, "source chat")
        log = validate_source_key(log_name, "source log")
        after = validate_source_cursor(after_cursor, "source log cursor")
        through = validate_source_cursor(through_cursor, "source log cut")
        if after > through:
            raise ValueError("source log cursor exceeds its cut")
        if type(limit) is not int or not 1 <= limit <= MAX_LEDGER_PAGE_SIZE:
            raise ValueError("invalid source log page limit")
        budget = validate_source_budget(max_bytes)
        raw = self._retry(lambda: self._sb().rpc(
            "ab_node_log_exact_page", {
                "p_root": self.root, "p_chat": chat, "p_log": log,
                "p_after": after, "p_through": through, "p_limit": limit,
                "p_max_bytes": budget,
            },
        ).execute()).data
        self._count(raw)
        required = {"id", "line", "page_has_more", "page_overflow"}
        try:
            if (type(raw) is not list or len(raw) > limit
                    or any(type(row) is not dict or not required.issubset(row)
                           for row in raw)):
                raise ValueError("invalid exact log response")
            if not raw:
                return ScopedLogPage(after, through, (), False)
            overflow = tuple(row["page_overflow"] for row in raw)
            more = tuple(row["page_has_more"] for row in raw)
            if (any(type(value) is not bool for value in overflow + more)
                    or len(set(more)) != 1):
                raise ValueError("invalid exact log metadata")
            if any(overflow):
                if (len(raw) != 1 or overflow != (True,) or more != (True,)
                        or raw[0]["id"] is not None or raw[0]["line"] is not None):
                    raise ValueError("invalid exact log overflow")
                raise ScopedSourceOverflow(
                    "Supabase log source row exceeds byte budget",
                )
            rows = []
            total = 0
            for row in raw:
                if type(row["line"]) is not str:
                    raise ValueError("invalid exact log payload")
                payload = row["line"].encode("utf-8")
                total += len(json.dumps(
                    row["line"], ensure_ascii=False,
                ).encode("utf-8")) + SOURCE_ROW_WIRE_OVERHEAD
                rows.append(ScopedLogRow(row["id"], payload))
            if total > budget:
                raise ValueError("exact log response exceeded byte budget")
            return ScopedLogPage(after, through, tuple(rows), more[0])
        except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
            raise TransportError("Supabase returned an invalid exact log page") from exc

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

    def create_doc(self, path: str, data: Any) -> None:
        """Insert a brand-new document without PostgREST's UPSERT path.

        Supabase evaluates UPDATE authorization for an UPSERT even when the
        target row is absent. Chat genesis is intentionally INSERT-only under
        RLS, so using put_doc there denies a legitimate creator. An identical
        existing row is accepted as a response-lost retry; a conflicting row
        remains a hard failure.
        """
        path = _check(path)
        row = {"root": self.root, "path": path, "data": data}
        if self._delta_ok():
            row["deleted"] = False

        def build(kw):
            def run():
                return self._sb().table("ab_docs").insert(row, **kw).execute()
            return run

        try:
            self._write(build)
        except Exception as e:  # noqa: BLE001 - conflict is checked below
            if _is_unique_violation(e):
                absent = object()
                if self.get_doc(path, absent) == data:
                    self._count()
                    self._hint_for(path)
                    return
                raise
            elif _is_missing_column(e) and "deleted" in row:
                self._delta = False
                self._delta_reprobe = time.monotonic() + _DELTA_REPROBE_S
                row.pop("deleted")
                self._write(build)
            else:
                raise
        self._count()
        self._hint_for(path)

    def effect_claims_ready(self) -> bool:
        """Probe the exact member-authenticated R142 transition protocol."""
        now = time.monotonic()
        if self._effects_ready is True:
            return True
        if self._effects_ready is False and now < self._effects_reprobe:
            return False
        try:
            client = self._sb()
            if not self.auth_mode.startswith("member:"):
                self._effects_ready = False
            else:
                response = self._retry(
                    lambda: client.rpc("ab_effects_ready").execute())
                self._effects_ready = getattr(response, "data", None) == 1
                self._count()
        except Exception:
            self._effects_ready = False
        if not self._effects_ready:
            self._effects_reprobe = now + 60.0
        return bool(self._effects_ready)

    def create_effect_doc(self, path: str, data: Any, *,
                          ask_envelope: Any = None,
                          decision_envelope: Any = None) -> None:
        path = _check(path)
        if not self.effect_claims_ready():
            raise ValidationError(
                "Supabase effect transition protocol is unavailable")
        response = self._retry(lambda: self._sb().rpc(
            "ab_effect_transition", {
                "p_root": self.root, "p_path": path, "p_data": data,
                "p_grant_ask": ask_envelope,
                "p_grant_decision": decision_envelope,
            }).execute())
        self._count()
        if getattr(response, "data", None) is not True:
            raise ValidationError("Supabase rejected the effect transition")
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
            .limit(1_000)
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
        try:
            return self._strict_blob_size(path)
        except Exception:  # noqa: BLE001
            return None

    def _strict_blob_size(self, path: str) -> int | None:
        """Return exact object size; unlike the read API, propagate failures."""
        full = f"{self.root}/{_check(path)}"
        parent, _, name = full.rpartition("/")
        rows = self._retry(lambda: self._store().list(parent))
        for obj in rows or []:
            if obj.get("name") == name:
                meta = obj.get("metadata") or {}
                return int(meta.get("size") or 0) or None
        return None

    def delete_blob(self, path: str) -> None:
        path = _check(path)
        # A missing object is already the goal. When it is currently visible,
        # require Storage to confirm that exact full key in its delete result;
        # the SDK may otherwise return an empty success under RLS.
        if self._strict_blob_size(path) is None:
            return
        full = f"{self.root}/{path}"

        def remove() -> None:
            rows = self._store().remove([full])
            names = {
                str(row.get("name") or "") for row in (rows or [])
                if isinstance(row, dict)
            }
            if full not in names:
                raise TransportError("storage did not confirm exact blob deletion")

        self._retry(remove)

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
        if (path.startswith(("runtime/owner-control/",
                             "runtime/member-control/"))
                or "/runtime/owner-control/" in path
                or "/runtime/member-control/" in path):
            self._hints.request(1.0)  # signed runtime controls are latency-critical
            return
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

    def wake_local(self) -> None:
        """Wake this process's watchers without spending a cloud broadcast."""
        self._on_hint()

    def realtime_status(self) -> str:
        rt = self._rt
        return rt.status() if rt is not None else "disconnected"

    def _rt_metric(self, name: str) -> None:
        with self._stats_lock:
            if name in self._stats:
                self._stats[name] += 1
            if name == "rt_ready":
                self._stats["rt_active"] += 1
                self._stats["rt_active_peak"] = max(
                    self._stats["rt_active_peak"], self._stats["rt_active"])
            elif name == "rt_socket_closes":
                self._stats["rt_active"] = max(
                    0, self._stats["rt_active"] - 1)
        if name == "rt_ready":
            with self._rt_state_lock:
                self._rt_ready_since = time.monotonic()

    def _ensure_rt(self):
        with self._rt_lock:
            if self._closed:
                return None
            observe_ledger = self._ledger_observation_requested()
            if self._rt is not None and self._rt.alive():
                if (not observe_ledger or self._rt.observes_change_ledger()):
                    return (self._rt if self._rt.status() in {"connecting", "ready"}
                            else None)
            old, self._rt = self._rt, None
            if old is not None:
                failed = old.status() == "disconnected"
                old.close()
                if old.alive():
                    self._rt = old
                    return None
                if failed:
                    now = time.monotonic()
                    with self._rt_state_lock:
                        if (self._rt_ready_since
                                and now - self._rt_ready_since >= 30.0):
                            self._rt_failures = 0
                        self._rt_failures += 1
                        delay = min(2 ** min(self._rt_failures - 1, 6),
                                    _RT_MAX_BACKOFF_S)
                        self._rt_retry_at = now + min(
                            _RT_MAX_BACKOFF_S,
                            delay * (0.85 + random.random() * 0.3))
                        self._rt_ready_since = 0.0
                    self._rt_metric("rt_disconnects")
            with self._rt_state_lock:
                retry_at = self._rt_retry_at
            if time.monotonic() < retry_at:
                return None
            try:
                self._rt_metric("rt_open_attempts")
                self._rt = _RealtimeThread(
                    self._env, self.root, self._on_hint, self._rt_metric,
                    observe_ledger=observe_ledger,
                    on_ledger_event=self._on_ledger_event,
                    ledger_auth_mode=self.auth_mode,
                )
            except Exception:  # noqa: BLE001 — no realtime = poll-only
                self._rt = None
                with self._rt_state_lock:
                    self._rt_failures += 1
                    self._rt_retry_at = time.monotonic() + min(
                        2 ** min(self._rt_failures - 1, 6),
                        _RT_MAX_BACKOFF_S)
            return self._rt

    def close(self) -> None:
        self._hints.close()
        with self._rt_lock:
            self._closed = True
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
        self._tx._ensure_rt()
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

    def __init__(self, env: dict[str, str], root: str, on_hint,
                 on_metric=lambda _name: None, *, observe_ledger: bool = False,
                 on_ledger_event=lambda _event_id: None,
                 ledger_auth_mode: str = "") -> None:
        self._env = env
        self._root = root
        self._on_hint = on_hint
        self._on_metric = on_metric
        self._observe_ledger = bool(observe_ledger)
        self._on_ledger_event = on_ledger_event
        self._ledger_auth_mode = ledger_auth_mode
        self._loop = asyncio.new_event_loop()
        self._channel = None
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._state = "connecting"
        self._closing = False
        self._task = None
        self._started = threading.Event()
        self._counted_ready = False
        self._ledger_pg_ready = False
        self._ledger_replication_ready = False
        self._ledger_joined = False
        self._ledger_ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ab-supabase-rt")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._main())
            self._started.set()
            if self._closing:
                self._task.cancel()
            self._loop.run_until_complete(self._task)
        except BaseException:  # cancellation/failure falls back to polling
            pass
        finally:
            if not self._closing:
                self._mark_disconnected()
            # This thread owns the loop. Supabase Auth can leave its token
            # refresh timer pending beyond the channel task; cancel owned
            # tasks before closing so reconnects do not leak dead-loop work.
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(
                    *pending, return_exceptions=True,
                ))
            self._loop.close()

    def _mark_disconnected(self) -> None:
        """Publish one local wake when a ready/connecting socket drops.

        Watchers may already be sleeping on the healthy 45-second cadence.
        Waking them makes the next wait choose the foreground recovery cadence;
        it carries no provider data or authority verdict.
        """
        changed = False
        with self._state_lock:
            if not self._closing and self._state != "disconnected":
                self._state = "disconnected"
                self._ready.clear()
                self._ledger_ready.clear()
                changed = True
        if changed:
            try:
                self._on_hint()
            except Exception:  # noqa: BLE001 - disconnect still stands
                pass

    def _subscription_status(self, status, _error=None) -> None:
        value = getattr(status, "value", status)
        value = str(value or "").upper()
        with self._state_lock:
            if value == "SUBSCRIBED":
                self._state = "ready"
                self._ready.set()
                self._ledger_joined = True
                if not self._counted_ready:
                    self._counted_ready = True
                    self._on_metric("rt_ready")
                self._maybe_mark_ledger_ready()
        if value in {"TIMED_OUT", "CLOSED", "CHANNEL_ERROR"}:
            self._mark_disconnected()

    def _system_status(self, payload) -> None:
        extension = str(getattr(payload, "extension", "") or "")
        message = str(getattr(payload, "message", "") or "")
        status = str(getattr(payload, "status", "") or "").lower()
        if status != "ok":
            self._mark_disconnected()
            return
        if extension == "postgres_changes":
            self._ledger_pg_ready = True
        elif extension == "system" and message == "Replication connection established":
            self._ledger_replication_ready = True
        self._maybe_mark_ledger_ready()

    def _maybe_mark_ledger_ready(self) -> None:
        if (self._ledger_joined and self._ledger_pg_ready
                and self._ledger_replication_ready
                and not self._ledger_ready.is_set()):
            self._ledger_ready.set()
            self._on_metric("ledger_ready")

    def _postgres_change(self, payload) -> None:
        try:
            data = payload.get("data") or {}
            record = data.get("record") or {}
            if record.get("root") != self._root:
                raise ValueError("wrong root")
            event_id = record.get("id")
            if type(event_id) is not int:
                raise ValueError("invalid event id")
        except (AttributeError, TypeError, ValueError):
            self._on_metric("ledger_invalid_events")
            return
        self._on_ledger_event(event_id)

    async def _main(self) -> None:
        from realtime import RealtimeChannelOptions
        from supabase import acreate_client

        # Broadcast alone is public. Ledger observation is RLS-filtered and
        # therefore must join as the same member credential class as PostgREST,
        # or explicitly use the legacy service credential.
        publishable = self._env.get("SUPABASE_PUBLISHABLE_KEY", "")
        secret = self._env.get("SUPABASE_SECRET_KEY", "")
        email = self._env.get("SUPABASE_MEMBER_EMAIL", "")
        password = self._env.get("SUPABASE_MEMBER_PASSWORD", "")
        member_auth = (
            self._observe_ledger
            and self._ledger_auth_mode.startswith("member:")
            and publishable and email and password
        )
        service_auth = (
            self._observe_ledger
            and (self._ledger_auth_mode == "service"
                 or self._ledger_auth_mode.endswith(":service"))
            and bool(secret)
        )
        key = publishable or secret
        if service_auth:
            key = secret
        if self._observe_ledger and not (member_auth or service_auth):
            raise ValidationError("Supabase ledger Realtime identity is unavailable")
        if not key:
            raise ValidationError("Supabase Realtime credential is unavailable")
        sb = None
        try:
            sb = await asyncio.wait_for(
                acreate_client(self._env.get("SUPABASE_URL", ""), key),
                timeout=10.0,
            )
            if member_auth:
                auth_response = await asyncio.wait_for(sb.auth.sign_in_with_password({
                    "email": email, "password": password,
                }), timeout=10.0)
                session = getattr(auth_response, "session", None)
                access_token = getattr(session, "access_token", "")
                if not access_token:
                    raise ValidationError("Supabase member Realtime token is unavailable")
                await asyncio.wait_for(
                    sb.realtime.set_auth(access_token), timeout=2.0,
                )
            self._channel = sb.channel(
                f"ab-{self._root}",
                RealtimeChannelOptions(config={"broadcast": {
                    "self": False,
                    "replication_ready": self._observe_ledger,
                }}))
            self._channel.on_broadcast("change", lambda _p: self._on_hint())
            if self._observe_ledger:
                from realtime import RealtimePostgresChangesListenEvent
                self._channel.on_postgres_changes(
                    RealtimePostgresChangesListenEvent.Insert,
                    self._postgres_change,
                    table="ab_change_events",
                    schema="public",
                    filter=f"root=eq.{self._root}",
                )
                self._channel.on_system(self._system_status)
            await asyncio.wait_for(
                self._channel.subscribe(self._subscription_status),
                timeout=10.0,
            )
            ledger_ready_deadline = time.monotonic() + 10.0
            while not self._closing:
                await asyncio.sleep(1.0)
                if self.status() == "disconnected":
                    return
                if (self._observe_ledger and not self._ledger_ready.is_set()
                        and time.monotonic() >= ledger_ready_deadline):
                    self._mark_disconnected()
                    return
                joined = getattr(self._channel, "is_joined", False)
                joined = joined() if callable(joined) else bool(joined)
                errored = getattr(self._channel, "is_errored", False)
                errored = errored() if callable(errored) else bool(errored)
                if self._ready.is_set() and (not joined or errored):
                    self._mark_disconnected()
                    return
        finally:
            channel, self._channel = self._channel, None
            try:
                if channel is not None:
                    await asyncio.wait_for(channel.unsubscribe(), timeout=2.0)
            except Exception:  # noqa: BLE001 - shutdown remains best effort
                pass
            rt_client = getattr(sb, "realtime", None) if sb is not None else None
            close = getattr(rt_client, "close", None)
            if callable(close):
                try:
                    await asyncio.wait_for(close(), timeout=2.0)
                except Exception:  # noqa: BLE001 - loop still must terminate
                    pass
            auth_client = getattr(sb, "auth", None) if sb is not None else None
            close_auth = getattr(auth_client, "close", None)
            if callable(close_auth):
                try:
                    await asyncio.wait_for(close_auth(), timeout=2.0)
                except Exception:  # noqa: BLE001 - loop still must terminate
                    pass
            if self._counted_ready:
                self._on_metric("rt_socket_closes")

    def status(self) -> str:
        with self._state_lock:
            return self._state

    def observes_change_ledger(self) -> bool:
        return self._observe_ledger

    def change_ledger_status(self) -> str:
        if not self._observe_ledger:
            return "unsupported"
        if self._ledger_ready.is_set():
            return "ready"
        return "disconnected" if self.status() == "disconnected" else "connecting"

    def alive(self) -> bool:
        return self._thread.is_alive()

    def send(self) -> None:
        if not self._ready.is_set() or self._channel is None:
            self._on_metric("broadcast_skipped")
            return

        async def _send():
            try:
                await self._channel.send_broadcast("change", {"r": 1})
                self._on_metric("broadcast_sent")
            except Exception:  # noqa: BLE001
                self._on_metric("broadcast_failures")

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception:  # noqa: BLE001
            self._on_metric("broadcast_failures")

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            self._state = "closed"
        self._ready.clear()
        self._ledger_ready.clear()
        try:
            self._started.wait(timeout=0.25)
            task = self._task
            if task is not None:
                self._loop.call_soon_threadsafe(task.cancel)
        except Exception:  # noqa: BLE001
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=5.0)
