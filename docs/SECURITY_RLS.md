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

### Identity: account creation IS membership (v2.2)

**No secret is ever transferred, and nobody approves anything.** A new
member's Supabase auth user is born on their own machine — self-signup
with the **publishable** key (public by design); the generated password
goes straight into their local `supabase.env` and never exists anywhere
else. The same act **self-claims** their username in `ab_members` — one
row, `uid = auth.uid()`, first come first served (the primary key
arbitrates, mirroring the app directory's own rule). Creating an account
on the app *is* joining the mesh, at both layers:

```
new machine:      supabase_admin join <username>     (URL + publishable key + root)
owner (rarely):   supabase_admin seed <username>     (mint for a machine that can't join)
                  supabase_admin revoke <username>   (eviction; service key)
```

`join` is also the primitive the app's account-creation flow calls once
the setup overhaul lands — signup provisions the Supabase identity in the
same breath, invisibly.

**What this trust model means, stated plainly.** The mesh is exactly as
private as its bootstrap config (URL + publishable key + root name):
possession of the bootstrap is the invite, like a group link. A bootstrap
holder who joins sees the mesh-wide lanes (directory, presence, status)
— the same thing any app account sees today. What RLS buys is the wall
*between members*: chat rows, logs, and blobs are invisible to anyone not
in that chat's meta — and the retirement of the god-mode service key from
member machines (no more read-everything/delete-everything credential in
every env file). If the bootstrap leaks beyond the intended circle,
rotate the publishable key in the dashboard and revoke stray auth users;
E2EE keeps every message body sealed regardless.

Two designs were rejected on the way here. Owner-minted credentials with
`app_metadata` claims: every signup needs the service key warm and the
owner online — the exact bottleneck Aryan flagged ("I cannot be minting
user keys for everyone"). Member-vouched admission queues: no secret
transfer, but an approval prompt per joiner — rejected as needless
ceremony; the app's own account creation has no admission step, and the
DB should mirror the product. (`user_metadata` was never an option — the
user edits it themself; and hand-minted JWTs die against this project's
asymmetric signing keys.)

One-time dashboard prerequisites for `join`: email signup enabled, email
confirmations OFF — the addresses are synthetic
(`<name>@<root>.agentbridge.local`), and an auth user that never claims a
row can see and touch nothing.

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
| other roots on the project | nobody without a members row for that root |

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
- **Username squatting on a leaked bootstrap**: whoever holds the
  bootstrap can claim any UNCLAIMED username (the app's directory has the
  same property). Claimed names are immutable to others (PK + uid bind);
  eviction and cleanup are the owner's service-key acts.

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

> All commands run from the repo root **with the project's own venv
> Python** — `.\.venv\Scripts\python.exe`, never bare `python` (that
> resolves to the hermes venv, which lacks the `supabase` package; from a
> subdirectory it also can't import `agentbridge`).

1. **Dashboard, one-time**: Authentication → Sign In/Up → email signup ON,
   email confirmations OFF (synthetic addresses; a signup grants nothing
   until it claims a members row, and RLS decides what a claim can do).
2. **Paste** `docs/supabase_schema.sql` (idempotent, whole file is fine).
   The fleet keeps running on the service key, which bypasses RLS —
   pasting only *arms* the gate for `authenticated` users.
3. **Join from each machine** (from the repo root):
   `.\.venv\Scripts\python.exe -m agentbridge.transport.supabase_admin join aryan`
   here, `... join aryanonavd` on the AVD. Each machine mints and installs
   its own credential locally — nothing to transfer.
4. **Restart each machine's app** (About → Updates → Restart app) and
   check the Connection panel says `Member (…)`.
5. **Verify**: `scripts/rls_probe.py` with a scratch `join`ed identity —
   global lanes readable, a chat it isn't in invisible, a foreign root
   invisible. Then `revoke` the scratch identity.
6. **Remove `SUPABASE_SECRET_KEY`** from both machines' `supabase.env`.
   Keep it ONLY wherever the owner administers from (it is now the
   offline root credential, like a CA key). Restart once more.

## 5. Verification record

- Pre-paste (2026-07-16): probe auth user provisioned via the admin API
  (synthetic address accepted there — no mail is sent on the admin path);
  publishable-key sign-in works; **sees zero rows / zero blobs** (RLS
  deny-by-default confirmed live) while the service-key fleet is
  unaffected.
- Self-signup, pre-toggle (2026-07-16): `sign_up` with synthetic domains
  is rejected as "invalid" and then rate-limited ("email rate limit
  exceeded") — both symptoms of **confirmations being ON**: every signup
  tries to SEND mail through the built-in mailer (2/hour free tier),
  which also validates deliverability. Runbook step 1 (confirmations OFF)
  removes the mail attempt entirely; retest `join` right after the
  toggle. **Contingency** if GoTrue still rejects synthetic domains with
  mail off: a 20-line Edge Function holding the service key that performs
  the admin-create — same self-serve semantics (anyone with the bootstrap
  can join), one more dashboard paste. Anonymous sign-ins were considered
  and rejected: no password means the rotating refresh token IS the
  credential, and our multi-process fleet (GUI + harness + workers each
  sign in independently) would race the rotation into family revocation.
- Post-paste: *(pending Aryan's paste + toggle — run `join` +
  `rls_probe.py` and record the matrix here.)*
