# Per-member Supabase RLS (R84) — trust model v2

The design record and the runbook. Read this before touching the Supabase
policies or the transport's auth path. Companion: `docs/SCALING.md` (the
connector economics contract), `docs/supabase_schema.sql` §R84 (the SQL),
`THREAT_MODEL.md`.

## 1. Why (the problem with v1)

Trust model v1 (R23): RLS enabled with **zero policies**, and every member's
machine holds the project's **service key** — which *bypasses RLS entirely*.
That was honest for a one-owner mesh, but:

- **One shared secret, held by every machine.** Any member (or anyone who
  reads any member's `supabase.env`) can read and rewrite *every* row of
  *every* mesh on the project: all chat logs (sealed), every presence/status
  doc (plaintext by design), every member's directory entry — and can
  delete all of it. Removal from a chat, or from the mesh, revokes nothing.
- **The repo is public (R74).** The code documents exactly where the key
  lives and what it can do; the key is the entire wall.
- E2EE seals message bodies and files, so *content* was never exposed —
  but metadata (who talks to whom, when, chat names in paths, presence)
  and *availability* (deletes) were all-or-nothing.

## 2. The design

### Identity: Supabase Auth users, admin-provisioned

Each mesh member gets one Supabase **auth user**, created by the mesh owner
with the service key (`python -m agentbridge.transport.supabase_admin
provision <username>`). The RLS claims live in **`app_metadata`** —
admin-set only. (`user_metadata` is editable by the user themself through
the auth API; gating on it would let anyone rename themselves into any
mesh. This is the single most important line in the design.)

```
app_metadata: { "ab_member": "aryan", "ab_roots": ["mesh2"] }
```

Custom hand-minted JWTs were rejected: this project uses the modern
asymmetric signing keys (`SUPABASE_JWKS_URL` is in the env), the auth
server rotates and revokes properly, and `supabase-py` already wires the
session token into PostgREST/Storage/Realtime and refreshes it.

### The ACL: the chat's own meta doc

`chats/<id>/meta.json` → `data.members` (an object keyed by username) is
the membership record RLS trusts, via one `SECURITY DEFINER` helper
(`ab_is_member`) so the lookup doesn't recurse through the very policy
evaluating it. Why meta and not a separate ACL table:

- it already exists and is **already maintained at every membership
  change** — no new client plumbing, no second source of truth to drift;
- genesis is ordered **meta-first** (R25: "so the member gate holds from
  here on") — the first log record never races its own gate;
- chat ids **commit to their genesis hash** (R13.5), so squatting a
  foreign id's meta path is not practical;
- the doc is a *rebuildable cache* of the signed event fold — fine for a
  coarse access gate. The authenticated fold + tenure + E2EE epochs remain
  the source of truth for what a member can actually **read**; RLS is the
  outer fence, not the arbiter.

Tombstoned metas still grant access on purpose: during the deletion grace
the members' janitors must reach the subtree to purge it.

### The lanes

| rows | who |
|---|---|
| `chats/<id>/**` (docs, logs, blobs) | members of `<id>` per its meta |
| genesis insert of `chats/<id>/meta.json` | any root member who lists themself in it |
| everything else under the root (`users/`, `status/`, `presence/`, `control/`, `machines/`, avatars) | any member of that root |
| other roots on the project | nobody without the root in `ab_roots` |

Global lanes stay mesh-wide because they are mesh-wide *in the product*
(directory, presence, run feeds, control lane); the app's own rules and
E2EE arbitrate within them. Tightening `status/asks/*` to owners or
`users/<x>` writes to x-only is a later pass — it needs an
account→member→machine ownership map the DB doesn't have yet.

### What deliberately stays open (phase 2 candidates)

- **Realtime pokes**: the broadcast channel carries `{"r": 1}` — nothing
  else (SCALING.md §3). Anyone with the publishable key could subscribe to
  poke *timing* or send fake pokes (bounded cost: hints are garnish, polls
  are floored). Private channels + `realtime.messages` policies later.
- **Write ownership inside a chat**: any member can upsert any doc under a
  chat they're in (same as v1; the signed log + fold reject forgeries at
  read time). Per-doc author checks would need doc-level claims.
- **`ab_member()` impersonation via provisioning**: whoever holds the
  service key mints members — the service key remains the root of trust,
  it just stops being *distributed*.

## 3. The transport (already shipped, inert until credentials exist)

`SupabaseTransport._sb()` prefers a **member credential**
(`SUPABASE_MEMBER_EMAIL`/`_PASSWORD` + `SUPABASE_PUBLISHABLE_KEY`) and
signs in as that member; the service key is the fallback — a mixed fleet
keeps working through the whole migration, and a failed member sign-in
falls back loudly instead of bricking the fleet. JWT expiry mid-run heals
in the retry path (`_refresh_auth`). The poke channel always prefers the
publishable key (it's public + content-free). The Connection panel shows
the honest mode: `Access · ✓ Member (aryan)` vs `Service key — shared,
bypasses row security`.

## 4. The runbook (in this order — nothing breaks at any step)

1. **Paste** `docs/supabase_schema.sql` §R84 (idempotent, whole file is
   fine). The fleet keeps running on the service key, which bypasses RLS —
   pasting only *arms* the gate for `authenticated` users, of which there
   are none yet.
2. **Verify with a probe**: `provision rlsprobe --out <file>`, sign in as
   it from a scratch script, and check the matrix: global lanes readable,
   a chat it's not in unreadable, a chat whose meta lists it readable.
   (Pre-paste, the same probe proves deny-by-default: it sees nothing.)
3. **Provision the real members**: `provision aryan --install` on this
   machine; `provision aryanonavd --out avd.env` and move the two lines to
   the AVD's `supabase.env` (never through chat — the mesh rides the very
   transport being rekeyed).
4. **Restart each machine's app** (About → Updates → Restart app) and
   check the Connection panel says `Member (…)`.
5. **Remove `SUPABASE_SECRET_KEY`** from both machines' `supabase.env`.
   Keep it ONLY wherever the owner provisions from (it is now the offline
   root credential, like a CA key). Restart once more.
6. Revocation drill (optional but recommended):
   `revoke rlsprobe` and confirm its session dies.

## 5. Verification record

- Pre-paste (2026-07-16): probe member provisioned; publishable-key
  sign-in works; **sees zero rows / zero blobs** (RLS deny-by-default
  confirmed live) while the service-key fleet is unaffected.
- Post-paste: *(pending Aryan's paste — run
  `scripts/rls_probe.py` and record the matrix here.)*
