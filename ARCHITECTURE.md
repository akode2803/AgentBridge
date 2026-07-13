# AgentBridge — architecture reference (v2)

The deep technical reference for the **v2 backend rewrite** (`agentbridge/`):
package layout, the mesh facade, the E2EE model, the agent harness stack, the
transport abstraction, and the hard-won invariants that aren't visible from any
single file. Read this before adding a feature so it follows the existing
contracts instead of quietly breaking one.

Companion docs, each with a different job — don't duplicate their content here:
- **README.md** — 30-second pitch and quick start.
- **WORKING_AGREEMENT.md** — how we work (the seven rules + per-round loop).
- **REWRITE_PLAN.md** — the round-by-round checklist + decision log (D1–D19)
  + per-round result summaries. The authoritative "what shipped when".
- **docs/THREAT_MODEL.md** — what the E2EE layer does and does NOT protect.
- **docs/FORMAT2.md** / **docs/DECISIONS.md** — on-disk format + library pins.
- **HANDOFF.md** — point-in-time state for a fresh session ("where are we now").
- **Project memory** (`~/.claude/projects/<this-project>/memory/`) — the
  narrative history + deferred-work backlog.

When this doc and the code disagree, **the code wins** — then fix this doc.

---

## 1. System overview

AgentBridge is a chat platform — WhatsApp/Telegram-shaped — where humans and AI
**agents** share named rooms. The rooms live on a **transport every participant
syncs in full**: today a synced folder (OneDrive / SharePoint / Google Drive
desktop sync, all identical to the app) or a Supabase project (R23). There is no
server process reachable from outside `127.0.0.1`; the transport's JSON/JSONL
documents *are* the data store, and every write is attributable to the identity
that made it.

Because the transport is a shared store an adversary can read and write in full,
**confidentiality and authenticity rest on cryptography, not access control**
(see §5 and THREAT_MODEL.md). The permission layer governs *policy*; E2EE
governs *secrecy + integrity of content*.

The one product invariant: **visibility = membership.** Everyone — human or
agent — sees and reads only the chats they are a member of. Every read path
funnels through one membership-gated accessor (`MessagingService.messages_for`
→ `readmodel.build_messages`), so no caller ever sees a body it shouldn't.

---

## 2. Package layout & layering

Everything is under `agentbridge/`, in strict one-way dependency layers:

```
core/            models, config, errors, timekit        (leaf — depends on nothing internal)
  ↓
crypto/          Ed25519 + X25519 + ChaCha20Poly1305 primitives (bytes in/out)
transport/       Transport ABC + FolderTransport + SupabaseTransport + make_transport
store/           SQLite read-cache (Store) + durable OutboxWorker
  ↓
mesh/            the services, glued by the Mesh facade (see §4)
  ↓
harness/         the agent runtime (see §7)   cli/   gui/   applink/   (connectors, see §8)
```

- **Connectors never reach past the facade.** The GUI server, the CLI/MCP
  server, and the agent harness all program against `mesh.Mesh`; none of them
  touches the transport or the store directly. This is what keeps the
  visibility invariant enforceable in exactly one place.
- **`legacy/`** holds the retired v1 app (`mesh.py`, `agent_worker.py`,
  `mesh_cli.py`, `bridge.py`, `handler_coco.py`, the migration tool + runbooks)
  — reference only, not imported by v2. Archived in R26.
- **`gui/`** (repo root, distinct from `agentbridge/gui/`) is the **static
  frontend** package (native ES modules under `static/js/`, §9) served by the
  v2 GUI server. No Python logic beyond the package marker.
- **Version source of truth:** `agentbridge/__init__.py` `__version__` (moved
  here from `gui/__init__.py` in R26). Bump once per shipped round, with the
  Edit tool — never PowerShell (it re-encodes to UTF-16+BOM and mangles
  em-dashes).

---

## 3. Data model & on-disk format (`core/models.py`, docs/FORMAT2.md)

Everything JSON-serializes without adapters (enums subclass `str`). `from_dict`
is **tolerant**: unknown keys are ignored (peers may run newer versions) and
unknown enum values **fail closed** to the most restrictive option.

- **`Envelope`** — the at-rest view of one message. Fields: `id`, `ns`, `ts`,
  `from`, `kind` (`message`|`info`), plus the sealed body fields
  `{epoch, nonce, ct, sig}` for messages, or a plaintext signed `event` dict
  for info events.
- **`BodyRecord`** — the *decrypted* payload inside a message's `ct`
  (`body`, `tags`, `reply_to`, `files`, `fwd`).
- **`Message`** — the decrypted read-model the services hand out; already
  filtered through membership + overlays, so no caller sees a hidden/deleted
  body.
- **`ChatSnapshot`** — `meta.json`, a **rebuildable cache** materialized from
  the info-event fold. Holds `members` (name → `Member{role, joined_ns}`),
  `permissions`, `kind`, and (R25) **`tenure`** = per-user membership intervals
  `{name: [[join_ns, leave_ns_or_0]]}` used to drop messages sent outside a
  sender's membership (see §5).
- **`Account`** — the user doc (`users/<name>.json`): `kind` (human/agent),
  `handle`, `keys` (published `sign_pub`/`agree_pub` + password/recovery-wrapped
  private bundle for humans), `agent{owner, machine, harness}`, `privacy`,
  `agent_rules`, `blocked`.

**`ns`, never `ts`, for ordering/cursors.** `ts` is second-resolution and ties;
a strict `>` against a tied cursor skips a message forever (a real, fixed bug).
`ns` is monotonic nanoseconds. (`unread_count` still uses `read_ts` on purpose.)

### Info events + the fold (`mesh/events.py`)

Chat *state* (creation, membership, admin, rename, permissions, deletion) is a
log of signed **info events** — the source of truth. `fold(chat_id, envelopes,
directory)` replays them in `(ns, from, id)` order into a `ChatSnapshot`;
`meta.json` is just the materialized result, and `refold` reproduces it
bit-for-bit. The fold **is the permission system replayed** — `_apply` re-runs
the same `authz` predicates at replay time, so a forged/backdated event on disk
is silently ignored even if it was written directly. Genesis integrity (R13.5):
a v2 chat id ends in `-g<16hex>` committing (sha256 + nonce) to its genesis
event, and every info event carries the author's Ed25519 signature over
`chat|id|ns|from|canonical(event)` — impersonation and cross-room replay both
fail. `tenure` is built here as members join/leave (including `_heal` cascades).

### Overlays (`mesh/overlays.py`)

Per-message and per-user side-data that would churn the append-only log:
- **Chat-level, one file per message** (concurrent actors never clobber):
  `edits/` (author-only, the new body is sealed+signed so a forged edit fails
  to open), `redactions/` (delete-for-everyone — **signed by the sender since
  R25**, §5), `pins/` (**signed by the pinner since R31** over
  `chat|pin|msg-id|by|ns|until_ns` — a dropped-in pin or a stretched expiry
  doesn't verify).
- **Per-user** (`state/<user>.json`, read-**merge**-write — never overwrite, a
  clobber once wiped stars): `read_ns`/`read_ts`, `starred`, `hidden`,
  `cleared`, `pinned`, `archived`, `deleted`, `forced_unread`, `mute`; plus
  per-user reaction files folded across members (**signed by their owner since
  R31** over the full mapping — the read fold drops unverified files). The
  state doc itself is **signed by its owner since R31.5** over
  `chat|state|user|ns|fields`: every verified reader (`messaging.state_of` —
  the owner's own view, receipts' cursors, the notifier's mute check) treats
  an unsigned/mis-signed doc as absent, and `_merge` starts from the verified
  read so a forged field is never laundered into a genuine write. Every
  state mutation holds a per-(chat, user) in-process lock: R30 moved the
  post path's `mark_read` onto a background thread, and an unlocked
  read-modify-write raced the user's own star/flag writes.

---

## 4. The mesh facade & services (`mesh/`)

`Mesh` (`mesh/service.py`) is one object binding transport + store + sealer +
services for **one identity on one root**. Connectors call `mesh.post(...)`,
`mesh.create_dm(...)`, `mesh.messages_for(...)` — a flat delegation surface over
the services below. `make_transport` resolves the root (`supabase://…` →
`SupabaseTransport`, else `FolderTransport`); the store path is derived from a
hash of the transport's cache key so each identity@machine@root gets its own
cache.

| Service | Responsibility |
|---|---|
| `MessagingService` | every mutating message op + the read choke-point `messages_for`; each public method gates via `_require_member` before touching anything (write gates too, not just reads) |
| `MembershipService` | chats/DMs/self-chats, the multi-admin model, create/add/remove/leave/rename/permissions/delete — emits signed info events then refolds; `authz` gate before every mutation |
| `AccountsService` | account lifecycle (scrypt auth for humans; agents never authenticate — machine identity), profile/status, agent create/adopt/delete; all agent edits owner-gated via `_writable_target` |
| `PrivacyService` | the R6 matrix (who may see/reach whom), blocks, agent outbound rules; `visible_profile` is the projection every connector serves instead of raw account docs |
| `PresenceService` / `ReceiptsService` | online/last-seen heartbeat + read receipts derived from per-member cursors (no new write path) |
| `Notifier` / `EventBus` | in-process pub/sub feeding the GUI SSE + the CLI long-poll |
| `SyncEngine` | pulls new records per chat by byte-offset, in parallel, **never reading logs of chats this identity isn't in**; drops any record whose `from` ≠ the log's owner (ingestion sanity). On a change-feed transport (R30) a tick is ONE "what changed since cursor?" query (cursor persisted in the store; a newly-joined chat gets one full scan since its history may sit below the cursor); the run loop survives a failing pass (a cloud transport can throw after retries — next tick heals) |

`harden_startup()` (R25, called by connectors on sign-in) is an idempotent
migration: it refolds pre-R25 chats to populate `tenure` and re-signs any
legacy unsigned redaction — and, since R31/R31.5, any legacy unsigned pin,
reaction file or per-user state doc — whose author is keyed on this machine.

---

## 5. E2EE & the security model (`crypto/`, `mesh/{sealer,keyring}.py`)

See **docs/THREAT_MODEL.md** for the full statement; the mechanics:

- **Identity** (per account): Ed25519 (sign) + X25519 (agree), a 64-byte
  bundle. Public halves in the account doc; the private bundle wrapped twice
  (password + one-time recovery code, D5) at rest, unlocked only in
  `~/.agentbridge/keys/<name>.key` — DPAPI-wrapped on Windows since R31.5
  (`crypto/dpapi.py`, per-OS-user; legacy plain files upgrade on first load).
- **Chat keys**: a 32-byte symmetric key per **epoch**, wrapped per member via
  ephemeral-X25519 ECDH → HKDF → ChaCha20Poly1305 (`keyring.ChatKeyService`).
  `ensure()` runs before every seal and **rotates** whenever the epoch's
  wrapped-set drifts from the member set — so removal/leave, and even a raced
  rotation, self-heal on the next message. A removed member keeps the epochs
  they already held (history stays readable to them) but never gets a new one.
- **Envelopes** (`sealer.E2EESealer`): body sealed with ChaCha20Poly1305 under
  the epoch key; AAD binds `chat|id|ns|from|epoch`; the Ed25519 signature covers
  `aad + nonce + ct`. Tampering with the body or **any** routing field makes it
  unopenable — a reader shows **nothing**, never a forged plaintext — and this
  defeats replay (an old ct re-posted under a new id/ns fails the bind).
  Epoch-0 plaintext envelopes and plain blobs are refused outright (R16.5).
  An unseal-result LRU (keyed including `sha1(ct|sig)` so tampered-at-rest
  records miss the cache and re-verify to blank) makes reads ~40× cheaper (R24).

**R25 hardening (see THREAT_MODEL "CLOSED R25"):**
- **Signed redactions.** Delete-for-everyone was applied on the mere *presence*
  of an overlay doc — any folder writer could censor any message. Redactions
  now carry the sender's signature over `chat|redact|msg-id|by|ns`
  (`events.redaction_signing_bytes`); the read model honors one only if the sig
  verifies against the sender AND `by` == the original sender. Forged/unsigned
  → ignored (message stays). (Edits were already sig-protected via the sealer.)
- **Tenure gate.** A removed member keeps the old epoch key, so they could
  seal+sign a *fresh* old-epoch envelope current members decrypt. The fold's
  `tenure` timeline lets `readmodel.build_messages` drop any MESSAGE sent
  outside the sender's membership — genuine pre-departure history stays.
- **Prompt/transcript injection** hardened (`prompt._safe_body` indents
  continuation lines so a body can't forge a transcript entry), and the peer
  channel got a per-requester ns replay floor.

**R27 closed the directory root of trust** with TOFU key pinning (pins resolve
every key read; a doc rewrite is inert for machines that knew the account and
raises a banner). **R31 closed the rest:** reaction/pin overlay fabrication is
signature-verified (see §3 Overlays), and every account has a **key
fingerprint** (`pins.key_fingerprint`, 8×4 hex groups over the pinned pair)
surfaced in the DM info Encryption card / Settings → Security / the key-change
banner, with an out-of-band **Mark as verified** state stored in the pin store
(`/api/mesh/key_verify`). **R31.5 closed the last overlay:** per-user state
docs are owner-signed and read through verified accessors (see §3 Overlays),
and the local keystore is DPAPI-wrapped on Windows (above). Remaining accepted
risks live in docs/THREAT_MODEL.md ("What is NOT protected").

---

## 6. Transports & storage

- **`transport/base.py`** — the `Transport` ABC: `get_doc`/`put_doc`/`delete_doc`,
  `list_docs`, `append_log`/`read_log`(offset-based)/`list_logs`,
  blob put/get/size, `list_chat_ids`, `watch()` (a wake-up *hint* only). Plus a
  `cache_key` for store partitioning. **Adding a connector = implement the
  abstract surface + one `make_transport` scheme entry.** Two OPTIONAL fast
  paths (R30) make a high-RTT driver feel local and degrade gracefully when
  absent: `get_docs(prefix)` (bulk read; default loops the required methods —
  the mirror warms from it) and `changed_logs(cursor)` +
  `has_change_feed = True` (a global monotonic change feed over the logs —
  the sync engine then polls every chat in ONE round-trip).
- **`transport/folder.py`** — the synced-folder impl. Retries transient
  `PermissionError` (OneDrive mid-sync locks), tolerates half-synced files (BOM
  strip, partial trailing JSONL line not consumed), a memoized path-escape guard
  (`_abs`), and a best-effort `ReadDirectoryChangesW` watcher that only shortens
  poll latency (polling stays the truth — OneDrive doesn't reliably notify for
  files synced *down*).
- **`transport/supabase.py`** (R23) — docs → `ab_docs`, logs → `ab_logs` (row
  id = read offset), blobs → the `ab-mesh` bucket, realtime broadcast hints on a
  daemon thread (degrade → poll). Trust model v1: **only the secret key** talks
  to the project (RLS on, no policies, so the publishable key can do nothing);
  bodies arrive pre-sealed, so the server only ever stores ciphertext.
  Implements both R30 fast paths: `get_docs` (one paged query) and
  `changed_logs` (`ab_logs` row ids are one global identity column, so
  "what changed since cursor?" is one indexed query — measured ~65-85 ms
  p50/op on the live project; `scripts/profile_supabase.py` re-measures
  every op against a throwaway root).
- **`transport/cache.py`** (R28, reworked R29) — `CachingTransport`, a warm
  in-memory **read mirror** (`get_doc`/`list_docs`/`list_chat_ids`; NOT logs or
  blobs) that `make_transport` wraps around a **cloud** transport only (a folder
  read is already free). One paged bulk query (`SupabaseTransport.get_docs`)
  loads every doc under the root; a background daemon re-pulls the snapshot
  every ~4 s (woken early by realtime hints), so the hot GUI read paths touch
  the network **zero** times (`/api/mesh/state`: ~3 s under R28's short-TTL
  read-through cache → ~12 ms mirrored). Stability rules: a FAILED refresh
  keeps serving the last good snapshot (stale beats vanished — under R28 a
  transient cloud fault read as "doc missing" and was cached, so chats/profiles
  flickered out of the sidebar); writes are write-through and update the mirror
  synchronously (a writer always sees its own writes); a refresh snapshot never
  clobbers a doc written locally after the snapshot query began (recent-write
  guard); returned docs are deep copies (callers patch documents in place).
  Cross-process staleness ≤ the refresh cadence + hint latency — within the
  mesh's existing eventual-consistency window (meta.json is already a
  rebuildable snapshot). The GUI shares ONE mirrored transport between the
  pre-auth directory and the Mesh (`GuiApp._build` passes `_tx0`).
  `mirror_status()` reports warmth + seconds since the last good refresh;
  the GUI Connection panel renders it as Connected / Reconnecting.
- **`store/db.py`** — a local SQLite **read cache** (messages + per-log
  offsets + a small cached-doc kv), rebuildable from the transport at any time.
- **`store/outbox.py`** — the durable send guarantee: a sealed envelope is
  cached optimistically (sender sees it instantly) and committed to the outbox
  *before* any transport attempt; `OutboxWorker` flushes with retry-forever.

---

## 7. The agent harness (`harness/`)

One symmetric runner for **every** agent — a model is data/config, never a
branch in the logic (the "one harness, all agents" rule). Per-agent differences
live only in an adapter preset.

- **`runner.py`** — one process per agent (`python -m agentbridge.harness <name>`;
  `--all` supervises every agent hosted on this machine, `--supervise` keeps one
  alive with capped backoff). A `SingleInstance` lock stops a second launcher's
  runner (it stands aside with rc 3). The loop: sync → scan triggers → dispatch
  → post. Honors the global stand-down (`control.json`) and a persisted local
  peer-hold. Calls `mesh.harden_startup()` on start.
- **`perf.py`** (R30) — per-run response-time profile: `pickup` (trigger
  posted → group claimed) / `context` (delivery build) / `model` (the
  responder run) / `post` (seal + commit). One JSONL record per run in
  `<home>/harness/perf/<agent>.jsonl` (size-capped), a human summary on the
  run feed ("Reply posted · 44.6s total · model 41.8s…") and a ⏱ line in the
  reply's Message-info task doc. Adapter-agnostic (times the Responder call,
  never reaches inside it) and best-effort — profiling never breaks a run.
- **`queue.py` / TimerService** — a durable work queue (two-legged answered
  guard) + scheduled wake-ups (surfaced to the owner in the GUI, R19.5).
  Dispatch groups a sender's rapid burst into ONE run answering the last
  message — intended anti-flood behavior, not a delivery gap (every message
  still reaches the agent's context). The reply always records which message
  it answers (`reply_to.id` — the answered-guard's transcript leg reads it),
  but displays **standalone** when it answers the newest message
  (`reply_to.quote=false`, R31) and as a visible quote once the chat has
  moved on past the trigger.
- **`conversation.py`** — builds the enriched `Delivery` (roster, triggers,
  pins, recalled memory, transcript tail) the prompt is rendered from.
- **`prompt.py` + `prompts/default.json`** (R17) — the PromptManager: all agent
  wording is **data** in a 3-layer JSON pack (shipped → machine → per-agent),
  assembled in a fixed order so an overlay can reword but not reorder the rails.
  The silence sentinel `<<<NO-REPLY>>>` is code-injected (unspoofable). Message
  lines are code-owned and carry `(id m-…)` so tools can only act on visible ids;
  bodies are indented to prevent transcript-line forgery (R25).
- **`broker.py`** (R18) — the Codex/Claude-Code-style PermissionBroker. Decision
  order: workspace path → allow; deny-root (harness home, mesh root) → refuse
  always; preset `auto_allow` (read-only tools) → allow; owner standing rule →
  allow; else **ASK the owner and block**. Timeout = **deny** (fail-closed),
  denial cached per-run. Deny-roots resolve `..`/symlinks before comparing.
- **`bridge.py`** (R18/R19/R20/R22) — a per-run FastMCP streamable-http server on
  an ephemeral `127.0.0.1` port, tools bound to the run's chat/workspace/policy:
  the `approve` permission gate + `ask_member`; capability tools
  (pin/star/react/forward/create_dm/create_group[capped]/schedule_timer, all
  chat-bound + id-validated); memory `remember`/`recall`/`forget`;
  `peer_diagnose`. Every tool sets `structured_output=False` (a spike lesson:
  FastMCP's `structuredContent` wrapping reads as an invalid permission
  response). There is deliberately NO tool for an agent to raise its own
  permissions — capabilities are owner-side config, and the broker ask-card is
  the only runtime channel (fail-closed).
- **`memory.py`** (R20) — a workspace `MEMORY.md` notepad + a local qdrant vector
  store (one path per agent process, collections per chat + global), behind an
  Embedder probe chain (fastembed → model2vec). `forget` (R31) deletes one
  entry — by the exact id `recall` reports, or by query when the single
  closest match clears a confidence gate (never "delete the closest thing to
  anything").
- **`retrieval.py`** (R21) — an incremental per-chat history index (`hist-<chat>`)
  with a planner seam and a score gate; recalled hits are injected before the
  transcript tail.
- **`peer.py`** (R22/R22.5) — peer harness access: signed request/response docs
  (Ed25519, `from` bound in the signature, per-requester ns replay floor),
  owner-gated `peer_access` (off/ask) + `peer_auto` for READ diagnostics
  (ping/status/run_feed), and a stricter second gate `peer_repair` for
  mutations (pause/resume/clear_queue/clear_timers — always re-prompt, injected
  by the runner, touch only harness-local runtime state). Every outcome audited.
- **`adapters/`** — the ModelRegistry + preset engine. A preset (`presets/*.json`)
  declares the CLI family, model list, effort support, blocklist, `auto_allow`,
  and `permission_args`. The `claude` preset wires the broker; `codex`/`cortex`
  rely on their own sandbox (`--sandbox read-only` / `--sql-read-only`); others
  are pure text generators. Safety flags (blocklist, sandbox) are **never**
  dropped, even on the minimal-flags fallback retry.

---

## 8. Connectors

### `agentbridge/gui/` — the human app
A stdlib `ThreadingHTTPServer` on `127.0.0.1` (no third-party deps by design).
Route tables are plain dicts contributed by the `api_*` modules; `@authed`
injects the live Mesh so an endpoint can't forget the check; `dispatch` maps a
domain error to `{"error": …}` JSON. One GuiApp = at most one signed-in human;
the session survives restarts via a local `gui_session.json` (the E2EE bundle is
already local, so restore never needs the password). `/api/mesh/events` is the
one SSE stream. Every mutating endpoint is a thin shim over the facade — the
gates live in the services, never re-implemented here. File serving is
membership-gated and provenance-checked (sha256 from the signed message) with a
path-traversal guard; uploads stage under a one-shot token. `/api/state`
carries a transport-aware `connection` block (folder root: `shared_ok` + the
cached OneDrive-process probe; cloud root: project host + `mirror_status()`)
that the no-chat home and Settings → Connection render; `/api/open` opens the
two FIXED local folders (`home` = the config dir, `shared` = a folder mesh
root — never a client-supplied path).

### `agentbridge/cli/` — the MCP surface (`server.py`)
`build_mcp(mesh)` exposes capability-parity tools (list/read/post/react/star/
pin/create/add/leave + a `next_events` long-poll) — everything a member can do
in a room, all gated by the same membership/privacy layers as the GUI. Account
management is **deliberately absent** (D19: owner-only, GUI-only). This is the
surface the `mesh-chat` skill and scripted agent actions drive.

### `agentbridge/applink/` — control lane
Presence/version announcements and the global stand-down (`control.json`),
outside the message log.

---

## 9. Frontend (`gui/static/js/`)

**22 native ES modules, zero build step** — the browser imports them directly,
so "run the app" and "see the current source" are the same action. Run
`python check_frontend.py` after every frontend edit (must print **22/22**; it
`node --check`s each module and verifies imports resolve — the only automated
frontend check).

Strict one-way layering:
```
util / icons / api / markdown / files      (leaf helpers)
  → state                                  (App / Mesh / Settings stores)
    → csel / modal / composer / picker      (UI primitives)
      → sidebar                            (below page views)
        → chat / details / media / search / members / forward / settings / wizard   (page views)
          → main                           (router + boot)
```
**Page views never import page views** — each registers its entry points on the
`V` registry (`views.js`) and calls sideways through it; `main.js` asserts the
`EXPECTED` set at boot, so a missing registration throws a named error instead
of "undefined is not a function" three clicks deep. All mutable UI state lives
in `state.js` (`App`/`Mesh`/`Settings`) so a re-render never loses a half-typed
draft, scroll position, or open edit field. A few paths (transcript scroll,
composer focus, inline edit) use imperative partial updates on purpose — a full
re-render on every poll would reset scroll / steal focus.

---

## 10. Cross-cutting invariants (read before touching chat/membership/harness)

1. **Visibility = membership**, for reads AND writes. Every mesh op resolves the
   snapshot and refuses non-members. When you add a mutating path, gate it the
   same way, and audit **every** mutating endpoint when you tighten a rule — not
   just the read paths.
2. **No agent in a room without a responsible human.** Enforced in the fold's
   `_heal`: removing a human cascades out any agent left without its owner. A
   human↔unowned-agent DM can't stay a DM (born as a small group, `auto_dm`).
3. **`ns`, never `ts`, for ordering/cursors** (§3).
4. **Per-user overlays merge, never overwrite.** Delete-for-everyone is the
   shared, signed chat-level redaction; everything per-user is merged.
5. **After editing `mesh/*` or `harness/*`, restart the affected process(es).**
   A running process imported the old module at startup and reloads nothing.
6. **Safety rails are never dropped in fallback paths.** Blocklist, read-only
   sandbox flags, and the broker's deny-roots/fail-closed timeout hold on every
   degrade path; unattended agents never get blanket auto-approve.
7. **One harness, all agents.** Never branch runner logic on the model — push
   the difference into an adapter preset.

---

## 11. Known sharp edges (inherent to the design, not bugs)

- **Status/feed files lag reality.** `status/<agent>_run.json` is written on the
  agent's machine and read through sync — it can say "running" seconds after the
  agent went idle. Never diagnose "stuck" from one read; cross-check the
  transcript.
- **A venv/uv launcher shows as TWO OS processes.** A uv-managed `.venv`
  Python's `pythonw.exe` is a stub that spawns the uv base interpreter as a
  child; killing the parent takes the child. So one logical harness/GUI process
  appears as a `.venv` + a `uv`-path pair — **not** a duplicate. Check the
  command line and parent PID before "cleaning up" a second process.
- **PowerShell text round-trips corrupt source** (UTF-16+BOM, mangled em-dashes).
  Edit `.py`/`.js` only with proper tools; bump the version with Edit.
- **`meta.json` is last-writer-wins** but rebuildable — a raced/clobbered write
  self-heals on the next mutation or `refold`. Never treat it as the source of
  truth; the info-event log is.
- **The transport is semi-trusted.** Anyone with folder/secret write can drop
  arbitrary files; integrity comes from signatures + the fold, not from the
  store. New overlay/document types must carry their own authentication (the
  R25 redaction lesson; R31/R31.5 brought pins/reactions/state to the same
  bar — don't add an unsigned one).

---

## 12. Versioning & release

- `agentbridge/__init__.py` `__version__` is the app's source of truth; bump
  once per shipped round (a "round" = one coherent, live-verified change set).
- Per-round loop (WORKING_AGREEMENT.md): implement → verify live in a scratch
  room (never the primary rooms) → bump version → commit + push → update memory
  + sync this doc / HANDOFF when the shape changed.
