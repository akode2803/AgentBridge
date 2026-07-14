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
- [x] **R19 — Data pipeline hardening. DONE 2026-07-13.** The
      messages-only-through-the-harness property held by construction since
      R18 (mesh folder is a deny root; context comes from `messages_for`) —
      now PINNED by a leak-audit test: a run's workspace tree never contains
      another chat's bodies. **Capability tools** ride the R18 bridge, bound
      to the agent's OWN Mesh facade so membership/privacy/R6 gates apply
      exactly as for any member: `pin_message`/`unpin_message`,
      `star_messages`, `react`, `forward_message` (attachments re-sealed for
      the target), `list_chats`, `create_dm`/`create_group` (R6-gated, owner
      rides along per D18, optional opening message, capped 2 creates/run —
      a refusal never burns the slot), and `schedule_timer` → Reply.timers →
      the R15 TimerService (owner-visible; the R17-deferred timer directive
      ships here). ``send``/``read`` tools DELIBERATELY absent — the reply
      pipeline owns posting (threading, rate caps, answered-guard) and the
      context file owns reading. Claude preset allows the whole `mcp__ab`
      server. Live probe found a real gap: the transcript carried no message
      ids, so the model INVENTED one and got an opaque backend error —
      transcript lines now carry `(id m-...)` and the tools validate ids
      with a plain refusal. Feed words the bridge tools ("Pinning a
      message", "Scheduling a wake-up"). Verified with the real claude CLI:
      pin landed on the correct message + timer scheduled and owner-visible.
      276 tests; live harness restarted onto it.
- [x] **R20 — Memory foundation. DONE 2026-07-13** (+ **R19.5** same day,
      Aryan's ask: timers + agent asks SURFACED in the GUI — sidebar
      hand-dots on chats where an agent waits, timer chips above the
      composer, a Scheduled row per agent in Settings; /api/mesh/asks now
      carries timers). Memory = two tiers: the workspace ``MEMORY.md``
      (free-form notepad, seeded per chat) and the VECTOR store —
      ``harness/memory.py``: qdrant **local mode** under
      ``home/harness/<agent>/memory`` (one path per agent process —
      portalocker), one collection per chat + one ``global``. Embeddings
      ride the D15 probe chain behind our own interface: fastembed →
      model2vec (probed at first use, never import; a box with neither
      reports memory unavailable, softly). Bridge tools ``remember`` /
      ``recall``; the GLOBAL scope follows the owner's ``global_memory``
      policy (dm | everywhere | off — default dm: a group can't quietly
      write into the cross-chat brain). Runner close releases the qdrant
      path lock through the responder. Tests inject a deterministic fake
      embedder (real backends download models — probed per box, not in CI).
      Verified on this box: fastembed correctly falls through (onnxruntime
      DLL block) → model2vec potion-base-8M (256d); REAL-CLI two-run arc:
      "remember Friday 3pm" → new run → recalled it. 282 tests; live
      harness restarted. DEFERRED: mem0/graphiti entity extraction (D16)
      needs an extraction LLM this box lacks (no ollama) — its own
      bring-up when one exists; pyproject ``memory-full`` extra carries
      fastembed+mem0ai for capable boxes.
- [x] **R21 — Retrieval over history. DONE 2026-07-13.** Long chats stop
      forgetting: ``harness/retrieval.py`` keeps an incremental per-chat
      vector index of the FULL history (fed from the read model = the
      SQLite cache; ns high-water mark in the agent's store, so a wiped
      qdrant dir just rebuilds) inside the agent's ONE qdrant path
      (``hist-<chat>``, beside R20's memory collections). The loop:
      trigger → **plan_query** (deterministic: trigger text + quoted
      parent — THE seam where a planner model slots in later, D11) →
      vector search → rank (score-gated at 0.30, tail-excluded, returned
      in story order) → a "possibly relevant earlier messages" context
      block before the transcript tail. File NAMES ride the index text.
      Retrieval is garnish by contract: any failure leaves the run intact.
      JUDGMENT CALL vs the original bullet: built on the R20
      qdrant+embedder foundation instead of llamaindex — same capability,
      zero new deps, and llama-index-embeddings-fastembed cannot run on
      this box anyway (onnxruntime); llamaindex stays reserved for FILE
      CONTENT parsing (its loaders) in a later round. Also deferred with
      reasons: prose summarization + LLM planner (need a local model —
      D11), D6 burst-resume (pure cost optimization; preset data can
      carry it). Verified real-CLI: a hostname buried 35 messages beyond
      the tail was retrieved into the context block and answered
      correctly. (Haiku mimicked the silence marker's <<<>>> decoration
      around its answer — the pack now says the marker is silence-only.)
      288 tests; live harness restarted.
- [x] **R22 — Peer harness access. DONE 2026-07-13.** With its owner's
      grant, another agent reaches THIS agent's harness to diagnose it.
      ``harness/peer.py``: SIGNED request/response docs (Ed25519, the
      info-event model — a forged request from a folder writer without the
      requester's key fails verification and is dropped; req_id is bound
      into the signature); one writer each way (``peer/<target>/req|resp/
      <requester>.json``). The owner gate (D19, never the agent's choice):
      ``peer_access`` = "off" (default: unreachable, requests denied
      silently but AUDITED) | "ask" (each session → owner popup, the R18
      ask surface, kind "peer"); "Always" persists ``peer_auto`` (owner-
      side write). ``serve_once`` runs in the tick NON-BLOCKING (even while
      standing down — diagnosing a stuck agent is the point): new request →
      verify → policy → auto? run : park awaiting; verdict lands → run/deny;
      no answer in 180s → deny (fail closed). Commands are READ-ONLY
      diagnostics — ping / status / run_feed; **repair mutations DEFERRED**
      (own gating, later). ``peer_diagnose`` bridge tool for a live agent to
      initiate (bounded wait, else "pending"). GUI: peer requests ride the
      ask-cards (chatless → shown in the open chat), Settings→My agents gets
      a Peer-access select + a recent peer-activity audit list. Every
      outcome (requested/allowed/denied/timed-out/denied-off) audit-logged,
      owner-visible. Verified two-agent live (@ops→@claude status, owner
      approved, signed response + audit) and browser-verified the card +
      verdict routing. 296 tests; live harness + GUI restarted.
- [x] **R22.5 — Peer REPAIR mutations. DONE 2026-07-13** (the surface R22
      deferred). Four harness-local repair commands — ``pause`` / ``resume``
      (a persisted, harness-LOCAL hold distinct from the owner active flag
      and the global control.json; honoured by ``standing_down``, survives a
      restart), ``clear_queue`` (drop poisoned pending; ledger + cursors
      untouched), ``clear_timers`` (cancel a runaway scheduler). A SECOND,
      stricter gate: ``peer_repair`` (default OFF) — repair is refused
      outright unless on, and ALWAYS surfaces a per-session owner popup even
      for a ``peer_auto`` peer (a diagnostics auto-grant NEVER covers a
      mutation). Actions act ONLY on the target harness's own runtime state
      (never chats, messages, accounts, keys) and are INJECTED by the runner,
      so peer.py can't reach anything it wasn't handed. GUI: repair requests
      render a louder (red) ask-card worded as the mutation, with no
      "always" shortcut; Settings→My agents gets a Peer-repair toggle.
      Verified two-agent live: @ops pause → owner-approved → @claude
      standing_down flips True (held, persisted) → resume → False; refused
      cleanly when peer_repair off. 301 tests; live harness + GUI restarted.
      Still deferred with reason: config-edit / restart mutations (need
      process-level control beyond the harness's own state).

### Phase 4 — Realtime backend + hardening

- [x] **R23 — Supabase driver. DONE 2026-07-13** (account opened + schema
      pasted by Aryan the same day). ``transport/supabase.py`` behind the
      SAME Transport contract: docs → ``ab_docs`` (atomic jsonb upserts),
      logs → ``ab_logs`` (**the row id IS the read offset** — the
      half-synced-line class can't exist), blobs → one private Storage
      bucket, sync hints → a realtime BROADCAST channel per root on a
      daemon thread (supabase realtime is async-only, the R1 note; socket
      death silently degrades to pure polling — poll stays truth, tenet 6).
      Schema = ``docs/supabase_schema.sql`` (one-time dashboard paste; two
      RPCs make list_logs/list_chat_ids single round-trips — PostgREST has
      no group-by). Trust model v1: SECRET key only, RLS enabled with NO
      policies (publishable key gets nothing); per-member Supabase auth +
      real RLS policies = a later round, recorded. E2EE identical — the
      smoke PROVED ciphertext at rest. Credentials in
      ``~/.agentbridge/supabase.env`` (never git; .gitignore blocks *.env).
      Wiring: mesh root ``supabase://<name>`` via a make_transport factory
      (Mesh/GUI/harness; Path() no longer mangles scheme specs; SQLite
      cache key is per (project, root); R18 deny-roots skip cloud roots).
      Verified LIVE on the real project (scripts/supabase_smoke.py, kept):
      raw contract + cross-client realtime hint + a two-identity E2EE mesh
      roundtrip through the cloud, SEALED at rest, self-cleaning. Connect
      spike lesson: broadcast needs self:true to echo to the sender. 310
      tests (9 hermetic driver tests on a fake client — CI never touches
      the project). DEFERRED with reasons: setup-wizard cloud-vs-folder
      choice copy (rides the setup/packaging overhaul round where the
      wizard is rebuilt); per-member RLS (needs the auth mapping round).
- [x] **R24 — Stress & soak. DONE 2026-07-13.** CI-sized deterministic
      stress tests (tests/test_stress.py, 7): 4-writer message storm
      converges bit-identically for every member (nothing lost, nothing
      duplicated, per-author ns strict); offline catch-up 5×60 in one sync
      (second sync no-op); crash-mid-send in BOTH windows (transport dies
      before the record lands → backoff retry, no loss; process dies AFTER
      the send but before the outbox ack → retry re-appends and the read
      model's dedup-by-id keeps readers at exactly one); cache rebuild from
      the transport is transcript-identical with overlays intact; TEN
      concurrent agent runners answer exactly once each, queues empty;
      queue lease recovery after a crashed claim. Heavy numbers via
      ``scripts/soak.py`` (kept; ``--supabase`` = light cloud soak).
      **Profiling found + fixed a real hot-path pathology**: messages_for
      spent ~95% of its time re-verifying Ed25519 sigs and re-reading the
      sender's account doc PER MESSAGE PER CALL → (1) an unseal-result LRU
      in E2EESealer keyed (chat,id,ns,epoch,nonce,sha1(ct|sig)) — the
      digest keeps "show nothing rather than lie" intact for records
      tampered at rest (the tamper test caught the naive key); successes
      only, so an unsynced key still retries; (2) a deterministic _abs
      path-resolution memo in FolderTransport. **messages_for @400: 198.6ms
      → 4.7ms (42×)**; @1000 depth: 26.3ms. Soak (10 agents/10 chats/1000
      msgs, E2EE): post 593 msg/s enqueue · flush 1504 msg/s · storm 4×250
      converge 2.2s · catch-up 10×100 0.1s · 10 agents reply 0.3s · full
      cache rebuild 0.6s. Cloud (real project): ~290ms/op RTT-bound —
      livable behind the async outbox; row-batching noted as a later
      optimization. Test-bug lesson recorded: a PLAIN writer next to E2EE
      readers is refused BY DESIGN (R16.5) — stress worlds must be
      uniformly encrypted. 317 tests; live harness + GUI restarted.
- [x] **R25 — Security review. DONE 2026-07-13 (v0.24.95, 323 tests).** Swept
      every mutating GUI/CLI endpoint (all route through the mesh's
      membership/owner/admin gates; membership ops re-check authority at fold
      time) + fanned two deep audits over the harness and crypto/transport.
      Confirmed-holding rails: broker ASK fails CLOSED + caches the denial,
      deny-roots resolve `..`/symlinks, auto_allow is read-only, capability
      tools are chat-bound + id-validated + per-run capped, peer requests are
      Ed25519-signed with a two-tier (access+repair) owner gate, epoch rotation
      wraps current members only, AAD binds chat|id|ns|sender|epoch, the fold
      re-verifies signatures + gid-pinned genesis, Supabase secret key never
      leaves memory. **Four holes CLOSED** (docs/THREAT_MODEL.md "CLOSED R25"):
      (1) redactions were applied by mere PRESENCE — now Ed25519-signed by the
      sender + verified at read (a folder writer could otherwise tombstone any
      message); (2) a removed member who kept the old epoch key could INJECT a
      fresh old-epoch envelope current members decrypt — the fold now records a
      membership TENURE timeline (`ChatSnapshot.tenure`) and the read model
      drops messages sent outside a sender's tenure; (3) message bodies could
      forge transcript lines into the agent's context.md — continuation lines
      now indented; (4) peer request replay — added a per-requester ns floor.
      Plus a latent bug fix (peer `ping` imported a nonexistent version).
      `Mesh.harden_startup` (idempotent, on GUI/harness sign-in) populates
      tenure on pre-R25 chats + re-signs any local legacy redaction. Residuals
      DOCUMENTED for their own round: the UNSIGNED directory root of trust
      (account-doc key overwrite → identity takeover; needs signed/pinned
      account docs + key history — the biggest open item, → **R27**) and the
      non-destructive reaction/pin overlays. Live-verified on a scratch mesh
      (boots clean, renders, tenure written); live @claude + GUI restarted.
- [x] **R26 — Docs & retirement. DONE 2026-07-13 (v0.24.96, 323 tests).**
      Retired the v1 app to `legacy/` (`git mv` mesh.py, agent_worker.py,
      mesh_cli.py, AgentWorker.pyw — bridge.py/handler_coco.py were already
      there; all confirmed unreferenced by v2/tests). Root now holds only the v2
      launchers (AgentBridge.pyw, AgentHarness.pyw) + check_frontend.py. Version
      source moved `gui/__init__.py` → **`agentbridge/__init__.py`** (updated
      app.py + peer.py refs, dropped it from gui/__init__.py, fixed
      CLAUDE.md/WORKING_AGREEMENT bump target + the stale 21→22 module count).
      **ARCHITECTURE.md fully rewritten for v2** (it was still a v1 doc — mesh.py
      monolith/connectors/agent_worker; now: package layers, mesh facade, E2EE +
      R25 security model, transports, the harness stack, 22-module frontend,
      invariants, sharp edges). **HANDOFF.md rewritten** as a tight v2
      orientation + packaging-prep notes (entry points, config discovery, deps,
      single-instance, PWA). Process-fleet diagnosis recorded: the "many
      processes" are one clean v2 fleet shown doubled by the uv-managed-venv
      launcher-stub+base pairing (ARCHITECTURE §11) PLUS stale v1 AgentWorker
      processes to stop. 323 tests, ruff clean, frontend 22/22.
- [x] **R27 — Directory root of trust. DONE 2026-07-13 (v0.24.98, 334 tests).**
      Closed the last big residual from R25: `users/<name>.json` publishes the
      keys every signature + epoch-wrap trusts, but is unsigned and
      transport-writable, so a folder/secret writer could overwrite a victim's
      `sign_pub`/`agree_pub` and take over the identity. **Chose TOFU key
      pinning** (the detection-first option) over signed docs / a mesh trust
      root: it needs no new key hierarchy, protects every established
      relationship immediately, and can't break provisioning. `mesh/pins.py`
      (`KeyPinStore`): one pin file per machine+root under `<home>/pins/`
      (NOT the rebuildable SQLite cache — trust state must survive a cache
      wipe), read-merge-write so the GUI + harness runners share it.
      `Directory.get` resolves `sign_pub`/`agree_pub` THROUGH the pin, so every
      consumer (fold `_authentic`, sealer authorship verify, redaction verify,
      keyring epoch wraps, peer verify) trusts the pinned keys automatically —
      a rewritten doc is inert for any machine that already knew the account.
      Provisioning pins explicitly at mint time (signup, first-login upgrade,
      agent adoption) so the creating machine trusts its own keys before any
      read races a concurrent write. A published-vs-pinned mismatch records a
      per-(name, key) alert surfaced at `/api/mesh/state` → a sidebar banner
      (dismiss = `POST /api/mesh/key_alert_ack`, clears the banner, never moves
      the pin). A future key-rotation flow can advance a pin via signed
      `keys.history` entries (each signed by the retiring key,
      `pins.rekey_signing_bytes`); nothing emits history yet, so today every
      mismatch alerts (safe default). Residual (narrow, documented in
      THREAT_MODEL): a machine that never saw an account pins first-read keys —
      out-of-band fingerprint compare is the eventual answer. 16 new tests
      (units + mesh-integration attack + GUI surface); **verified live** on a
      scratch rig (doc-rewrite attack → red banner renders → dismiss persists →
      pinned keys keep verifying the victim's real messages; zero console
      errors). frontend 22/22, ruff clean. THREAT_MODEL "CLOSED R27" written.
- [x] **R28 — Supabase-primary perf (metadata read cache). DONE + LIVE
      2026-07-13 (v0.24.99, 347 tests). Supabase is now PRIMARY.** Cutover
      (Aryan-approved): timed cloud state (**13.5 s → 2.7 s** uncached→cached,
      ~3 s/poll live) → pre-flight per-log folder-vs-cloud count check (all 6
      matched — the migrator's log skip is coarse, not per-record; check before
      any future cutover) → re-ran the idempotent migration (folder untouched) →
      verified E2EE decrypts through cloud (19/19) → repointed `config.json`
      `mesh_root` → `supabase://mesh2` (folder path kept in
      `mesh_root_folder_backup` = rollback) → restarted the fleet (also put R27
      live), verified GUI :7787 v0.24.99 cloud-backed + fleet stable + app
      renders with zero console errors. Unblocks the R14-era Supabase switch
      that was rolled back because `/api/mesh/state` took ~30 s on cloud (117 ms
      on folder): the hot GUI endpoints read metadata STRAIGHT from the
      transport and re-read the same docs many times per request
      (`PrivacyService.visible_profile` fetches an account doc ~8× per user;
      `presence_of` re-scans every presence doc per user; `chats_for` re-reads
      every meta) — O(users×fields[×chats]) network round-trips. **Chose the
      short-TTL transport read cache** (over threading cached snapshots through
      privacy/presence): one place, covers every hot path, no service rewrites.
      `transport/cache.py` `CachingTransport` wraps `get_doc`/`list_docs`/
      `list_chat_ids` (NOT logs/blobs — message latency must not lag) with a
      ~2 s TTL; writes through it write-through + invalidate so a writer always
      sees its own writes; `make_transport` wraps ONLY cloud roots (a folder
      read is already free, and the well-tested folder path stays untouched).
      Correctness rides the mesh's existing eventual consistency (meta.json is a
      rebuildable last-writer-wins snapshot; cross-process staleness ≤ TTL, far
      under cloud sync latency). 13 new tests incl. a representative state-sweep
      collapse (58 transport reads → 14 for 4 users/3 chats; scales with
      DISTINCT docs, not users×fields). frontend 22/22, ruff clean. **REMAINING
      (all live, Aryan-gated):** time `/api/mesh/state` on the real Supabase
      project; re-run `scripts/migrate_folder_to_supabase.py` (idempotent copy,
      folder untouched); repoint `config.json` `mesh_root` → `supabase://mesh2`
      (keep `mesh_root_folder_backup`); restart the fleet (this also puts R27
      live).

- [x] **R29 — cloud mirror (stability + smoothness on Supabase). DONE + LIVE
      2026-07-13 (v0.24.100, 352 tests).** Post-cutover the app was slow AND
      unstable: (1) the R28 TTL (2 s) was always cold by the next state fetch —
      the frontend refetches `/api/mesh/state` on every SSE event/route
      change/poll — so every sidebar repaint re-paid ~14 sequential cloud RTTs
      (**measured 2.8–4.1 s live**); (2) `SupabaseTransport.get_doc` was the
      ONE read without `_retry`, so a transient cloud fault read as "doc
      missing" and the cache CACHED that miss — chats/profiles/presence
      flickered out of the GUI; (3) each ~3 s state build piled up server
      threads + concurrent cloud calls (and a second stray GUI process was
      found sharing :7787 — killed at the restart). Fix, transport layer only
      (mesh + folder path untouched): `CachingTransport` reworked from
      TTL-read-through into a **warm mirror** — one paged bulk query
      (`SupabaseTransport.get_docs`, new) loads every doc; a background daemon
      re-pulls every ~4 s, woken early by realtime hints; hot reads are
      RAM-only; a failed refresh keeps serving the last good snapshot; writes
      write-through + update the mirror synchronously with a recent-write
      guard against racing refreshes; deep-copied returns (no aliasing);
      `get_doc` retried like every other driver op. GUI shares ONE mirrored
      transport (pre-auth + Mesh) and kicks `warm_async()` at boot; first-boot
      sidebar shows a loading skeleton + indeterminate bar while the first
      state fetch warms (the "loading slider"). **Measured live:
      `/api/mesh/state` 2.8–4.1 s → 11–13 ms; `/api/mesh/chat` 3 ms; post
      264 ms (write RTT, unchanged by design).** Verified live end-to-end on
      the real project: scratch room create → E2EE post → read → delete (all
      reflected instantly through the mirror), SSE stream up, zero console
      errors, fleet stable (8 logical procs). 15 mirror tests replace the 13
      TTL tests (failure-serves-stale, racing-write guard, offline cold-start
      fallback + recovery, zero-read state sweep once warm). frontend 22/22,
      ruff clean. Future levers if doc count grows large: delta refresh on
      `ab_docs.updated` + periodic full pull; persist the mirror.

- [x] **R30 — Supabase connector performance pass + agent response profiling.
      DONE + LIVE 2026-07-14 (v0.24.102, 363 tests).** Three asks from Aryan:
      profile agent response time, keep connectors easy to add, full connector
      perf pass. (1) **Change-feed sync** — `ab_logs` row ids are one global
      identity column, so `SupabaseTransport.changed_logs(cursor)` answers
      "what changed since?" in ONE indexed query; `SyncEngine` uses it when
      `tx.has_change_feed` (cursor persisted in the store; a newly-joined
      chat gets one full scan since its history may sit below the cursor; a
      failed feed query keeps the cursor and retries; the run loop now
      survives a failing pass — previously a cloud blip after retries KILLED
      the sync thread until relaunch). Idle tick: 1 query per process instead
      of `list_logs` × chats — O(1) in chat count. (2) **Post latency** —
      the composer's 264 ms was the SYNCHRONOUS `mark_read` overlay write
      (one cloud RTT) after the (already outbox-backed) post; it now runs off
      the response path. (3) **Connector contract formalized** (base.py):
      required abstract surface + two OPTIONAL fast paths with working
      defaults — `get_docs` bulk read (mirror warm source) and
      `changed_logs`/`has_change_feed` (sync fast path); a future driver
      (gdrive://…) works correctly with neither. `CachingTransport` delegates
      both explicitly (base-class attrs would shadow `__getattr__`). (4)
      **Response profiling** (`harness/perf.py`): pickup/context/model/post
      stage timings per run → `<home>/harness/perf/<agent>.jsonl` +
      run-feed summary + ⏱ line in the Message-info task doc (zero new UI;
      adapter-agnostic per the one-harness rule). (5)
      `scripts/profile_supabase.py` — rerunnable per-op profile against a
      throwaway root (live p50: doc/log ops ~62-84 ms, bulk 40-doc get_docs
      68 ms, mirror warm ~200 ms, mirror read 0 ms). Merged over the parallel
      session's v0.24.101 Connection-panel round (one import conflict).

- [x] **R31 — threat-model closeout + QA-list app fixes. DONE 2026-07-14
      (v0.24.103, 373 tests).** Aryan: "close out the remaining surfaces in
      the threat model once and for all" + the improvement list from his
      @claude chat. Threat model (docs/THREAT_MODEL.md "CLOSED R31"): (1)
      **reaction + pin overlays signed** — the per-user reaction file signs
      its full mapping (`reaction_signing_bytes`), the pin doc binds
      `chat|pin|msg-id|by|ns|until_ns`; reads verify signature + (tenure-)
      membership against the PINNED key, `harden_startup` re-signs locally
      keyed legacy overlays; deletion stays possible (absence has no
      signature — documented under Availability). (2) **Key fingerprints**
      (the R27 first-contact answer): sha256 over `name|sign_pub|agree_pub`
      of the pinned pair as 8×4 hex groups; DM info gets an Encryption card
      (+ **Mark as verified** → the pin store), Settings → Security shows
      your own, the key-change banner shows trusted vs published
      (`/api/mesh/key_verify`; verified live — both scratch machines derived
      identical codes, forged reaction/pin docs planted on the transport
      rendered nothing). QA list: (3) memory **forget** tool (by recall id,
      or single confident query match; global scope policy-gated like
      remember); (4) **standalone agent replies** — `reply_to.quote=false`
      when answering the newest message (attribution kept: the
      answered-guard's transcript leg reads reply_to.id; the first attempt
      that DROPPED reply_to broke exactly that guard — caught by its test);
      (5) **sidebar fixes** — composer send now repaints the sidebar (a
      local post fires no SSE), chats sort pinned-then-recency, pure
      reorders MOVE row nodes (no flush); (6) **pin-banner scroll jump**
      fixed (banner synced before the scroll measurement it used to
      invalidate); (7) burst batching + no-self-permission-escalation
      documented as by-design. BONUS regression fix caught by the suite:
      R30's background `mark_read` raced the user's own star/flag writes —
      per-user state files now mutate under a per-(chat,user) lock.

- [x] **R31.5 — state-doc authentication + keystore wrap. DONE 2026-07-14
      (v0.24.104, 381 tests).** The delta a parallel session found while
      running the same threat-model sweep (its overlapping reaction/pin/
      fingerprint work was dropped in favor of R31's live implementation;
      this round carries only what R31 didn't cover — see docs/THREAT_MODEL
      "CLOSED R31.5"): (1) **per-user STATE docs signed** — previously
      undocumented and sharper than the reaction/pin class: a store writer
      could drop `hidden`/`cleared` into a victim's doc to blank history
      from their OWN view, forge `read_ns` to fabricate a read receipt, or
      set `mute` to silence their pings. Signed by the owner over
      `chat|state|user|ns|fields` (`events.state_signing_bytes`); every
      reader goes through the verified accessor `messaging.state_of` (own
      view, receipts' cursors, the notifier's mute check) and treats
      anything else as absent; `_merge` starts from the VERIFIED read so a
      forgery is never laundered into a genuine write; `harden_startup`
      gains `_reseal_state`. (2) **Keystore DPAPI wrap** (`crypto/dpapi.py`,
      ctypes, per-OS-user, Windows): `keys/<name>.key` is unreadable off
      this machine/user; legacy plain files upgrade in place on first load;
      plain fallback so a wrap failure never costs a key; the write is
      atomic (load-triggered upgrades race concurrent readers). (3)
      `Mesh._sign_event` caches the unlocked bundle — signing now sits on
      hot paths (mark_read/react) and re-reading the key file per call
      (now + a DPAPI unwrap) was waste. 8 new tests: state/cursor/mute
      forgeries, harden state re-sign, a star-vs-mark_read race hammer, and
      the keystore wrap/upgrade/garbage trio.

- [x] **R32 — E2EE notice pill + verification nudge (the e2ee closeout's
      last mile). DONE 2026-07-14 (v0.24.105).** Aryan's two calls after the
      residual review: (1) the **encryption pill** — every encrypted chat
      opens with a WhatsApp-style "Messages are end-to-end encrypted" pill
      at the top of the transcript. Deliberately SYNTHETIC (client-rendered
      from `encrypted:true`, never a log event): retroactive in every
      existing chat, no migration, no presentational data in the
      authenticated log (WhatsApp's own banner is client-rendered too). In
      a DM whose peer is unverified it appends "Tap to verify @name's keys"
      (calm accent, not a warning); clicking any pill routes to
      `#/chats/<id>/details`, where R31's Encryption card carries the
      fingerprint + Mark as verified — the nudge disappears exactly when
      it's satisfied. chat.js parts[] + the delegated transcript click +
      `.enc-pill` css; no backend change. (2) **Signed unpin tombstones
      considered and SKIPPED** (decision recorded in THREAT_MODEL): a
      delete-capable adversary deletes a tombstone too — signatures can't
      authenticate absence; the real deletion-close is per-member Supabase
      RLS (queued cloud follow-up), zero format change needed then. Also
      settled at review: social-graph/membership encryption is a deliberate
      v3-scope non-goal (Signal-zkgroup-class project); signing ephemeral
      presence/typing not worth it.

- [x] **R33 — delivered-vs-read receipts + Message-info timings. DONE
      2026-07-14 (v0.24.106, 385 tests).** First round of the reopened
      backlog (see `BACKLOG.md`, created this session after Aryan flagged that
      most of the live-QA asks were already in the original brief and had been
      dropped by a too-coarse checklist). (1) **Delivered is now a real
      per-recipient receipt** — a `delivered_ns`/`delivered_ts` cursor in the
      per-user state doc (signed, rides R31.5), advanced by the sync pump
      (`service._pump` → `messaging.mark_delivered`) the moment a client OR
      harness FETCHES a message: "worker receives message = Delivered", for
      humans and agents alike. Presence stays the floor (an online member with
      no cursor yet still shows Delivered), so nothing regresses; `_member_tier`
      = read_ns≥ns → Read, delivered_ns≥ns OR last_seen≥ns → Delivered. (2)
      **Message info shows the timings** (Q17): the dialog rendered only "Sent"
      because the client gated on a `mine` field the backend never emitted;
      `message_info` now returns `mine`/`kind` + per-member Delivered/Read
      timestamps, and the dialog renders them (DM = two rows, group =
      read-by/delivered-to/pending). (3) **Bubble ticks are three-state**: grey
      single (sent) / grey double (delivered) / accent double (read); the
      transcript refresh signature folds in the receipt tier so ticks advance
      live. Verified live on the two-machine scratch rig: full Sent→Delivered
      (on fetch, no heartbeat)→Read ladder, dialog showed "Read … / Delivered
      …". 4 receipt tests updated to the fetch-delivered semantics + 2 new
      (delivered-without-presence, message-info timings).

- [x] **R32.1 — pill polish (v0.24.107).** Two fixes on the shipped R32 pill
      (rebased on top of R33): (1) it was a button in EVERY encrypted chat, so
      clicking it opened the info pane even for groups / self-chats /
      already-verified DMs that have nothing to verify. Now it's a STATIC
      `.enc-notice` (inert — no pointer, no click) everywhere except an
      unverified DM peer, where it stays the clickable `.enc-pill` nudge.
      (2) The nudge no longer routes to the info pane (the Encryption card sits
      below the fold, needing a scroll); it opens a **focused verification
      dialog** (`V.openKeyVerify`, a modal via openModal) showing the
      fingerprint + Mark as verified directly. One shared `markKeyVerified(name)`
      mutation now backs BOTH the modal and the info-pane card (no duplicated
      verify/patch logic); on success the transcript rebuilds so the pill drops
      to static immediately. Frontend-only (chat.js/details.js/views.js/
      style.css). Live-verified on a scratch rig with all three chat kinds
      (unverified DM = clickable modal, verified DM + group = inert static
      notice, static click is a no-op).

- [x] **R34 — agent message ops (self edit/delete) + unpin ids. DONE
      2026-07-14 (v0.24.108, 385 tests).** BACKLOG Q33 + the agent-side of
      Q18/Q15 (H11 capability parity). (1) **Pins carry their id into the
      agent's context** — `context_pinned` was `[PINNED by @x] body` with the
      id dropped, so a pin older than the transcript tail was un-unpinnable;
      now `[PINNED by @x] (id m-…) body` (Q33). (2) **edit_message /
      delete_message bridge tools** — the agent edits or deletes-for-everyone
      its OWN live messages, author-only exactly like a human (the mesh gate
      already enforces author==sender; a `mine()` check gives a clean refusal
      on anyone else's message, no backend error leak). Advertised in the
      prompt pack + activity labels. Verified: a real-HTTP bridge test drives
      both tools against an encrypted mesh (own message edits/deletes, the
      owner's message refused on both) and the owner then sees the edit +
      tombstone; the GUI render path (edited flag / deleted tombstone) is the
      same author-agnostic readmodel path already used for humans, spot-checked
      live. **Split off to its own round** (owner acts on the agent's message +
      owner-only undo): that relaxes the author-only gate to let an owner act
      AS its co-hosted agent's identity — an authorization + crypto-authorship
      change that earns a dedicated security pass.

- [x] **R35 — status surfacing (read_status tool + GUI header/details/agent
      editor). DONE 2026-07-14 (v0.24.109, 386 tests).** BACKLOG Q32 / M7
      close. (1) **read_status bridge tool** — an agent checks a member's
      availability + presence on demand before messaging, returning only what
      that member shares with it (reuses `privacy.visible_profile` +
      `presence.visible_presence`, matrix-gated exactly like the per-run
      delivery enrichment). (2) **GUI status/presence** — the DM chat-info
      identity block shows the peer's status (state + text, below @username)
      and online/last-seen below it, each only when shared (no empty field);
      the DM header shows online/last-seen with a `.has-sub` push-up. (3)
      **Owner sets the agent's status** — an Availability row in Settings →
      My agents (`set_status` with `agent=`, already owner-gated server-side).
      Agent default about verified `"<Owner>'s <Agent> on <machine>"`.
      Live-verified all paths on the scratch rig (peer status busy/dnd +
      last-seen rendered; owner set scratbot to dnd). Frontend-only touch to
      chat.js/details.js/settings.js/style.css + one bridge tool; no overlap
      with the parallel session.

- [x] **R36 — run UX (stop / one-line progress / run history / labels) +
      agent privacy in the agents page. DONE 2026-07-14 (v0.24.110, 387
      tests).** BACKLOG Q9/Q10/Q11/Q12 + verbal V1–V4 + M6's last GUI gap.
      (1) **Owner stop** — `POST /api/mesh/agent_stop` (owner-gated) drops a
      stop doc; the CLI adapter polls it beside the timeout watchdog and
      kills the subprocess (`RunStopped`); the runner records a DELIBERATE
      stop: no error notice, rate slot refunded, triggers marked handled,
      feed state "stopped". Buttons: top-right of the working bubble (this
      chat) + Settings → My agents "Stop current run" (any chat).
      Integration-tested with a 30s-sleep stub killed in ~2s. (2) **One-line
      progress** — the bubble shows dots + the CURRENT activity on one line
      ("…working" gone); right-click lists the timestamped tasks so far
      (feed doc now publishes `steps`). (3) **Run history** — finished runs
      append to `status/<agent>_runs.json` (cap 20) → "Recent runs" in the
      agent card (the missing "tasks completed by agent" list). (4)
      **Labels** — unmapped tools humanize ("Using search issues (github)"),
      context.md reads as "Reading the conversation". (5) **Agent privacy
      matrix** in the agent card (owner-set via `set_privacy agent=`;
      read-receipts toggle) + surfaced the backend-only "agents" audience
      tier in ALL privacy pickers; photo keeps everyone/nobody. (6)
      **Presence polish** — the DM header's online/last-seen patches in
      place on every poll (it froze at chat-open before); details-pane
      status+last-seen share one comma-separated line; lowercase
      "today"/"yesterday" via fmtTimeLower. All live-verified on the rig.

- [x] **R37 — composer + transcript bug bash. DONE 2026-07-14 (v0.24.111,
      388 tests).** BACKLOG Q16/Q19/Q24/Q25/Q27/Q29/Q31 + V7.
      (1) **Files fixed end-to-end (Q27)** — the frontend spoke v1
      (`a.path` attachments, `?id=<chat>&path=` serving, `data-path`
      clicks) against the v2 backend (upload token, `?chat=&id=`,
      `{chat_id, id}`): a human's attachment was silently DROPPED at post
      (no chip at all) while an agent's chip rendered but never opened —
      the two halves of the user report. Unified on v2 across composer/
      files/api/chat/details/media incl. bulk save. (2) **Reactions render
      (Q24)** — in-bubble chips (count, mine highlighted, reactor tooltip),
      quick-react bar atop the message menu, click-to-toggle; a `mutSig`
      folds edits/redactions/reactions into the content key so in-place
      mutations repaint on the partial path (they froze before — latent for
      R34 edits too). (3) **Delete chat = real delete-for-me (Q25)** — the
      `deleted` flag stores the deletion ns; read model hides ≤ it,
      sidebar filters `c.hidden`, a new message resurrects the chat with
      only post-delete history, undo restores everything
      (`delete_chat_for_me`, membership-gated, tested). (4) **Edit in the
      composer (Q31)** — edit bar + check-to-save + Escape cancel + draft
      restore; the edit window retired. (5) **Send disabled when empty
      (Q16)**; (6) **clamp cuts on the straddling child's own line grid**
      (code blocks/lists no longer sliced mid-line) + symmetric transcript
      padding (Q29); (7) **clear-chat consistency live-confirmed (Q19)**;
      (8) **V7**: mountCsels made idempotent — the R36 agents-page sweep
      stacked a reply-rule dropdown under every privacy row. All
      live-verified on the rig (real composer file-input drive, second
      identity for reactions/reappear).

- [x] **R38 — agent profile + permissions. DONE 2026-07-14 (v0.24.112, 389
      tests).** BACKLOG V5/V6/V8/V9. (1) **D19 carve-out** — status + about
      are now the agent's OWN to keep current (`_writable_target
      allow_self=True` on exactly those two setters; privacy/blocks/handle/
      display/avatar still refuse the agent identity — test pins both
      halves). Owner and agent write the same account field; most recent
      wins. (2) **Bridge tools** — `set_status(state, working_on)`,
      `set_about`, and `read_permissions` (no arg: its own owner-set privacy
      matrix + outbound rules; a username: that member's PUBLIC gates only).
      Prompt pack advertises them + nudges the agent to keep its status
      current; activity labels added. (3) **GUI** — agent card gains About
      (input + save) and a "Reach" section (May message / May add to groups
      → `set_agent_rules`, which had no GUI); the DM details identity block
      now shows the peer's About (previously rendered nowhere) and the
      public gates line ("Accepts messages from … · group adds from …",
      §M6's by-design-public settings). Gate pickers relabeled "Agents only"
      (strict — the R36 label wrongly promised the owner ride-along tier).
      (4) WORKING_AGREEMENT gains the code-organization standing convention
      (owning-module rule + same-commit protocol-spelling sweeps). Verified
      live: scratbot's real identity self-set status/about (privacy refused),
      owner overwrite won, rules persisted, identity block shows all three
      lines.

- [x] **R39–R41 — settings + model config. DONE 2026-07-14 (v0.24.113–115,
      393 tests).** BACKLOG Q13/Q14/Q20/Q21/Q23/Q30 + M11/H6/H9 closes +
      H8's picker half, shipped as three verified commits.
      **R39 (.113):** the reasoning-effort picker was dead because no live
      preset declared efforts — claude.json now carries `--effort`
      low…max (from `claude --help`); presets gain per-model
      `model_efforts` narrowing (registry + GUI are model-aware). MCP-only
      agents: adapter "none" → no runner spawned, clean stand-down, resolve
      refuses. The chat's agents pane gains the FULL per-audience card
      (H9); precedence confirmed per the brief (chat → current → audience →
      preset). **R40 (.114):** Privacy is its own Settings section with the
      matrix + a Blocked list (block had NO GUI — DM details gains
      Block/Unblock); Delete account (password-confirmed) + Delete agent
      buttons; the M11 departed display implemented (grey messages, DM
      "This account was deleted" info text, details "Account deleted").
      **R41 (.115):** per-chat "Context here" (1–90-day ceiling applied to
      the transcript tail AND vector recall) + "Global memory" override
      (resolves before the bridge's memory gate), account-wide global-memory
      picker, and the "Standing approvals" list — every Always-allow grant
      visible + revocable (tool+chat matched). All live-verified on the rig.

- [x] **R42 — notifications (GUI + CLI). DONE 2026-07-14 (v0.24.116, 398
      tests).** BACKLOG Q26 + the M3 remainder. **Server:** the Notifier
      gains the read-state rule (`read_ns` ≥ msg → catch-up, not news — one
      verified state read now covers mute + read); the SSE frame gains a
      `notify` lane (chat name, sender, 120-char preview) decided by
      `notifier.consider()` — the ONLY body bytes that ever ride the
      stream, on the owner's authed session. Genesis fix: founding members
      of someone else's GROUP now get ADDED_TO_CHAT (the roster is baked
      into `created`; member_added never fired for them; DMs stay quiet).
      **GUI:** new `notify.js` (23rd module) — desktop toasts with per-chat
      `tag` coalescing ("Chat (3 new)"), click-to-jump, open+focused
      suppression, prefs in localStorage; Settings → Notifications (enable
      = the permission-request gesture; show-preview; browser-block
      surfaced); the "(n) AgentBridge" title badge (muted chats excluded);
      REAL mute controls replacing the R10 stub toasts — 8h/1week/Always
      modal, Unmute flip in the header + row menus, slashed-bell indicator,
      grey badge. **CLI:** `watch [--json] [-- CMD…]` — foreground notifier
      watcher (own sync cadence, heartbeat off), one line per ping + the
      R10 CommandHook (AB_* env vars); agents bare, humans password-gated;
      running the process IS the registration (nothing persists auto-run
      commands). Live-verified on the rig: message/added toasts + coalesce
      + preview-off + mute/unmute round trip + title badge, and the watcher
      catching exactly the one unread historical message before streaming
      the new group's two events through stdout AND the hook file.

- [x] **R43 — docs tool + ask cards. DONE 2026-07-14 (v0.24.117, 401
      tests).** BACKLOG Q7 + Q28 + Q11's remainder; closes H2. **Data:**
      `prompts/tooldocs.json` — per tool an `ask` verb phrase, a `short`
      one-liner and a `long` manual entry, plus conceptual `guides`
      (workspace/files/chat/profile/memory/timers/permissions);
      owner-overridable at `<home>/prompts/tooldocs.json` (docs.py, same
      chain as the prompt pack). **Q7:** the bridge gains `read_docs` —
      catalog or full entry on demand; the inline `bridge` prompt para
      shrank to behaviour rules + tool roster + the read_docs pointer.
      **Q28:** ask-cards went Claude-Code-style — permission heads read
      "wants to write a file" (broker stamps the phrase into the ask doc;
      raw id on hover; unmapped tools humanize), `ask_member` offers ≤4
      one-tap OPTION pills with an "Other…" free-text escape, and Deny is
      two-stage with an optional tell-it-what-to-do-instead note that
      reaches the agent as the deny reason. **H2 close:** per-agent "Safe
      permissions" toggles — "Reads don't ask" (auto_allow on/off) and
      "Web access" (`aux_web` tools leave the blocklist INTO the ask gate;
      applies ONLY while the gate is live, only for families declaring
      both aux_web and permission_args; shell/subtask tools have no
      toggle). Plus the R42 polish: mute-dialog options now highlight on
      hover. Live-verified on the rig (planted asks: phrase head, deny-note
      round trip, option tap; switches persist + restore); read_docs and
      options tested over real MCP HTTP.

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
