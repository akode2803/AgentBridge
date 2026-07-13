# AgentBridge backend rewrite — master plan & checklist

**Read this every round** (it is in the CLAUDE.md read-order). Tick boxes as
rounds land; never delete a line — strike it through with a note if a decision
changes. The conversation gets compacted; this file is the spine that survives.

- Status: **APPROVED by Aryan 2026-07-12** (D1, D3, D4/D5 explicitly agreed;
  emoji reactions IN but low priority; D2 pending only the account opening).
- Mission + working rules: `WORKING_AGREEMENT.md` (the seven rules).
- Companion docs (created as rounds land): `docs/DECISIONS.md`,
  `docs/FORMAT2.md` (storage format v2), `docs/THREAT_MODEL.md`,
  `docs/HARNESS.md`, `ARCHITECTURE.md` (rewritten at the end).

---

## 1. Target architecture

One installable top-level package. Old `mesh.py` / `agent_worker.py` /
`gui/server.py` keep running the live mesh untouched until the cutover round.

```
agentbridge/
  __init__.py          version re-export (gui/__init__.py stays source of truth til R26)
  core/                models (Account, Agent, Chat, Message, Member), ns/time,
                       errors, config layer (absorbs legacy/bridge.py utils:
                       DEFAULT_HOME, read_json, atomic_write_json+retry, utcnow)
  transport/           base interface (read/write/watch/local_path) +
                       synced_folder driver; supabase driver later; the ONLY
                       layer that touches bytes-at-rest
  store/               SQLite local cache (messages, cursors) + durable outbox
                       queue + index feed for retrieval
  crypto/              identity keys (Ed25519 sign + X25519 agree), per-chat
                       keys, envelope encrypt/decrypt, key wrap/rotation
  mesh/                services: messaging, membership, permissions, presence,
                       accounts, receipts, events, sync — each its own module,
                       glued by a thin Mesh facade (the one public API)
  harness/             the agent harness (successor to agent_worker.py):
                       runner, queue, conversation, prompts, planner, context,
                       memory/, retrieval, adapters/, registry, permissions
                       (broker), workspace, peer, pipeline
  cli/                 mesh-cli v2: MCP server + human CLI (one install, two
                       entry points; agents never authenticate)
  gui/                 server connector (rewritten thin over mesh facade);
                       static/ frontend stays 21 vanilla ES modules
```

**Data flow (the one sentence):** transport moves ciphertext; crypto unseals it
for members only; store caches plaintext locally per member; mesh services
expose the single membership-filtered API; the GUI connector, the CLI/MCP
server, and the harness are all just clients of that API — **no component ever
reads the folder directly again** (today's agent "read the file yourself" dies).

**Invariant carried over, upgraded:** visibility = membership, now enforced
cryptographically (E2EE), not just at app level.

---

## 2. Decision log

| # | Decision | Status |
|---|---|---|
| D1 | Backend drops the stdlib-only rule; deps managed via `pyproject.toml` + uv lock. Frontend stays vanilla ES modules, no build step. | **APPROVED 2026-07-12** |
| D2 | Cloud realtime backend = **Supabase** (Postgres + RLS + realtime + storage; open-source/self-hostable; RLS enforces visibility=membership server-side). Firebase rejected: lock-in, Firestore query limits, weaker relational rules. Synced-folder option stays for private setups; transport interface keeps both swappable. | approved in principle; **account still to open** (needed by R23 — remind Aryan) |
| D3 | Rewrite lands in a **parallel v2 root** (`mesh2/` + scratch roots), never in-place; live `mesh/` untouched until the dedicated migration/cutover round. | **APPROVED 2026-07-12** |
| D4 | E2EE v1 = per-account keypairs + per-chat symmetric keys wrapped per member + rotation on membership change (lib: `cryptography`). No per-message forward secrecy (double ratchet) in v1 — documented in the threat model, upgradeable. Removed members keep pre-removal history (WhatsApp/Signal semantics). | **APPROVED 2026-07-12** |
| D5 | Key recovery: private key stored in the folder wrapped by a password-derived key (scrypt) → sign-in on a new device just works; **forgotten password without the recovery code = history unreadable** (Signal-style). Recovery code generated at account creation. | **APPROVED 2026-07-12** |
| D6 | Provider **sessions are no longer the context mechanism**. Harness invocations are stateless-per-message with our own context assembly; adapters may opt into short "burst resume" purely as a cost/caching optimization. | approved with plan |
| D7 | Sandboxing = **pluggable levels**: `workspace` (default: per-chat workspace dir, read-only-by-default outside it) → leaning on the inner CLI's own sandbox (claude-code sandbox / codex approvals) → `container` backend possible later without redesign. Docker NOT the default (UX). | approved with plan |
| D14 | **Emoji reactions are IN, low priority** (Aryan 2026-07-12): data layer rides R4 as one more per-user overlay; frontend surface whenever convenient, never blocking a round. | **APPROVED 2026-07-12** |
| D15 | Embeddings behind our own interface with a **runtime probe chain**: fastembed → model2vec (pure numpy) → ollama → API. Cause: onnxruntime DLLs blocked on corporate-managed Windows (incl. the dev box). Details in `docs/DECISIONS.md`. | decided R1 |
| D18 | **Agent oversight model (Aryan's correction 2026-07-12, REVERSES the original "no owner ride-along" line):** owners always ride along when their agent messages anyone; every chat born from messaging an agent (auto_dm, either direction) or created by an agent makes **all humans at genesis admins** (agents never); pull-ins into preexisting groups join as plain members; **agents may add members, never remove**; agent adds governed by two new group toggles — `agents_add_if_owner_admin` (default ON) and `agents_add_if_members_can` (default OFF). "Agents only" messaging audience gates who may KNOCK; the owner comes in regardless. Bottom line: nothing without oversight. | **APPROVED 2026-07-12** |
| D19 | **Agent lifecycle rules (Aryan 2026-07-13):** owner deletion soft-deactivates all their agents (already R7); **logout does NOT touch agents** — they belong to the account, not the session (the explicit stand-down switch remains); **login claims this machine's agents** (`claim_machine_agents` — ownership transfers to the signed-in member; the invariant cascades the agent out of rooms the new owner isn't in); owners get `delete_agent` + a standing rule: **a member may always remove their own agent from any room, admin or not** (write + fold enforced); **agents never self-manage their account** (profile/status/privacy/blocks are owner-only, GUI-only — the CLI/MCP surface never exposes them); every human account option is owner-manageable for agents (avatar rides R13). | **APPROVED 2026-07-13** |
| D16 | Graph memory default = **mem0 v2 built-in entity linking** (embedded in local qdrant, zero servers). Graphiti-grade KG = optional, server-backed, later (Kuzu archived; FalkorDB-Lite has no Windows wheels). | decided R1 |
| D17 | **CPython 3.12** pinned via uv (`llama-index-embeddings-fastembed` needs <3.13; ML wheel lag on newer). `requires-python >=3.11`. | decided R1 |
| D8 | Nothing model-specific is hardcoded. Adapters + JSON preconfigs for **claude, codex, grok, ollama, deepseek**; a model/CLI is data. API-based adapters later behind the same interface. | agreed (mission) |
| D9 | mesh-cli v2 speaks **MCP** (official python SDK) instead of a bespoke API; humans get a normal CLI on the same core. | agreed (Aryan's call) |
| D10 | Local cache + retrieval index = **SQLite** (stdlib, thread-safe enough, powers instant startup, offline reads, and the qdrant/llamaindex ingest). | proposed |
| D11 | Embeddings run **locally by default** (fastembed/ONNX); memory-extraction LLM calls route to a configurable cheap local model (ollama default) — no surprise API costs. | proposed |
| D12 | Group **owner role is retired** → multi-admin (admins appoint admins; agents can never be admin; last admin can't leave without passing it on). Group permission toggles are config-driven so **channels** reuse the same machinery. | agreed (Aryan's call) |
| D13 | "Members only" audience (privacy matrix) is defined as: *shares at least one chat with me* — there is no contact book. | proposed |

---

## 3. Phases & rounds

Definition of done for EVERY round (from WORKING_AGREEMENT — not repeated
below): design+critique first → implement → tests pass (`pytest`) → live/manual
verification where applicable → version bump → commit + push → memory update.
Rounds are elastic: split when big (rule 5), merge when trivial.

### Phase 0 — Groundwork

- [x] **R0 — This plan.** Approved by Aryan; committed.
- [x] **R1 — Research & decision spike. DONE 2026-07-12** — all verdicts,
      pins + fallbacks in `docs/DECISIONS.md`; 7 smoke scripts in `spikes/r1/`
      all pass. Headlines: crypto D4/D5 flow prototyped end-to-end; qdrant
      embedded OK (single-process per path!); **onnxruntime blocked on the dev
      box → D15 probe chain**; **graphiti deferred → D16 mem0-v2 entity
      linking**; MCP SDK v2 migration budgeted into R12; supabase realtime =
      async-only (R23 wraps it); Python pinned 3.12 (D17).
- [x] **R2 — Skeleton. DONE 2026-07-12** — `agentbridge/core/` (models,
      timekit, config, errors), 14 tests incl. regressions for the ns-tie and
      OneDrive-lock burns, GitHub Actions CI (ubuntu+windows, core-only sync +
      frontend check), `docs/FORMAT2.md` drafted. Key v2 design upgrade
      recorded there: **info events are the source of truth; meta.json demotes
      to a rebuildable snapshot** (kills the last-writer-wins data-loss class).
      Tolerance rules: unknown JSON keys ignored, unknown enum values FAIL
      CLOSED (privacy→nobody, perms→admins, kind→agent).

### Phase 1 — Mesh core (rounds run against scratch roots; live mesh untouched)

- [x] **R3 — Transport + store. DONE 2026-07-12** — `agentbridge/transport/`
      (base interface + FolderTransport: retrying writes, incremental
      `read_log` by byte offset that never consumes a half-synced line,
      traversal guard, ported ReadDirectoryChangesW hint-watcher) +
      `agentbridge/store/` (SQLite WAL cache, offsets/cursors/doc-cache,
      **lease-based durable outbox** + OutboxWorker: transient failures retry
      forever, only structural failures go dead, crash-mid-send self-heals via
      lease expiry). 30 new tests incl. OneDrive-reality cases; live Windows
      watcher test green. FORMAT2 updated: **per-device logs**
      `msgs/<sender>@<machine>.jsonl` (multi-device humans can't fork a file)
      + tenet 6 (watch=hint, poll=truth).
- [x] **R4 — Messaging service. DONE 2026-07-12** — `agentbridge/mesh/`:
      paths, **Sealer seam** (PlainSealer now; R9 swaps in E2EE without
      touching callers), overlays (**one file per message** for edits/
      redactions/pins — concurrent actors can't clobber; per-user reactions =
      D14 done; UserState merge-never-overwrite), readmodel (THE choke point:
      dedup→ns-sort→unseal→edits→redactions-win→reply-blank→hidden→cleared;
      **edit-marks-unread as pure derivation** — v1's cross-user-write blocker
      dissolved), MessagingService (every endpoint membership-gated,
      post = optimistic cache + durable outbox), SyncEngine (parallel
      catch-up, membership gate = fetch-only-what-you-need), Mesh facade.
      Stars = live-resolved ids (v1 snapshots would leak redacted bodies under
      E2EE). 24 new tests (68 total). BUG FOUND VIA py-spy: CloseHandle hangs
      while RDCW blocks on the same handle → CancelIoEx first.
- [x] **R5 — Membership & groups. DONE 2026-07-12** — `events.py` (the fold:
      info events = source of truth, authority checked DURING replay so forged
      events die at read time; cascades + auto-promote), `authz.py` (one home
      for permission predicates, used at write AND fold time), `directory.py`
      (accounts read-side + ported `missing_owners`), `membership.py`
      (create chat/DM/self with v1 auto_dm semantics verified symmetric,
      add/remove/leave, multi-admin grant/revoke — agents never admins,
      rename/description/set_permissions, `refold` self-heal, `chats_for`).
      Screenshot toggles live: edit_settings / send_messages / add_members /
      send_history (**default TRUE — divergence from WhatsApp: agents joining
      need context**) / approve_members (toggle stored; approval flow rides
      R6/R13). Marquee tests: meta clobber heals bit-for-bit; forged
      member_added/admin_granted ignored; fold deterministic across log
      distributions. 22 new tests (90 total).
- [x] **R6 — Privacy & permission layer. DONE 2026-07-12** —
      `agentbridge/mesh/privacy.py` (PrivacyService) + `AgentRules` model +
      enforcement wired into create_dm (messaging gate), create_chat/
      add_members (add gate incl. **pulled owners** — if the owner can't be
      added, chatting the agent fails cleanly), post-in-DM (block check).
      Semantics locked: profile `agents` audience admits agent-OWNING humans
      (relay rationale); the two GATES are PUBLIC (`public_gates()` — agents
      check before messaging, reasons are showable); blocks kill DMs both
      directions without leaking, groups unaffected (WhatsApp); photo =
      everyone/nobody; agent settings owner-only; receipts need BOTH toggles
      (`may_see_receipts_of`, consumed by R8). Delivers backlog: messaging-
      permission model (HANDOFF #1) + block-a-user (#4). 16 new tests.
      **R6.1 correction (D18)** — the `agents` gate audience means "who may
      KNOCK" only: owners ALWAYS ride along; genesis-admin rule (all humans
      admin in auto_dm/agent-created chats); agents add-but-never-remove;
      two new agent-add toggles. 6 more tests (111 total).
- [x] **R7 — Accounts v2. DONE 2026-07-13** — `agentbridge/mesh/accounts.py`.
      **Username change = the Telegram split**: `name` is the immutable
      identity (logs/cursors/memberships never churn), `handle` is the mutable
      @-username (unique across names+handles, reserved words barred) —
      renames are free. scrypt auth (humans only; agents never authenticate);
      password change re-hashes w/ fresh salt (R9 hooks key re-wrap there);
      machine-login ownership (`create_agent` binds owner+machine; default
      about "<Owner>'s <Agent> on <machine>"); per-machine sign-out flips only
      that machine's agents. **Deletion falls out of the invariants**: leave
      every group → the fold cascades the ex-owner's agents out of every room
      + auto-promotes; soft `active=false` on account + all owned agents;
      names stay resolvable (grey-out); DMs to deleted accounts refused on
      create AND on post into existing DMs without leaking why. 9 new tests
      (120 total).
- [x] **R8 — Presence, status & about. DONE 2026-07-13** — `presence.py`
      (per-device heartbeats, throttled write-on-change; merge = any FRESH
      online device wins, staleness window ~3 missed beats covers crashes;
      matrix-gated `visible_presence`; close() never stamps presence for a
      device that never announced — would falsely advance Delivered) +
      `receipts.py` (**Delivered SHIPPED** exactly per the HANDOFF ns-compare
      design: read_ns ≥ msg.ns → Read, else presence last_seen_ns ≥ msg.ns →
      Delivered, else Sent; group tick = lowest tier; BOTH tiers gated by the
      receipt toggles per HANDOFF's privacy note; message_info payload;
      deactivated accounts never deliver — no heartbeat). `set_status` = ONE
      logical account-level status (available/busy/dnd/…), owner-gated for
      agents, matrix-visible so agents check before disturbing. Closes the
      v1 Delivered stub. 11 new tests (131 total).
- [x] **R9 — E2EE. DONE 2026-07-13** — `agentbridge/crypto/` (pure primitives,
      R1-spike-proven) + `mesh/keyring.py` (KeyStore + ChatKeyService: ns-id
      epochs, `ensure()` seal-time self-heal for rotation races, history-aware
      add hook) + `E2EESealer` behind the R4 seam (AAD-bound ChaCha20Poly1305
      + Ed25519 sig; id/ns minted first for replay-proofing; forged/plaintext
      injection → blank, never a lie). Accounts grew identity keygen,
      password+recovery double-wrap (D5), `unlock`/`unlock_with_recovery`,
      password-change re-wrap. `docs/THREAT_MODEL.md` written. Bodies+edits
      encrypted; **metadata stays readable by design** (documented). 8 new
      E2EE tests + all 131 prior green (139 total). File-blob encryption →
      R13 (no upload path yet); migration tool → **R9.5** (touches live data,
      isolated review).
- [x] **R9.5 — v1→v2 migration tool. DONE 2026-07-13** — `agentbridge/migrate.py`
      (+`python -m agentbridge.migrate --src --dest [--dry-run]`). Source is
      READ-ONLY, dest must be empty, `--dry-run` writes nothing. Maps: v1
      PBKDF2 auth kept + verified in v2 (`accounts.verify_password` grew a
      pbkdf2 path; upgrades to scrypt at password-change/login), owners[0]→
      `agent.owner`+machine="migrated"+harness=v1 settings, per-sender jsonl→
      `<sender>@migrated.jsonl` epoch-0 envelopes (ids/ns PRESERVED so
      cursors+receipts survive), synthesized genesis info event (owner→admin;
      ns=oldest-1, kept positive — the store filters ns>0), v1 info pills→inert
      `legacy_note`, redactions/edits/pins/state overlays (star snapshots→id
      lists), blobs byte-for-byte. Built-in verify: re-fold==meta + line count
      v1+1. `docs/MIGRATION_RUNBOOK.md` = the R14 operational procedure +
      rollback (source never mutated). 8 new tests (179 total). **PHASE 1
      COMPLETE.**
- [x] **R10 — Events & notifications. DONE 2026-07-13** — `eventbus.py`
      (bounded drop-oldest subscriptions: a slow consumer can never stall
      sync; store stays source of truth) + `notify.py` (Notifier: message
      pings unless muted — `mute` supports True/forever AND ns-until values;
      added-to-chat always pings; own/info never; previews decrypted locally
      + truncated; `CommandHook` = the CLI on-message command, argv+env,
      never shell) + the pump: `Store.upsert_messages` now returns ONLY
      actually-new records → sync publishes exactly-once (own echoes and
      replays never ping) and **info events auto-refold** remote snapshots
      (meta stays warm without manual refold). SSE endpoint (R13) and MCP
      notifications (R12) are thin consumers of this bus. **Mute stub
      closed.** 9 new tests (153 total).
- [x] **R11 — App-to-app channel. DONE 2026-07-13** — `agentbridge/applink/`:
      `machines.py` (each machine announces version/platform/capabilities;
      stale window) + `control.py` (machine-to-machine request/reply RPC:
      per-machine inboxes, single-writer id-docs, local seen-cursor →
      idempotent, best-effort gc) + `update.py` (detection + **mandatory
      SHA-256 verification**; `fetch`/`install` INJECTED so the backend never
      downloads-or-executes; `apply` refuses without confirm() AND a digest
      match — a tampered artifact raises loudly; a peer version-advert is only
      a hint, the trusted digest comes from the GitHub release) +
      `setup_assist.py` (rides the lane; **owner-gated by new
      `AgentRules.setup_assist`**, default off; unpermitted/unknown-agent
      requests auto-decline and leak nothing; reply is a PROPOSAL the
      requester reviews). Facade: `mesh.applink`. 12 new tests (163 total).
- [x] **R12 — mesh-cli v2 (MCP). DONE 2026-07-13** — `agentbridge/cli/`:
      `server.py` = FastMCP surface (mcp~=1.28; SDK-v2 rename lands ~07-27,
      re-check then): list/read/chat_info/who_is/my_unread + send (threaded
      reply support) /edit/delete/react/pin/star/mark_read + create_dm/
      create_group/add_members/leave + **`next_events` long-poll** draining
      the R10 bus (transport-agnostic near-realtime). **D19 structural:
      account tools + remove_member simply don't exist on this surface** —
      test asserts their absence. `main.py` = human CLI (send/read/chats;
      password-verified humans only; agents use `mcp` mode passwordless) +
      `mcp` stdio entry. Verified: 9 MCP tests through REAL in-memory client
      sessions (errors surface as tool errors; privacy respected in who_is;
      D18 ride-along over MCP) + live human-CLI smoke (send/read/refusal).
      CI grew `--extra mcp`. 172 tests total.

### Phase 2 — GUI cutover

- [ ] **R13 — GUI connector rewrite.** The NEW server lives at
      `agentbridge/gui/` (the v1 `gui/server.py` keeps serving the live app
      untouched until R14 — cutover is a launcher flip); ONE shared frontend
      speaks both dialects via a caps probe until R14 retires v1.
      Decomposed (rule 5):
  - [x] **R13a — connector core. DONE 2026-07-13** — `agentbridge/gui/`
        (context/routing/serialize/api_auth/api_chats/sse/app): stdlib
        ThreadingHTTPServer on 127.0.0.1 serving `gui/static/` + JSON API
        over the facade; session survives restarts via local
        `gui_session.json` + keystore (no password re-entry); signup returns
        the ONE-TIME recovery code; login runs `accounts.upgrade_login`
        (NEW: pbkdf2→scrypt re-hash + identity-key provisioning for
        migrated v1 accounts — the code is returned to show once); failed
        login/signup never drops the current session; SSE
        `/api/mesh/events` off the R10 bus (minimal frames — no body ever
        rides the stream, client refetches via the read model); state/chat
        payloads emit v1+v2 spellings (`admins` + `owners`-compat, `handle`,
        per-user `archived`). Read-side helpers: `Directory.names()`,
        `messaging.chat_overview()` (one-pass sidebar), `my_state()`.
        pytest-timeout added (R3 CI-hang lesson). 8 HTTP-level tests over
        real sockets incl. E2EE peer delivery + SSE + traversal guard
        (187 total).
  - [x] **R13b — endpoint parity + sealed file blobs. DONE 2026-07-13** —
        full v1 endpoint surface over the facade (star/pin[+`until_ns` lazy
        expiry]/edit/delete/undelete/clear/react/forward[re-seals blobs per
        target]/flags[archive/pin/hide/mark-unread/mute]/chat_info/typing/
        livefeed; membership+admins+rename/description/permissions;
        profile/handle/about/status/privacy/blocks/password/delete-account;
        agents create/patch[harness = model-picker scaffold via NEW
        `accounts.set_agent_harness`]/delete/stand-down + `control.json`
        pause; avatars user/agent/group). **Sealed blobs close OPEN(R13):**
        `Sealer.seal_blob`/`open_blob` (`AB2E`+epoch+nonce+ct, AAD binds
        chat|blob|id|epoch; plain honored only in epoch-less legacy chats;
        provenance = `files[].sha256` inside the SIGNED message, verified
        before serving). NEW fold events: `avatar` (group photo marker in
        the fold, not LWW meta) + `chat_deleted` (admins, groups, TERMINAL —
        empty member list, later events incl. forged re-`created` ignored).
        D19 login-claims wired into GUI login. Docs synced (FORMAT2 blobs
        SETTLED + THREAT_MODEL). 25 new tests — 204 total. **Two review
        catches:** the R5-era forged-event fold tests were HOLLOW (backdated
        ns died on the before-genesis rule, never reaching the authority
        checks — now post-genesis and genuinely exercising them), and that
        audit surfaced the **genesis-forgery gap → R13.5.**
  - [x] **R13.5 — fold genesis integrity. DONE 2026-07-13.** The fold now
        runs an authenticity gate (`events._authentic`) before any event
        applies: (1) v2 chat ids end in `-g<16hex>` committing (sha256+nonce)
        to their genesis — a backdated/roster-changed `created` re-hashes
        differently and is rejected, so genesis theft is dead; (2) info
        events are Ed25519-signed over `chat|id|ns|from|event` (signer wired
        via the facade from the keystore; `Directory.sign_pub` feeds the
        verifier) — impersonating a keyed author fails and the chat binding
        blocks cross-room replay; (3) sync drops records whose `from` ≠ the
        log owner. The proposed separate manifest-anchor gate was redundant
        (subsumed by the gid/legacy split + membership isolation + the
        sealer's existing epoch-0 refusal) — noted in THREAT_MODEL. Migrated
        (legacy-id, unsigned) chats still fold. 7 new tests (218 total); the
        gid-bound id verified live (`…-g0dbae7d942ec7d96`). Residual
        (migrated-chat self-genesis) documented for R24/R25.
        ORIGINAL SPEC below:
  - [~] **R13.5 — fold genesis integrity (MUST land before R14).** Found by
        our own tests: a BACKDATED forged `created` event wins "first
        created wins" and steals the whole chat (fold re-derives from forged
        membership; real events then fail authority checks). Fix set:
        (1) Ed25519 signatures on info events — build_event signs
        id|ns|from|event, fold verifies whenever the author has published
        keys (impersonation dies; unsigned accepted only for pre-upgrade
        legacy authors); (2) v2 chat ids commit to their genesis
        (digest-bound id suffix; fold refuses a `created` whose digest
        doesn't match the chat id); (3) epoch-0 (plaintext) envelopes+blobs
        accepted only for chats the migration manifest lists (kills
        fabricated-chat attribution); (4) sync-ingestion sanity: drop
        records whose `from` ≠ log-owner (defense-in-depth vs buggy
        clients). Residual (documented in THREAT_MODEL): a member of a
        MIGRATED chat backdating their own signed genesis — revisit R24/25.
  - [x] **R13c — frontend wiring. DONE 2026-07-13** — caps probe
        (`isV2()`/`meshCaps()` read the v2 `{v:2, caps}`; v1 sends neither so
        the app serves both until R14) + `realtime.js` (EventSource on
        `/api/mesh/events`, repaints the sidebar + open transcript per frame,
        auto-reconnect, bounded manual retry; inert on v1) + poll backs off
        to a 20s safety-net tick when the stream is live + admin adapter
        (`chatAdmins`/`meshIsAdmin`: v2 multi-admin `admins` list vs v1 single
        `owner` — replaces the `meta.owner === me` checks in details/chat/
        sidebar; the member chip now reads "Admin"). Server got a compat
        `/api/state` (the shell the frontend boots+polls on) and the `chat`
        endpoint now emits `pins` as an ARRAY of `{id, until, body}` +
        `created`/`created_by` (the frontend maps these; a dict blanked the
        transcript — a LIVE catch). **Verified live in the browser preview
        against a scratch v2 root:** signup → SSE connected → self-chat →
        message posts + renders with markdown + Delivered tick; Settings
        renders; zero console errors. check_frontend 22/22. 4 new shape/SSE
        tests (210 total).
  - [x] **R13d — new settings surfaces (v2-gated). DONE 2026-07-13** — D5
        recovery-code modal (signup + first-migrated-login; ack-gated
        Continue); Account page over a new `/api/mesh/me` (owner-only,
        GUI-only per D19): @handle change (Telegram model), about + status
        editors, the privacy matrix (7 audience selects + a read-receipts
        toggle), a change-password modal (re-wraps E2EE keys — proven by
        logout→re-login); group info gained the D12 multi-admin UI
        (per-member Make/Dismiss admin + remove, agents never promotable)
        and the "Group permissions" card (edit_settings/send_messages/
        add_members levels + send_history/approve_members + the two D18
        agent-add toggles; visible to all, editable by admins). Model-picker
        scaffold = the existing agent editor, un-broken by fixing the `agent`
        endpoint to accept the FLAT patch it sends. Delivered tick already
        renders. Fixes: static assets now `Cache-Control: no-cache` (stale
        module after an app update); `mountCsels` scoped to the agents page
        (was doubling every privacy dropdown); `csel` gained `disabled`.
        **All verified live** in the browser preview. check_frontend 22/22.
        ---
        **R13 COMPLETE.** The v2 GUI connector is a thin, membership-gated,
        E2EE, SSE-realtime app over the Mesh facade, shape-compatible with the
        shared frontend. NEXT: **R13.5** (fold genesis integrity — MUST land
        before R14), then **R14** (live migration + cutover).
  - [x] **R13 hardening (the Windows-CI flake, 3 real fixes).** v0.24.67:
        `read_json` retries transient locks (a reader hitting another
        thread's `os.replace` spuriously read "no such chat"). v0.24.72:
        64 striped in-process I/O locks shared by JSON read/write (Windows
        `os.replace` fails while ANY same-process handle is open — CPython
        opens without FILE_SHARE_DELETE). v0.24.73: **refold treats the meta
        write as a CACHE write (tenet 3)** — a transiently blocked write
        (CI's scanner holds fresh files past six backoffs) logs + defers
        instead of failing the user action; the next mutation heals the
        snapshot. Plus a fresh tmp name per retry attempt. Each fix carries a
        regression test (incl. multi-writer/multi-reader stress). These are
        LIVE-MESH-relevant fixes (OneDrive locks behave like the scanner),
        found because the GUI tests run the full concurrent stack.
- [x] **R14 — Migration & live cutover. DONE 2026-07-13.** Followed
      `docs/MIGRATION_RUNBOOK.md`: froze agents (control.json paused) → stopped
      the local v1 worker + GUI → snapshotted `mesh/` to
      `~/Downloads/mesh.backup-20260713` (verified identical) → dry-run
      (4 users / 12 chats / 393 msgs / 48 info / 48 overlays / 26 blobs,
      verification PASS; 55 warnings = empty scratch dirs, confirmed
      zero-record) → real migrate to `<synced>/mesh2` → validated at the
      facade (busiest chat folds 127 msgs + a tombstone; receipts sane; live
      `mesh/` byte-identical to backup). **Aryan chose full cutover now**
      (accepting agents offline until R15). Cutover: `AgentBridge.pyw`
      repointed to launch `agentbridge.gui` in `.venv`; `mesh_root=mesh2`
      persisted in `~/.agentbridge/config.json` (merged, v1 keys preserved);
      `agentbridge.gui.main()` now defaults `--root` from that config and
      opens the Edge app window (`desktop.launch_window`); verified live on
      port 7787 (v2, 4 users, caps). **Open manual step: stop/re-point the
      REMOTE CoCo/AVD v1 worker (can't reach it from here) — noted in
      HANDOFF.** Rollback = restart v1 on `mesh/` (untouched) + restore the
      backup. Findings: (a) migration paths MUST be Windows-form
      (`C:/…`/`C:\…`), the MSYS `/c/…` form reads empty under Windows Python;
      (b) the migrator exits 1 on benign empty-scratch warnings — expected.

### Phase 3 — Agent harness (the rename: worker → harness)

- [x] **R15 — Harness core. DONE 2026-07-13** — `agentbridge/harness/`
      (settings/triggers/queue/conversation/timers/feed/responder/runner):
      one symmetric `AgentRunner` per agent over the Mesh facade (never the
      folder); scan = truth, watcher = hint (tenet 6). **Durable WorkQueue**
      in the agent's store with lease recovery + owner-set `concurrency`
      (groups dispatch per (chat, sender): a sender's burst = ONE reply,
      different senders run in parallel, in one chat or across).
      **Answered-guard is two-legged** — ledger keyed `(msg_id, edit_ns)` +
      the transcript itself (my reply_to proves it) — total local-state loss
      can't double-reply (test-proven). Catch-up policy owner-set
      (`recent`(48h)/`none`/`all`, batched); edit-retrigger rides the same
      `(id, edit_ns)` key (v1's process-baseline dance gone); rule-`all`
      own-tail damping kept, scoped. ConversationManager delivers enriched
      bundles (roster w/ reply behaviour, pins, matrix-gated sender
      status/presence, edits applied via the read model); plain `render()`
      until R17. **TIMERS shipped:** `Reply.timers` → durable TimerService →
      due timers dispatch through the same pipeline; timers + queue mirrored
      to `status/<agent>_harness.json`, served owner-only at
      `GET /api/mesh/agent_harness` (frontend surface rides R16/R18). Run
      feed keeps the v1 `status/<agent>_run.json` shape (draft body dropped
      — content, not metadata, in an E2EE mesh); task steps recorded to
      `chats/<id>/tasks/<msg_id>.json` and attached by message_info. Rate
      cap = ATOMIC slot claim (parallel groups can't both pass cap 1) with
      refunds on silent runs. Responder = the injected seam (R16 adapters);
      `--dry-run` CLI works today; NO_REPLY hygiene ported (R17 replaces).
      Ported: SingleInstance lock, `supervise()`, error notices (capped).
      **D19 kept structural:** the runner never mutates accounts — new
      owner-side `accounts.adopt_agent` re-homes a MIGRATED (keyless) agent
      to the owner's machine + mints its identity (`POST /api/mesh/adopt_agent`);
      keyed agents are refused a re-key (old event signatures would orphan —
      machine moves need published key history, later). ALSO SHIPPED
      (v0.24.77, pre-R15 hardening found designing this): epoch-0 acceptance
      now keys on "predates the chat's FIRST epoch" in legacy chats — the
      first sealed post into a migrated room no longer blanks its v1
      history/files. 24 new tests (243 total) + a real-run-loop smoke
      (threaded reply, owner-visible timer firing, task steps, read receipt)
      + CLI dry-run/wrong-machine smokes. **Agents come back ONLINE at R16**
      (the registry/adapters give the Responder a real model; then adopt
      @claude/@coco and start harnesses).
- [x] **R16 — Model registry & adapters. DONE 2026-07-13** —
      `agentbridge/harness/adapters/`: preset JSONs (claude/cortex/codex/
      grok/ollama/deepseek — a model/CLI is DATA, D8; owners can overlay or
      add presets in `<home>/adapters/` with zero code) + `ModelRegistry`
      (loads catalog, probes THIS machine's installs, resolves owner config →
      one invocation per audience; model order: override-all → per-audience →
      preset default; single-install machines resolve without picking) +
      `CliResponder` (ONE subprocess engine for every family: argv lists
      never shell, streamed stdout + watchdog, live steps → run feed,
      claude-stream/codex-jsonl/text parsers, staged-in attachments +
      outbox-out files, usage-error minimal retry that never drops safety
      args/blocklists). **Per-purpose routing** joined HarnessSettings
      (owner/people/agents: enable + model; disabled audiences resolve at
      scan, before slots/rate). GUI: the raw model/reasoning fields became
      the real picker (family via `GET /api/mesh/harness_options` probe,
      current model, effort when supported, 3 routing rows; no-model
      families degrade to enable-only; off-machine agents get one-click
      Adopt) — verified live in the preview (options populate, save
      round-trips, reload re-hydrates). Export tool shipped
      (`python -m agentbridge.export`, `--legacy-only` = the purge set) +
      `--all` supervise mode + `AgentHarness.pyw` (AgentWorker.pyw's
      successor). **VERIFIED WITH THE REAL claude CLI on a scratch root**
      ("I'm Claude Haiku 4.5." — full stack: preset → registry → engine →
      runner → threaded E2EE reply + task steps); finding: modern claude
      headless denies file reads by default → the preset allows
      Read/Write/Edit/Glob/Grep (R18's broker scopes this properly).
      **Live @claude is PREPPED, not started:** adopted to the dev box
      (identity minted+published), harness = {adapter: claude, catchup:
      none}, live dry-run = 0 triggers; Aryan flips it on (runbook in
      HANDOFF). CoCo needs the same on the AVD (v1 worker stop still open).
      10+7 new tests (263 total). Presets for CLIs not installed here
      (codex/grok/ollama/deepseek) are best-effort data pending their own
      bring-up (`verified: false` in the preset).
- [x] **R16.5 — Legacy purge. DONE 2026-07-13** (Aryan: "remove the legacy
      code, do not add junk to keep the legacy items working"). Executed in
      the only safe order — export → delete → strip:
      (1) all 12 migrated chats EXPORTED to plain text (11 as aryan, the
      Fable↔Fabot DM as fable) to `~/Downloads/agentbridge-legacy-exports-
      20260713/`; verified zero blank bodies — incl. the two rooms the
      pre-v0.24.77 bug had blanked (the fix proven on live data);
      (2) the 12 legacy chat dirs backed up to `~/Downloads/mesh2-legacy-
      chats-backup-20260713/` (verified file counts) and DELETED from the
      live `mesh2/` (OneDrive marks dirs ReadOnly+ReparsePoint — deletion
      needs attrib -R first); only the gid-bound chat remains, directory
      intact, @claude's stale per-chat rules dropped;
      (3) STRIPPED: sealer epoch-0/plain-blob acceptance (+`first_epoch`,
      blob-ns parsing), the fold's non-gid genesis acceptance, AND the
      keyless-author unsigned-event allowance — every event now requires a
      valid signature; an account without published keys cannot mutate
      (keys mint at signup/login/adoption; live-verified: the remaining
      chat folds, aryan+claude keyed, fable/coco simply can't write until
      their next login/adoption). Test fixtures moved to a shared KEYED
      `seed_account` (conftest); `migrate.py` + its runbook retired to
      `legacy/`, its tests dropped; THREAT_MODEL + FORMAT2 updated — the
      previously documented migrated-chat genesis residual is gone with
      the chats that carried it. `is_legacy_chat_id` survives as a pure
      tooling predicate (the exporter's inventory selector). Safe-by-
      construction keepers: pbkdf2 login upgrade (accounts that never
      logged in) and adopt_agent's keyless minting (@coco's bring-up).
      (4) new-code rule, effective NOW: nothing new accommodates v1 shapes.
      The v1 source tree itself still retires at R26. 248 tests.
- [x] **R16.6 — Per-chat behaviour fixes (first live-use feedback). DONE
      2026-07-13.** (1) a DM with an agent now defaults to the **'all'**
      reply rule — talking to it one-on-one IS addressing it (v1 semantics;
      the GUI already advertised this, the harness never implemented it);
      explicit per-chat rules still win. (2) per-chat rule writes from chat
      info were LANDING IN THE WRONG STORE (`rules` is two stores under one
      key: gate audiences vs chat-id→rule) and errored on validation — now
      partitioned in the endpoint; every write had failed since R15.
      (3) NEW: per-chat **model picker** on the chat's Your-agents page;
      resolution = chat pick → current model → audience route → preset
      default. (4) `set_agent_harness` merges dict values one level deep
      (inner null deletes) so per-chat writes never wipe sibling chats.
      250 tests; verified in the dev-rig browser (DM shows "Default — reply
      to every message", group shows the agent default; writes round-trip,
      clears drop the key, model pick survives). Live GUI + @claude harness
      restarted onto the new code.
- [x] **R17 — Prompt manager. DONE 2026-07-13.** Every word an agent is told
      is DATA: `harness/prompts/default.json` (persona / roster / task /
      capabilities / etiquette / silence blocks + the feed's `activity`
      wording map), overlaid key-by-key by `<home>/prompts/default.json`,
      then the agent's own `harness["prompts"]` dict (per-agent tweaks stay
      config — one harness, all agents). `prompt.py` (PromptManager/
      PromptPack) assembles in fixed order so overlays reword but can't
      drop the rails; the silence block always carries the REAL sentinel,
      injected — pack and parser can never disagree; broken templates
      degrade to raw text. NO_REPLY → **`<<<NO-REPLY>>>`** (the bare word
      could silence an agent merely discussing it; old word is now just a
      word). Reply-vs-tag: threading to the answered message stays enforced
      (safe); tagging is prompted judgment (author already notified — never
      tag them; tag-only agents are forced by tags — only genuine needs).
      Livefeed/task steps: cli.py extract_step() extracts FACTS, the pack
      words them ("Searching for …", "Reading context.md" — paths basename
      AFTER extraction; a 90-char pre-cut had produced "Reading f164" live);
      the sentinel never leaks into the feed. Delivery is pure data now
      (render() moved into prompt.py). Verified with the REAL claude CLI on
      a scratch root: intro reply threaded + clean feed lines, then an FYI
      → sentinel → nothing posted, feed "No reply needed". 260 tests; live
      @claude harness restarted onto it.
- [x] **R18 — Permission broker + workspaces. DONE 2026-07-13.** Each run
      gets a per-chat **workspace** (`home/harness/<agent>/workspaces/
      <chat_id>/` — context, inbox, outbox, cwd; R20 adds memory here). The
      **broker** (broker.py) decides every tool use, in order: inside the
      workspace → allow; inside a DENY ROOT (harness home, mesh root — keys,
      caches, other members' bodies) → refuse outright, no owner can grant
      it (protects visibility = membership); a preset `auto_allow` read-class
      tool → allow; an owner standing rule (`harness["approvals"]`
      [{tool,chat}], chat "*" = all) → allow; else **ASK** the owner and
      block. No answer in `ask_timeout_s` (default 120) = **deny** (fail
      closed); a denied intent is cached per-run (inner CLIs retry — the
      spike saw 3 asks for one Write). The **2-way channel** (bridge.py) is
      a per-run FastMCP streamable-http server bound to that run's
      chat/workspace/policy; tools `approve` (permission gate) and
      `ask_member` (agent → owner question); `structured_output=False` is
      mandatory (spike: FastMCP's structuredContent wrap reads as invalid).
      Claude preset drops the interim `--allowedTools Read,Write,Edit,Glob,
      Grep` for `--permission-prompt-tool mcp__ab__approve` +
      `--mcp-config`; `permission_args`/`auto_allow` are preset DATA, kept
      in BOTH full and minimal argv. GUI: Codex-style cards above the
      composer (Allow / Always allow here / Deny, or a text answer for a
      question), owner-only, on a 2s poll while the chat is open (the run is
      blocked). Asks/answers ride two one-writer docs (`status/asks/
      <agent>.json` harness-written, `_answers.json` GUI-written). Verified
      with the REAL claude CLI on a scratch root: in-workspace write ran
      free; an out-of-workspace write paused until the "owner" approved,
      then wrote + confirmed. Browser-verified both card types round-trip.
      273 tests; live harness + GUI restarted onto it. Deferred to a later
      round: auxiliary-flag allow/deny UI (presets carry safety flags as
      data today); codex/other-family permission wiring (their own bring-up).
- [ ] **R19 — Data pipeline hardening.** The agent receives messages ONLY
      through the harness (mesh API + the agent's keys — it never touches the
      folder); leak audit: workspace contains no raw mesh data, prompts carry
      only membership-filtered content; uniform capability exposure through
      harness tools (pin, star, forward, **create chat/DM — agent-initiated,
      gated by R6**, group creation, etc.).
- [ ] **R20 — Memory.** qdrant (embedded/local per R1) + fastembed; **chat
      memory lives in that chat's workspace; global memory is separate**;
      default policy: agents read/write GLOBAL memory only in DMs (owner can
      change); mem0 / graphiti knowledge-graph layer per the R1 spike outcome;
      extraction via configurable local model (D11).
- [ ] **R21 — Retrieval & planner.** llamaindex search over chat history +
      files (fed from the SQLite cache); the loop: request → **planner** →
      search/retrieve → rank → load summaries → build prompt → agent;
      rolling **context summarization**; session policy per D6
      (stateless + optional burst-resume).
- [ ] **R22 — Peer harness access.** With the owner's grant, another agent may
      talk to this agent's harness (diagnose/repair when the agent is down —
      "remote access, almost"); same R6/R18 permission rules apply to the
      peer; frontend shows an explicit confirmation popup before any peer
      session; every peer action audit-logged.

### Phase 4 — Realtime backend + hardening

- [ ] **R23 — Supabase driver.** Schema + RLS mirroring visibility=membership;
      realtime subscriptions feeding the same event bus; storage for files;
      setup-time choice **cloud vs synced-folder** with honest pros/cons copy
      (cloud: more users, near-realtime; folder: more private, no third
      party); E2EE applies identically (server stores ciphertext).
- [ ] **R24 — Stress & soak.** Simulated 10-agent machine, message storms,
      offline catch-up at scale, crash-mid-send recovery, queue durability,
      cache-rebuild-from-transport, perf profiling; fix what breaks.
- [ ] **R25 — Security review.** Permission-bypass hunt across every mutating
      endpoint; E2EE audit (key rotation, removed-member access, envelope
      misuse); prompt-injection resistance of harness prompts; peer-access
      abuse cases; blocklist/read-only rails verified in every fallback path.
- [ ] **R26 — Docs & retirement.** ARCHITECTURE.md rewritten for v2; HANDOFF
      updated; `mesh.py`, `agent_worker.py`, `legacy/bridge.py`,
      `handler_coco.py` moved to `legacy/`; version source moves to
      `agentbridge/__init__.py`; packaging-prep notes for the next session
      (setup wizard, installers, PWA).

---

## 4. Backlog cross-check (every known item → where it lands)

| Backlog item (source) | Covered in |
|---|---|
| Settings overhaul: messaging-permission model (HANDOFF #1) | R6 |
| Who may pin / group toggles (HANDOFF #1) | R5 (rides *Edit group settings*) |
| Per-agent CLI/tool scoping incl. sql-read-only vs sandbox-DDL (memory #14) | R16 + R18 |
| Agent-initiated chat creation, owner-pull-in reuse (HANDOFF #1) | R19 (gate from R6) |
| Setup/account overhaul: machine ownership, retire legacy/bridge.py (HANDOFF #2) | R7 (model) + R2/R26 (bridge.py); packaging = next session |
| CoCo legacy-handler Phase 3 delete (HANDOFF #2) | R26 |
| Worker overhaul: unread queue, parallel, duplicate-guard (HANDOFF #3) | R15 |
| edit-marks-unread (HANDOFF #3) | R4 |
| Reply-vs-tag choice (HANDOFF #3, memory #15) | R17 |
| Uniform capability exposure (memory #15) | R19 |
| Block a user (HANDOFF #4) | R6 |
| History-on-join policy (HANDOFF #4, memory #5) | R5 |
| Multi-admin roles (HANDOFF #4) | R5 |
| Group invite links (HANDOFF #4) | **EXCLUDED** (Aryan: skip invite-via-link) |
| Emoji reactions (HANDOFF #4) | R4 data layer (D14, low priority) |
| Profile photos (HANDOFF #4) | already shipped (v0.24.24–32) |
| Agent swarms/clones (HANDOFF #5) | registry designed for it in R16; swarm round stays AFTER this plan |
| True privacy (HANDOFF #6) | R9 + R23 |
| Delivered tick (stub) | R8 |
| Mute notifications (stub) | R10 |
| Livefeed raw tool noise (memory #1) | R17 |
| Agent context/session mgmt design (memory #3) | R21 (D6) |
| Action files (memory #4) | superseded by R19 capability API |
| Rate-limit design (memory #6) | R15 (queue + caps) |
| Debug-mode reply rule (memory #7) | R16 (adapter/config surface) — low |
| Client-side file caching (memory #10) | R3; PWA serving = next session |
| In-chat ACK card + output formatting (memory #11) | R15/R17 |
| NO_REPLY sentinel replacement (memory #12) | R17 |
| Stand-down PermissionError resilience (memory #19) | R2 (retrying writes are the default primitive) |
| Password-change UI + username change | R7 + R13 |
| Status/about support (new) | R8 |
| Privacy matrix incl. read-receipt toggles (new) | R6 |
| Local caching (new) | R3 |
| MCP mesh-cli + CLI notifications (new) | R10 + R12 |
| E2EE (new) | R9 |
| App-to-app: auto-update, setup-assist (new) | R11 |
| Account deletion UX (new) | R7 |
| Vector memory / KG / semantic search (new) | R20 + R21 |
| Codex-style permission popups (new) | R18 |
| Peer harness access (new) | R22 |
| Per-purpose models + picker + reasoning effort (new) | R16 |
| Realtime backend choice (new) | R23 |
| **Not this plan (frontend/other):** camera framing (low), hash-ify info-pane subviews, wizard/PWA/packaging | next sessions |

## 5. Risks

1. **Heavy deps on Windows** (graphiti/kuzu, qdrant-local) — R1 spike gates
   them; fallbacks pre-agreed so the mesh never waits on the memory stack.
2. **Live-mesh disruption** — parallel root (D3); nothing live before R14;
   R14 has a written rollback.
3. **Key loss = unreadable history** (D5) — recovery code UX must be loud.
4. **Harness-on-harness fragility** (driving CLIs, not APIs) — adapter
   contract tests per CLI; API adapters later ease this.
5. **LLM cost for memory extraction** — local-first (D11), batching.
6. **Scope creep** — rounds are gates; anything new goes in this file first.
7. **Parallel Claude sessions on one tree** — one session at a time (memory
   lesson: v0.24.24 collision + mojibake).

## 6. Open questions — RESOLVED 2026-07-12

1. ~~D1/D3/D4/D5 approvals~~ → all approved.
2. **Supabase account**: still to open (supabase.com, free tier, org acct
   recommended) — not needed until R23; remind Aryan as Phase 4 approaches.
3. ~~Emoji reactions~~ → IN, low priority (D14).
