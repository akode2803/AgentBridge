# SCALING.md — metered-transport economics (R76, V84)

The 2026-07-15 incident: Supabase free-tier usage hit **857% egress (42.8 GB
in the first 2 days of the cycle), 170% realtime messages (3.4M), 102%
realtime peak connections (204/200)** — account deactivation imminent. This
document is the deliberation, the measured cost model, the design, and the
contract every FUTURE connector must satisfy so this class of bug cannot
land silently again.

The framing (Aryan's): treat it as a scaling problem. If 10,000 people used
the app, how much could we squeeze out of the limits while sacrificing as
little of the experience as possible?

---

## 1. The measured cost model (why it burned)

Live numbers (2026-07-15, root `mesh2`): 244 docs, full-snapshot payload
**157.5 KB** (biggest classes: `chats/*/tasks` 85 KB, `status/*` 17 KB,
overlays 17 KB). Fleet: GUI + per-agent runners ≈ 6 long-lived processes,
each wrapping the cloud driver in its own `CachingTransport` mirror.

Three compounding causes, in order of blame:

1. **The mirror refreshed by FULL SNAPSHOT on a fixed 4 s cadence.**
   `CachingTransport._refresh_loop` pulled `get_docs("")` — every doc in the
   root — every ≤4 s, per process, *forever*, idle or not. Realtime hints
   only woke it EARLY; they never let it rest.
   6 mirrors × 170 KB / 4 s ≈ 255 KB/s ≈ **21.4 GB/day — matches the
   observed 42.8 GB / 2 days almost exactly.**
2. **`supervise_all` leaked one mirror + one realtime socket every 30 s.**
   Each roster re-scan called `hosted_agents(root)` which built a fresh
   `Directory(make_transport(root))`; the first read warmed the mirror,
   which started an unstoppable 4 s snapshot daemon + a realtime websocket
   — never closed, accumulating all day. This is the 204-connection peak,
   and every broadcast hint was delivered to every leaked subscriber —
   the 3.4M realtime messages.
3. **Every write broadcast a hint, and presence never stops writing.**
   Each process heartbeats `presence/<user>@<machine>.json` every 12 s;
   every heartbeat fired a broadcast that woke every mirror into a full
   snapshot. The mesh was structurally incapable of being idle.

Secondary: doc upserts returned full row echoes (representation) we never
read; avatars and inline attachment images were re-downloaded from Storage
on every GUI request (`Cache-Control` helped browsers, but the server
itself never cached, and dev hard-reloads bust everything).

Why OneDrive never showed this: a synced folder's polls are served from the
local disk — the OS sync client pays the (optimized, delta-based) network
cost. Polling was free, so cadence never mattered. **The bug was porting
free-poll assumptions onto a metered transport.** That assumption is now an
explicit, typed contract (§4) instead of an implicit vibe.

## 2. The pattern we adopt: poke → delta-pull → reconcile

This is the battle-tested shape used by Replicache ("poke and pull"),
Linear's sync engine, and Figma's multiplayer backend:

- **Poke (realtime hint):** content-free wake-up signal. Lossy, cheap,
  never trusted. We already had this (tenet 6: "the hint is garnish; the
  poll is truth").
- **Delta pull:** the client keeps a server-assigned monotonic **cursor**
  (`ab_docs.seq`, a global sequence bumped by trigger on every
  insert/update) and asks "rows where seq > cursor". Idle cost: one tiny
  empty query. Deletes become **soft deletes** (`deleted=true`, seq bumped)
  so they ride the same feed; `delete_chat` soft-deletes its doc subtree.
- **Reconcile:** a rare full snapshot (boot + every 6 h + when delta looks
  wrong) heals anything a lossy feed or purged tombstone could miss.

Message logs already had exactly this shape (`ab_logs` row ids +
`changed_logs(cursor)` since R30). Docs were the missing half.

**Rejected alternatives**
- *Supabase `postgres_changes` subscriptions:* every row change × every
  subscriber counts against the 2M realtime-message quota (~7M/month at our
  write rate); needs replication-slot config; still lossy so still needs
  reconcile. Poke/pull with coalesced pokes is strictly cheaper.
- *One shared local mirror daemon per machine (N processes → 1 sync):*
  right long-term (it divides every cost by N), but it adds an IPC server,
  lifecycle and single-instance semantics — too much surface for an
  emergency round. Delta sync makes N mirrors cheap; the daemon can come
  with packaging.
- *Client-stamped `updated` timestamps:* clock skew corrupts cursors. The
  DB assigns seq; writers never stamp their own order.

## 3. Cadence + hint policy (the experience/cost trade)

Latency-critical events keep instant pokes; chatty maintenance classes are
coalesced or silent. Writer-side coalescing has a **trailing edge** — a
burst's last write always fires a poke at window end (otherwise it waits
for a safety poll).

| write class                     | poke policy        | why |
|---------------------------------|--------------------|-----|
| message log appends             | instant (0.5 s coalesce) | message latency IS the product |
| meta/roster/keys/overlays (edits, redactions, reactions, settings) | instant (1 s) | user-visible, rare |
| `status/` run feed (spinner)    | ≥5 s coalesce      | progress, not content |
| `chats/*/state/` (read receipts)| ≥10 s coalesce     | ticks may settle lazily |
| `presence/` heartbeats          | **never** (polled) | 6 writers × forever; flips (sign-in/out) poke via `hint_now()` |

Cadences (from the transport profile, §4):
- **Mirror delta pull:** on poke (0.3 s coalesce) + **45 s idle safety
  poll**; full reconcile at boot + every 6 h.
- **Log sync (`changed_logs`):** on poke + 45 s idle safety poll (was a
  fixed 4–5 s). The runner's own 5 s tick only reads the warm mirror —
  free.
- **Hint watchdog:** if a SAFETY poll finds changes that no poke announced,
  hints are marked suspect and polls drop to 10 s for 10 minutes
  (self-healing when realtime silently dies; restores itself). Classes
  whose writers are DELIBERATELY silent (`profile.silent_prefixes` —
  presence beats) don't count: without that exclusion every heartbeat
  caught by a safety poll re-trips the watchdog forever (v0.24.153 fix,
  caught watching the live steady state post-migration).
- **Presence:** beat 12 s → **30 s**; `STALE_S` 40 s → **120 s** (must
  exceed beat + worst poll + margin, or everyone flickers offline between
  polls). Delivered-tick upgrades ride pokes during active chat; lag only
  when idle.
- Doc writes use `returning="minimal"` — no row echoes.
- Blobs (avatars + inline attachment images) get a content-addressed disk
  cache on the GUI server (`<home>/gui_cache/blobs/<sha256>`), so Storage
  is hit once per content version per machine. URLs are already
  sha-versioned, so `Cache-Control: immutable` is safe.

**Budget after (6-process fleet, heavy dev day):** boot snapshots ~1 MB +
reconciles ~4 MB + poke-driven deltas ~40 MB + presence delta rows ~35 MB +
log-feed ticks ~10 MB + writes ~5 MB ≈ **<100 MB/day ≈ <3 GB/month** vs
21 GB/day before (~250× cut), inside the 5 GB free tier with margin.
Realtime: ~5–6 K pokes/day × ~6 subscribers ≈ 1M messages/month (cap 2M);
connections: one socket per long-lived process ≈ 6 (cap 200).

## 4. The connector contract (`TransportProfile`)

Every transport now DECLARES its economics — a typed profile on the class,
not folklore. `CachingTransport`, `SyncEngine` callers, presence and the
GUI read their cadences from it. A new connector must fill this in, and the
review checklist below is part of its definition of done.

```python
class TransportProfile:
    metered: bool          # False = polling is locally served & free (synced folder)
    supports_doc_delta: bool  # get_docs_delta(cursor) available?
    idle_poll_s: float     # safety-poll cadence when hints look healthy
    fallback_poll_s: float # cadence when hints are absent/suspect
    reconcile_s: float     # full-snapshot healing interval
    presence_beat_s: float # heartbeat write cadence
    presence_stale_s: float  # >= beat + worst poll + margin
```

- `folder`: `metered=False` — nothing changes; 4–5 s polls stay.
- `supabase`: `metered=True, supports_doc_delta=True` (after migration),
  45/10/21600/30/120.
- A metered driver without a delta feed (e.g. a future Google Drive API
  connector) still gets adaptive cadence + hint coalescing; it pays full
  snapshots but rarely, and the Connection panel shows delta as
  unavailable.

**New-connector checklist (all must hold before it ships):**
1. Profile filled in; no caller hard-codes a cadence.
2. Nothing polls a metered driver on a fixed fast loop; every loop is
   hint-woken with a slow safety poll.
3. Every transport/mirror created is closed; no helper builds one per call
   (the `hosted_agents` lesson — helpers take a Transport, not a root).
4. Writes are echo-free; hints are class-coalesced with a trailing edge.
5. Blob reads are content-addressed-cacheable and cached server-side.
6. Byte/query counters exposed (`transfer_stats()`) so the About panel —
   and a soak test — can SEE the cost. Verification of a connector round
   includes a measured idle-hour and active-hour budget.
7. **No steady-state writer without a change guard.** An IDLE fleet must
   produce zero pokes (a 90s hint-meter check is part of sign-off). The
   R76 soak caught `PeerService._publish_pending` rewriting its doc — with
   a fresh `updated` stamp — on every 5s tick forever: one such writer
   pins every mirror at the fallback floor and defeats all the idle
   economics above. Status docs write when their CONTENT moves
   (`publish_status`'s `_last_doc` pattern), never on a timer.

## 5. Schema migration (one dashboard paste)

`docs/supabase_schema.sql` gained an idempotent R76 section: `ab_docs.seq`
(bigint, trigger-assigned from a global sequence on insert/update),
`ab_docs.deleted` flag, backfill, and an `(root, seq)` index. The app
**probes** for `seq` once per process and falls back to legacy
full-snapshot mode (with the new slow cadence — still ~15× cheaper than
before) until the paste happens; the Connection panel says which mode is
live. While legacy, the probe re-runs on each full refresh, so pasting the
migration upgrades the fleet within a minute, no restart.

Tombstone hygiene: soft-deleted rows are purged by the janitor after 30
days; any client offline longer heals via its boot reconcile.

## 6. The 10,000-user roadmap (documented, deliberately not built now)

Free-tier ceilings with this round's shape: egress scales with (writes ×
readers-per-root). One root = one org/team; ~50 active members in one root
fits the 5 GB tier. Beyond that, in order:
1. **Per-member RLS + per-chat delta scopes** — clients pull only chats
   they're members of (already queued as its own round; also the real
   security close).
2. **Per-chat realtime channels** — only foregrounded chats subscribe;
   pokes stop fanning out org-wide.
3. **Presence out of `ab_docs`** — ephemeral realtime presence or a tiny
   TTL table; foreground-only beats (mobile pattern).
4. **One sync daemon per machine** (N processes → 1 puller), from the
   packaging round's single-instance work.
5. **Blobs via CDN-cacheable signed URLs** — Supabase counts CDN traffic
   against a separate "Cached Egress" bucket (observed at <1% while raw
   egress hit 857%); E2EE blobs are ciphertext so CDN caching is safe.

## 7. Verification protocol for this round — RESULTS (2026-07-15)

Unit: 466 tests pass (delta apply/ordering/write-guard, legacy fallback +
live upgrade, hint classes + trailing edge, presence profile + flip pokes,
blob cache, `hosted_agents` tx reuse, idle-tick publish guards).

Live (single fleet on v0.24.152, pre-migration legacy mode, throwaway
scratch room deleted after): **17/17** —
- agent mention → reply: **18.1 s** end-to-end ("EGRESS-OK");
- cross-process propagation via a second-process probe mirror: new chat
  15 s, edit 7 s, delete-for-everyone 11 s, reaction 12 s, pin 13 s,
  meta rename 10 s, read-state ≤1 s (all ≤ the legacy hint-floor design;
  delta mode makes these poke-fast after the paste);
- read model applies the edit + tombstone; avatar and attachment second
  hits cost **+0 storage bytes** (disk cache, persists across processes);
- Connection panel renders mode/warning/traffic; zero console errors.

Idle soak: the first soak measured ~57 MB/h on the GUI alone and the hint
meter showed **33 pokes/90 s from an idle fleet** — which caught the
`peer_pending` steady writer (checklist item 7 above). After the guard:
**0 pokes in 90 s** (only the poke-free 30 s presence beats move), and a
clean 10.7-minute idle window measured **14.3 MB/h for the GUI process**
(63 queries — exactly the modeled 45 s full pull + log tick + presence
writes; ~183 KB/pull, repr-approximated).

**Read the idle number honestly:** legacy mode ≈ 14 MB/h × ~4
mirror-bearing processes ≈ 1.4 GB/day fleet-idle — a ~15× cut from the
21 GB/day fire, which buys DAYS, not a subscription cycle. The delta
migration replaces those full pulls with ~1 KB empty cursor queries
(idle fleet <1 GB/MONTH, activity-proportional beyond that) — **the
dashboard paste is the actual fix; legacy mode is the tourniquet.**

**Migration landed (2026-07-15, ~23:40):** Aryan pasted the R76 SQL and
the live fleet self-upgraded with no restart — Connection panel flipped to
`mode: delta`. The watchdog tripped exactly as designed on the paste
itself (the backfill bumps every row's seq with no poke — DDL doesn't
broadcast) and self-cleared. Post-migration probe: cross-process tombstone
in 5.5 s poke-fast; a whole probe cycle moved **1.2 KB** where legacy
moved ~190 KB per pull. Boot nuance (accepted): a freshly-created
transport's FIRST poke can drop while its socket is still subscribing —
the safety poll heals it; long-lived fleet processes keep warm sockets.
One follow-up logged (BACKLOG V101): silent MESSAGES found by a safety
poll don't feed the watchdog yet, so message latency degrades to ~45 s
(not ~10 s) during a realtime outage — observed once during a
Supabase-side incident window.

Still pending: a Supabase usage-page check the next day.
