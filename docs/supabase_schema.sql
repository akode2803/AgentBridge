-- AgentBridge Supabase schema (R23) — one-time setup.
-- Paste into the dashboard SQL editor (project > SQL) and Run.
--
-- Trust model v1: RLS is ENABLED with NO policies, so the publishable key
-- can touch nothing; only the service (secret) key — held by the app on the
-- member's machine — can read/write. Per-member Supabase auth with real RLS
-- policies is a later round. E2EE is unchanged either way: message bodies
-- and files arrive here already sealed (the server only stores ciphertext).

-- JSON documents (meta snapshots, accounts, status docs, overlays…)
create table if not exists public.ab_docs (
  root    text not null,
  path    text not null,
  data    jsonb not null,
  updated timestamptz not null default now(),
  primary key (root, path)
);

-- Append-only message logs (one row per record; id = the read offset)
create table if not exists public.ab_logs (
  id       bigint generated always as identity primary key,
  root     text not null,
  chat_id  text not null,
  log_name text not null,
  line     text not null
);
create index if not exists ab_logs_scan
  on public.ab_logs (root, chat_id, log_name, id);

alter table public.ab_docs enable row level security;
alter table public.ab_logs enable row level security;
-- no policies on purpose: service key only (v1 trust model)

-- One round-trip helpers (PostgREST cannot group-by without an RPC)
create or replace function public.ab_list_logs(p_root text, p_chat text)
returns table (log_name text, head bigint)
language sql stable as $$
  select log_name, max(id) as head
  from public.ab_logs
  where root = p_root and chat_id = p_chat
  group by log_name
$$;

create or replace function public.ab_chat_ids(p_root text)
returns table (chat_id text)
language sql stable as $$
  select distinct chat_id from public.ab_logs where root = p_root
  union
  select distinct split_part(path, '/', 2)
  from public.ab_docs
  where root = p_root and path like 'chats/%'
$$;

-- Storage: the app creates the private bucket itself ("ab-mesh").

-- ---------------------------------------------------------------------------
-- R76 (V84, the egress round) — incremental doc sync. Idempotent; paste the
-- whole file again (or just this section) and Run. Until this lands the app
-- runs in a slower legacy full-snapshot mode and the Connection panel says so.
--
-- Every insert/update gets a globally monotonic `seq` from one sequence, so
-- "what changed since seq X?" is one indexed query (the docs twin of the
-- ab_logs id feed). Deletes become SOFT (deleted=true, seq bumped) so they
-- ride the same feed; the app purges old tombstones and heals via periodic
-- full reconciles.

alter table public.ab_docs add column if not exists seq bigint not null default 0;
alter table public.ab_docs add column if not exists deleted boolean not null default false;

create sequence if not exists public.ab_docs_ver;

create or replace function public.ab_docs_touch() returns trigger
language plpgsql as $$
begin
  new.seq := nextval('public.ab_docs_ver');
  new.updated := now();
  return new;
end $$;

drop trigger if exists ab_docs_touch on public.ab_docs;
create trigger ab_docs_touch before insert or update on public.ab_docs
  for each row execute function public.ab_docs_touch();

-- backfill: assign a seq to every pre-migration row (no-op update fires the
-- trigger; rerunning skips rows that already have one)
update public.ab_docs set deleted = deleted where seq = 0;

create index if not exists ab_docs_delta on public.ab_docs (root, seq);
-- Prefix listings are the bounded recovery path for runtime ledgers.  The
-- default (root, path) index cannot support LIKE 'literal-prefix%' under every
-- project collation, which made each tiny handoff/pause lookup scan the whole
-- root and evaluate chat RLS thousands of times.
create index if not exists ab_docs_live_path_prefix
  on public.ab_docs (root, path text_pattern_ops)
  where not deleted;

-- tombstones must not resurrect chat ids in the listing helper
create or replace function public.ab_chat_ids(p_root text)
returns table (chat_id text)
language sql stable as $$
  select distinct chat_id from public.ab_logs where root = p_root
  union
  select distinct split_part(path, '/', 2)
  from public.ab_docs
  where root = p_root and path like 'chats/%' and not deleted
$$;

-- ---------------------------------------------------------------------------
-- R84 — per-member RLS (trust model v2). Idempotent; paste and Run.
-- Design + runbook: docs/SECURITY_RLS.md. Until members hold their own
-- credentials the fleet keeps using the service key (which BYPASSES RLS),
-- so pasting this changes nothing for a running mesh — it arms the gate.
--
-- Identity (v2.2 — account creation IS membership): a member's Supabase
-- auth user is BORN ON THEIR OWN MACHINE via self-signup with the
-- publishable key (the password never exists anywhere else — nothing to
-- transfer, nothing to delete, no owner minting, no admission prompt).
-- Creating an app account then SELF-CLAIMS the username here: one row,
-- first-come-first-served, exactly the app directory's own rule. The
-- mesh is as private as its bootstrap config (URL + publishable key +
-- root name) — possession of the bootstrap IS the invite, like a group
-- link; what RLS buys on top is CHAT-level scoping between members and
-- the retirement of the god-mode service key from member machines.
-- Nothing gates on JWT metadata (user_metadata is self-editable;
-- app_metadata would drag the service key back into every signup).

-- who IS a member of which root (uid = auth.users.id)
create table if not exists public.ab_members (
  root     text not null,
  username text not null,
  uid      uuid not null,
  added_at timestamptz not null default now(),
  primary key (root, username),
  unique (root, uid)
);
alter table public.ab_members enable row level security;
-- A name is never freed (docs/NAME_RETENTION_AND_PRIVATE_POKES.md): leaving
-- sets revoked_at instead of deleting the row, so the name stays taken and
-- the login keeps no access. Null means active.
alter table public.ab_members add column if not exists revoked_at timestamptz;

-- SECURITY DEFINER: these run inside policies over the very tables they
-- read — without definer they'd recurse through RLS. Owned by the schema
-- owner; search_path pinned.
create or replace function public.ab_member(p_root text) returns text
language sql stable security definer set search_path = public as $$
  select coalesce((select username from public.ab_members
                   where root = p_root and uid = (select auth.uid())
                     and revoked_at is null), '')
$$;
revoke all on function public.ab_member(text) from public;
grant execute on function public.ab_member(text) to authenticated;

create or replace function public.ab_root_ok(p_root text) returns boolean
language sql stable as $$
  select public.ab_member(p_root) <> ''
$$;

create or replace function public.ab_chat_of(p_path text) returns text
language sql immutable as $$
  select case when p_path like 'chats/%'
              then split_part(p_path, '/', 2) else '' end
$$;

-- The chat-lane ACL is the chat's own meta doc (chats/<id>/meta.json ->
-- data.members object): maintained on every membership change, written
-- meta-FIRST at genesis (R25: "so the member gate holds from here on"),
-- ids commit to their genesis hash (R13.5). Deliberately does NOT filter
-- tombstoned metas: during the deletion grace the members' janitors still
-- need access to purge the subtree.
create or replace function public.ab_is_member(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.ab_docs m
    where m.root = p_root
      and m.path = 'chats/' || p_chat || '/meta.json'
      and m.data->'members' ? public.ab_member(p_root)
  )
$$;
revoke all on function public.ab_is_member(text, text) from public;
grant execute on function public.ab_is_member(text, text) to authenticated;

-- A deleted room has no current members, but the members it had when it was
-- deleted (its final_members, written by the deleting client's fold) must
-- still be able to observe the terminal meta/log evidence and perform bounded
-- janitor cleanup. People who had left before the deletion may not (D-018). A
-- deletion by an older client carries no final_members; then its tenure (all
-- past members) decides. This is read/delete authority only; insert/update
-- policies continue to use ab_is_member, so terminal rooms cannot receive new
-- content. One lookup: every chat-path policy calls this per row.
create or replace function public.ab_can_read_chat(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.ab_docs m
    where m.root = p_root
      and m.path = 'chats/' || p_chat || '/meta.json'
      and (
        m.data->'members' ? public.ab_member(p_root)
        or (
          m.data @> '{"deleted": true}'::jsonb
          and case
            when m.data ? 'final_members'
            then pg_catalog.jsonb_typeof(m.data->'final_members') = 'array'
                 and m.data->'final_members' ? public.ab_member(p_root)
            else m.data->'tenure' ? public.ab_member(p_root)
          end
        )
      )
  )
$$;
revoke all on function public.ab_can_read_chat(text, text) from public;
grant execute on function public.ab_can_read_chat(text, text) to authenticated;

-- Of those, the ones who are not current members: a deleted room's final
-- members. Its staged attachments are theirs to list and remove before the
-- purge (no other principal may touch another uploader's staged object).
create or replace function public.ab_chat_terminal_reader(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = public as $$
  select public.ab_can_read_chat(p_root, p_chat)
    and not public.ab_is_member(p_root, p_chat)
$$;
revoke all on function public.ab_chat_terminal_reader(text, text) from public;
grant execute on function public.ab_chat_terminal_reader(text, text) to authenticated;

-- Discover candidate chat ids while bypassing row-by-row RLS over every log
-- and document, then apply the same current-member/deleted-tenure authority
-- predicate once per candidate.  The function exposes no data other than ids
-- the caller could already read.  A fixed search_path and explicit grants are
-- load-bearing because this is a SECURITY DEFINER boundary.
create or replace function public.ab_chat_ids(p_root text)
returns table (chat_id text)
language sql stable security definer set search_path = pg_catalog as $$
  with candidates as (
    select distinct l.chat_id
    from public.ab_logs l
    where l.root = p_root
    union
    select distinct pg_catalog.split_part(d.path, '/', 2)
    from public.ab_docs d
    where d.root = p_root and d.path like 'chats/%' and not d.deleted
  )
  select c.chat_id
  from candidates c
  where c.chat_id <> ''
    and (
      auth.role() = 'service_role'
      or (
        public.ab_root_ok(p_root)
        and public.ab_can_read_chat(p_root, c.chat_id)
      )
    )
$$;
revoke all on function public.ab_chat_ids(text)
  from public, anon, authenticated;
grant execute on function public.ab_chat_ids(text)
  to authenticated, service_role;

-- ab_members: SELF-claim on insert — your own uid, an unclaimed username
-- (the PK is the arbiter, first come first served, mirroring the app
-- directory's rule); one identity per uid per root (unique root+uid).
-- Rows are never deleted (the name-retention section near the end of this
-- file): leaving is the typed member_leave, eviction is the operator's
-- revoke, and both set revoked_at. Members see their root's roster;
-- outsiders and revoked logins see nothing.
drop policy if exists ab_members_select on public.ab_members;
create policy ab_members_select on public.ab_members
for select to authenticated using (public.ab_root_ok(root));

drop policy if exists ab_members_admit on public.ab_members;
drop policy if exists ab_members_claim on public.ab_members;
create policy ab_members_claim on public.ab_members
for insert to authenticated with check (uid = (select auth.uid()));

-- The legacy self-delete freed the name for the next claimer; it is gone.
drop policy if exists ab_members_leave on public.ab_members;

-- (v2.1's ab_pending queue is retired — account creation is membership;
-- drop it if an earlier paste created it)
drop table if exists public.ab_pending;

-- ab_docs: global lanes (users/, status/, control/, machines/, presence/…)
-- are mesh-wide by design — any member of the root reads and writes them
-- (the app's own rules arbitrate within; E2EE seals what must be sealed).
-- chats/ lanes are members-only, with ONE insert exception: genesis — a
-- fresh meta.json for a chat with no meta yet, listing the creator among
-- its members. Chat ids commit to their genesis hash (R13.5), so squatting
-- a foreign id is not practical. The client MUST issue INSERT, not UPSERT,
-- for this first row: PostgreSQL evaluates an UPSERT's UPDATE policy before
-- the membership-bearing meta exists, so the intentional INSERT-only
-- exception cannot authorize it.
drop policy if exists ab_docs_member_select on public.ab_docs;
create policy ab_docs_member_select on public.ab_docs
for select to authenticated using (
  public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_can_read_chat(root, public.ab_chat_of(path))
  )
);

drop policy if exists ab_docs_member_insert on public.ab_docs;
create policy ab_docs_member_insert on public.ab_docs
for insert to authenticated with check (
  public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_is_member(root, public.ab_chat_of(path))
    or (
      path = 'chats/' || public.ab_chat_of(path) || '/meta.json'
      and data->'members' ? public.ab_member(root)
    )
  )
  and path not like 'chats/%/runtime/effects/%'
);

drop policy if exists ab_docs_member_update on public.ab_docs;
create policy ab_docs_member_update on public.ab_docs
for update to authenticated using (
  public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_is_member(root, public.ab_chat_of(path))
  )
  and path not like 'chats/%/runtime/effects/%'
) with check (
  public.ab_root_ok(root)
  and path not like 'chats/%/runtime/effects/%'
);

-- Members hard-delete only tombstones (the janitor's purge): a live row's
-- hard delete never reaches another reader's delta feed, so it would force
-- every reader in the root to pull everything (P7's floor). Deleting a doc
-- is a soft delete (deleted = true); only an operator removes live rows.
drop policy if exists ab_docs_member_delete on public.ab_docs;
create policy ab_docs_member_delete on public.ab_docs
for delete to authenticated using (
  deleted
  and public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_can_read_chat(root, public.ab_chat_of(path))
  )
  and path not like 'chats/%/runtime/effects/%'
);

-- R142: versioned, authenticated append-only effect transitions. Direct
-- member writes to the lane are denied above. This definer function is the
-- sole writer and validates caller identity, current room membership, routing,
-- predecessor existence and one fixed path per state version.
create or replace function public.ab_effects_ready()
returns integer
language sql stable security invoker set search_path = public as $$
  select 1
$$;
revoke all on function public.ab_effects_ready() from public, anon;
grant execute on function public.ab_effects_ready() to authenticated;

drop function if exists public.ab_effect_transition(text, text, jsonb);
create or replace function public.ab_effect_transition(
  p_root text, p_path text, p_data jsonb,
  p_grant_ask jsonb, p_grant_decision jsonb
) returns boolean
language plpgsql security definer set search_path = public as $$
declare
  v_member text := public.ab_member(p_root);
  v_chat text := split_part(p_path, '/', 2);
  v_run text := split_part(p_path, '/', 5);
  v_call text := split_part(p_path, '/', 6);
  v_file text := split_part(p_path, '/', 7);
  v_meta jsonb := p_data->'meta';
  v_actor text := v_meta->>'actor';
  v_previous text;
  v_existing jsonb;
  v_base text := left(p_path, length(p_path) - length(v_file));
  v_grant_ask_path text := v_base || 'grant-ask.json';
  v_grant_decision_path text := v_base || 'grant-decision.json';
begin
  if v_member = '' or not public.ab_is_member(p_root, v_chat)
     or not exists (
       select 1 from public.ab_docs u
       where u.root = p_root and u.path = 'users/' || v_actor || '.json'
         and not u.deleted and u.data->>'kind' = 'agent'
         and (u.data->>'active')::boolean is true
         and u.data->'agent'->>'owner' = v_member
     )
     or not exists (
       select 1 from public.ab_docs m
       where m.root = p_root
         and m.path = 'chats/' || v_chat || '/meta.json'
         and not m.deleted
         and m.data->'members' ? v_member
         and m.data->'members' ? v_actor
     ) then
    return false;
  end if;
  if p_path !~ '^chats/[^/]+/runtime/effects/[^/]+/[^/]+/(claim|state-[23])\.json$'
     or jsonb_typeof(p_data) <> 'object'
     or jsonb_typeof(v_meta) <> 'object'
     or v_meta->>'kind' <> 'effect'
     or coalesce(v_actor, '') = ''
     or v_meta->>'signer' <> v_actor
     or v_meta->>'chat_id' <> v_chat
     or v_meta->>'run_id' <> v_run
     or v_meta->>'root_run_id' <> v_run
     or v_meta->>'call_id' <> v_call then
    return false;
  end if;

  if v_file = 'claim.json' then
    v_previous := null;
    if jsonb_typeof(p_grant_ask) <> 'object'
       or jsonb_typeof(p_grant_decision) <> 'object'
       or p_grant_ask->'header'->>'kind' <> 'permission_ask'
       or p_grant_ask->'header'->>'sender' <> v_actor
       or p_grant_ask->'header'->>'recipient' <> v_member
       or p_grant_ask->'header'->>'agent' <> v_actor
       or p_grant_ask->'header'->>'chat_id' <> v_chat
       or p_grant_decision->'header'->>'kind' <> 'permission_decision'
       or p_grant_decision->'header'->>'sender' <> v_member
       or p_grant_decision->'header'->>'recipient' <> v_actor
       or p_grant_decision->'header'->>'agent' <> v_actor
       or p_grant_decision->'header'->>'chat_id' <> v_chat then
      return false;
    end if;
  elsif v_file = 'state-2.json' then
    v_previous := 'chats/' || v_chat || '/runtime/effects/' ||
                  v_run || '/' || v_call || '/claim.json';
  elsif v_file = 'state-3.json' then
    v_previous := 'chats/' || v_chat || '/runtime/effects/' ||
                  v_run || '/' || v_call || '/state-2.json';
  else
    return false;
  end if;

  if v_previous is not null and not exists (
    select 1 from public.ab_docs d
    where d.root = p_root and d.path = v_previous and not d.deleted
      and d.data->'meta'->>'actor' = v_actor
      and d.data->'meta'->>'chat_id' = v_chat
      and d.data->'meta'->>'run_id' = v_run
      and d.data->'meta'->>'call_id' = v_call
  ) then
    return false;
  end if;

  begin
    if v_file = 'claim.json' then
      insert into public.ab_docs(root, path, data, deleted) values
        (p_root, v_grant_ask_path, p_grant_ask, false),
        (p_root, v_grant_decision_path, p_grant_decision, false);
    end if;
    insert into public.ab_docs(root, path, data, deleted)
    values (p_root, p_path, p_data, false);
    return true;
  exception when unique_violation then
    select data into v_existing from public.ab_docs
    where root = p_root and path = p_path and not deleted;
    if v_existing <> p_data then
      return false;
    end if;
    if v_file = 'claim.json' then
      return exists (
        select 1 from public.ab_docs a, public.ab_docs d
        where a.root = p_root and a.path = v_grant_ask_path
          and a.data = p_grant_ask and not a.deleted
          and d.root = p_root and d.path = v_grant_decision_path
          and d.data = p_grant_decision and not d.deleted
      );
    end if;
    return true;
  end;
end
$$;

revoke all on function public.ab_effect_transition(text, text, jsonb, jsonb, jsonb)
  from public, anon;
grant execute on function public.ab_effect_transition(text, text, jsonb, jsonb, jsonb)
  to authenticated;

-- ab_logs: members only, both ways; deletes cover the janitor's purge of a
-- deleted chat's logs. Genesis is safe by ordering: meta.json lands before
-- the first log record (R25).
drop policy if exists ab_logs_member_select on public.ab_logs;
create policy ab_logs_member_select on public.ab_logs
for select to authenticated using (
  public.ab_root_ok(root) and public.ab_can_read_chat(root, chat_id)
);

drop policy if exists ab_logs_member_insert on public.ab_logs;
create policy ab_logs_member_insert on public.ab_logs
for insert to authenticated with check (
  public.ab_root_ok(root) and public.ab_is_member(root, chat_id)
);

drop policy if exists ab_logs_member_delete on public.ab_logs;
create policy ab_logs_member_delete on public.ab_logs
for delete to authenticated using (
  public.ab_root_ok(root) and public.ab_can_read_chat(root, chat_id)
);

-- Storage (bucket "ab-mesh", keys "<root>/<path>"): chat blobs are
-- members-only; everything else under the root (user/group avatars) is
-- mesh-wide like the global doc lanes.
drop policy if exists ab_blobs_member_select on storage.objects;
create policy ab_blobs_member_select on storage.objects
for select to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(split_part(name, '/', 1))
  and (
    split_part(name, '/', 2) <> 'chats'
    or public.ab_can_read_chat(split_part(name, '/', 1), split_part(name, '/', 3))
  )
);

drop policy if exists ab_blobs_member_insert on storage.objects;
create policy ab_blobs_member_insert on storage.objects
for insert to authenticated with check (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(split_part(name, '/', 1))
  and (
    split_part(name, '/', 2) <> 'chats'
    or public.ab_is_member(split_part(name, '/', 1), split_part(name, '/', 3))
  )
);

drop policy if exists ab_blobs_member_update on storage.objects;
create policy ab_blobs_member_update on storage.objects
for update to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(split_part(name, '/', 1))
  and (
    split_part(name, '/', 2) <> 'chats'
    or public.ab_is_member(split_part(name, '/', 1), split_part(name, '/', 3))
  )
);

drop policy if exists ab_blobs_member_delete on storage.objects;
create policy ab_blobs_member_delete on storage.objects
for delete to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(split_part(name, '/', 1))
  and (
    split_part(name, '/', 2) <> 'chats'
    or public.ab_can_read_chat(split_part(name, '/', 1), split_part(name, '/', 3))
  )
);

-- ---------------------------------------------------------------------------
-- Durable Realtime change ledger (observation stage). Realtime notifications
-- are low-latency wakes; these append-only rows are the bounded replay source.
-- They contain positions and scopes, never payloads or authority verdicts.

create table if not exists public.ab_change_epochs (
  root           text primary key,
  epoch          uuid not null default gen_random_uuid(),
  minimum_cursor bigint not null default 0 check (minimum_cursor >= 0),
  schema_version integer not null default 1 check (schema_version = 1),
  updated_at     timestamptz not null default now()
);

create table if not exists public.ab_change_events (
  id          bigint generated always as identity primary key,
  root        text not null,
  stream_kind text not null check (stream_kind in ('root', 'chat')),
  stream_id   text not null default '',
  domain      text not null check (domain in ('docs', 'logs', 'visibility')),
  doc_head    bigint check (doc_head >= 0),
  log_head    bigint check (log_head >= 0),
  created_at  timestamptz not null default now(),
  check (
    (stream_kind = 'root' and stream_id = '')
    or (stream_kind = 'chat' and stream_id <> '')
  ),
  check (
    (domain = 'docs' and doc_head is not null and log_head is null)
    or (
      domain = 'logs' and stream_kind = 'chat'
      and log_head is not null and doc_head is null
    )
    or (domain = 'visibility' and doc_head is null and log_head is null)
  )
);
-- Exact raw identity is optional on historical rows. Current RLS can expose an
-- old identity-less event after a prior visible fence (for example on rejoin),
-- so NULL means the node must reconcile that event's whole root/chat scope.
-- The constraint also rejects oversized new source identities transactionally;
-- an RPC page cannot materialize an unbounded source key before Python checks it.
alter table public.ab_change_events
  add column if not exists source_key text;
alter table public.ab_change_events
  drop constraint if exists ab_change_events_source_identity_size;
alter table public.ab_change_events
  add constraint ab_change_events_source_identity_size check (
    pg_catalog.octet_length(stream_id) <= 1024
    and (
      source_key is null
      or pg_catalog.octet_length(source_key) <= 4096
    )
  ) not valid;
alter table public.ab_change_events
  validate constraint ab_change_events_source_identity_size;
create index if not exists ab_change_events_replay
  on public.ab_change_events (root, id);

alter table public.ab_change_epochs enable row level security;
alter table public.ab_change_events enable row level security;

revoke all on table public.ab_change_epochs from public, anon, authenticated;
revoke all on table public.ab_change_events from public, anon, authenticated;
grant select on table public.ab_change_epochs to authenticated;
grant select on table public.ab_change_events to authenticated;
revoke all on sequence public.ab_change_events_id_seq
  from public, anon, authenticated;

drop policy if exists ab_change_epochs_member_select
  on public.ab_change_epochs;
create policy ab_change_epochs_member_select on public.ab_change_epochs
for select to authenticated using ((select public.ab_root_ok(root)));

drop policy if exists ab_change_events_member_select
  on public.ab_change_events;
create policy ab_change_events_member_select on public.ab_change_events
for select to authenticated using (
  (select public.ab_root_ok(root))
  and (
    stream_kind = 'root'
    or (select public.ab_can_read_chat(root, stream_id))
  )
);

-- Probe the complete replay contract rather than inferring readiness from a
-- table that may have been installed by an older schema paste.
create or replace function public.ab_change_ledger_ready() returns integer
language sql stable security invoker set search_path = pg_catalog as $$
  select 1
$$;
revoke all on function public.ab_change_ledger_ready()
  from public, anon, authenticated;
grant execute on function public.ab_change_ledger_ready()
  to authenticated, service_role;

-- Identity values are allocated before commit, so a direct `id > cursor`
-- query can observe a later transaction and permanently skip an earlier one.
-- Writers take a shared transaction lock before allocating an event id. This
-- bounded RPC takes the matching exclusive lock: existing writers settle,
-- later writers wait until the page snapshot is captured, and concurrent
-- writers never block one another. RLS remains the authority filter.
create or replace function public.ab_change_events_page(
  p_root text, p_after bigint, p_limit integer
) returns table (
  id bigint,
  stream_kind text,
  stream_id text,
  domain text,
  doc_head bigint,
  log_head bigint
)
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
begin
  if p_root is null or p_root = ''
     or p_after is null or p_after < 0
     or p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'invalid change-ledger page request'
      using errcode = '22023';
  end if;
  if not public.ab_root_ok(p_root) then
    raise exception 'change-ledger root is unavailable'
      using errcode = '42501';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  return query
    select e.id, e.stream_kind, e.stream_id, e.domain, e.doc_head, e.log_head
    from public.ab_change_events e
    where e.root = p_root and e.id > p_after
    order by e.id
    limit p_limit;
end
$$;
revoke all on function public.ab_change_events_page(text, bigint, integer)
  from public, anon, authenticated;
grant execute on function public.ab_change_events_page(text, bigint, integer)
  to authenticated, service_role;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create or replace function private.ab_record_doc_change() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_chat text;
begin
  -- Presence heartbeats have their own expiry/revalidation owner. Logging
  -- each refresh would create permanent replay and Realtime traffic.
  if new.path like 'presence/%' then
    return new;
  end if;

  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || new.root, 0)
  );
  insert into public.ab_change_epochs(root) values (new.root)
  on conflict (root) do nothing;

  if new.path like 'chats/%' then
    v_chat := split_part(new.path, '/', 2);
    insert into public.ab_change_events(
      root, stream_kind, stream_id, domain, source_key, doc_head
    ) values (new.root, 'chat', v_chat, 'docs', new.path, new.seq);

    if new.path = 'chats/' || v_chat || '/meta.json' then
      if tg_op = 'INSERT' then
        insert into public.ab_change_events(
          root, stream_kind, stream_id, domain
        ) values (new.root, 'root', '', 'visibility');
      elsif old.data->'members' is distinct from new.data->'members'
         or old.data->'tenure' is distinct from new.data->'tenure'
         or old.data->'final_members' is distinct from new.data->'final_members'
         or old.data->'deleted' is distinct from new.data->'deleted'
         or old.deleted is distinct from new.deleted then
        insert into public.ab_change_events(
          root, stream_kind, stream_id, domain
        ) values (new.root, 'root', '', 'visibility');
      end if;
    end if;
  else
    insert into public.ab_change_events(
      root, stream_kind, stream_id, domain, source_key, doc_head
    ) values (new.root, 'root', '', 'docs', new.path, new.seq);
  end if;
  return new;
end
$$;
revoke all on function private.ab_record_doc_change()
  from public, anon, authenticated;

drop trigger if exists ab_docs_change_ledger on public.ab_docs;
create trigger ab_docs_change_ledger
after insert or update on public.ab_docs
for each row execute function private.ab_record_doc_change();

-- Physical deletion is rare (normally documents become tombstones first), but
-- the node must never infer completeness from a ledger that silently misses a
-- purge or an administrative hard delete. Emit one root recovery event instead
-- of one event per deleted row, so a large tombstone sweep cannot amplify the
-- durable ledger. Presence remains outside durable replay.
create or replace function private.ab_record_doc_delete_scope() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
begin
  for v_root in
    select distinct d.root
    from old_rows d
    where d.path not like 'presence/%'
    order by d.root
  loop
    perform pg_catalog.pg_advisory_xact_lock_shared(
      pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
    );
    insert into public.ab_change_epochs(root) values (v_root)
    on conflict (root) do nothing;
    insert into public.ab_change_events(
      root, stream_kind, stream_id, domain
    ) values (v_root, 'root', '', 'visibility');
  end loop;
  return null;
end
$$;
revoke all on function private.ab_record_doc_delete_scope()
  from public, anon, authenticated;

drop trigger if exists ab_docs_delete_scope_ledger on public.ab_docs;
create trigger ab_docs_delete_scope_ledger
after delete on public.ab_docs
referencing old table as old_rows
for each statement execute function private.ab_record_doc_delete_scope();

create or replace function private.ab_record_log_change() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
begin
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || new.root, 0)
  );
  insert into public.ab_change_epochs(root) values (new.root)
  on conflict (root) do nothing;
  insert into public.ab_change_events(
    root, stream_kind, stream_id, domain, source_key, log_head
  ) values (new.root, 'chat', new.chat_id, 'logs', new.log_name, new.id);
  return new;
end
$$;
revoke all on function private.ab_record_log_change()
  from public, anon, authenticated;

drop trigger if exists ab_logs_change_ledger on public.ab_logs;
create trigger ab_logs_change_ledger
after insert on public.ab_logs
for each row execute function private.ab_record_log_change();

-- Log deletion can remove an arbitrarily long room history. One root-scoped
-- event is intentionally coarse but bounded; recovery must re-enumerate under
-- current RLS before it can admit absence.
create or replace function private.ab_record_log_delete_scope() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
begin
  for v_root in
    select distinct l.root from old_rows l order by l.root
  loop
    perform pg_catalog.pg_advisory_xact_lock_shared(
      pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
    );
    insert into public.ab_change_epochs(root) values (v_root)
    on conflict (root) do nothing;
    insert into public.ab_change_events(
      root, stream_kind, stream_id, domain
    ) values (v_root, 'root', '', 'visibility');
  end loop;
  return null;
end
$$;
revoke all on function private.ab_record_log_delete_scope()
  from public, anon, authenticated;

drop trigger if exists ab_logs_delete_scope_ledger on public.ab_logs;
create trigger ab_logs_delete_scope_ledger
after delete on public.ab_logs
referencing old table as old_rows
for each statement execute function private.ab_record_log_delete_scope();

create or replace function private.ab_record_membership_roots(p_roots text[])
returns void
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
begin
  foreach v_root in array coalesce(p_roots, array[]::text[])
  loop
    perform pg_catalog.pg_advisory_xact_lock_shared(
      pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
    );
    insert into public.ab_change_epochs(root) values (v_root)
    on conflict (root) do nothing;
    insert into public.ab_change_events(
      root, stream_kind, stream_id, domain
    ) values (v_root, 'root', '', 'visibility');
  end loop;
end
$$;
revoke all on function private.ab_record_membership_roots(text[])
  from public, anon, authenticated;

create or replace function private.ab_record_membership_insert()
returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_roots text[];
begin
  select pg_catalog.array_agg(roots.root order by roots.root) into v_roots
  from (select distinct member.root from new_rows member) roots;
  perform private.ab_record_membership_roots(v_roots);
  return null;
end
$$;
create or replace function private.ab_record_membership_update()
returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_roots text[];
begin
  select pg_catalog.array_agg(roots.root order by roots.root) into v_roots
  from (
    select member.root from old_rows member
    union
    select member.root from new_rows member
  ) roots;
  perform private.ab_record_membership_roots(v_roots);
  return null;
end
$$;
create or replace function private.ab_record_membership_delete()
returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_roots text[];
begin
  select pg_catalog.array_agg(roots.root order by roots.root) into v_roots
  from (select distinct member.root from old_rows member) roots;
  perform private.ab_record_membership_roots(v_roots);
  return null;
end
$$;
revoke all on function private.ab_record_membership_insert()
  from public, anon, authenticated;
revoke all on function private.ab_record_membership_update()
  from public, anon, authenticated;
revoke all on function private.ab_record_membership_delete()
  from public, anon, authenticated;

drop trigger if exists ab_members_change_ledger on public.ab_members;
drop function if exists private.ab_record_root_membership_change();
drop trigger if exists ab_members_insert_change_ledger on public.ab_members;
drop trigger if exists ab_members_update_change_ledger on public.ab_members;
drop trigger if exists ab_members_delete_change_ledger on public.ab_members;
create trigger ab_members_insert_change_ledger
after insert on public.ab_members
referencing new table as new_rows
for each statement execute function private.ab_record_membership_insert();
create trigger ab_members_update_change_ledger
after update on public.ab_members
referencing old table as old_rows new table as new_rows
for each statement execute function private.ab_record_membership_update();
create trigger ab_members_delete_change_ledger
after delete on public.ab_members
referencing old table as old_rows
for each statement execute function private.ab_record_membership_delete();

-- Source-ledger v1 did not record physical deletes or member-row updates. An
-- existing node may therefore have acknowledged a cursor while retaining data
-- that disappeared before v2 was installed. Advance the per-root contract and
-- rotate its epoch exactly once so every v1 frontier requires root recovery.
-- Set the new-row default before the backfill: a root created concurrently with
-- this migration must be born at v2 rather than escape the invalidation cut.
alter table public.ab_change_epochs
  add column if not exists source_schema_version integer not null default 1;
alter table public.ab_change_epochs
  drop constraint if exists ab_change_epochs_source_schema_version;
alter table public.ab_change_epochs
  add constraint ab_change_epochs_source_schema_version check (
    source_schema_version >= 1
  ) not valid;
alter table public.ab_change_epochs
  validate constraint ab_change_epochs_source_schema_version;
alter table public.ab_change_epochs
  alter column source_schema_version set default 2;
update public.ab_change_epochs
set source_schema_version = 2,
    epoch = pg_catalog.gen_random_uuid(),
    updated_at = pg_catalog.now()
where source_schema_version < 2;

-- Local-node source replay.  The fence/page lock pairs with the shared writer
-- lock above, so identity allocation order cannot be mistaken for commit order.
-- RLS remains the disclosure boundary; source_key is work identity only and is
-- never a membership or visibility verdict.
create or replace function public.ab_node_source_ledger_fence(p_root text)
returns table (
  epoch uuid, minimum_cursor bigint, cursor bigint, schema_version integer
)
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
begin
  if p_root is null or p_root = '' then
    raise exception 'invalid source-ledger fence request' using errcode = '22023';
  end if;
  if auth.role() <> 'service_role' and not public.ab_root_ok(p_root) then
    raise exception 'source-ledger root is unavailable' using errcode = '42501';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  return query
    select s.epoch, s.minimum_cursor,
           greatest(
             s.minimum_cursor, coalesce(tail.id, 0)
           )::bigint,
           s.source_schema_version
    from public.ab_change_epochs s
    left join lateral (
      select e.id
      from public.ab_change_events e
      where e.root = s.root
      order by e.id desc
      limit 1
    ) tail on true
    where s.root = p_root
    limit 1;
end
$$;

create or replace function public.ab_node_source_events_page(
  p_root text, p_after bigint, p_limit integer
) returns table (
  id bigint, stream_kind text, stream_id text, domain text,
  source_key text, doc_head bigint, log_head bigint
)
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
declare
  v_minimum bigint;
begin
  if p_root is null or p_root = ''
     or p_after is null or p_after < 0
     or p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'invalid source-ledger page request' using errcode = '22023';
  end if;
  if auth.role() <> 'service_role' and not public.ab_root_ok(p_root) then
    raise exception 'source-ledger root is unavailable' using errcode = '42501';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  select s.minimum_cursor into v_minimum
  from public.ab_change_epochs s where s.root = p_root;
  if v_minimum is null or p_after < v_minimum then
    raise exception 'source-ledger cursor is unavailable' using errcode = '22023';
  end if;
  return query
    select e.id, e.stream_kind, e.stream_id, e.domain,
           e.source_key, e.doc_head, e.log_head
    from public.ab_change_events e
    where e.root = p_root and e.id > p_after
    order by e.id
    limit p_limit;
end
$$;
revoke all on function public.ab_node_source_ledger_fence(text)
  from public, anon, authenticated;
revoke all on function public.ab_node_source_events_page(text, bigint, integer)
  from public, anon, authenticated;
grant execute on function public.ab_node_source_ledger_fence(text)
  to authenticated, service_role;
grant execute on function public.ab_node_source_events_page(text, bigint, integer)
  to authenticated, service_role;

-- Install the capability marker last. A partial schema paste must fail closed
-- instead of advertising exact replay before both functions and triggers exist.
create or replace function public.ab_node_source_ledger_ready() returns integer
language sql stable security invoker set search_path = pg_catalog as $$
  select 2
$$;
revoke all on function public.ab_node_source_ledger_ready()
  from public, anon, authenticated;
grant execute on function public.ab_node_source_ledger_ready()
  to authenticated, service_role;

-- Exact, response-bounded payload reads for the inactive local node. Stored
-- sizes are generated without firing the document/log change triggers during
-- migration. They make the byte preflight narrow: payload values are selected
-- only after their exact keys and cumulative admitted size are known.
create or replace function public.ab_source_json_bytes(p_value jsonb)
returns integer language sql immutable strict set search_path = pg_catalog as $$
  select pg_catalog.octet_length(
    pg_catalog.convert_to(p_value::text, 'UTF8')
  )::integer
$$;
create or replace function public.ab_source_text_bytes(p_value text)
returns integer language sql immutable strict set search_path = pg_catalog as $$
  select pg_catalog.octet_length(
    pg_catalog.convert_to(pg_catalog.to_json(p_value)::text, 'UTF8')
  )::integer
$$;
revoke all on function public.ab_source_json_bytes(jsonb)
  from public, anon, authenticated;
revoke all on function public.ab_source_text_bytes(text)
  from public, anon, authenticated;
grant execute on function public.ab_source_json_bytes(jsonb)
  to authenticated, service_role;
grant execute on function public.ab_source_text_bytes(text)
  to authenticated, service_role;

alter table public.ab_docs add column if not exists source_bytes integer
  generated always as (public.ab_source_json_bytes(data)) stored;
alter table public.ab_logs add column if not exists source_bytes integer
  generated always as (public.ab_source_text_bytes(line)) stored;
alter table public.ab_docs
  drop constraint if exists ab_docs_node_source_size;
alter table public.ab_docs add constraint ab_docs_node_source_size check (
  source_bytes between 0 and 8355840
) not valid;
alter table public.ab_docs validate constraint ab_docs_node_source_size;
alter table public.ab_logs
  drop constraint if exists ab_logs_node_source_size;
alter table public.ab_logs add constraint ab_logs_node_source_size check (
  source_bytes between 0 and 8355840
) not valid;
alter table public.ab_logs validate constraint ab_logs_node_source_size;

create or replace function public.ab_node_docs_exact(
  p_root text, p_paths text[], p_max_bytes bigint
) returns table (
  path text, seq bigint, data jsonb, deleted boolean, batch_overflow boolean
)
language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare
  v_count integer;
begin
  v_count := pg_catalog.cardinality(p_paths);
  if p_root is null or p_root = ''
     or v_count is null or v_count < 1 or v_count > 128
     or p_max_bytes is null or p_max_bytes < 1 or p_max_bytes > 8388608
     or pg_catalog.array_ndims(p_paths) <> 1
     or exists (
       select 1 from pg_catalog.unnest(p_paths) requested(path)
       where requested.path is null or requested.path = ''
          or pg_catalog.octet_length(requested.path) > 4096
     )
     or (
       select pg_catalog.count(distinct requested.path)
       from pg_catalog.unnest(p_paths) requested(path)
     ) <> v_count then
    raise exception 'invalid exact-document source request' using errcode = '22023';
  end if;
  if auth.role() <> 'service_role' and not public.ab_root_ok(p_root) then
    raise exception 'exact-document source root is unavailable'
      using errcode = '42501';
  end if;

  return query
    with requested(path, ord) as (
      select value, ordinality
      from pg_catalog.unnest(p_paths) with ordinality request(value, ordinality)
    ), candidates as materialized (
      select r.ord, d.path, d.seq, d.deleted,
             (public.ab_source_text_bytes(d.path)
              + case when d.deleted then 0 else d.source_bytes end
              + 256)::bigint as row_bytes
      from requested r
      join public.ab_docs d on d.root = p_root and d.path = r.path
    ), state as materialized (
      select coalesce(pg_catalog.sum(c.row_bytes), 0)::bigint as bytes
      from candidates c
    ), payload as materialized (
      select c.ord, d.path, d.seq,
             case when d.deleted then null else d.data end as data,
             d.deleted
      from candidates c
      cross join state s
      join public.ab_docs d on d.root = p_root and d.path = c.path
      where s.bytes <= p_max_bytes
    ), result as (
      select p.ord, p.path, p.seq, p.data, p.deleted, false as batch_overflow
      from payload p
      union all
      select 0::bigint, null::text, null::bigint, null::jsonb, null::boolean, true
      from state s where s.bytes > p_max_bytes
    )
    select r.path, r.seq, r.data, r.deleted, r.batch_overflow
    from result r order by r.ord;
end
$$;

create or replace function public.ab_node_log_exact_page(
  p_root text, p_chat text, p_log text, p_after bigint, p_through bigint,
  p_limit integer, p_max_bytes bigint
) returns table (
  id bigint, line text, page_has_more boolean, page_overflow boolean
)
language plpgsql stable security invoker set search_path = pg_catalog, public as $$
begin
  if p_root is null or p_root = ''
     or p_chat is null or p_chat = '' or pg_catalog.octet_length(p_chat) > 1024
     or p_log is null or p_log = '' or pg_catalog.octet_length(p_log) > 4096
     or p_after is null or p_after < 0
     or p_through is null or p_through < p_after
     or p_limit is null or p_limit < 1 or p_limit > 1000
     or p_max_bytes is null or p_max_bytes < 1 or p_max_bytes > 8388608 then
    raise exception 'invalid exact-log source request' using errcode = '22023';
  end if;
  if auth.role() <> 'service_role' and (
    not public.ab_root_ok(p_root)
    or not public.ab_can_read_chat(p_root, p_chat)
  ) then
    return;
  end if;

  return query
    with candidates as materialized (
      select l.id, (l.source_bytes + 256)::bigint as row_bytes
      from public.ab_logs l
      where l.root = p_root and l.chat_id = p_chat and l.log_name = p_log
        and l.id > p_after and l.id <= p_through
      order by l.id
      limit p_limit + 1
    ), ranked as materialized (
      select c.id, c.row_bytes,
             pg_catalog.row_number() over (order by c.id) as rn,
             pg_catalog.sum(c.row_bytes) over (
               order by c.id rows between unbounded preceding and current row
             ) as cumulative_bytes
      from candidates c
    ), selected as materialized (
      select r.id
      from ranked r
      where r.rn <= p_limit and r.cumulative_bytes <= p_max_bytes
    ), state as materialized (
      select (select pg_catalog.count(*) from candidates) as candidates,
             (select pg_catalog.count(*) from selected) as selected,
             (select r.row_bytes from ranked r where r.rn = 1) as first_bytes
    ), payload as materialized (
      select l.id, l.line
      from selected s
      join public.ab_logs l on l.id = s.id
      where l.root = p_root and l.chat_id = p_chat and l.log_name = p_log
    )
    select p.id, p.line, st.candidates > st.selected, false
    from payload p cross join state st
    union all
    select null, null, true, true
    from state st
    where st.candidates > 0 and st.selected = 0
      and st.first_bytes > p_max_bytes
    order by id nulls first;
end
$$;

revoke all on function public.ab_node_docs_exact(text, text[], bigint)
  from public, anon, authenticated;
revoke all on function public.ab_node_log_exact_page(
  text, text, text, bigint, bigint, integer, bigint
) from public, anon, authenticated;
grant execute on function public.ab_node_docs_exact(text, text[], bigint)
  to authenticated, service_role;
grant execute on function public.ab_node_log_exact_page(
  text, text, text, bigint, bigint, integer, bigint
) to authenticated, service_role;

-- Capability marker last: a failed rewrite, validation, function or grant
-- leaves callers on the previous path rather than advertising unsafe bounds.
create or replace function public.ab_node_scoped_source_ready() returns integer
language sql stable security invoker set search_path = pg_catalog as $$
  select 1
$$;
revoke all on function public.ab_node_scoped_source_ready()
  from public, anon, authenticated;
grant execute on function public.ab_node_scoped_source_ready()
  to authenticated, service_role;

-- Existing roots start at cursor zero and perform their normal complete local
-- catch-up before observing new ledger events. No historical authority is
-- inferred from this backfill.
insert into public.ab_change_epochs(root)
select root from public.ab_members
union select root from public.ab_docs
union select root from public.ab_logs
on conflict (root) do nothing;

-- Publication setup is idempotent and remains inert if this SQL is exercised
-- outside a Supabase project. Only INSERT events exist on this table.
do $$
begin
  if exists (
    select 1 from pg_catalog.pg_publication where pubname = 'supabase_realtime'
  ) and not exists (
    select 1 from pg_catalog.pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'ab_change_events'
  ) then
    alter publication supabase_realtime add table public.ab_change_events;
  end if;
end
$$;

-- ---------------------------------------------------------------------------
-- Inactive whole-root recovery source, v1. The root lock is acquired in a
-- separate statement; the ONE subsequent SELECT below evaluates current RLS,
-- the bounded keyset/payload, and the source cut in one fresh statement
-- snapshot. This avoids using a lock CTE whose snapshot could precede a writer
-- waiting on the lock. Cut positions are delivery evidence, never authority.
-- Service-role callers are excluded: the v1 visible-chat manifest relies on
-- current member RLS and cannot enumerate service-role orphan data completely.
-- LIMIT bounds the response, while RLS filtering can still examine more
-- provider rows. Recovery is advertised only when the calling database role
-- already has a bounded statement_timeout; timeout is failure, never a
-- complete page. A function-local statement_timeout cannot arm the statement
-- that is already executing, so these RPCs deliberately do not claim it can.
-- Remove an older marker before replacing any prerequisite. If an idempotent
-- upgrade stops partway through, callers must reprobe and remain disabled.
drop function if exists public.ab_node_recovery_ready();

-- One row per append-only stream keeps manifest work independent of message
-- history length. It is raw provider index state, not a visibility cache: RLS
-- and ab_can_read_chat are still evaluated on every recovery request.
create table if not exists public.ab_log_stream_heads (
  root     text not null,
  chat_id  text not null check (
    pg_catalog.octet_length(chat_id) between 1 and 1024
  ),
  log_name text not null check (
    pg_catalog.octet_length(log_name) between 1 and 4096
  ),
  head     bigint not null check (head > 0),
  primary key (root, chat_id, log_name)
);
alter table public.ab_log_stream_heads enable row level security;
revoke all on table public.ab_log_stream_heads
  from public, anon, authenticated;
grant select on table public.ab_log_stream_heads to authenticated;
drop policy if exists ab_log_stream_heads_member_select
  on public.ab_log_stream_heads;
create policy ab_log_stream_heads_member_select on public.ab_log_stream_heads
for select to authenticated using (
  public.ab_root_ok(root) and public.ab_can_read_chat(root, chat_id)
);

create or replace function private.ab_recovery_record_log_heads()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_stream record;
begin
  for v_stream in
    select distinct l.root, l.chat_id, l.log_name
    from new_rows l order by l.root, l.chat_id, l.log_name
  loop
    perform pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        'agentbridge-log-head:' ||
        pg_catalog.jsonb_build_array(
          v_stream.root, v_stream.chat_id, v_stream.log_name
        )::text,
        0
      )
    );
  end loop;
  insert into public.ab_log_stream_heads(root, chat_id, log_name, head)
  select l.root, l.chat_id, l.log_name, pg_catalog.max(l.id)
  from new_rows l group by l.root, l.chat_id, l.log_name
  on conflict (root, chat_id, log_name) do update
    set head = greatest(
      public.ab_log_stream_heads.head, excluded.head
    );
  return null;
end
$$;
revoke all on function private.ab_recovery_record_log_heads()
  from public, anon, authenticated;
drop trigger if exists ab_recovery_log_stream_head on public.ab_logs;
create trigger ab_recovery_log_stream_head
after insert on public.ab_logs
referencing new table as new_rows
for each statement execute function private.ab_recovery_record_log_heads();

-- Replace the existing physical-delete evidence writer so stream heads and
-- its root recovery event commit together. Locks are acquired in root order;
-- the per-stream MAX uses the existing (root,chat_id,log_name,id) index.
create or replace function private.ab_record_log_delete_scope() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
  v_stream record;
  v_head bigint;
begin
  for v_root in
    select distinct l.root from old_rows l order by l.root
  loop
    perform pg_catalog.pg_advisory_xact_lock_shared(
      pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
    );
    insert into public.ab_change_epochs(root) values (v_root)
    on conflict (root) do nothing;
    insert into public.ab_change_events(
      root, stream_kind, stream_id, domain
    ) values (v_root, 'root', '', 'visibility');
  end loop;
  for v_stream in
    select distinct l.root, l.chat_id, l.log_name
    from old_rows l order by l.root, l.chat_id, l.log_name
  loop
    perform pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(
        'agentbridge-log-head:' ||
        pg_catalog.jsonb_build_array(
          v_stream.root, v_stream.chat_id, v_stream.log_name
        )::text,
        0
      )
    );
    select pg_catalog.max(l.id) into v_head
    from public.ab_logs l
    where l.root = v_stream.root and l.chat_id = v_stream.chat_id
      and l.log_name = v_stream.log_name;
    if v_head is null then
      delete from public.ab_log_stream_heads h
      where h.root = v_stream.root and h.chat_id = v_stream.chat_id
        and h.log_name = v_stream.log_name;
    else
      insert into public.ab_log_stream_heads(root, chat_id, log_name, head)
      values (v_stream.root, v_stream.chat_id, v_stream.log_name, v_head)
      on conflict (root, chat_id, log_name) do update set head = excluded.head;
    end if;
  end loop;
  return null;
end
$$;
revoke all on function private.ab_record_log_delete_scope()
  from public, anon, authenticated;

-- Backfill/reconcile under one table lock in the same statement transaction.
-- Concurrent log mutations wait, then their triggers advance the completed
-- inventory. A failed backfill leaves the readiness marker absent.
do $$
begin
  lock table public.ab_logs in share row exclusive mode;
  insert into public.ab_log_stream_heads(root, chat_id, log_name, head)
  select l.root, l.chat_id, l.log_name, pg_catalog.max(l.id)
  from public.ab_logs l
  group by l.root, l.chat_id, l.log_name
  on conflict (root, chat_id, log_name) do update set head = excluded.head;
  delete from public.ab_log_stream_heads h
  where not exists (
    select 1 from public.ab_logs l
    where l.root = h.root and l.chat_id = h.chat_id
      and l.log_name = h.log_name
  );
end
$$;

create or replace function public.ab_node_recovery_page(
  p_root text, p_family text, p_after text, p_after_log text,
  p_after_id bigint, p_limit integer, p_max_bytes bigint
) returns jsonb
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
declare
  v_result jsonb;
begin
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or p_family is null or p_family not in ('documents','chats','streams','events')
     or p_after is null or pg_catalog.octet_length(p_after) > 4096
     or p_after_log is null or pg_catalog.octet_length(p_after_log) > 4096
     or p_after_id is null or p_after_id < 0
     or p_limit is null or p_limit < 1 or p_limit > 256
     or p_max_bytes is null or p_max_bytes < 1024 or p_max_bytes > 8388608
     or (p_family = 'streams' and
         (pg_catalog.octet_length(p_after) > 1024
          or (p_after = '') <> (p_after_log = '')))
     or (p_family = 'chats' and pg_catalog.octet_length(p_after) > 1024)
     or (p_family <> 'streams' and p_after_log <> '')
     or (p_family <> 'events' and p_after_id <> 0)
     or (p_family = 'events' and p_after <> '')
     or p_max_bytes < 1024 + (case p_family
       when 'streams' then pg_catalog.octet_length(pg_catalog.convert_to(
         pg_catalog.jsonb_build_array(p_after, p_after_log)::text, 'UTF8'))
       when 'events' then pg_catalog.octet_length(pg_catalog.convert_to(
         pg_catalog.to_jsonb(p_after_id)::text, 'UTF8'))
       else public.ab_source_text_bytes(p_after) end) then
    raise exception 'invalid recovery page request' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'recovery requires read committed isolation' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() <> 'authenticated'
     or not public.ab_root_ok(p_root) then
    raise exception 'recovery root is unavailable' using errcode = '42501';
  end if;
  perform pg_catalog.set_config('lock_timeout', case
    when pg_catalog.current_setting('lock_timeout') = '0'
      or pg_catalog.current_setting('lock_timeout')::interval
         > interval '2 seconds' then '2000ms'
    else pg_catalog.current_setting('lock_timeout') end, true);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  with identity as materialized (
    select auth.uid() account_id, auth.role() role,
           public.ab_root_ok(p_root) root_ok
  ), state as materialized (
    select i.account_id, i.role,
           public.ab_node_recovery_ready() = 1 capability_ok,
           (i.account_id is not null and i.role = 'authenticated'
            and i.root_ok) authorized,
           ep.epoch source_epoch, ep.minimum_cursor,
           ep.source_schema_version,
           greatest(ep.minimum_cursor,
             coalesce(tail.id, 0)) cursor
    from identity i
    left join public.ab_change_epochs ep on ep.root = p_root
    left join lateral (
      select e.id from public.ab_change_events e
      where e.root = p_root order by e.id desc limit 1
    ) tail on true
  ), candidate_source as (
    select d.path key1, ''::text key2, 0::bigint event_id,
           d.seq doc_seq, d.deleted doc_deleted,
           null::bigint log_head, null::text stream_kind,
           null::text stream_id, null::text domain,
           null::text source_key,
           (case when pg_catalog.octet_length(d.path) > 4096
                       or pg_catalog.left(d.path, 1) = '/'
                       or pg_catalog.right(d.path, 1) = '/'
                       or pg_catalog.strpos(d.path, '//') <> 0
                       or pg_catalog.strpos(d.path, E'\\') <> 0
                       or pg_catalog.cardinality(
                         pg_catalog.string_to_array(d.path, '/')
                       ) > 32
                       or pg_catalog.array_position(
                         pg_catalog.string_to_array(d.path, '/'), '.'
                       ) is not null
                       or pg_catalog.array_position(
                         pg_catalog.string_to_array(d.path, '/'), '..'
                       ) is not null
              then p_max_bytes + 1
              else public.ab_source_text_bytes(d.path)
                 + case when d.deleted then 0 else d.source_bytes end + 256
            end)::bigint row_bytes
    from public.ab_docs d cross join state s
    where p_family = 'documents' and s.capability_ok and s.authorized
      and s.source_schema_version = 2
      and d.root = p_root and d.path not like 'presence/%'
      and d.path collate "C" > p_after collate "C"
    union all
    select pg_catalog.split_part(d.path, '/', 2), ''::text, 0::bigint,
           null::bigint, null::boolean, null::bigint, null::text,
           null::text, null::text, null::text,
           (public.ab_source_text_bytes(pg_catalog.split_part(d.path, '/', 2))
            + 256)::bigint
    from public.ab_docs d cross join state s
    where p_family = 'chats' and s.capability_ok and s.authorized
      and s.source_schema_version = 2 and d.root = p_root
      and d.path like 'chats/%/meta.json'
      and d.path = 'chats/' || pg_catalog.split_part(d.path, '/', 2)
                   || '/meta.json'
      and pg_catalog.split_part(d.path, '/', 2) collate "C" > p_after collate "C"
      and public.ab_can_read_chat(p_root, pg_catalog.split_part(d.path, '/', 2))
    union all
    select h.chat_id, h.log_name, 0::bigint,
           null::bigint, null::boolean, h.head,
           null::text, null::text, null::text, null::text,
           (public.ab_source_text_bytes(h.chat_id)
            + public.ab_source_text_bytes(h.log_name) + 256)::bigint
    from public.ab_log_stream_heads h cross join state s
    where p_family = 'streams' and s.capability_ok and s.authorized
      and s.source_schema_version = 2 and h.root = p_root
      and (h.chat_id collate "C", h.log_name collate "C") >
          (p_after collate "C", p_after_log collate "C")
      and public.ab_can_read_chat(p_root, h.chat_id)
    union all
    select ''::text, ''::text, e.id,
           e.doc_head, null::boolean, e.log_head,
           e.stream_kind, e.stream_id, e.domain, e.source_key,
           (public.ab_source_text_bytes(e.stream_id)
            + coalesce(public.ab_source_text_bytes(e.source_key), 0)
            + 256)::bigint
    from public.ab_change_events e cross join state s
    where p_family = 'events' and s.capability_ok and s.authorized
      and s.source_schema_version = 2 and e.root = p_root
      and p_after_id >= s.minimum_cursor and p_after_id <= s.cursor
      and e.id > p_after_id and e.id <= s.cursor
  ), candidates as materialized (
    select c.* from candidate_source c
    order by c.key1 collate "C", c.key2 collate "C", c.event_id
    limit p_limit + 1
  ), ranked as materialized (
    select c.*,
      pg_catalog.row_number() over
        (order by c.key1 collate "C", c.key2 collate "C", c.event_id) rn,
      pg_catalog.sum(c.row_bytes) over
        (order by c.key1 collate "C", c.key2 collate "C", c.event_id
         rows unbounded preceding) total_bytes,
      case p_family
        when 'streams' then pg_catalog.octet_length(pg_catalog.convert_to(
          pg_catalog.jsonb_build_array(c.key1, c.key2)::text, 'UTF8'))
        when 'events' then pg_catalog.octet_length(pg_catalog.convert_to(
          pg_catalog.to_jsonb(c.event_id)::text, 'UTF8'))
        else public.ab_source_text_bytes(c.key1)
      end continuation_bytes
    from candidates c
  ), prefix_checked as materialized (
    select r.*, pg_catalog.bool_and(
      r.rn <= p_limit
      and r.total_bytes + r.continuation_bytes <= p_max_bytes - 1024
      and (p_family not in ('chats','streams')
           or pg_catalog.octet_length(r.key1) between 1 and 1024)
      and (p_family <> 'streams'
           or pg_catalog.octet_length(r.key2) between 1 and 4096)
      and (p_family <> 'documents'
           or pg_catalog.octet_length(r.key1) between 1 and 4096)
      and (p_family <> 'events'
           or (pg_catalog.octet_length(r.stream_id) <= 1024
               and (r.source_key is null
                    or pg_catalog.octet_length(r.source_key) <= 4096)))
    ) over (
      order by r.key1 collate "C", r.key2 collate "C", r.event_id
      rows unbounded preceding
    ) prefix_ok
    from ranked r
  ), selected as materialized (
    select r.* from prefix_checked r where r.prefix_ok
  ), payload as materialized (
    select s.key1, s.key2, s.event_id,
      case p_family
        when 'documents' then pg_catalog.jsonb_build_object(
          'path', d.path, 'seq', s.doc_seq, 'deleted', s.doc_deleted,
          'data', case when s.doc_deleted then null else d.data end)
        when 'chats' then pg_catalog.to_jsonb(s.key1)
        when 'streams' then pg_catalog.jsonb_build_object(
          'chat_id', s.key1, 'log_name', s.key2, 'head', s.log_head)
        else pg_catalog.jsonb_build_object(
          'id', s.event_id, 'stream_kind', s.stream_kind,
          'stream_id', s.stream_id, 'domain', s.domain,
          'source_key', s.source_key, 'doc_head', s.doc_seq,
          'log_head', s.log_head)
      end item
    from selected s
    left join public.ab_docs d on p_family = 'documents'
      and d.root = p_root and d.path = s.key1
  )
  select pg_catalog.jsonb_build_object(
      'cut', case when st.capability_ok and st.authorized
                        and st.source_schema_version = 2 then
        pg_catalog.jsonb_build_object(
          'schema_version', 1, 'index_contract', 'root-manifest-v1',
          'source_schema_version', st.source_schema_version,
          'source_epoch', st.source_epoch, 'account_id', st.account_id,
          'role', st.role, 'minimum_cursor', st.minimum_cursor,
          'cursor', st.cursor) else null end,
      'rows', (select coalesce(pg_catalog.jsonb_agg(p.item
        order by p.key1 collate "C", p.key2 collate "C", p.event_id), '[]'::jsonb)
        from payload p),
      'has_more', (select pg_catalog.count(*) from candidates)
                    > (select pg_catalog.count(*) from selected),
      'empty_terminal', (select pg_catalog.count(*) from candidates) = 0,
      'overflow', (select pg_catalog.count(*) from candidates) > 0
                  and (select pg_catalog.count(*) from selected) = 0,
      'outcome', case
        when (select pg_catalog.count(*) from candidates) = 0 then 'empty_terminal'
        when (select pg_catalog.count(*) from selected) = 0 then 'overflow'
        else 'page' end,
      'continuation', coalesce((
        select case p_family
          when 'streams' then pg_catalog.jsonb_build_array(s.key1, s.key2)
          when 'events' then pg_catalog.to_jsonb(s.event_id)
          else pg_catalog.to_jsonb(s.key1) end
        from selected s
        order by s.key1 collate "C" desc, s.key2 collate "C" desc,
                 s.event_id desc limit 1),
        case p_family
          when 'streams' then pg_catalog.jsonb_build_array(p_after, p_after_log)
          when 'events' then pg_catalog.to_jsonb(p_after_id)
          else pg_catalog.to_jsonb(p_after) end),
      'denied', not st.authorized,
      'unsupported', not st.capability_ok,
      'invalid_source', st.source_schema_version is distinct from 2,
      'compacted', p_family = 'events' and
                   (p_after_id < st.minimum_cursor or p_after_id > st.cursor)
    ) into v_result from state st;
  if v_result->>'denied' = 'true' then
    raise exception 'recovery root is unavailable' using errcode = '42501';
  end if;
  if v_result->>'unsupported' = 'true'
     or v_result->>'invalid_source' = 'true'
     or v_result->>'compacted' = 'true' then
    raise exception 'recovery source cursor/schema is unavailable'
      using errcode = '22023';
  end if;
  return v_result - 'denied' - 'unsupported' - 'invalid_source' - 'compacted';
end
$$;

-- The fence shares the same lock and one post-lock statement. A standalone
-- fence is useful before enumeration and after replay to close a recovery.
create or replace function public.ab_node_recovery_fence(p_root text)
returns jsonb
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
declare
  v_result jsonb;
begin
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024 then
    raise exception 'invalid recovery root' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'recovery requires read committed isolation' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() <> 'authenticated'
     or not public.ab_root_ok(p_root) then
    raise exception 'recovery root is unavailable' using errcode = '42501';
  end if;
  perform pg_catalog.set_config('lock_timeout', case
    when pg_catalog.current_setting('lock_timeout') = '0'
      or pg_catalog.current_setting('lock_timeout')::interval
         > interval '2 seconds' then '2000ms'
    else pg_catalog.current_setting('lock_timeout') end, true);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  with identity as materialized (
    select auth.uid() account_id, auth.role() role,
           public.ab_root_ok(p_root) root_ok
  ), state as materialized (
    select i.account_id, i.role,
           public.ab_node_recovery_ready() = 1 capability_ok,
           (i.account_id is not null and i.role = 'authenticated'
            and i.root_ok) authorized,
           ep.epoch source_epoch, ep.minimum_cursor,
           ep.source_schema_version,
           greatest(ep.minimum_cursor,
             coalesce(tail.id, 0)) cursor
    from identity i
    left join public.ab_change_epochs ep on ep.root = p_root
    left join lateral (
      select e.id from public.ab_change_events e
      where e.root = p_root order by e.id desc limit 1
    ) tail on true
  )
  select pg_catalog.jsonb_build_object(
    'schema_version', 1, 'index_contract', 'root-manifest-v1',
    'source_schema_version', s.source_schema_version,
    'source_epoch', s.source_epoch, 'account_id', s.account_id,
    'role', s.role, 'minimum_cursor', s.minimum_cursor,
    'cursor', s.cursor, 'denied', not s.authorized,
    'unsupported', not s.capability_ok)
  into v_result from state s;
  if v_result->>'denied' = 'true' then
    raise exception 'recovery root is unavailable' using errcode = '42501';
  end if;
  if v_result->>'unsupported' = 'true'
     or (v_result->>'source_schema_version')::integer is distinct from 2 then
    raise exception 'recovery source schema is unavailable' using errcode = '22023';
  end if;
  return v_result - 'denied' - 'unsupported';
end
$$;

-- Existing mutation lanes lack log UPDATE/TRUNCATE evidence and old-root
-- evidence on a document identity remap. Reject these operations so a source
-- cut cannot silently miss them. Normal log deletes still emit root recovery.
create or replace function private.ab_recovery_immutable_identity()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  if tg_table_name = 'ab_logs' then
    raise exception 'source log rows are immutable' using errcode = '22023';
  end if;
  if new.root is distinct from old.root or new.path is distinct from old.path then
    raise exception 'source document identity is immutable' using errcode = '22023';
  end if;
  return new;
end
$$;
drop trigger if exists ab_recovery_doc_identity on public.ab_docs;
create trigger ab_recovery_doc_identity
before update on public.ab_docs for each row
execute function private.ab_recovery_immutable_identity();
drop trigger if exists ab_recovery_log_immutable on public.ab_logs;
create trigger ab_recovery_log_immutable
before update on public.ab_logs for each row
execute function private.ab_recovery_immutable_identity();
-- Remove redundant guard names from the short-lived review schema. The two
-- canonical triggers above remain the sole identity/update guards.
drop trigger if exists ab_recovery_doc_identity_fixed on public.ab_docs;
drop trigger if exists ab_recovery_logs_append_only on public.ab_logs;
drop function if exists private.ab_recovery_doc_identity_fixed();
drop function if exists private.ab_recovery_log_append_only();
create or replace function private.ab_recovery_no_truncate()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  raise exception 'source tables cannot be truncated during recovery'
    using errcode = '22023';
end
$$;
revoke all on function private.ab_recovery_immutable_identity()
  from public, anon, authenticated;
revoke all on function private.ab_recovery_no_truncate()
  from public, anon, authenticated;
drop trigger if exists ab_recovery_docs_no_truncate on public.ab_docs;
create trigger ab_recovery_docs_no_truncate before truncate on public.ab_docs
for each statement execute function private.ab_recovery_no_truncate();
drop trigger if exists ab_recovery_logs_no_truncate on public.ab_logs;
create trigger ab_recovery_logs_no_truncate before truncate on public.ab_logs
for each statement execute function private.ab_recovery_no_truncate();
drop trigger if exists ab_recovery_members_no_truncate on public.ab_members;
create trigger ab_recovery_members_no_truncate before truncate on public.ab_members
for each statement execute function private.ab_recovery_no_truncate();
drop trigger if exists ab_recovery_events_no_truncate on public.ab_change_events;
create trigger ab_recovery_events_no_truncate
before truncate on public.ab_change_events for each statement
execute function private.ab_recovery_no_truncate();

create or replace function private.ab_recovery_no_event_rewrite()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  raise exception 'source events are append-only' using errcode = '22023';
end
$$;
revoke all on function private.ab_recovery_no_event_rewrite()
  from public, anon, authenticated;
drop trigger if exists ab_recovery_events_append_only on public.ab_change_events;
create trigger ab_recovery_events_append_only
before update or delete on public.ab_change_events for each row
execute function private.ab_recovery_no_event_rewrite();

create or replace function private.ab_recovery_member_root_fixed()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  if new.root is distinct from old.root then
    raise exception 'member root is immutable' using errcode = '22023';
  end if;
  return new;
end
$$;
revoke all on function private.ab_recovery_member_root_fixed()
  from public, anon, authenticated;
drop trigger if exists ab_recovery_member_root_fixed on public.ab_members;
create trigger ab_recovery_member_root_fixed
before update on public.ab_members for each row
execute function private.ab_recovery_member_root_fixed();

-- Validate the bounded identities a page must copy before it can advertise
-- v1. Canonical path rules are enforced by current writers and by the bounded
-- page expression below rather than by a validated table constraint: older
-- releases accepted empty/dot segments, and upgrading must not fail merely
-- because one such legacy identity still needs operator-led create/delete
-- migration. Recovery for that root fails closed at the exact legacy row.
alter table public.ab_docs
  drop constraint if exists ab_docs_recovery_path_size;
alter table public.ab_docs add constraint ab_docs_recovery_path_size check (
  pg_catalog.octet_length(path) between 1 and 4096
) not valid;
alter table public.ab_docs validate constraint ab_docs_recovery_path_size;
alter table public.ab_logs
  drop constraint if exists ab_logs_recovery_identity_size;
alter table public.ab_logs add constraint ab_logs_recovery_identity_size check (
  pg_catalog.octet_length(chat_id) between 1 and 1024
  and pg_catalog.octet_length(log_name) between 1 and 4096
) not valid;
alter table public.ab_logs validate constraint ab_logs_recovery_identity_size;

-- Epoch creation must join the same lock protocol as the ordinary
-- document/log/member triggers. Change events are accepted only as nested
-- writes from those already-locked triggers: a direct INSERT allocates its
-- identity before a row trigger can acquire the lock and therefore cannot be
-- safely fenced. Epoch/event rewrite remains disabled until a future retention
-- RPC can rotate the epoch under the exclusive lock and publish its new
-- minimum atomically.
create or replace function private.ab_recovery_source_insert_lock()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  if tg_table_name = 'ab_change_events' and pg_catalog.pg_trigger_depth() < 2 then
    raise exception 'direct source event inserts are unsupported'
      using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || new.root, 0)
  );
  return new;
end
$$;
revoke all on function private.ab_recovery_source_insert_lock()
  from public, anon, authenticated;
drop trigger if exists ab_recovery_event_insert_lock on public.ab_change_events;
create trigger ab_recovery_event_insert_lock
before insert on public.ab_change_events for each row
execute function private.ab_recovery_source_insert_lock();
drop trigger if exists ab_recovery_epoch_insert_lock on public.ab_change_epochs;
create trigger ab_recovery_epoch_insert_lock
before insert on public.ab_change_epochs for each row
execute function private.ab_recovery_source_insert_lock();

create or replace function private.ab_recovery_no_epoch_rewrite()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  raise exception 'source epochs require the versioned retention protocol'
    using errcode = '22023';
end
$$;
revoke all on function private.ab_recovery_no_epoch_rewrite()
  from public, anon, authenticated;
drop trigger if exists ab_recovery_epochs_immutable on public.ab_change_epochs;
create trigger ab_recovery_epochs_immutable
before update or delete on public.ab_change_epochs for each row
execute function private.ab_recovery_no_epoch_rewrite();
drop trigger if exists ab_recovery_epochs_no_truncate on public.ab_change_epochs;
create trigger ab_recovery_epochs_no_truncate
before truncate on public.ab_change_epochs for each statement
execute function private.ab_recovery_no_truncate();

revoke all on function public.ab_node_recovery_page(
  text,text,text,text,bigint,integer,bigint) from public, anon, authenticated;
revoke all on function public.ab_node_recovery_fence(text)
  from public, anon, authenticated;
grant execute on function public.ab_node_recovery_page(
  text,text,text,text,bigint,integer,bigint) to authenticated;
grant execute on function public.ab_node_recovery_fence(text) to authenticated;

-- Marker is installed only after guards, functions and grants succeed. The
-- caller/session timeout must already be armed because setting it inside a
-- function cannot limit the top-level statement currently executing.
create or replace function public.ab_node_recovery_ready() returns integer
language sql stable security invoker set search_path = pg_catalog as $$
  select case
    when auth.role() = 'authenticated' and auth.uid() is not null
      and pg_catalog.current_setting('transaction_isolation') = 'read committed'
      and pg_catalog.current_setting('statement_timeout')::interval
          > interval '0 seconds'
      and pg_catalog.current_setting('statement_timeout')::interval
          <= interval '10 seconds' then 1
    else 0
  end
$$;
revoke all on function public.ab_node_recovery_ready()
  from public, anon, authenticated;
grant execute on function public.ab_node_recovery_ready() to authenticated;

-- ---------------------------------------------------------------------------
-- Independent inactive-reference metadata source, v1. This deliberately does
-- not reuse ab_node_recovery_page: equivalence evidence must observe the same
-- admitted provider rows through a separately implemented path. Payloads are
-- fetched afterward through the exact-read RPCs and every page is bracketed
-- by recovery fences in the client. Returning one jsonb value keeps the
-- server-computed continuation bit intact even when PostgREST has a low
-- maximum-row setting. Current RLS is evaluated on every request.
drop function if exists public.ab_node_reference_ready();

create or replace function public.ab_node_reference_page(
  p_root text, p_family text, p_after text, p_after_log text, p_limit integer
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare
  v_result jsonb;
begin
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or p_family is null or p_family not in ('documents','streams')
     or p_after is null or pg_catalog.octet_length(p_after) > 4096
     or p_after_log is null or pg_catalog.octet_length(p_after_log) > 4096
     or p_limit is null or p_limit < 1 or p_limit > 256
     or (p_family = 'documents' and (p_after_log <> '' or p_limit > 128))
     or (p_family = 'streams' and
         (pg_catalog.octet_length(p_after) > 1024
          or (p_after = '') <> (p_after_log = ''))) then
    raise exception 'invalid reference page request' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() <> 'authenticated'
     or not public.ab_root_ok(p_root) then
    raise exception 'reference root is unavailable' using errcode = '42501';
  end if;

  if p_family = 'documents' then
    with candidates as materialized (
      select d.path, d.seq, d.deleted, d.source_bytes
      from public.ab_docs d
      where d.root = p_root and d.path not like 'presence/%'
        and d.path collate "C" > p_after collate "C"
      order by d.path collate "C"
      limit p_limit + 1
    ), selected as materialized (
      select c.* from candidates c
      order by c.path collate "C" limit p_limit
    )
    select pg_catalog.jsonb_build_object(
      'rows', coalesce((select pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
          'path', s.path, 'seq', s.seq, 'deleted', s.deleted,
          'source_bytes', s.source_bytes
        ) order by s.path collate "C") from selected s), '[]'::jsonb),
      'has_more', (select pg_catalog.count(*) from candidates) > p_limit
    ) into v_result;
  else
    with candidates as materialized (
      select h.chat_id, h.log_name, h.head
      from public.ab_log_stream_heads h
      where h.root = p_root
        and (h.chat_id collate "C", h.log_name collate "C") >
            (p_after collate "C", p_after_log collate "C")
        and public.ab_can_read_chat(p_root, h.chat_id)
      order by h.chat_id collate "C", h.log_name collate "C"
      limit p_limit + 1
    ), selected as materialized (
      select c.* from candidates c
      order by c.chat_id collate "C", c.log_name collate "C" limit p_limit
    )
    select pg_catalog.jsonb_build_object(
      'rows', coalesce((select pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
          'chat_id', s.chat_id, 'log_name', s.log_name, 'head', s.head
        ) order by s.chat_id collate "C", s.log_name collate "C")
        from selected s), '[]'::jsonb),
      'has_more', (select pg_catalog.count(*) from candidates) > p_limit
    ) into v_result;
  end if;
  return v_result;
end
$$;

create or replace function public.ab_node_reference_docs_exact(
  p_root text, p_paths text[], p_max_bytes bigint
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare
  v_count integer;
  v_result jsonb;
begin
  v_count := pg_catalog.cardinality(p_paths);
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or v_count is null or v_count < 1 or v_count > 128
     or p_max_bytes is null or p_max_bytes < 1 or p_max_bytes > 8388608
     or pg_catalog.array_ndims(p_paths) <> 1
     or exists (
       select 1 from pg_catalog.unnest(p_paths) requested(path)
       where requested.path is null or requested.path = ''
          or pg_catalog.octet_length(requested.path) > 4096
     )
     or (select pg_catalog.count(distinct requested.path)
         from pg_catalog.unnest(p_paths) requested(path)) <> v_count then
    raise exception 'invalid reference-document request' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() <> 'authenticated'
     or not public.ab_root_ok(p_root) then
    raise exception 'reference root is unavailable' using errcode = '42501';
  end if;

  with requested(path, ord) as (
    select value, ordinality
    from pg_catalog.unnest(p_paths) with ordinality request(value, ordinality)
  ), candidates as materialized (
    select r.ord, d.path, d.seq, d.deleted, d.source_bytes,
           (public.ab_source_text_bytes(d.path)
            + case when d.deleted then 0 else d.source_bytes end
            + 256)::bigint row_bytes
    from requested r
    join public.ab_docs d on d.root = p_root and d.path = r.path
  ), state as materialized (
    select coalesce(pg_catalog.sum(c.row_bytes), 0)::bigint bytes
    from candidates c
  ), payload as materialized (
    select c.ord, d.path, d.seq, d.deleted, d.source_bytes,
           case when d.deleted then null else d.data end data
    from candidates c cross join state s
    join public.ab_docs d on d.root = p_root and d.path = c.path
    where s.bytes <= p_max_bytes
  )
  select pg_catalog.jsonb_build_object(
    'rows', coalesce((select pg_catalog.jsonb_agg(
      pg_catalog.jsonb_build_object(
        'path', p.path, 'seq', p.seq, 'deleted', p.deleted,
        'source_bytes', p.source_bytes, 'data', p.data
      ) order by p.ord) from payload p), '[]'::jsonb),
    'overflow', s.bytes > p_max_bytes
  ) into v_result from state s;
  return v_result;
end
$$;

create or replace function public.ab_node_reference_logs_exact(
  p_root text, p_chat text, p_log text, p_ids bigint[], p_max_bytes bigint
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare
  v_count integer;
  v_result jsonb;
begin
  v_count := pg_catalog.cardinality(p_ids);
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or p_chat is null or p_chat = '' or pg_catalog.octet_length(p_chat) > 1024
     or p_log is null or p_log = '' or pg_catalog.octet_length(p_log) > 4096
     or v_count is null or v_count < 1 or v_count > 256
     or p_max_bytes is null or p_max_bytes < 1 or p_max_bytes > 8388608
     or pg_catalog.array_ndims(p_ids) <> 1
     or exists (select 1 from pg_catalog.unnest(p_ids) requested(id)
                where requested.id is null or requested.id < 1)
     or (select pg_catalog.count(distinct requested.id)
         from pg_catalog.unnest(p_ids) requested(id)) <> v_count then
    raise exception 'invalid reference-log request' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() <> 'authenticated'
     or not public.ab_root_ok(p_root) then
    raise exception 'reference root is unavailable' using errcode = '42501';
  end if;
  if not public.ab_can_read_chat(p_root, p_chat) then
    return pg_catalog.jsonb_build_object(
      'rows', '[]'::jsonb, 'overflow', false);
  end if;

  with requested(id, ord) as (
    select value, ordinality
    from pg_catalog.unnest(p_ids) with ordinality request(value, ordinality)
  ), candidates as materialized (
    select r.ord, l.id, l.source_bytes,
           (l.source_bytes + 256)::bigint row_bytes
    from requested r join public.ab_logs l on l.id = r.id
    where l.root = p_root and l.chat_id = p_chat and l.log_name = p_log
  ), state as materialized (
    select coalesce(pg_catalog.sum(c.row_bytes), 0)::bigint bytes
    from candidates c
  ), payload as materialized (
    select c.ord, l.id, l.line, l.source_bytes
    from candidates c cross join state s
    join public.ab_logs l on l.id = c.id
    where l.root = p_root and l.chat_id = p_chat and l.log_name = p_log
      and s.bytes <= p_max_bytes
  )
  select pg_catalog.jsonb_build_object(
    'rows', coalesce((select pg_catalog.jsonb_agg(
      pg_catalog.jsonb_build_object(
        'id', p.id, 'line', p.line, 'source_bytes', p.source_bytes
      ) order by p.ord) from payload p), '[]'::jsonb),
    'overflow', s.bytes > p_max_bytes
  ) into v_result from state s;
  return v_result;
end
$$;

revoke all on function public.ab_node_reference_page(
  text,text,text,text,integer) from public, anon, authenticated;
revoke all on function public.ab_node_reference_docs_exact(
  text,text[],bigint) from public, anon, authenticated;
revoke all on function public.ab_node_reference_logs_exact(
  text,text,text,bigint[],bigint) from public, anon, authenticated;
grant execute on function public.ab_node_reference_page(
  text,text,text,text,integer) to authenticated;
grant execute on function public.ab_node_reference_docs_exact(
  text,text[],bigint) to authenticated;
grant execute on function public.ab_node_reference_logs_exact(
  text,text,text,bigint[],bigint) to authenticated;

-- Installed last so partial schema pastes fail closed. As with recovery, the
-- caller must already have a bounded statement timeout.
create or replace function public.ab_node_reference_ready() returns integer
language sql stable security invoker set search_path = pg_catalog as $$
  select case
    when auth.role() = 'authenticated' and auth.uid() is not null
      and pg_catalog.current_setting('statement_timeout')::interval
          > interval '0 seconds'
      and pg_catalog.current_setting('statement_timeout')::interval
          <= interval '10 seconds' then 1
    else 0
  end
$$;
revoke all on function public.ab_node_reference_ready()
  from public, anon, authenticated;
grant execute on function public.ab_node_reference_ready() to authenticated;

-- ---------------------------------------------------------------------------
-- Inactive receipt-bearing mutation protocol (v1). Installing this section
-- keeps every root in legacy-write mode. The administrative seal is not
-- granted to an API role, and current clients do not probe or use this
-- contract yet. A later typed-RPC migration must be complete before sealing.

create table if not exists public.ab_mutation_roots (
  root                  text primary key,
  protocol_epoch        uuid not null default gen_random_uuid(),
  protocol_version      integer not null default 1
                          check (protocol_version = 1),
  legacy_writes_enabled boolean not null default true,
  created_at            timestamptz not null default now(),
  check (pg_catalog.octet_length(root) between 1 and 1024)
);

create table if not exists public.ab_database_mutation_seals (
  root                  text primary key
                          references public.ab_mutation_roots(root),
  seal_id               uuid not null unique,
  plan_digest           text not null
                          check (plan_digest ~ '^[0-9a-f]{64}$'),
  sealed_from_epoch     uuid not null,
  protocol_epoch        uuid not null,
  source_epoch          uuid not null,
  minimum_cursor        bigint not null check (minimum_cursor >= 0),
  cursor                 bigint not null check (cursor >= minimum_cursor),
  sealed_at             timestamptz not null default now()
);

-- No API role can insert a receipt in v1. Typed mutation RPCs added in the
-- next stage will be the only writers and must enforce their active-receipt
-- ceiling before INSERT. Until then public receipt growth is exactly zero.
create table if not exists public.ab_mutation_receipts (
  root            text not null references public.ab_mutation_roots(root),
  protocol_epoch  uuid not null,
  operation_id    uuid not null,
  actor_uid       uuid not null,
  operation_kind  text not null check (operation_kind in (
    'doc_put','doc_compare_put','reaction_pair','meta_append','doc_create','doc_delete','log_append','effect_transition',
    'chat_delete','member_claim','member_leave','blob_manifest'
  )),
  payload_digest  text not null check (payload_digest ~ '^[0-9a-f]{64}$'),
  result          jsonb not null,
  created_at      timestamptz not null default now(),
  primary key (root, protocol_epoch, operation_id),
  check (pg_catalog.octet_length(result::text) <= 16384)
);
create index if not exists ab_mutation_receipts_actor
  on public.ab_mutation_receipts
    (root, protocol_epoch, actor_uid, created_at, operation_id);

alter table public.ab_mutation_roots enable row level security;
alter table public.ab_database_mutation_seals enable row level security;
alter table public.ab_mutation_receipts enable row level security;
revoke all on table public.ab_mutation_roots
  from public, anon, authenticated, service_role;
revoke all on table public.ab_database_mutation_seals
  from public, anon, authenticated, service_role;
revoke all on table public.ab_mutation_receipts
  from public, anon, authenticated, service_role;

-- Every direct source-table mutation joins the source-cut lock before reading
-- the gate. The check must remain after the shared lock: checking first would
-- let a paused old statement commit after an irreversible seal.
create or replace function private.ab_mutation_legacy_gate()
returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
  v_enabled boolean;
begin
  v_root := case when tg_op = 'DELETE' then old.root else new.root end;
  if v_root is null or v_root = ''
     or pg_catalog.octet_length(v_root) > 1024 then
    raise exception 'invalid mutation root' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'legacy mutation gate requires read committed isolation'
      using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
  );
  insert into public.ab_mutation_roots(root) values (v_root)
  on conflict (root) do nothing;
  select r.legacy_writes_enabled into v_enabled
  from public.ab_mutation_roots r where r.root = v_root;
  if v_enabled is distinct from true then
    raise exception 'legacy provider writes are sealed'
      using errcode = '55000';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;
revoke all on function private.ab_mutation_legacy_gate()
  from public, anon, authenticated, service_role;

drop trigger if exists ab_mutation_legacy_gate on public.ab_docs;
create trigger ab_mutation_legacy_gate
before insert or update or delete on public.ab_docs
for each row execute function private.ab_mutation_legacy_gate();
drop trigger if exists ab_mutation_legacy_gate on public.ab_logs;
create trigger ab_mutation_legacy_gate
before insert or update or delete on public.ab_logs
for each row execute function private.ab_mutation_legacy_gate();
drop trigger if exists ab_mutation_legacy_gate on public.ab_members;
create trigger ab_mutation_legacy_gate
before insert or update or delete on public.ab_members
for each row execute function private.ab_mutation_legacy_gate();

-- Existing roots are born in legacy mode. The gate handles a concurrent new
-- root before its first source mutation. A future seal is admin-only and
-- cannot race this backfill because this migration does not invoke it.
insert into public.ab_mutation_roots(root)
select roots.root from (
  select root from public.ab_members
  union select root from public.ab_docs
  union select root from public.ab_logs
  union select root from public.ab_change_epochs
) roots
on conflict (root) do nothing;

-- This irreversible operation is intentionally unavailable through PostgREST.
-- A project administrator supplies the exact local inventory digest and old
-- epoch after every receipt-bearing typed RPC has been installed. The result
-- is an idempotent provider receipt bound to one source-recovery cut.
create or replace function public.ab_admin_seal_legacy_database_mutations(
  p_root text, p_expected_epoch uuid, p_seal_id uuid, p_plan_digest text
) returns jsonb
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
declare
  v_root public.ab_mutation_roots%rowtype;
  v_seal public.ab_database_mutation_seals%rowtype;
  v_source_epoch uuid;
  v_minimum bigint;
  v_cursor bigint;
  v_next_epoch uuid;
begin
  if p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_expected_epoch is null or p_seal_id is null
     or p_plan_digest is null
     or p_plan_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid mutation seal request' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'mutation seal requires read committed isolation'
      using errcode = '22023';
  end if;
  perform pg_catalog.set_config('lock_timeout', case
    when pg_catalog.current_setting('lock_timeout') = '0'
      or pg_catalog.current_setting('lock_timeout')::interval
         > interval '2 seconds' then '2000ms'
    else pg_catalog.current_setting('lock_timeout') end, true);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  insert into public.ab_mutation_roots(root) values (p_root)
  on conflict (root) do nothing;
  select r.* into strict v_root from public.ab_mutation_roots r
  where r.root = p_root for update;
  select s.* into v_seal from public.ab_database_mutation_seals s
  where s.root = p_root;
  if found then
    if v_seal.seal_id is distinct from p_seal_id
       or v_seal.plan_digest is distinct from p_plan_digest
       or v_seal.sealed_from_epoch is distinct from p_expected_epoch
       or v_root.legacy_writes_enabled
       or v_root.protocol_epoch is distinct from v_seal.protocol_epoch then
      raise exception 'mutation root is already sealed by another request'
        using errcode = '55000';
    end if;
    return pg_catalog.jsonb_build_object(
      'schema_version', 1, 'root', v_seal.root,
      'seal_id', v_seal.seal_id,
      'plan_digest', v_seal.plan_digest,
      'sealed_from_epoch', v_seal.sealed_from_epoch,
      'protocol_epoch', v_seal.protocol_epoch,
      'source_epoch', v_seal.source_epoch,
      'minimum_cursor', v_seal.minimum_cursor,
      'cursor', v_seal.cursor, 'seal_scope', 'database-v1',
      'blob_writes_sealed', false
    );
  end if;
  if not v_root.legacy_writes_enabled
     or v_root.protocol_epoch is distinct from p_expected_epoch then
    raise exception 'mutation seal precondition changed' using errcode = '55000';
  end if;
  if not exists (select 1 from public.ab_members m where m.root = p_root) then
    raise exception 'mutation root is unavailable' using errcode = '42501';
  end if;
  select e.epoch, e.minimum_cursor,
         greatest(e.minimum_cursor, coalesce(tail.id, 0))
    into v_source_epoch, v_minimum, v_cursor
  from public.ab_change_epochs e
  left join lateral (
    select c.id from public.ab_change_events c
    where c.root = p_root order by c.id desc limit 1
  ) tail on true
  where e.root = p_root and e.source_schema_version = 2;
  if v_source_epoch is null then
    raise exception 'mutation source cut is unavailable' using errcode = '55000';
  end if;
  v_next_epoch := pg_catalog.gen_random_uuid();
  insert into public.ab_database_mutation_seals(
    root, seal_id, plan_digest, sealed_from_epoch, protocol_epoch,
    source_epoch, minimum_cursor, cursor
  ) values (
    p_root, p_seal_id, p_plan_digest, p_expected_epoch, v_next_epoch,
    v_source_epoch, v_minimum, v_cursor
  );
  update public.ab_mutation_roots
  set protocol_epoch = v_next_epoch, legacy_writes_enabled = false
  where root = p_root and protocol_epoch = p_expected_epoch
    and legacy_writes_enabled;
  if not found then
    raise exception 'mutation seal precondition changed' using errcode = '55000';
  end if;
  return pg_catalog.jsonb_build_object(
    'schema_version', 1, 'root', p_root,
    'seal_id', p_seal_id, 'plan_digest', p_plan_digest,
    'sealed_from_epoch', p_expected_epoch,
    'protocol_epoch', v_next_epoch, 'source_epoch', v_source_epoch,
    'minimum_cursor', v_minimum, 'cursor', v_cursor,
    'seal_scope', 'database-v1', 'blob_writes_sealed', false
  );
end
$$;
revoke all on function public.ab_admin_seal_legacy_database_mutations(
  text,uuid,uuid,text) from public, anon, authenticated, service_role;

-- Authenticated clients may inspect only their own root's mode and exact seal
-- receipt. This is observation, never permission to clear local evidence.
create or replace function public.ab_mutation_protocol_state(p_root text)
returns jsonb
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_result jsonb;
begin
  if p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024 then
    raise exception 'invalid mutation root' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() <> 'authenticated'
     or not public.ab_root_ok(p_root) then
    raise exception 'mutation root is unavailable' using errcode = '42501';
  end if;
  select pg_catalog.jsonb_build_object(
    'schema_version', r.protocol_version,
    'account_id', auth.uid(), 'role', auth.role(),
    'root', r.root, 'protocol_epoch', r.protocol_epoch,
    'legacy_writes_enabled', r.legacy_writes_enabled,
    'database_seal', case when s.root is null then null else
      pg_catalog.jsonb_build_object(
        'seal_id', s.seal_id, 'plan_digest', s.plan_digest,
        'sealed_from_epoch', s.sealed_from_epoch,
        'protocol_epoch', s.protocol_epoch,
        'source_epoch', s.source_epoch,
        'minimum_cursor', s.minimum_cursor, 'cursor', s.cursor,
        'seal_scope', 'database-v1'
      ) end,
    'blob_writes_sealed', false
  ) into v_result
  from public.ab_mutation_roots r
  left join public.ab_database_mutation_seals s on s.root = r.root
  where r.root = p_root;
  if v_result is null then
    raise exception 'mutation protocol is unavailable' using errcode = '55000';
  end if;
  return v_result;
end
$$;
revoke all on function public.ab_mutation_protocol_state(text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_protocol_state(text)
  to authenticated;

-- Marker installed last: a partial paste cannot advertise the contract.
create or replace function public.ab_mutation_protocol_ready() returns integer
language sql stable security invoker set search_path = pg_catalog as $$
  select case
    when auth.role() = 'authenticated' and auth.uid() is not null
      and pg_catalog.current_setting('statement_timeout')::interval
          > interval '0 seconds'
      and pg_catalog.current_setting('statement_timeout')::interval
          <= interval '10 seconds' then 1
    else 0
  end
$$;
revoke all on function public.ab_mutation_protocol_ready()
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_protocol_ready()
  to authenticated;

-- ---------------------------------------------------------------------------
-- P1b core: bounded typed-mutation receipts and sealed-write admission.
--
-- This installs no typed source-mutation RPC and no typed-mutation readiness
-- marker.  Legacy writes therefore remain the only application path until all
-- operation families and the durable local journal have landed.  The private
-- helpers below are exercised directly only by disposable database gates.

alter table public.ab_mutation_roots
  drop constraint if exists ab_mutation_roots_protocol_version_check;
alter table public.ab_mutation_roots
  add constraint ab_mutation_roots_protocol_version_check
  check (protocol_version in (1, 2));
alter table public.ab_mutation_roots
  add column if not exists previous_protocol_epoch uuid;
alter table public.ab_mutation_roots
  add column if not exists rotation_id uuid;
alter table public.ab_mutation_roots
  add column if not exists rotated_at timestamptz;
-- Stable lineage plus monotonic closure counter: UUID epochs alone cannot
-- provide a bounded local retirement floor across multiple rotations.
alter table public.ab_mutation_roots
  add column if not exists receipt_lifecycle_id uuid not null
  default pg_catalog.gen_random_uuid();
alter table public.ab_mutation_roots
  add column if not exists receipt_generation bigint not null default 0
  check (receipt_generation >= 0);
create unique index if not exists ab_mutation_roots_rotation_id
  on public.ab_mutation_roots(rotation_id) where rotation_id is not null;

alter table public.ab_mutation_receipts
  add column if not exists provider_digest text;
alter table public.ab_mutation_receipts
  add column if not exists receipt_slot integer;
alter table public.ab_mutation_receipts
  add column if not exists receipt_state text;
alter table public.ab_mutation_receipts
  add column if not exists applying_xid xid8;
alter table public.ab_mutation_receipts
  add column if not exists acknowledged_at timestamptz;

-- P1a exposes no receipt writer, so a legitimate upgrade reaches this block
-- with an empty table.  Refuse to invent P1b identity for any out-of-contract
-- row an administrator may have inserted manually.
do $$
begin
  if exists (
    select 1 from public.ab_mutation_receipts r
    where r.provider_digest is null or r.receipt_slot is null
       or r.receipt_state is null
  ) then
    raise exception 'legacy mutation receipt requires operator reconciliation'
      using errcode = '55000';
  end if;
end
$$;
alter table public.ab_mutation_receipts
  alter column provider_digest set not null;
alter table public.ab_mutation_receipts
  alter column receipt_slot set not null;
alter table public.ab_mutation_receipts
  alter column receipt_state set not null;
alter table public.ab_mutation_receipts
  drop constraint if exists ab_mutation_receipts_provider_digest_check;
alter table public.ab_mutation_receipts
  add constraint ab_mutation_receipts_provider_digest_check
  check (provider_digest ~ '^[0-9a-f]{64}$');
alter table public.ab_mutation_receipts
  drop constraint if exists ab_mutation_receipts_slot_check;
alter table public.ab_mutation_receipts
  add constraint ab_mutation_receipts_slot_check
  check (receipt_slot between 0 and 4095);
alter table public.ab_mutation_receipts
  drop constraint if exists ab_mutation_receipts_state_check;
alter table public.ab_mutation_receipts
  add constraint ab_mutation_receipts_state_check check (
    (receipt_state = 'applying' and applying_xid is not null
      and acknowledged_at is null)
    or (receipt_state = 'terminal' and applying_xid is null)
  );
create unique index if not exists ab_mutation_receipts_slot
  on public.ab_mutation_receipts(root, protocol_epoch, receipt_slot);
create unique index if not exists ab_mutation_receipts_applying_xid
  on public.ab_mutation_receipts(root, applying_xid)
  where receipt_state = 'applying';

create or replace function private.ab_mutation_kind_permits(
  p_kind text, p_table text, p_operation text
) returns boolean
language sql immutable set search_path = pg_catalog as $$
  select case p_kind
    when 'doc_put' then p_table = 'ab_docs'
      and p_operation in ('INSERT', 'UPDATE')
    when 'doc_compare_put' then p_table = 'ab_docs'
      and p_operation in ('INSERT', 'UPDATE')
    when 'reaction_pair' then p_table = 'ab_docs'
      and p_operation in ('INSERT', 'UPDATE')
    when 'meta_append' then
      (p_table = 'ab_docs' and p_operation = 'UPDATE')
      or (p_table = 'ab_logs' and p_operation = 'INSERT')
    when 'doc_create' then p_table = 'ab_docs' and p_operation = 'INSERT'
    when 'doc_delete' then p_table = 'ab_docs'
      and p_operation in ('UPDATE', 'DELETE')
    when 'log_append' then p_table = 'ab_logs' and p_operation = 'INSERT'
    when 'effect_transition' then p_table = 'ab_docs'
      and p_operation = 'INSERT'
    when 'chat_delete' then
      (p_table = 'ab_logs' and p_operation = 'DELETE')
      or (p_table = 'ab_docs' and p_operation in ('UPDATE', 'DELETE'))
    when 'member_claim' then p_table = 'ab_members' and p_operation = 'INSERT'
    when 'member_leave' then p_table = 'ab_members' and p_operation = 'UPDATE'
    when 'blob_manifest' then p_table = 'ab_blob_manifests'
      and p_operation = 'INSERT'
    else false
  end
$$;
revoke all on function private.ab_mutation_kind_permits(text,text,text)
  from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_receipt_json(
  p_receipt public.ab_mutation_receipts
) returns jsonb
language sql stable set search_path = pg_catalog as $$
  select pg_catalog.jsonb_build_object(
    'schema_version', 2,
    'root', p_receipt.root,
    'protocol_epoch', p_receipt.protocol_epoch,
    'operation_id', p_receipt.operation_id,
    'actor_id', p_receipt.actor_uid,
    'kind', p_receipt.operation_kind,
    'client_digest', p_receipt.payload_digest,
    'provider_digest', p_receipt.provider_digest,
    'result', p_receipt.result
  )
$$;
revoke all on function private.ab_mutation_receipt_json(
  public.ab_mutation_receipts) from public, anon, authenticated, service_role;

-- Prospective owner admission is explicitly disabled until an operator gates a
-- sealed empty epoch. Every admitted leg holds one receipt slot until rotation;
-- cancellation never frees capacity early or changes the terminal receipt wire.
alter table public.ab_mutation_roots
  add column if not exists admission_required boolean not null default false;
create table if not exists public.ab_mutation_admission_groups (
  root text not null references public.ab_mutation_roots(root),
  protocol_epoch uuid not null,
  owner_id uuid not null,
  actor_uid uuid not null,
  primary key (root, protocol_epoch, owner_id)
);
create table if not exists public.ab_mutation_admission_legs (
  root text not null,
  protocol_epoch uuid not null,
  owner_id uuid not null,
  operation_id uuid not null,
  ordinal integer not null check (ordinal between 1 and 1025),
  operation_kind text not null check (operation_kind in (
    'doc_put','doc_compare_put','reaction_pair','meta_append','doc_create','doc_delete','log_append',
    'effect_transition','chat_delete','member_claim','member_leave','blob_manifest')),
  client_digest text not null check (client_digest ~ '^[0-9a-f]{64}$'),
  receipt_slot integer not null check (receipt_slot between 0 and 4095),
  cancelled boolean not null default false,
  cancel_acknowledged boolean not null default false,
  check (not cancel_acknowledged or cancelled),
  primary key (root, protocol_epoch, operation_id),
  unique (root, protocol_epoch, receipt_slot),
  unique (root, protocol_epoch, owner_id, ordinal),
  foreign key (root, protocol_epoch, owner_id)
    references public.ab_mutation_admission_groups(root, protocol_epoch, owner_id)
    on delete cascade
);
alter table public.ab_mutation_admission_groups enable row level security;
alter table public.ab_mutation_admission_legs enable row level security;
revoke all on table public.ab_mutation_admission_groups
  from public, anon, authenticated, service_role;
revoke all on table public.ab_mutation_admission_legs
  from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_begin(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_operation_kind text, p_payload_digest text, p_provider_digest text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_root public.ab_mutation_roots%rowtype;
  v_receipt public.ab_mutation_receipts%rowtype;
  v_base integer;
  v_slot integer;
  v_probe integer;
  v_count integer;
  v_leg record;
begin
  if p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_protocol_epoch is null or p_operation_id is null
     or p_operation_kind is null or p_operation_kind not in (
       'doc_put','doc_compare_put','reaction_pair','meta_append','doc_create','doc_delete','log_append','effect_transition',
       'chat_delete','member_claim','member_leave','blob_manifest'
     )
     or p_payload_digest is null
     or p_payload_digest !~ '^[0-9a-f]{64}$'
     or p_provider_digest is null
     or p_provider_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid typed mutation request' using errcode = '22023';
  end if;
  if v_actor is null then
    raise exception 'typed mutation principal is unavailable'
      using errcode = '42501';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'typed mutation requires read committed isolation'
      using errcode = '22023';
  end if;

  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended(
      'agentbridge-mutation-epoch:' || p_root, 0)
  );
  select r.* into v_root from public.ab_mutation_roots r
  where r.root = p_root;
  if v_root.root is null
     or v_root.protocol_epoch is distinct from p_protocol_epoch then
    raise exception 'typed mutation epoch is stale' using errcode = '55000';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-mutation-operation:' || p_root || ':'
      || p_protocol_epoch::text || ':' || p_operation_id::text, 0)
  );
  select r.* into v_receipt from public.ab_mutation_receipts r
  where r.root = p_root and r.protocol_epoch = p_protocol_epoch
    and r.operation_id = p_operation_id;
  if found then
    if v_receipt.actor_uid is distinct from v_actor
       or v_receipt.operation_kind is distinct from p_operation_kind
       or v_receipt.payload_digest is distinct from p_payload_digest
       or v_receipt.provider_digest is distinct from p_provider_digest
       or v_receipt.receipt_state is distinct from 'terminal' then
      raise exception 'typed mutation operation collides with existing receipt'
        using errcode = '55000';
    end if;
    return pg_catalog.jsonb_build_object(
      'replay', true,
      'receipt', private.ab_mutation_receipt_json(v_receipt)
    );
  end if;

  if v_root.admission_required then
    -- Admission is optional per owner. Shared, this mutex excludes only an
    -- admission (held exclusive) from this lookup through commit, so an
    -- operation is either admitted or receipted unadmitted, never both, and
    -- admission counts every slot taken here. Order: epoch, operation, mutex.
    perform pg_catalog.pg_advisory_xact_lock_shared(
      pg_catalog.hashtextextended('agentbridge-mutation-admission:' || p_root, 0));
    select l.*, g.actor_uid into v_leg from public.ab_mutation_admission_legs l
      join public.ab_mutation_admission_groups g using(root, protocol_epoch, owner_id)
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.operation_id = p_operation_id;
    if found then
      -- An admitted operation runs only through its exact uncancelled leg;
      -- it never falls back to an unadmitted slot.
      if v_leg.actor_uid is distinct from v_actor
         or v_leg.operation_kind is distinct from p_operation_kind
         or v_leg.client_digest is distinct from p_payload_digest or v_leg.cancelled then
        raise exception 'typed mutation requires matching uncancelled admission' using errcode = '55000';
      end if;
      insert into public.ab_mutation_receipts(root, protocol_epoch, operation_id, actor_uid,
        operation_kind, payload_digest, provider_digest, receipt_slot, receipt_state, applying_xid, result)
      values (p_root, p_protocol_epoch, p_operation_id, v_actor, p_operation_kind,
        p_payload_digest, p_provider_digest, v_leg.receipt_slot, 'applying',
        pg_catalog.pg_current_xact_id(), '{}'::jsonb) returning * into v_receipt;
      return pg_catalog.jsonb_build_object('replay', false,
        'receipt', private.ab_mutation_receipt_json(v_receipt));
    end if;
  end if;

  v_base := (pg_catalog.hashtextextended(p_operation_id::text, 0) & 4095)::integer;
  for v_probe in 0..4095 loop
    v_slot := (v_base + v_probe) % 4096;
    -- Do not accumulate one heavyweight transaction lock per immutable
    -- occupied slot in a near-full epoch.  The first read skips settled rows;
    -- the second closes the race after the nonblocking candidate lock.
    -- Admitted owners' reserved slots are never taken; they cannot change
    -- while the shared admission mutex above is held.
    if exists (
      select 1 from public.ab_mutation_receipts r
      where r.root = p_root and r.protocol_epoch = p_protocol_epoch
        and r.receipt_slot = v_slot
    ) or exists (
      select 1 from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch
        and l.receipt_slot = v_slot
    ) then
      continue;
    end if;
    begin
      if not pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
          'agentbridge-mutation-slot:' || p_root || ':'
          || p_protocol_epoch::text || ':' || v_slot::text, 0)
      ) then
        continue;
      end if;
      if exists (
        select 1 from public.ab_mutation_receipts r
        where r.root = p_root and r.protocol_epoch = p_protocol_epoch
          and r.receipt_slot = v_slot
      ) then
        -- Roll back this inner block so the race-lost transaction advisory
        -- lock is released instead of accumulating until outer commit.
        raise unique_violation;
      end if;
      insert into public.ab_mutation_receipts(
        root, protocol_epoch, operation_id, actor_uid, operation_kind,
        payload_digest, provider_digest, receipt_slot, receipt_state,
        applying_xid, result
      ) values (
        p_root, p_protocol_epoch, p_operation_id, v_actor, p_operation_kind,
        p_payload_digest, p_provider_digest, v_slot, 'applying',
        pg_catalog.pg_current_xact_id(), '{}'::jsonb
      ) returning * into v_receipt;
      return pg_catalog.jsonb_build_object(
        'replay', false,
        'receipt', private.ab_mutation_receipt_json(v_receipt)
      );
    exception when unique_violation then
      null;
    end;
  end loop;

  -- Reserved admission slots are occupied too; only a free slot held by an
  -- in-flight writer is contention.
  select pg_catalog.count(*) into v_count from (
    select r.receipt_slot from public.ab_mutation_receipts r
    where r.root = p_root and r.protocol_epoch = p_protocol_epoch
    union
    select l.receipt_slot from public.ab_mutation_admission_legs l
    where l.root = p_root and l.protocol_epoch = p_protocol_epoch) occupied;
  if v_count >= 4096 then
    raise exception 'typed mutation receipt epoch is full'
      using errcode = '54000';
  end if;
  raise exception 'typed mutation receipt slots are contended'
    using errcode = '40001';
end
$$;
revoke all on function private.ab_mutation_begin(
  text,uuid,uuid,text,text,text) from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_finish(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_operation_kind text, p_payload_digest text, p_provider_digest text,
  p_result jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_receipt public.ab_mutation_receipts%rowtype;
begin
  if v_actor is null or p_result is null
     or pg_catalog.octet_length(p_result::text) > 16384 then
    raise exception 'invalid typed mutation result' using errcode = '22023';
  end if;
  update public.ab_mutation_receipts r
  set receipt_state = 'terminal', applying_xid = null, result = p_result
  where r.root = p_root and r.protocol_epoch = p_protocol_epoch
    and r.operation_id = p_operation_id and r.actor_uid = v_actor
    and r.operation_kind = p_operation_kind
    and r.payload_digest = p_payload_digest
    and r.provider_digest = p_provider_digest
    and r.receipt_state = 'applying'
    and r.applying_xid = pg_catalog.pg_current_xact_id()
  returning * into v_receipt;
  if not found then
    raise exception 'typed mutation applying receipt changed'
      using errcode = '55000';
  end if;
  return private.ab_mutation_receipt_json(v_receipt);
end
$$;
revoke all on function private.ab_mutation_finish(
  text,uuid,uuid,text,text,text,jsonb)
  from public, anon, authenticated, service_role;

-- Replace the P1a gate with a same-transaction receipt-aware gate.  A source
-- write still joins the change-ledger lock before the post-lock mode check.
create or replace function private.ab_mutation_legacy_gate()
returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
  v_epoch uuid;
  v_enabled boolean;
  v_kind text;
begin
  v_root := case when tg_op = 'DELETE' then old.root else new.root end;
  if v_root is null or v_root = ''
     or pg_catalog.octet_length(v_root) > 1024 then
    raise exception 'invalid mutation root' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'legacy mutation gate requires read committed isolation'
      using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
  );
  insert into public.ab_mutation_roots(root) values (v_root)
  on conflict (root) do nothing;
  select r.protocol_epoch, r.legacy_writes_enabled into v_epoch, v_enabled
  from public.ab_mutation_roots r where r.root = v_root;
  if v_enabled is distinct from true then
    select r.operation_kind into v_kind
    from public.ab_mutation_receipts r
    where r.root = v_root and r.protocol_epoch = v_epoch
      and r.receipt_state = 'applying'
      and r.applying_xid = pg_catalog.pg_current_xact_id();
    if v_kind is null
       or not private.ab_mutation_kind_permits(
         v_kind, tg_table_name, tg_op) then
      raise exception 'legacy provider writes are sealed'
        using errcode = '55000';
    end if;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;
revoke all on function private.ab_mutation_legacy_gate()
  from public, anon, authenticated, service_role;

-- The mutation-epoch lock precedes the change-ledger lock.  Exact replay is
-- bound to the immutable seal receipt and remains valid after P1b rotations.
create or replace function public.ab_admin_seal_legacy_database_mutations(
  p_root text, p_expected_epoch uuid, p_seal_id uuid, p_plan_digest text
) returns jsonb
language plpgsql volatile security invoker set search_path = pg_catalog, public as $$
declare
  v_root public.ab_mutation_roots%rowtype;
  v_seal public.ab_database_mutation_seals%rowtype;
  v_source_epoch uuid;
  v_minimum bigint;
  v_cursor bigint;
  v_next_epoch uuid;
begin
  if p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_expected_epoch is null or p_seal_id is null
     or p_plan_digest is null
     or p_plan_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid mutation seal request' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'mutation seal requires read committed isolation'
      using errcode = '22023';
  end if;
  perform pg_catalog.set_config('lock_timeout', case
    when pg_catalog.current_setting('lock_timeout') = '0'
      or pg_catalog.current_setting('lock_timeout')::interval
         > interval '2 seconds' then '2000ms'
    else pg_catalog.current_setting('lock_timeout') end, true);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-mutation-epoch:' || p_root, 0)
  );
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  insert into public.ab_mutation_roots(root) values (p_root)
  on conflict (root) do nothing;
  select r.* into strict v_root from public.ab_mutation_roots r
  where r.root = p_root for update;
  select s.* into v_seal from public.ab_database_mutation_seals s
  where s.root = p_root;
  if found then
    if v_seal.seal_id is distinct from p_seal_id
       or v_seal.plan_digest is distinct from p_plan_digest
       or v_seal.sealed_from_epoch is distinct from p_expected_epoch
       or v_root.legacy_writes_enabled then
      raise exception 'mutation root is already sealed by another request'
        using errcode = '55000';
    end if;
    return pg_catalog.jsonb_build_object(
      'schema_version', 1, 'root', v_seal.root,
      'seal_id', v_seal.seal_id,
      'plan_digest', v_seal.plan_digest,
      'sealed_from_epoch', v_seal.sealed_from_epoch,
      'protocol_epoch', v_seal.protocol_epoch,
      'source_epoch', v_seal.source_epoch,
      'minimum_cursor', v_seal.minimum_cursor,
      'cursor', v_seal.cursor, 'seal_scope', 'database-v1',
      'blob_writes_sealed', false
    );
  end if;
  if not v_root.legacy_writes_enabled
     or v_root.protocol_epoch is distinct from p_expected_epoch then
    raise exception 'mutation seal precondition changed' using errcode = '55000';
  end if;
  if exists (
    select 1 from public.ab_mutation_receipts r
    where r.root = p_root and r.protocol_epoch = p_expected_epoch
  ) then
    raise exception 'mutation seal has retained typed receipts'
      using errcode = '55000';
  end if;
  if not exists (select 1 from public.ab_members m where m.root = p_root) then
    raise exception 'mutation root is unavailable' using errcode = '42501';
  end if;
  select e.epoch, e.minimum_cursor,
         greatest(e.minimum_cursor, coalesce(tail.id, 0))
    into v_source_epoch, v_minimum, v_cursor
  from public.ab_change_epochs e
  left join lateral (
    select c.id from public.ab_change_events c
    where c.root = p_root order by c.id desc limit 1
  ) tail on true
  where e.root = p_root and e.source_schema_version = 2;
  if v_source_epoch is null then
    raise exception 'mutation source cut is unavailable' using errcode = '55000';
  end if;
  v_next_epoch := pg_catalog.gen_random_uuid();
  insert into public.ab_database_mutation_seals(
    root, seal_id, plan_digest, sealed_from_epoch, protocol_epoch,
    source_epoch, minimum_cursor, cursor
  ) values (
    p_root, p_seal_id, p_plan_digest, p_expected_epoch, v_next_epoch,
    v_source_epoch, v_minimum, v_cursor
  );
  update public.ab_mutation_roots
  set protocol_epoch = v_next_epoch, legacy_writes_enabled = false
  where root = p_root and protocol_epoch = p_expected_epoch
    and legacy_writes_enabled;
  if not found then
    raise exception 'mutation seal precondition changed' using errcode = '55000';
  end if;
  return pg_catalog.jsonb_build_object(
    'schema_version', 1, 'root', p_root,
    'seal_id', p_seal_id, 'plan_digest', p_plan_digest,
    'sealed_from_epoch', p_expected_epoch,
    'protocol_epoch', v_next_epoch, 'source_epoch', v_source_epoch,
    'minimum_cursor', v_minimum, 'cursor', v_cursor,
    'seal_scope', 'database-v1', 'blob_writes_sealed', false
  );
end
$$;
revoke all on function public.ab_admin_seal_legacy_database_mutations(
  text,uuid,uuid,text) from public, anon, authenticated, service_role;

create or replace function public.ab_mutation_receipt(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_provider_digest text
) returns jsonb
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_receipt public.ab_mutation_receipts%rowtype;
begin
  if v_actor is null or p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_protocol_epoch is null or p_operation_id is null
     or p_payload_digest is null
     or p_payload_digest !~ '^[0-9a-f]{64}$'
     or p_provider_digest is null
     or p_provider_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid mutation receipt request' using errcode = '22023';
  end if;
  select r.* into v_receipt from public.ab_mutation_receipts r
  where r.root = p_root and r.protocol_epoch = p_protocol_epoch
    and r.operation_id = p_operation_id and r.actor_uid = v_actor
    and r.payload_digest = p_payload_digest
    and r.provider_digest = p_provider_digest
    and r.receipt_state = 'terminal';
  if not found then
    raise exception 'mutation receipt is unavailable' using errcode = '55000';
  end if;
  return private.ab_mutation_receipt_json(v_receipt);
end
$$;
revoke all on function public.ab_mutation_receipt(
  text,uuid,uuid,text,text) from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_receipt(
  text,uuid,uuid,text,text) to authenticated;

create or replace function public.ab_ack_mutation_receipt(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_provider_digest text
) returns boolean
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
begin
  if v_actor is null or p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_protocol_epoch is null or p_operation_id is null
     or p_payload_digest is null
     or p_payload_digest !~ '^[0-9a-f]{64}$'
     or p_provider_digest is null
     or p_provider_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid mutation receipt acknowledgment'
      using errcode = '22023';
  end if;
  update public.ab_mutation_receipts r
  set acknowledged_at = coalesce(r.acknowledged_at, pg_catalog.clock_timestamp())
  where r.root = p_root and r.protocol_epoch = p_protocol_epoch
    and r.operation_id = p_operation_id and r.actor_uid = v_actor
    and r.payload_digest = p_payload_digest
    and r.provider_digest = p_provider_digest
    and r.receipt_state = 'terminal';
  if not found then
    raise exception 'mutation receipt is unavailable' using errcode = '55000';
  end if;
  return true;
end
$$;
revoke all on function public.ab_ack_mutation_receipt(
  text,uuid,uuid,text,text) from public, anon, authenticated, service_role;
grant execute on function public.ab_ack_mutation_receipt(
  text,uuid,uuid,text,text) to authenticated;

create or replace function public.ab_admin_rotate_mutation_epoch(
  p_root text, p_expected_epoch uuid, p_rotation_id uuid
) returns jsonb
language plpgsql volatile security invoker set search_path = pg_catalog as $$
declare
  v_root public.ab_mutation_roots%rowtype;
  v_next_epoch uuid;
begin
  if p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_expected_epoch is null or p_rotation_id is null then
    raise exception 'invalid mutation epoch rotation' using errcode = '22023';
  end if;
  if pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'mutation rotation requires read committed isolation'
      using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-mutation-epoch:' || p_root, 0)
  );
  select r.* into v_root from public.ab_mutation_roots r
  where r.root = p_root for update;
  if v_root.root is null then
    raise exception 'mutation root is unavailable' using errcode = '55000';
  end if;
  if v_root.protocol_epoch is distinct from p_expected_epoch then
    if v_root.previous_protocol_epoch = p_expected_epoch
       and v_root.rotation_id = p_rotation_id then
      return pg_catalog.jsonb_build_object(
        'schema_version', 2, 'root', p_root,
        'previous_protocol_epoch', p_expected_epoch,
        'protocol_epoch', v_root.protocol_epoch,
        'rotation_id', p_rotation_id
      );
    end if;
    raise exception 'mutation rotation precondition changed'
      using errcode = '55000';
  end if;
  if v_root.legacy_writes_enabled
     or not exists (
       select 1 from public.ab_database_mutation_seals s
       where s.root = p_root
     ) then
    raise exception 'mutation root is not sealed' using errcode = '55000';
  end if;
  if exists (
    select 1 from public.ab_mutation_receipts r
    where r.root = p_root and r.protocol_epoch = p_expected_epoch
      and (r.receipt_state <> 'terminal' or r.acknowledged_at is null)
  ) then
    raise exception 'mutation receipts are not fully acknowledged'
      using errcode = '55000';
  end if;
  if exists (select 1 from public.ab_mutation_admission_legs l
      join public.ab_mutation_admission_groups g using(root, protocol_epoch, owner_id)
      left join public.ab_mutation_receipts r on r.root=l.root
        and r.protocol_epoch=l.protocol_epoch and r.operation_id=l.operation_id
      where l.root=p_root and l.protocol_epoch=p_expected_epoch
        and ((l.cancelled and l.cancel_acknowledged and r.operation_id is null)
          or (not l.cancelled and r.receipt_state='terminal' and r.acknowledged_at is not null
            and r.actor_uid=g.actor_uid and r.operation_kind=l.operation_kind
            and r.payload_digest=l.client_digest and r.receipt_slot=l.receipt_slot)) is not true) then
    raise exception 'mutation admission legs are not settled and acknowledged' using errcode = '55000';
  end if;
  if v_root.receipt_generation = 9223372036854775807 then
    raise exception 'mutation epoch generation exhausted' using errcode = '54000';
  end if;
  delete from public.ab_mutation_receipts r
  where r.root = p_root and r.protocol_epoch = p_expected_epoch;
  delete from public.ab_mutation_admission_groups g
    where g.root = p_root and g.protocol_epoch = p_expected_epoch;
  v_next_epoch := pg_catalog.gen_random_uuid();
  update public.ab_mutation_roots
  set previous_protocol_epoch = p_expected_epoch,
      protocol_epoch = v_next_epoch,
      protocol_version = 2,
      receipt_generation = receipt_generation + 1,
      rotation_id = p_rotation_id,
      rotated_at = pg_catalog.clock_timestamp()
  where root = p_root and protocol_epoch = p_expected_epoch;
  if not found then
    raise exception 'mutation rotation precondition changed'
      using errcode = '55000';
  end if;
  return pg_catalog.jsonb_build_object(
    'schema_version', 2, 'root', p_root,
    'previous_protocol_epoch', p_expected_epoch,
    'protocol_epoch', v_next_epoch,
    'rotation_id', p_rotation_id
  );
end
$$;
revoke all on function public.ab_admin_rotate_mutation_epoch(text,uuid,uuid)
  from public, anon, authenticated, service_role;

-- Authenticated observation of receipt closure, not membership authority.
-- Every increase in this lineage is produced only by the all-ACK rotation
-- above. Removed members can recover their own local completed/ACK-lost work;
-- Only nonsecret protocol metadata for the supplied root is exposed; neither
-- the root label nor this observation grants membership or source authority.
create or replace function public.ab_mutation_epoch_state(p_root text)
returns jsonb
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_result jsonb;
begin
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024 then
    raise exception 'invalid mutation root' using errcode = '22023';
  end if;
  if auth.uid() is null or auth.role() is distinct from 'authenticated' then
    raise exception 'mutation root is unavailable' using errcode = '42501';
  end if;
  select pg_catalog.jsonb_build_object(
    'schema_version', 1, 'root', r.root, 'actor_id', auth.uid(),
    'role', auth.role(), 'lifecycle_id', r.receipt_lifecycle_id,
    'protocol_epoch', r.protocol_epoch, 'generation', r.receipt_generation,
    'sealed', true
  ) into v_result
  from public.ab_mutation_roots r
  where r.root = p_root and not r.legacy_writes_enabled
    and exists (select 1 from public.ab_database_mutation_seals s where s.root = r.root);
  if v_result is null then
    raise exception 'sealed mutation lifecycle is unavailable' using errcode = '55000';
  end if;
  return v_result;
end
$$;
revoke all on function public.ab_mutation_epoch_state(text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_epoch_state(text) to authenticated;

-- ---------------------------------------------------------------------------
-- P1b document/log slice: inactive receipt-bearing source mutation RPCs.
--
-- These functions are executable protocol evidence, but they do not install a
-- readiness marker and the application does not call them yet.  Every entry
-- point resolves an exact terminal replay before current authority checks.  A
-- genuinely new operation creates its applying receipt, revalidates authority,
-- performs exactly one typed source mutation and finalizes the receipt in the
-- same transaction.

create or replace function private.ab_mutation_provider_digest(p_request jsonb)
returns text
language sql immutable set search_path = pg_catalog as $$
  select pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(p_request::text, 'UTF8')), 'hex'
  )
$$;
revoke all on function private.ab_mutation_provider_digest(jsonb)
  from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_path_valid(
  p_value text, p_max_bytes integer, p_max_segments integer
) returns boolean
language sql immutable set search_path = pg_catalog as $$
  select p_value is not null
    and p_value <> ''
    and pg_catalog.octet_length(p_value) between 1 and p_max_bytes
    and pg_catalog.left(p_value, 1) <> '/'
    and pg_catalog.right(p_value, 1) <> '/'
    and pg_catalog.strpos(p_value, E'\\') = 0
    and pg_catalog.cardinality(pg_catalog.string_to_array(p_value, '/'))
        between 1 and p_max_segments
    and not ('/' || p_value || '/') like '%//%'
    and not exists (
      select 1
      from pg_catalog.unnest(pg_catalog.string_to_array(p_value, '/')) part
      where part in ('.', '..')
    )
$$;
revoke all on function private.ab_mutation_path_valid(text,integer,integer)
  from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_root_sealed(p_root text)
returns boolean
language sql stable security definer set search_path = pg_catalog as $$
  select exists (
    select 1
    from public.ab_mutation_roots r
    join public.ab_database_mutation_seals s on s.root = r.root
    where r.root = p_root and not r.legacy_writes_enabled
  )
$$;
revoke all on function private.ab_mutation_root_sealed(text)
  from public, anon, authenticated, service_role;

-- Fresh typed authority remains stable until the effect commits.  Root-member
-- rows are locked before room locks; room source writes share one lock while
-- meta authority changes and terminal cleanup take it exclusively.  Every
-- waiter rechecks authority only after acquiring the room lock.
create or replace function private.ab_mutation_lock_actor(p_root text)
returns text
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_member text;
begin
  -- A revoked row is no member. A concurrent leave holds FOR UPDATE, so
  -- this waits for it and then re-checks the revoked row: ''.
  select m.username into v_member
  from public.ab_members m
  where m.root = p_root and m.uid = auth.uid() and m.revoked_at is null
  for key share;
  return coalesce(v_member, '');
end
$$;
revoke all on function private.ab_mutation_lock_actor(text)
  from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_lock_chat(
  p_root text, p_chat text, p_exclusive boolean
) returns void
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_key bigint;
begin
  v_key := pg_catalog.hashtextextended(
    'agentbridge-chat-mutation:'
    || pg_catalog.jsonb_build_array(p_root, p_chat)::text, 0
  );
  if p_exclusive then
    perform pg_catalog.pg_advisory_xact_lock(v_key);
  else
    perform pg_catalog.pg_advisory_xact_lock_shared(v_key);
  end if;
end
$$;
revoke all on function private.ab_mutation_lock_chat(text,text,boolean)
  from public, anon, authenticated, service_role;

create or replace function public.ab_mutation_doc_put(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_path text, p_data jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_chat text;
  v_provider_digest text;
  v_begin jsonb;
begin
  if not private.ab_mutation_path_valid(p_path, 4096, 32)
     or p_data is null
     or public.ab_source_json_bytes(p_data) > 8355840 then
    raise exception 'invalid typed document put' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('doc_put', p_path, p_data)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'doc_put',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_chat <> '' then
    perform private.ab_mutation_lock_chat(
      p_root, v_chat, p_path = 'chats/' || v_chat || '/meta.json'
    );
  end if;
  if v_actor is null or v_member = ''
     or p_path like 'chats/%/runtime/effects/%'
     or (v_chat <> '' and not public.ab_is_member(p_root, v_chat)) then
    raise exception 'typed document put is unauthorized'
      using errcode = '42501';
  end if;
  insert into public.ab_docs(root, path, data, deleted)
  values (p_root, p_path, p_data, false)
  on conflict (root, path) do update
    set data = excluded.data, deleted = false;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'doc_put',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('path', p_path, 'updated', true)
  );
end
$$;
revoke all on function public.ab_mutation_doc_put(
  text,uuid,uuid,text,text,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_doc_put(
  text,uuid,uuid,text,text,jsonb) to authenticated;

create or replace function public.ab_mutation_doc_create(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_path text, p_data jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_chat text;
  v_provider_digest text;
  v_begin jsonb;
begin
  if not private.ab_mutation_path_valid(p_path, 4096, 32)
     or p_data is null
     or public.ab_source_json_bytes(p_data) > 8355840 then
    raise exception 'invalid typed document create' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('doc_create', p_path, p_data)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'doc_create',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_chat <> '' then
    perform private.ab_mutation_lock_chat(
      p_root, v_chat, p_path = 'chats/' || v_chat || '/meta.json'
    );
  end if;
  if v_actor is null or v_member = ''
     or p_path like 'chats/%/runtime/effects/%'
     or (
       v_chat <> ''
       and not public.ab_is_member(p_root, v_chat)
       and not coalesce((
         p_path = 'chats/' || v_chat || '/meta.json'
         and pg_catalog.jsonb_typeof(p_data->'members') = 'object'
         and p_data->'members' ? v_member
       ), false)
     ) then
    raise exception 'typed document create is unauthorized'
      using errcode = '42501';
  end if;
  begin
    insert into public.ab_docs(root, path, data, deleted)
    values (p_root, p_path, p_data, false);
  exception when unique_violation then
    raise exception 'typed document already exists' using errcode = '55000';
  end;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'doc_create',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('path', p_path, 'created', true)
  );
end
$$;
revoke all on function public.ab_mutation_doc_create(
  text,uuid,uuid,text,text,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_doc_create(
  text,uuid,uuid,text,text,jsonb) to authenticated;

create or replace function public.ab_mutation_doc_delete(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_path text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_chat text;
  v_provider_digest text;
  v_begin jsonb;
  v_deleted boolean;
begin
  if not private.ab_mutation_path_valid(p_path, 4096, 32) then
    raise exception 'invalid typed document delete' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('doc_delete', p_path)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'doc_delete',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_chat <> '' then
    perform private.ab_mutation_lock_chat(
      p_root, v_chat, p_path = 'chats/' || v_chat || '/meta.json'
    );
  end if;
  -- A chat's meta is never tombstoned directly: only chat_delete ends a
  -- room, so a purge always removes its logs too.
  if v_actor is null or v_member = ''
     or p_path like 'chats/%/runtime/effects/%'
     or (v_chat <> '' and (p_path = 'chats/' || v_chat || '/meta.json'
                           or not public.ab_can_read_chat(p_root, v_chat))) then
    raise exception 'typed document delete is unauthorized'
      using errcode = '42501';
  end if;
  update public.ab_docs
  set data = '{}'::jsonb, deleted = true
  where root = p_root and path = p_path and not deleted;
  v_deleted := found;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'doc_delete',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('path', p_path, 'deleted', v_deleted)
  );
end
$$;
revoke all on function public.ab_mutation_doc_delete(
  text,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_doc_delete(
  text,uuid,uuid,text,text) to authenticated;

create or replace function public.ab_mutation_log_append(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text, p_log text, p_record jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_provider_digest text;
  v_begin jsonb;
  v_row_id bigint;
  v_line text;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1)
     or not private.ab_mutation_path_valid(p_log, 4096, 32)
     or p_record is null
     or pg_catalog.jsonb_typeof(p_record) <> 'object' then
    raise exception 'invalid typed log append' using errcode = '22023';
  end if;
  v_line := p_record::text;
  if public.ab_source_text_bytes(v_line) > 8355840 then
    raise exception 'invalid typed log append' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('log_append', p_chat, p_log, p_record)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'log_append',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  if v_actor is null or v_member = '' then
    raise exception 'typed log append is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, p_chat, false);
  if not public.ab_is_member(p_root, p_chat) then
    raise exception 'typed log append is unauthorized'
      using errcode = '42501';
  end if;
  insert into public.ab_logs(root, chat_id, log_name, line)
  values (p_root, p_chat, p_log, v_line)
  returning id into v_row_id;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'log_append',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object(
      'chat_id', p_chat, 'log_name', p_log, 'row_id', v_row_id
    )
  );
end
$$;
revoke all on function public.ab_mutation_log_append(
  text,uuid,uuid,text,text,text,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_log_append(
  text,uuid,uuid,text,text,text,jsonb) to authenticated;

-- P1b lifecycle slice: inactive receipt-bearing membership and terminal-chat
-- mutation RPCs.  Bootstrap remains an operator action: these entry points
-- require an already sealed root, and no readiness marker is installed here.

create or replace function public.ab_mutation_member_claim(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_username text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_provider_digest text;
  v_begin jsonb;
begin
  if p_username is null or p_username = ''
     or pg_catalog.octet_length(p_username) > 1024 then
    raise exception 'invalid typed member claim' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('member_claim', p_username)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'member_claim',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  if v_actor is null then
    raise exception 'typed member claim is unauthorized'
      using errcode = '42501';
  end if;
  begin
    insert into public.ab_members(root, username, uid)
    values (p_root, p_username, v_actor);
  exception when unique_violation then
    raise exception 'typed member claim conflicts with existing identity'
      using errcode = '55000';
  end;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'member_claim',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('username', p_username, 'claimed', true)
  );
end
$$;
revoke all on function public.ab_mutation_member_claim(
  text,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_member_claim(
  text,uuid,uuid,text,text) to authenticated;

create or replace function public.ab_mutation_member_leave(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_username text;
  v_provider_digest text;
  v_begin jsonb;
begin
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('member_leave')
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'member_leave',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  if v_actor is null then
    raise exception 'typed member leave is unauthorized'
      using errcode = '42501';
  end if;
  -- FOR UPDATE is load-bearing: the revoking UPDATE alone takes FOR NO KEY
  -- UPDATE, which does not wait for writes in flight holding lock_actor's
  -- FOR KEY SHARE. Leaving revokes; the row and its name are kept forever.
  select m.username into v_username
  from public.ab_members m
  where m.root = p_root and m.uid = v_actor and m.revoked_at is null
  for update;
  if v_username is null then
    raise exception 'typed member leave is unauthorized'
      using errcode = '42501';
  end if;
  update public.ab_members m set revoked_at = pg_catalog.now()
  where m.root = p_root and m.uid = v_actor and m.revoked_at is null;
  if not found then
    raise exception 'typed member leave authority changed'
      using errcode = '55000';
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'member_leave',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('username', v_username, 'left', true)
  );
end
$$;
revoke all on function public.ab_mutation_member_leave(
  text,uuid,uuid,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_member_leave(
  text,uuid,uuid,text) to authenticated;

create or replace function public.ab_mutation_chat_delete(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_meta jsonb;
  v_purged boolean;
  v_prefix text;
  v_provider_digest text;
  v_begin jsonb;
  v_logs_deleted bigint;
  v_docs_tombstoned bigint;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1) then
    raise exception 'invalid typed chat delete' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('chat_delete', p_chat)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'chat_delete',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  if v_actor is null or v_member = '' then
    raise exception 'typed chat delete is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, p_chat, true);
  select d.data, d.deleted into v_meta, v_purged
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || p_chat || '/meta.json'
  for update;
  if v_purged then
    -- Already purged (another final member's janitor got there first):
    -- nothing is left to purge or to authorize against, so every root member
    -- gets the same empty answer and a lost race settles.
    return private.ab_mutation_finish(
      p_root, p_protocol_epoch, p_operation_id, 'chat_delete',
      p_payload_digest, v_provider_digest,
      pg_catalog.jsonb_build_object(
        'chat_id', p_chat, 'logs_deleted', 0, 'documents_tombstoned', 0)
    );
  end if;
  -- Only a deleted room's final members (D-018) purge it.
  if v_meta is null
     or not coalesce(v_meta @> '{"deleted": true}'::jsonb, false)
     or pg_catalog.jsonb_typeof(v_meta->'members') is distinct from 'object'
     or v_meta->'members' is distinct from '{}'::jsonb
     or not public.ab_can_read_chat(p_root, p_chat) then
    raise exception 'typed chat delete is unauthorized'
      using errcode = '42501';
  end if;

  delete from public.ab_logs l
  where l.root = p_root and l.chat_id = p_chat;
  get diagnostics v_logs_deleted = row_count;

  -- Prefix comparison is deliberate: LIKE would treat '%' and '_' inside a
  -- valid chat ID as wildcards and could retire a different room's subtree.
  v_prefix := 'chats/' || p_chat || '/';
  update public.ab_docs d
  set data = '{}'::jsonb, deleted = true
  where d.root = p_root
    and pg_catalog.left(d.path, pg_catalog.length(v_prefix)) = v_prefix
    and not d.deleted;
  get diagnostics v_docs_tombstoned = row_count;
  if v_docs_tombstoned < 1 then
    raise exception 'typed chat delete authority changed'
      using errcode = '55000';
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'chat_delete',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object(
      'chat_id', p_chat, 'logs_deleted', v_logs_deleted,
      'documents_tombstoned', v_docs_tombstoned
    )
  );
end
$$;
revoke all on function public.ab_mutation_chat_delete(
  text,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_chat_delete(
  text,uuid,uuid,text,text) to authenticated;

-- P1b effect slice: the existing R142 transition remains the semantic effect
-- validator/writer.  This inactive wrapper adds sealed-root receipt identity,
-- holds mutable authority through that write and rejects a fresh operation for
-- an already-existing effect path.  Exact response-loss recovery uses only the
-- operation receipt above, rather than R142's legacy same-document retry.
create or replace function public.ab_mutation_effect_transition(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_path text, p_data jsonb,
  p_grant_ask jsonb, p_grant_decision jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_chat text;
  v_run text;
  v_call text;
  v_file text;
  v_effect_actor text;
  v_meta jsonb;
  v_actor_doc jsonb;
  v_room jsonb;
  v_previous text;
  v_previous_data jsonb;
  v_existing jsonb;
  v_provider_digest text;
  v_begin jsonb;
  v_transitioned boolean;
begin
  if not private.ab_mutation_path_valid(p_path, 4096, 32)
     or p_path !~ '^chats/[^/]+/runtime/effects/[^/]+/[^/]+/(claim|state-[23])\.json$'
     or p_data is null
     or pg_catalog.jsonb_typeof(p_data) <> 'object'
     or public.ab_source_json_bytes(p_data) > 8355840
     or (
       p_grant_ask is not null
       and public.ab_source_json_bytes(p_grant_ask) > 8355840
     )
     or (
       p_grant_decision is not null
       and public.ab_source_json_bytes(p_grant_decision) > 8355840
     ) then
    raise exception 'invalid typed effect transition' using errcode = '22023';
  end if;
  v_chat := pg_catalog.split_part(p_path, '/', 2);
  v_run := pg_catalog.split_part(p_path, '/', 5);
  v_call := pg_catalog.split_part(p_path, '/', 6);
  v_file := pg_catalog.split_part(p_path, '/', 7);
  v_meta := p_data->'meta';
  v_effect_actor := v_meta->>'actor';
  if pg_catalog.jsonb_typeof(v_meta) is distinct from 'object'
     or v_effect_actor is null or v_effect_actor = ''
     or v_meta->>'kind' is distinct from 'effect'
     or v_meta->>'signer' is distinct from v_effect_actor
     or v_meta->>'chat_id' is distinct from v_chat
     or v_meta->>'run_id' is distinct from v_run
     or v_meta->>'root_run_id' is distinct from v_run
     or v_meta->>'call_id' is distinct from v_call then
    raise exception 'invalid typed effect transition' using errcode = '22023';
  end if;
  if v_file = 'claim.json' then
    if pg_catalog.jsonb_typeof(p_grant_ask) is distinct from 'object'
       or pg_catalog.jsonb_typeof(p_grant_decision) is distinct from 'object'
       or p_grant_ask->'header'->>'kind' is distinct from 'permission_ask'
       or p_grant_ask->'header'->>'sender' is distinct from v_effect_actor
       or p_grant_ask->'header'->>'agent' is distinct from v_effect_actor
       or p_grant_ask->'header'->>'chat_id' is distinct from v_chat
       or p_grant_decision->'header'->>'kind'
            is distinct from 'permission_decision'
       or p_grant_decision->'header'->>'recipient'
            is distinct from v_effect_actor
       or p_grant_decision->'header'->>'agent'
            is distinct from v_effect_actor
       or p_grant_decision->'header'->>'chat_id' is distinct from v_chat then
      raise exception 'invalid typed effect transition' using errcode = '22023';
    end if;
  elsif p_grant_ask is not null or p_grant_decision is not null then
    raise exception 'invalid typed effect transition' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array(
      'effect_transition', p_path, p_data, p_grant_ask, p_grant_decision
    )
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'effect_transition',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := pg_catalog.split_part(p_path, '/', 2);
  if v_actor is null or v_member = '' then
    raise exception 'typed effect transition is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, v_chat, false);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-effect-transition:'
      || pg_catalog.jsonb_build_array(p_root, p_path)::text, 0
    )
  );

  -- Agent ownership/active state is part of R142 authority and may change
  -- independently of room metadata.  Hold the exact actor document stable
  -- while the existing transition revalidates and writes.  Room metadata and
  -- cleanup are stable under the shared room lock above.
  select d.data into v_actor_doc
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'users/' || v_effect_actor || '.json'
    and not d.deleted
  for share;

  select d.data into v_room
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || v_chat || '/meta.json'
    and not d.deleted
  for share;
  if pg_catalog.jsonb_typeof(v_actor_doc) is distinct from 'object'
     or not coalesce(
       v_actor_doc @> '{"kind":"agent","active":true}'::jsonb, false
     )
     or pg_catalog.jsonb_typeof(v_actor_doc->'agent')
          is distinct from 'object'
     or v_actor_doc->'agent'->>'owner' is distinct from v_member
     or pg_catalog.jsonb_typeof(v_room) is distinct from 'object'
     or not coalesce(v_room->'members' ? v_member, false)
     or not coalesce(v_room->'members' ? v_effect_actor, false) then
    raise exception 'typed effect transition is unauthorized'
      using errcode = '42501';
  end if;
  if v_file = 'claim.json' then
    if p_grant_ask->'header'->>'recipient' is distinct from v_member
       or p_grant_decision->'header'->>'sender' is distinct from v_member then
      raise exception 'typed effect transition is unauthorized'
        using errcode = '42501';
    end if;
  elsif v_file = 'state-2.json' then
    v_previous := 'chats/' || v_chat || '/runtime/effects/'
      || v_run || '/' || v_call || '/claim.json';
  else
    v_previous := 'chats/' || v_chat || '/runtime/effects/'
      || v_run || '/' || v_call || '/state-2.json';
  end if;
  if v_previous is not null then
    select d.data into v_previous_data
    from public.ab_docs d
    where d.root = p_root and d.path = v_previous and not d.deleted;
    if v_previous_data is null
       or v_previous_data->'meta'->>'actor' is distinct from v_effect_actor
       or v_previous_data->'meta'->>'chat_id' is distinct from v_chat
       or v_previous_data->'meta'->>'run_id' is distinct from v_run
       or v_previous_data->'meta'->>'call_id' is distinct from v_call then
      raise exception 'typed effect transition predecessor is unavailable'
        using errcode = '55000';
    end if;
  end if;

  perform 1 from public.ab_docs d
  where d.root = p_root and d.path = p_path;
  if found then
    raise exception 'typed effect transition already exists'
      using errcode = '55000';
  end if;

  v_transitioned := public.ab_effect_transition(
    p_root, p_path, p_data, p_grant_ask, p_grant_decision
  );
  if v_transitioned is distinct from true then
    raise exception 'typed effect transition is unauthorized'
      using errcode = '42501';
  end if;
  select d.data into v_existing
  from public.ab_docs d
  where d.root = p_root and d.path = p_path and not d.deleted;
  if not found or v_existing is distinct from p_data then
    raise exception 'typed effect transition did not commit exact effect'
      using errcode = '55000';
  end if;

  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'effect_transition',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('path', p_path, 'transitioned', true)
  );
end
$$;
revoke all on function public.ab_mutation_effect_transition(
  text,uuid,uuid,text,text,jsonb,jsonb,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_effect_transition(
  text,uuid,uuid,text,text,jsonb,jsonb,jsonb) to authenticated;

-- P1b blob-manifest slice: immutable Storage objects are uploaded through the
-- Storage API before this receipt-bearing publication RPC runs.  PostgreSQL
-- proves the exact Storage row's owner, immutable API object ID and bounded size; it
-- does not claim to read or hash the underlying object bytes.

create table if not exists public.ab_blob_manifests (
  root             text not null
                     references public.ab_mutation_roots(root),
  protocol_epoch   uuid not null,
  operation_id     uuid not null,
  chat_id          text not null,
  blob_id          text not null,
  actor_uid        uuid not null,
  object_id        uuid not null,
  sealed_bytes     bigint not null check (
                     sealed_bytes between 1 and 52428800
                   ),
  sealed_sha256    text not null check (
                     sealed_sha256 ~ '^[0-9a-f]{64}$'
                   ),
  manifest_digest  text not null check (
                     manifest_digest ~ '^[0-9a-f]{64}$'
                   ),
  created_at       timestamptz not null default now(),
  primary key (root, chat_id, blob_id),
  unique (root, protocol_epoch, operation_id),
  check (pg_catalog.octet_length(root) between 1 and 1024),
  check (pg_catalog.octet_length(chat_id) between 1 and 1024),
  check (pg_catalog.octet_length(blob_id) between 1 and 180)
);
alter table public.ab_blob_manifests enable row level security;
revoke all on table public.ab_blob_manifests
  from public, anon, authenticated, service_role;

drop trigger if exists ab_mutation_legacy_gate on public.ab_blob_manifests;
create trigger ab_mutation_legacy_gate
before insert or update or delete on public.ab_blob_manifests
for each row execute function private.ab_mutation_legacy_gate();

-- Storage metadata is provider-owned and must remain read-only to this
-- protocol.  Invalid or oversized metadata is unavailable rather than cast.
create or replace function private.ab_storage_object_size(p_metadata jsonb)
returns bigint
language sql immutable set search_path = pg_catalog as $$
  select case
    when p_metadata->>'size' ~ '^[0-9]{1,8}$'
    then case
      when (p_metadata->>'size')::bigint between 1 and 52428800
      then (p_metadata->>'size')::bigint
      else null
    end
    else null
  end
$$;
revoke all on function private.ab_storage_object_size(jsonb)
  from public, anon, authenticated, service_role;

-- One exact predicate supports staged-object SELECT policy.  Before
-- publication, only the current uploader/current room member may read the
-- object back for verification.  After any manifest exists, owner fallback is
-- disabled and the exact published object ID/size plus current chat visibility
-- are required.
create or replace function public.ab_staged_blob_readable(
  p_root text, p_chat text, p_blob text, p_owner_id text,
  p_object_id uuid, p_metadata jsonb
) returns boolean
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_sealed_bytes bigint := private.ab_storage_object_size(p_metadata);
begin
  if v_actor is null or p_root is null or p_chat is null or p_blob is null then
    return false;
  end if;
  if p_owner_id is not null and p_object_id is not null
     and v_sealed_bytes is not null then
    if exists (
      select 1 from public.ab_blob_manifests m
      where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob
    ) then
      if public.ab_can_read_chat(p_root, p_chat) and exists (
        select 1 from public.ab_blob_manifests m
        where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob
          and m.object_id = p_object_id
          and m.sealed_bytes = v_sealed_bytes
      ) then
        return true;
      end if;
    elsif p_owner_id = v_actor::text
          and public.ab_is_member(p_root, p_chat) then
      return true;
    end if;
  end if;
  -- A deleted room's final members see every staged object it holds, so
  -- their janitor can list and remove them all before the purge. Checked
  -- last, so a live room's common reads never pay for it.
  return public.ab_chat_terminal_reader(p_root, p_chat);
end
$$;
revoke all on function public.ab_staged_blob_readable(
  text,text,text,text,uuid,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_staged_blob_readable(
  text,text,text,text,uuid,jsonb) to authenticated;

-- Existing policies are permissive.  Replace rather than supplement them so
-- the legacy non-chat branch cannot accidentally admit blob-stage objects.
-- Staged keys have exactly four non-empty segments:
--   <root>/blob-stage/<chat-id>/<blob-id>
drop policy if exists ab_blobs_member_select on storage.objects;
create policy ab_blobs_member_select on storage.objects
for select to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and (
    (
      pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
      and (
        pg_catalog.split_part(name, '/', 2) <> 'chats'
        or public.ab_can_read_chat(
          pg_catalog.split_part(name, '/', 1),
          pg_catalog.split_part(name, '/', 3)
        )
      )
    )
    or (
      pg_catalog.split_part(name, '/', 2) = 'blob-stage'
      and pg_catalog.split_part(name, '/', 3) <> ''
      and pg_catalog.split_part(name, '/', 4) <> ''
      and pg_catalog.split_part(name, '/', 5) = ''
      and public.ab_staged_blob_readable(
        pg_catalog.split_part(name, '/', 1),
        pg_catalog.split_part(name, '/', 3),
        pg_catalog.split_part(name, '/', 4),
        owner_id,
        id,
        metadata
      )
    )
  )
);

drop policy if exists ab_blobs_member_insert on storage.objects;
create policy ab_blobs_member_insert on storage.objects
for insert to authenticated with check (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and (
    (
      pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
      and (
        pg_catalog.split_part(name, '/', 2) <> 'chats'
        or public.ab_is_member(
          pg_catalog.split_part(name, '/', 1),
          pg_catalog.split_part(name, '/', 3)
        )
      )
    )
    or (
      pg_catalog.split_part(name, '/', 2) = 'blob-stage'
      and pg_catalog.split_part(name, '/', 3) <> ''
      and pg_catalog.split_part(name, '/', 4) <> ''
      and pg_catalog.split_part(name, '/', 5) = ''
      and owner_id = auth.uid()::text
      and public.ab_is_member(
        pg_catalog.split_part(name, '/', 1),
        pg_catalog.split_part(name, '/', 3)
      )
    )
  )
);

drop policy if exists ab_blobs_member_update on storage.objects;
create policy ab_blobs_member_update on storage.objects
for update to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
  and (
    pg_catalog.split_part(name, '/', 2) <> 'chats'
    or public.ab_is_member(
      pg_catalog.split_part(name, '/', 1),
      pg_catalog.split_part(name, '/', 3)
    )
  )
) with check (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
  and (
    pg_catalog.split_part(name, '/', 2) <> 'chats'
    or public.ab_is_member(
      pg_catalog.split_part(name, '/', 1),
      pg_catalog.split_part(name, '/', 3)
    )
  )
);

drop policy if exists ab_blobs_member_delete on storage.objects;
create policy ab_blobs_member_delete on storage.objects
for delete to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and (
    (
      pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
      and (
        pg_catalog.split_part(name, '/', 2) <> 'chats'
        or public.ab_can_read_chat(
          pg_catalog.split_part(name, '/', 1),
          pg_catalog.split_part(name, '/', 3)
        )
      )
    )
    or (
      pg_catalog.split_part(name, '/', 2) = 'blob-stage'
      and pg_catalog.split_part(name, '/', 3) <> ''
      and pg_catalog.split_part(name, '/', 4) <> ''
      and pg_catalog.split_part(name, '/', 5) = ''
      and (
        (
          owner_id = auth.uid()::text
          and public.ab_is_member(
            pg_catalog.split_part(name, '/', 1),
            pg_catalog.split_part(name, '/', 3)
          )
        )
        -- A deleted room's final members clear its staged objects (D-018).
        or public.ab_chat_terminal_reader(
          pg_catalog.split_part(name, '/', 1),
          pg_catalog.split_part(name, '/', 3)
        )
      )
    )
  )
);

create or replace function public.ab_mutation_blob_manifest(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text, p_blob text,
  p_object_id uuid, p_sealed_bytes bigint,
  p_sealed_sha256 text, p_manifest_digest text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_provider_digest text;
  v_begin jsonb;
  v_storage_size bigint;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1)
     or p_blob is null
     or p_blob !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$'
     or p_object_id is null
     or p_sealed_bytes is null
     or p_sealed_bytes not between 1 and 52428800
     or p_sealed_sha256 is null
     or p_sealed_sha256 !~ '^[0-9a-f]{64}$'
     or p_manifest_digest is null
     or p_manifest_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid typed blob manifest' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array(
      'blob_manifest', p_chat, p_blob, p_object_id,
      p_sealed_bytes, p_sealed_sha256, p_manifest_digest
    )
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'blob_manifest',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  if v_actor is null or v_member = '' then
    raise exception 'typed blob manifest is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, p_chat, false);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-blob-manifest:'
      || pg_catalog.jsonb_build_array(p_root, p_chat, p_blob)::text, 0
    )
  );

  perform 1 from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || p_chat || '/meta.json'
    and not d.deleted
    and pg_catalog.jsonb_typeof(d.data->'members') = 'object'
    and d.data->'members' ? v_member
  for share;
  if not found then
    raise exception 'typed blob manifest is unauthorized'
      using errcode = '42501';
  end if;

  perform 1 from public.ab_blob_manifests m
  where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob;
  if found then
    raise exception 'typed blob manifest already exists'
      using errcode = '55000';
  end if;

  select private.ab_storage_object_size(o.metadata) into v_storage_size
  from storage.objects o
  where o.bucket_id = 'ab-mesh'
    and o.name = p_root || '/blob-stage/' || p_chat || '/' || p_blob
    and o.owner_id = v_actor::text
    and o.id = p_object_id
    and o.archived_at is null
    and not coalesce(o.is_delete_marker, false)
  for share;
  if not found or v_storage_size is distinct from p_sealed_bytes then
    raise exception 'typed blob manifest object is unavailable'
      using errcode = '55000';
  end if;

  insert into public.ab_blob_manifests(
    root, protocol_epoch, operation_id, chat_id, blob_id, actor_uid,
    object_id, sealed_bytes, sealed_sha256, manifest_digest
  ) values (
    p_root, p_protocol_epoch, p_operation_id, p_chat, p_blob, v_actor,
    p_object_id, p_sealed_bytes, p_sealed_sha256, p_manifest_digest
  );
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'blob_manifest',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object(
      'chat_id', p_chat, 'blob_id', p_blob,
      'object_id', p_object_id, 'sealed_bytes', p_sealed_bytes
    )
  );
end
$$;
revoke all on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,uuid,bigint,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,uuid,bigint,text,text) to authenticated;

-- Inactive conditional mutable-document replacement. A conflict is a terminal
-- outcome, including an obsolete source epoch; it never leaves an applying
-- receipt that callers could accidentally retry with regenerated content.
alter table public.ab_mutation_receipts
  drop constraint if exists ab_mutation_receipts_operation_kind_check;
alter table public.ab_mutation_receipts
  add constraint ab_mutation_receipts_operation_kind_check check (
    operation_kind in (
      'doc_put','doc_compare_put','reaction_pair','meta_append','doc_create','doc_delete','log_append',
      'effect_transition','chat_delete','member_claim','member_leave','blob_manifest'
    )
  );

create or replace function public.ab_mutation_doc_compare_put(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_path text, p_data jsonb,
  p_expected_source_epoch uuid, p_expected_seq bigint, p_expected_deleted boolean
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_chat text;
  v_provider_digest text;
  v_begin jsonb;
  v_source_epoch uuid;
  v_applied boolean := false;
begin
  if not private.ab_mutation_path_valid(p_path, 4096, 32)
     or p_data is null
     or public.ab_source_json_bytes(p_data) > 8355840
     or p_expected_source_epoch is null
     or p_expected_seq is null or p_expected_seq < 0
     or p_expected_deleted is null
     or (p_expected_seq = 0 and p_expected_deleted) then
    raise exception 'invalid conditional document put' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('doc_compare_put', p_path, p_data,
      p_expected_source_epoch, p_expected_seq, p_expected_deleted)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'doc_compare_put',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;
  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_chat <> '' then
    perform private.ab_mutation_lock_chat(
      p_root, v_chat, p_path = 'chats/' || v_chat || '/meta.json'
    );
  end if;
  if v_actor is null or v_member = ''
     or p_path like 'chats/%/runtime/effects/%'
     or (v_chat <> '' and not public.ab_is_member(p_root, v_chat)) then
    raise exception 'conditional document put is unauthorized'
      using errcode = '42501';
  end if;
  -- The root epoch cannot reset between comparison and source mutation. The
  -- ordinary source triggers acquire this same shared ledger lock.
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  select e.epoch into v_source_epoch from public.ab_change_epochs e
  where e.root = p_root and e.source_schema_version = 2;
  if v_source_epoch = p_expected_source_epoch then
    if p_expected_seq = 0 then
      -- Unique-key arbitration also covers concurrent unconditional puts and
      -- creates. READ COMMITTED waits for their outcome; a winner means conflict.
      insert into public.ab_docs(root, path, data, deleted)
      values (p_root, p_path, p_data, false)
      on conflict (root, path) do nothing;
      v_applied := found;
    else
      -- UPDATE rechecks the predicate after waiting on a concurrent row writer.
      -- A put/delete changes seq, so a stale observed version cannot overwrite it.
      update public.ab_docs d set data = p_data, deleted = false
      where d.root = p_root and d.path = p_path
        and d.seq = p_expected_seq and d.deleted = p_expected_deleted;
      v_applied := found;
    end if;
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'doc_compare_put',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('path', p_path, 'applied', v_applied)
  );
end
$$;
revoke all on function public.ab_mutation_doc_compare_put(
  text,uuid,uuid,text,text,jsonb,uuid,bigint,boolean)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_doc_compare_put(
  text,uuid,uuid,text,text,jsonb,uuid,bigint,boolean) to authenticated;

-- Inactive atomic reaction/checkpoint replacement. Both source versions are
-- compared, and the inner subtransaction rolls back the first write if the
-- second comparison loses. Sequence allocation gaps are harmless; neither
-- source content nor source change evidence escapes a conflicting pair.
create or replace function public.ab_mutation_reaction_pair(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text, p_user text,
  p_state_data jsonb, p_reaction_data jsonb, p_expected_source_epoch uuid,
  p_expected_state_seq bigint, p_expected_state_deleted boolean,
  p_expected_reaction_seq bigint, p_expected_reaction_deleted boolean
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_provider_digest text;
  v_begin jsonb;
  v_source_epoch uuid;
  v_paths text[];
  v_data jsonb[];
  v_seqs bigint[];
  v_deleted boolean[];
  v_index integer;
  v_applied boolean := false;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1)
     or not private.ab_mutation_path_valid(p_user, 1024, 1)
     or p_state_data is null or p_reaction_data is null
     or pg_catalog.jsonb_typeof(p_state_data) <> 'object'
     or pg_catalog.jsonb_typeof(p_reaction_data) <> 'object'
     or public.ab_source_json_bytes(p_state_data)
        + public.ab_source_json_bytes(p_reaction_data) > 8355840
     or p_expected_source_epoch is null
     or p_expected_state_seq is null or p_expected_state_seq < 0
     or p_expected_reaction_seq is null or p_expected_reaction_seq < 0
     or p_expected_state_deleted is null or p_expected_reaction_deleted is null
     or (p_expected_state_seq = 0 and p_expected_state_deleted)
     or (p_expected_reaction_seq = 0 and p_expected_reaction_deleted) then
    raise exception 'invalid reaction pair' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('reaction_pair', p_chat, p_user,
      p_state_data, p_reaction_data, p_expected_source_epoch,
      p_expected_state_seq, p_expected_state_deleted,
      p_expected_reaction_seq, p_expected_reaction_deleted)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'reaction_pair',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;
  v_member := private.ab_mutation_lock_actor(p_root);
  perform private.ab_mutation_lock_chat(p_root, p_chat, false);
  if v_actor is null or v_member = '' or not public.ab_is_member(p_root, p_chat) then
    raise exception 'reaction pair is unauthorized' using errcode = '42501';
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  select e.epoch into v_source_epoch from public.ab_change_epochs e
  where e.root = p_root and e.source_schema_version = 2;
  -- Fixed lexical order across all pair writers prevents pair/pair deadlocks.
  -- Ordinary single-document writes arbitrate through the same row/unique key.
  v_paths := array[
    'chats/' || p_chat || '/overlays/reactions/' || p_user || '.json',
    'chats/' || p_chat || '/overlays/state/' || p_user || '.json'];
  v_data := array[p_reaction_data, p_state_data];
  v_seqs := array[p_expected_reaction_seq, p_expected_state_seq];
  v_deleted := array[p_expected_reaction_deleted, p_expected_state_deleted];
  if v_source_epoch = p_expected_source_epoch then
    begin
      for v_index in 1..2 loop
        if v_seqs[v_index] = 0 then
          insert into public.ab_docs(root, path, data, deleted)
          values (p_root, v_paths[v_index], v_data[v_index], false)
          on conflict (root, path) do nothing;
        else
          update public.ab_docs d set data = v_data[v_index], deleted = false
          where d.root = p_root and d.path = v_paths[v_index]
            and d.seq = v_seqs[v_index] and d.deleted = v_deleted[v_index];
        end if;
        if not found then
          raise exception 'reaction pair comparison conflict' using errcode = 'P0R01';
        end if;
      end loop;
      v_applied := true;
    exception when sqlstate 'P0R01' then
      v_applied := false;
    end;
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'reaction_pair',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('chat_id', p_chat, 'username', p_user,
                                 'applied', v_applied)
  );
end
$$;
revoke all on function public.ab_mutation_reaction_pair(
  text,uuid,uuid,text,text,text,jsonb,jsonb,uuid,bigint,boolean,bigint,boolean)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_reaction_pair(
  text,uuid,uuid,text,text,text,jsonb,jsonb,uuid,bigint,boolean,bigint,boolean)
  to authenticated;

-- Effect-free immutable owner admission, optional per owner: single-leg
-- writes may take free slots directly. Lock order is epoch, then operation,
-- then the root admission mutex. Admission holds the mutex exclusive and never
-- takes an operation lock; execution in an admission-enabled epoch holds it
-- shared after its operation lock; cancellation never takes it. No cycle.
create or replace function public.ab_admin_enable_mutation_admission(
  p_root text, p_expected_epoch uuid
) returns jsonb
language plpgsql volatile security invoker set search_path = pg_catalog as $$
declare
  v_root public.ab_mutation_roots%rowtype;
begin
  if p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or p_expected_epoch is null
     or pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'invalid admission enable request' using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-mutation-epoch:' || p_root, 0));
  select r.* into v_root from public.ab_mutation_roots r where r.root = p_root for update;
  if v_root.root is null or v_root.protocol_epoch is distinct from p_expected_epoch
     or v_root.legacy_writes_enabled
     or not exists (select 1 from public.ab_database_mutation_seals s where s.root = p_root) then
    raise exception 'admission enable precondition changed' using errcode = '55000';
  end if;
  if not v_root.admission_required then
    if exists (select 1 from public.ab_mutation_receipts r where r.root = p_root)
       or exists (select 1 from public.ab_mutation_admission_groups g where g.root = p_root)
       or exists (select 1 from public.ab_mutation_admission_legs l where l.root = p_root) then
      raise exception 'admission requires an empty receipt epoch' using errcode = '55000';
    end if;
    update public.ab_mutation_roots set admission_required = true where root = p_root;
  end if;
  return pg_catalog.jsonb_build_object('schema_version', 1, 'root', p_root,
    'protocol_epoch', p_expected_epoch, 'admission_required', true);
end
$$;
revoke all on function public.ab_admin_enable_mutation_admission(text,uuid)
  from public, anon, authenticated, service_role;

create or replace function public.ab_admit_mutation_owner(
  p_root text, p_protocol_epoch uuid, p_owner_id uuid, p_plan jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_root public.ab_mutation_roots%rowtype;
  v_group public.ab_mutation_admission_groups%rowtype;
  v_item jsonb;
  v_plan jsonb;
  v_ids uuid[] := array[]::uuid[];
  v_id uuid;
  v_count integer;
  v_occupied integer;
  v_ordinal integer := 0;
  v_slots integer[];
  v_member text;
begin
  if v_actor is null or auth.role() is distinct from 'authenticated'
     or p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or p_protocol_epoch is null or p_owner_id is null
     or p_plan is null or pg_catalog.jsonb_typeof(p_plan) <> 'array'
     or pg_catalog.octet_length(p_plan::text) > 524288
     or pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'invalid mutation admission request' using errcode = '22023';
  end if;
  v_count := pg_catalog.jsonb_array_length(p_plan);
  if v_count < 1 or v_count > 1025 then
    raise exception 'invalid admission plan count' using errcode = '22023';
  end if;
  for v_item in select value from pg_catalog.jsonb_array_elements(p_plan) loop
    if pg_catalog.jsonb_typeof(v_item) <> 'object'
       or not (v_item ?& array['operation_id','kind','client_digest'])
       or (v_item - array['operation_id','kind','client_digest']) <> '{}'::jsonb
       or pg_catalog.jsonb_typeof(v_item->'operation_id') <> 'string'
       or pg_catalog.jsonb_typeof(v_item->'kind') <> 'string'
       or pg_catalog.jsonb_typeof(v_item->'client_digest') <> 'string'
       or (v_item->>'kind') not in (
         'doc_put','doc_compare_put','reaction_pair','meta_append','doc_create','doc_delete','log_append',
         'effect_transition','chat_delete','member_claim','member_leave','blob_manifest')
       or (v_item->>'client_digest') !~ '^[0-9a-f]{64}$' then
      raise exception 'invalid admission plan leg' using errcode = '22023';
    end if;
    begin
      v_id := (v_item->>'operation_id')::uuid;
    exception when invalid_text_representation then
      raise exception 'invalid admission operation id' using errcode = '22023';
    end;
    if v_id::text is distinct from (v_item->>'operation_id') or v_id = any(v_ids) then
      raise exception 'noncanonical or repeated admission operation id' using errcode = '22023';
    end if;
    v_ids := pg_catalog.array_append(v_ids, v_id);
  end loop;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-mutation-epoch:' || p_root, 0));
  select r.* into v_root from public.ab_mutation_roots r where r.root = p_root;
  if v_root.root is null or v_root.protocol_epoch is distinct from p_protocol_epoch
     or not v_root.admission_required or v_root.legacy_writes_enabled then
    raise exception 'mutation admission epoch unavailable' using errcode = '55000';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('agentbridge-mutation-admission:' || p_root, 0));
  select g.* into v_group from public.ab_mutation_admission_groups g
    where g.root = p_root and g.protocol_epoch = p_protocol_epoch and g.owner_id = p_owner_id;
  if found then
    select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'operation_id', l.operation_id, 'kind', l.operation_kind, 'client_digest', l.client_digest)
        order by l.ordinal) into v_plan
      from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.owner_id = p_owner_id;
    if v_group.actor_uid is distinct from v_actor or v_plan is distinct from p_plan then
      raise exception 'mutation admission owner collision' using errcode = '55000';
    end if;
    return pg_catalog.jsonb_build_object('schema_version', 1, 'root', p_root,
      'actor_id', v_actor, 'protocol_epoch', p_protocol_epoch, 'owner_id', p_owner_id,
      'status', 'admitted', 'plan', v_plan);
  end if;
  -- Fresh admission is authorization, but exact admitted replay is not: it
  -- remains available to the original actor after root membership removal.
  v_member := private.ab_mutation_lock_actor(p_root);
  -- A nonmember may reserve exactly the existing authenticated member-claim
  -- operation; this adds no source authority and preserves the join RPC's
  -- current authorization/uniqueness checks at execution. No mixed plan.
  if (v_member is null or v_member = '')
     and not (v_count = 1 and p_plan->0->>'kind' = 'member_claim') then
    raise exception 'mutation admission requires root membership' using errcode = '42501';
  end if;
  -- An unaffiliated principal must pin no more capacity than one direct claim
  -- could before admission existed: one reservation not yet settled for epoch
  -- closure at a time, and a small claim total per epoch. Without this, one
  -- Auth account could reserve every slot and block rotation indefinitely.
  -- The admission mutex serializes this check with every other admission.
  if v_member is null or v_member = '' then
    if exists (select 1 from public.ab_mutation_admission_legs l
        join public.ab_mutation_admission_groups g using(root, protocol_epoch, owner_id)
        left join public.ab_mutation_receipts r on r.root = l.root
          and r.protocol_epoch = l.protocol_epoch and r.operation_id = l.operation_id
        where l.root = p_root and l.protocol_epoch = p_protocol_epoch
          and g.actor_uid = v_actor
          and not (l.cancelled and l.cancel_acknowledged)
          and not (r.receipt_state = 'terminal' and r.acknowledged_at is not null)) then
      raise exception 'unaffiliated mutation admission is already pending' using errcode = '55000';
    end if;
    if (select pg_catalog.count(*) from public.ab_mutation_admission_legs l
        join public.ab_mutation_admission_groups g using(root, protocol_epoch, owner_id)
        where l.root = p_root and l.protocol_epoch = p_protocol_epoch
          and g.actor_uid = v_actor and l.operation_kind = 'member_claim') >= 4 then
      raise exception 'unaffiliated mutation admission budget exhausted' using errcode = '54000';
    end if;
  end if;
  -- An operation that already executed unadmitted can never settle a new leg.
  if exists (select 1 from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.operation_id = any(v_ids))
     or exists (select 1 from public.ab_mutation_receipts r
      where r.root = p_root and r.protocol_epoch = p_protocol_epoch and r.operation_id = any(v_ids)) then
    raise exception 'mutation admission operation collision' using errcode = '55000';
  end if;
  -- Admission is optional per owner: single-leg writes may take free slots
  -- directly. Occupancy is every reserved or receipted slot; neither table
  -- ever frees a slot within the epoch, so a counted full is final.
  select pg_catalog.count(*) into v_occupied from (
    select l.receipt_slot from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch
    union
    select r.receipt_slot from public.ab_mutation_receipts r
      where r.root = p_root and r.protocol_epoch = p_protocol_epoch) occupied;
  if v_occupied + v_count > 4096 then
    -- No control row is needed: no source effect is eligible without positive
    -- durable admission. Slots, including cancelled legs, never free in-epoch.
    return pg_catalog.jsonb_build_object('schema_version', 1, 'root', p_root,
      'actor_id', v_actor, 'protocol_epoch', p_protocol_epoch, 'owner_id', p_owner_id,
      'status', 'full', 'plan', p_plan);
  end if;
  -- Unadmitted writes hold this mutex shared from their leg lookup through
  -- commit, so under the exclusive hold no unadmitted receipt is in flight.
  select pg_catalog.array_agg(candidate order by candidate) into v_slots from (
    select slot as candidate from pg_catalog.generate_series(0,4095) as slot
    where not exists (select 1 from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.receipt_slot = slot)
      and not exists (select 1 from public.ab_mutation_receipts r
      where r.root = p_root and r.protocol_epoch = p_protocol_epoch and r.receipt_slot = slot)
    order by slot limit v_count
  ) free_slots;
  insert into public.ab_mutation_admission_groups(root, protocol_epoch, owner_id, actor_uid)
    values (p_root, p_protocol_epoch, p_owner_id, v_actor);
  for v_item in select value from pg_catalog.jsonb_array_elements(p_plan) loop
    v_ordinal := v_ordinal + 1;
    insert into public.ab_mutation_admission_legs(root, protocol_epoch, owner_id,
      operation_id, ordinal, operation_kind, client_digest, receipt_slot)
    values (p_root, p_protocol_epoch, p_owner_id, (v_item->>'operation_id')::uuid,
      v_ordinal, v_item->>'kind', v_item->>'client_digest', v_slots[v_ordinal]);
  end loop;
  return pg_catalog.jsonb_build_object('schema_version', 1, 'root', p_root,
    'actor_id', v_actor, 'protocol_epoch', p_protocol_epoch, 'owner_id', p_owner_id,
    'status', 'admitted', 'plan', p_plan);
end
$$;
revoke all on function public.ab_admit_mutation_owner(text,uuid,uuid,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_admit_mutation_owner(text,uuid,uuid,jsonb) to authenticated;

-- Shared exact cancellation resolver. It updates an existing leg, never
-- allocates a control slot, and serializes with _begin on the operation lock.
create or replace function private.ab_mutation_admission_cancel(
  p_root text, p_protocol_epoch uuid, p_owner_id uuid, p_operation_id uuid,
  p_kind text, p_client_digest text, p_acknowledge boolean
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_leg public.ab_mutation_admission_legs%rowtype;
  v_status text;
begin
  if v_actor is null or auth.role() is distinct from 'authenticated'
     or p_root is null or p_root = '' or pg_catalog.octet_length(p_root) > 1024
     or p_protocol_epoch is null or p_owner_id is null or p_operation_id is null
     or p_kind is null or p_client_digest is null
     or p_client_digest !~ '^[0-9a-f]{64}$' or p_acknowledge is null
     or pg_catalog.current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'invalid admission cancellation request' using errcode = '22023';
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-mutation-epoch:' || p_root, 0));
  if not exists (select 1 from public.ab_mutation_roots r where r.root = p_root
      and r.protocol_epoch = p_protocol_epoch and r.admission_required and not r.legacy_writes_enabled) then
    raise exception 'mutation admission epoch unavailable' using errcode = '55000';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-mutation-operation:' || p_root || ':' || p_protocol_epoch::text || ':' || p_operation_id::text, 0));
  select l.* into v_leg from public.ab_mutation_admission_legs l
    join public.ab_mutation_admission_groups g using(root, protocol_epoch, owner_id)
    where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.owner_id = p_owner_id
      and l.operation_id = p_operation_id and g.actor_uid = v_actor;
  if not found or v_leg.operation_kind is distinct from p_kind
     or v_leg.client_digest is distinct from p_client_digest then
    raise exception 'mutation admission leg unavailable' using errcode = '55000';
  end if;
  if p_acknowledge then
    if not v_leg.cancelled then
      raise exception 'mutation admission leg is not cancelled' using errcode = '55000';
    end if;
    update public.ab_mutation_admission_legs set cancel_acknowledged = true
      where root = p_root and protocol_epoch = p_protocol_epoch and operation_id = p_operation_id;
    v_status := 'cancelled';
  elsif exists (select 1 from public.ab_mutation_receipts r
      where r.root = p_root and r.protocol_epoch = p_protocol_epoch and r.operation_id = p_operation_id) then
    v_status := 'executed';
  else
    update public.ab_mutation_admission_legs set cancelled = true
      where root = p_root and protocol_epoch = p_protocol_epoch and operation_id = p_operation_id;
    v_status := 'cancelled';
  end if;
  return pg_catalog.jsonb_build_object('schema_version', 1, 'root', p_root,
    'actor_id', v_actor, 'protocol_epoch', p_protocol_epoch, 'owner_id', p_owner_id,
    'operation_id', p_operation_id, 'kind', p_kind, 'client_digest', p_client_digest,
    'status', v_status);
end
$$;
revoke all on function private.ab_mutation_admission_cancel(text,uuid,uuid,uuid,text,text,boolean)
  from public, anon, authenticated, service_role;

create or replace function public.ab_cancel_mutation_admission(
  p_root text, p_protocol_epoch uuid, p_owner_id uuid, p_operation_id uuid,
  p_kind text, p_client_digest text
) returns jsonb
language sql volatile security definer set search_path = pg_catalog as $$
  select private.ab_mutation_admission_cancel(p_root,p_protocol_epoch,p_owner_id,
    p_operation_id,p_kind,p_client_digest,false)
$$;
revoke all on function public.ab_cancel_mutation_admission(text,uuid,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_cancel_mutation_admission(text,uuid,uuid,uuid,text,text) to authenticated;

create or replace function public.ab_ack_mutation_admission_cancel(
  p_root text, p_protocol_epoch uuid, p_owner_id uuid, p_operation_id uuid,
  p_kind text, p_client_digest text
) returns boolean
language plpgsql volatile security definer set search_path = pg_catalog as $$
begin
  perform private.ab_mutation_admission_cancel(p_root,p_protocol_epoch,p_owner_id,
    p_operation_id,p_kind,p_client_digest,true);
  return true;
end
$$;
revoke all on function public.ab_ack_mutation_admission_cancel(text,uuid,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_ack_mutation_admission_cancel(text,uuid,uuid,uuid,text,text) to authenticated;

-- Inactive atomic membership write: a conditional chat meta.json replacement
-- and the actor's signed membership events, appended to one log in array
-- order, commit together or not at all. Authority is the CURRENT (pre-swap)
-- meta, exactly as for doc_compare_put and log_append, so a member who leaves
-- or deletes the chat still records the event that removes their access.
-- This grants nothing those two families do not: any member may already
-- replace the meta and append to a log of the chat. It never creates a meta
-- or replaces a tombstoned one. Anyone in the chat's tenure (a current or
-- former member) gets an answer: only a current member of the exact version
-- swaps, and everyone else gets the same terminal applied=false whatever
-- version they send. So an attempt that lost a race to its own removal, or to
-- the group's deletion, settles instead of being refused until an epoch
-- closure, and the answer tells a former member nothing about the chat since
-- they left (whether its meta changed, or when). Others are refused, except
-- once the room is purged (its meta row tombstoned): then every root member
-- gets that applied=false, so a former member cannot learn of the purge.
alter table public.ab_mutation_admission_legs
  drop constraint if exists ab_mutation_admission_legs_operation_kind_check;
alter table public.ab_mutation_admission_legs
  add constraint ab_mutation_admission_legs_operation_kind_check check (
    operation_kind in (
      'doc_put','doc_compare_put','reaction_pair','meta_append','doc_create',
      'doc_delete','log_append','effect_transition','chat_delete','member_claim',
      'member_leave','blob_manifest'
    )
  );

create or replace function public.ab_mutation_meta_append(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text, p_data jsonb,
  p_expected_source_epoch uuid, p_expected_seq bigint, p_expected_deleted boolean,
  p_log text, p_records jsonb
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_provider_digest text;
  v_begin jsonb;
  v_source_epoch uuid;
  v_record jsonb;
  v_bytes bigint;
  v_meta jsonb;
  v_seq bigint;
  v_deleted boolean;
  v_applied boolean := false;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1)
     or not private.ab_mutation_path_valid(p_log, 4096, 32)
     or p_data is null
     or pg_catalog.jsonb_typeof(p_data) <> 'object'
     or p_records is null
     or pg_catalog.jsonb_typeof(p_records) <> 'array'
     or pg_catalog.jsonb_array_length(p_records) not between 1 and 256
     or p_expected_source_epoch is null
     or p_expected_seq is null or p_expected_seq < 1
     or p_expected_deleted is distinct from false then
    raise exception 'invalid meta append' using errcode = '22023';
  end if;
  v_bytes := public.ab_source_json_bytes(p_data);
  for v_record in select r.value from pg_catalog.jsonb_array_elements(p_records) r loop
    if pg_catalog.jsonb_typeof(v_record) <> 'object' then
      raise exception 'invalid meta append' using errcode = '22023';
    end if;
    v_bytes := v_bytes + public.ab_source_text_bytes(v_record::text);
  end loop;
  if v_bytes > 8355840 then
    raise exception 'invalid meta append' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('meta_append', p_chat, p_data,
      p_expected_source_epoch, p_expected_seq, p_expected_deleted,
      p_log, p_records)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'meta_append',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;
  -- Same lock order as doc_compare_put on a meta path and chat_delete.
  v_member := private.ab_mutation_lock_actor(p_root);
  perform private.ab_mutation_lock_chat(p_root, p_chat, true);
  select d.data, d.seq, d.deleted into v_meta, v_seq, v_deleted
  from public.ab_docs d
  where d.root = p_root and d.path = 'chats/' || p_chat || '/meta.json';
  -- Tenure (current or former member) earns an answer; see the header. So
  -- does every root member once the room is purged: nothing is left to tell
  -- former members from others, and a refusal would reveal the purge to them.
  if v_actor is null or v_member = '' or v_meta is null
     or not (v_deleted or coalesce(v_meta->'members' ? v_member
                                   or v_meta->'tenure' ? v_member, false)) then
    raise exception 'meta append is unauthorized' using errcode = '42501';
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || p_root, 0)
  );
  select e.epoch into v_source_epoch from public.ab_change_epochs e
  where e.root = p_root and e.source_schema_version = 2;
  -- Only a current member of the exact version swaps; for anyone else in the
  -- tenure the answer is applied=false whatever the version (see the header).
  if v_source_epoch = p_expected_source_epoch and v_seq = p_expected_seq
     and v_deleted = p_expected_deleted
     and public.ab_is_member(p_root, p_chat) then
    update public.ab_docs d set data = p_data, deleted = false
    where d.root = p_root and d.path = 'chats/' || p_chat || '/meta.json'
      and d.seq = p_expected_seq and d.deleted = p_expected_deleted;
    v_applied := found;
    if v_applied then
      for v_record in
        select r.value from pg_catalog.jsonb_array_elements(p_records)
          with ordinality r(value, ordinal)
        order by r.ordinal
      loop
        insert into public.ab_logs(root, chat_id, log_name, line)
        values (p_root, p_chat, p_log, v_record::text);
      end loop;
    end if;
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'meta_append',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('chat_id', p_chat, 'log_name', p_log,
                                 'applied', v_applied)
  );
end
$$;
revoke all on function public.ab_mutation_meta_append(
  text,uuid,uuid,text,text,jsonb,uuid,bigint,boolean,text,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_meta_append(
  text,uuid,uuid,text,text,jsonb,uuid,bigint,boolean,text,jsonb)
  to authenticated;

-- ---------------------------------------------------------------------------
-- Name retention and the private poke channel (2026-10-17). Idempotent;
-- paste and Run. Design: docs/NAME_RETENTION_AND_PRIVATE_POKES.md.
--
-- A name is held by an ab_members row (a login), by a users/<name>.json row
-- (an account, human or agent), or by both. Rows of both kinds are kept
-- forever, and the two tables agree on who owns a name, so no login can
-- inherit chat access by claiming a name someone else used or uses.

alter table public.ab_members add column if not exists revoked_at timestamptz;
-- Declared since R84, but a database built another way may lack it, and
-- the trigger below compares it.
alter table public.ab_members
  add column if not exists added_at timestamptz not null default now();

-- Every role, the service role included: no delete; username, uid and
-- added_at are fixed; revoked_at only goes from null to a time. The database
-- owner can still remove a row by disabling this trigger on purpose.
create or replace function private.ab_members_keep_names()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'a claimed name is never freed; revoke it instead'
      using errcode = '55000';
  end if;
  if new.username is distinct from old.username
     or new.uid is distinct from old.uid
     or new.added_at is distinct from old.added_at then
    raise exception 'member identity is immutable' using errcode = '22023';
  end if;
  if old.revoked_at is not null
     and new.revoked_at is distinct from old.revoked_at then
    raise exception 'a revoked name stays revoked' using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function private.ab_members_keep_names()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_members_keep_names on public.ab_members;
create trigger ab_members_keep_names
before update or delete on public.ab_members for each row
execute function private.ab_members_keep_names();

-- One transaction lock per (root, name), taken by both triggers below
-- before they look, so a claim and an account creation for the same name
-- cannot both pass under READ COMMITTED.
create or replace function private.ab_lock_name(p_root text, p_name text)
returns void language sql volatile set search_path = pg_catalog as $$
  select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-name:' || pg_catalog.jsonb_build_array(p_root, p_name)::text,
    0))
$$;
revoke all on function private.ab_lock_name(text, text)
  from public, anon, authenticated, service_role;

-- A login may not claim a name that belongs to an account (an agent, or a
-- human whose doc exists), live or tombstoned. A row this login or name
-- already has falls through to the unique constraints, which refuse it as
-- before, so "already yours" stays recognisable. Definer: a not-yet-member
-- cannot read ab_docs. The service role and the database owner are exempt
-- (the operator's seed; the legacy service-key fleet).
create or replace function private.ab_members_one_owner()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
begin
  perform private.ab_lock_name(new.root, new.username);
  if coalesce(auth.role(), '') in ('', 'service_role') then
    return new;
  end if;
  if exists (
    select 1 from public.ab_members m
    where m.root = new.root and (m.uid = new.uid or m.username = new.username)
  ) then
    return new;
  end if;
  if exists (
    select 1 from public.ab_docs d
    where d.root = new.root and d.path = 'users/' || new.username || '.json'
  ) then
    raise exception 'name belongs to an existing account'
      using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function private.ab_members_one_owner()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_members_one_owner on public.ab_members;
create trigger ab_members_one_owner
before insert on public.ab_members for each row
execute function private.ab_members_one_owner();

-- An account doc may not be created for a name another login holds. A
-- human's own doc passes (their login holds the name), and so does a new
-- agent's (no login holds it). An upsert fires this too, which is cheap.
create or replace function private.ab_docs_one_owner()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_name text;
begin
  if new.path !~ '^users/[^/]+\.json$' then
    return new;
  end if;
  v_name := pg_catalog.substr(new.path, 7, pg_catalog.length(new.path) - 11);
  perform private.ab_lock_name(new.root, v_name);
  if coalesce(auth.role(), '') in ('', 'service_role') then
    return new;
  end if;
  if exists (
    select 1 from public.ab_members m
    where m.root = new.root and m.username = v_name
      and m.uid is distinct from auth.uid()
  ) then
    raise exception 'name is claimed by another login'
      using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function private.ab_docs_one_owner()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_one_owner on public.ab_docs;
create trigger ab_docs_one_owner
before insert on public.ab_docs for each row
when (new.path like 'users/%')
execute function private.ab_docs_one_owner();

-- Account rows outlive everything: a hard delete of a users/ row is skipped
-- (the row stays, no error), for every role. Skipping rather than raising
-- keeps an older client's bulk tombstone purge working for every other row.
-- Soft deletes (deleted = true) are updates and still work.
create or replace function private.ab_docs_keep_accounts()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  return null;
end
$$;
revoke all on function private.ab_docs_keep_accounts()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_keep_accounts on public.ab_docs;
create trigger ab_docs_keep_accounts
before delete on public.ab_docs for each row
when (old.path like 'users/%')
execute function private.ab_docs_keep_accounts();

-- The poke channel ab-<root> is private: only active members of the root
-- may join (receive) or send. Roots may contain '-', so the topic is never
-- split; the root is everything after the 'ab-' prefix. Supabase also needs
-- "Allow public access" turned off in the Realtime settings.
create or replace function public.ab_poke_topic_ok(p_topic text)
returns boolean language sql stable set search_path = pg_catalog as $$
  select coalesce(
    pg_catalog.left(p_topic, 3) = 'ab-'
    and pg_catalog.length(p_topic) > 3
    and public.ab_root_ok(pg_catalog.substr(p_topic, 4)),
    false)
$$;
revoke all on function public.ab_poke_topic_ok(text) from public, anon;
grant execute on function public.ab_poke_topic_ok(text) to authenticated;

do $realtime$
begin
  -- Plain PostgreSQL (the CI gate) has no Realtime schema.
  if pg_catalog.to_regclass('realtime.messages') is null then
    return;
  end if;
  execute 'drop policy if exists ab_poke_receive on realtime.messages';
  execute $policy$
    create policy ab_poke_receive on realtime.messages
    for select to authenticated using (
      realtime.messages.extension = 'broadcast'
      and (select public.ab_poke_topic_ok((select realtime.topic())))
    )
  $policy$;
  execute 'drop policy if exists ab_poke_send on realtime.messages';
  execute $policy$
    create policy ab_poke_send on realtime.messages
    for insert to authenticated with check (
      realtime.messages.extension = 'broadcast'
      and (select public.ab_poke_topic_ok((select realtime.topic())))
    )
  $policy$;
end
$realtime$;

-- ---------------------------------------------------------------------------
-- Commit-ordered feeds (P7, 2026-10-18). Idempotent; paste and Run.
-- Design: docs/COMMIT_ORDERED_CURSORS.md.
--
-- ab_docs.seq and ab_logs.id are handed out before commit, so a reader of
-- `seq > cursor` can pass a row whose transaction commits later and never
-- see it. Each row now records its writing transaction, and the two feeds
-- below serve only rows below the oldest transaction still running (the
-- frontier): a transaction below it has finished and can write nothing
-- more, so a cursor never passes a row that has not committed. Old clients
-- keep reading seq and id directly and see no change.

-- Constant defaults: no table rewrite. Existing rows are older than any
-- frontier a client stores from now on.
alter table public.ab_docs
  add column if not exists txid xid8 not null default '0';
alter table public.ab_logs
  add column if not exists txid xid8 not null default '0';

-- The top-level transaction, also inside subtransactions. A separate
-- trigger rather than ab_docs_touch, so that function keeps one definition.
create or replace function private.ab_docs_record_txid()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  new.txid := pg_catalog.pg_current_xact_id();
  return new;
end
$$;
revoke all on function private.ab_docs_record_txid()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_txid on public.ab_docs;
create trigger ab_docs_txid
before insert or update on public.ab_docs for each row
execute function private.ab_docs_record_txid();

-- Ids within one log follow commit order, so a client's per-log offset
-- (`id > offset`) can never pass a row still in flight. The trigger takes
-- the lock ab_recovery_record_log_heads already takes after every append,
-- but before the id is drawn: a second append to the same log waits until
-- the first commits or rolls back, then draws a larger id. The identity
-- value drawn before this trigger is discarded (ids are not dense anyway).
-- Lock order: the change ledger's shared root lock first, this log lock
-- second, on every path. The trigger takes the ledger lock itself rather
-- than rely on ab_mutation_legacy_gate (absent on a project without the
-- typed protocol, where the ledger's own triggers take it only AFTER the
-- row): a log-then-ledger writer could otherwise deadlock with a
-- ledger-then-log one behind a queued exclusive page read, seal or fence.
-- Re-taking a shared lock this transaction holds costs nothing.
-- Definer: the inserting role has no right to call nextval directly. An
-- import that must keep its ids disables this trigger on purpose.
create or replace function private.ab_order_log_id()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
begin
  perform pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended(
    'agentbridge-change-ledger:' || new.root, 0));
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-log-head:' || pg_catalog.jsonb_build_array(
      new.root, new.chat_id, new.log_name)::text,
    0));
  new.id := pg_catalog.nextval(
    pg_catalog.pg_get_serial_sequence('public.ab_logs', 'id')::regclass);
  new.txid := pg_catalog.pg_current_xact_id();
  return new;
end
$$;
revoke all on function private.ab_order_log_id()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_order_log_id on public.ab_logs;
create trigger ab_order_log_id
before insert on public.ab_logs for each row
execute function private.ab_order_log_id();

create index if not exists ab_docs_feed_order
  on public.ab_docs (root, txid, seq);
create index if not exists ab_logs_feed_order
  on public.ab_logs (root, txid, id);

-- "Cursor too old": a hard-deleted doc row leaves no trace in the feed, so
-- a reader that may have missed it must pull everything. A purged tombstone
-- raises the root's floor to its own (txid, seq): a reader past it already
-- applied the delete. A live row (only an operator can hard-delete one; the
-- member delete policy allows tombstones only) raises it to the deleting
-- transaction itself, (txid, 1): no reader can learn of that delete from
-- the feed, so every cursor up to it is too old. In practice the janitor's
-- purge of tombstones older than 30 days. Its own table: ab_change_epochs
-- refuses every update. An empty delete inserts nothing.
create table if not exists public.ab_doc_floors (
  root text primary key,
  txid xid8 not null,
  seq  bigint not null
);
alter table public.ab_doc_floors enable row level security;
revoke all on table public.ab_doc_floors from public, anon, authenticated;
grant select on table public.ab_doc_floors to authenticated;
drop policy if exists ab_doc_floors_member_select on public.ab_doc_floors;
create policy ab_doc_floors_member_select on public.ab_doc_floors
for select to authenticated using ((select public.ab_root_ok(root)));

create or replace function private.ab_docs_raise_floor()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
begin
  insert into public.ab_doc_floors as f (root, txid, seq)
  select distinct on (c.root) c.root, c.txid, c.seq
  from (
    select d.root,
           case when d.deleted then d.txid
                else pg_catalog.pg_current_xact_id() end as txid,
           case when d.deleted then d.seq else 1::bigint end as seq
    from old_rows d
  ) c
  order by c.root, c.txid desc, c.seq desc
  on conflict (root) do update
    set txid = excluded.txid, seq = excluded.seq
    where (excluded.txid, excluded.seq) > (f.txid, f.seq);
  return null;
end
$$;
revoke all on function private.ab_docs_raise_floor()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_floor on public.ab_docs;
create trigger ab_docs_floor
after delete on public.ab_docs
referencing old table as old_rows
for each statement execute function private.ab_docs_raise_floor();

-- The feeds. Each answer is one jsonb value computed in one statement, so
-- the frontier, the page, the floor and the epoch share one snapshot.
-- `has_more` comes from reading one row past the limit, so a server row cap
-- can never make a cut-off page look final. `next` is the last row's
-- position while has_more, else (frontier, 0): everything below the
-- frontier has been read, and that frontier is what a client stores.
-- Transaction ids travel as bigint. RLS filters rows as for direct reads.
-- `too_old`: the cursor is below the floor, or above the server's next
-- transaction id (the server went back in time: a restore); the client
-- then pulls everything. The epoch changes on a restore (P10).
create or replace function public.ab_feeds_frontier(p_root text)
returns jsonb
language sql stable security invoker set search_path = pg_catalog as $$
  select pg_catalog.jsonb_build_object(
    'frontier', pg_catalog.pg_snapshot_xmin(
      pg_catalog.pg_current_snapshot())::text::bigint,
    'epoch', (select e.epoch from public.ab_change_epochs e
              where e.root = p_root))
$$;
revoke all on function public.ab_feeds_frontier(text) from public, anon;
grant execute on function public.ab_feeds_frontier(text)
  to authenticated, service_role;

create or replace function public.ab_docs_feed(
  p_root text, p_after_txid bigint, p_after_seq bigint, p_limit integer
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog as $$
declare
  v_out jsonb;
begin
  if p_root is null or p_root = ''
     or p_after_txid is null or p_after_txid < 0
     or p_after_seq is null or p_after_seq < 0
     or p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'invalid doc feed request' using errcode = '22023';
  end if;
  with snap as (
    select pg_catalog.pg_current_snapshot() as s
  ), bounds as (
    select pg_catalog.pg_snapshot_xmin(snap.s) as frontier,
           pg_catalog.pg_snapshot_xmax(snap.s) as horizon,
           p_after_txid::text::xid8 as after_txid
    from snap
  ), doc_floor as (
    select f.txid, f.seq from public.ab_doc_floors f where f.root = p_root
  ), verdict as (
    select b.*,
           (b.after_txid > b.horizon
            or exists (select 1 from doc_floor f
                       where (b.after_txid, p_after_seq) < (f.txid, f.seq)))
             as too_old
    from bounds b
  ), page as (
    select d.path, d.data, d.deleted, d.seq, d.txid
    from public.ab_docs d, verdict v
    where not v.too_old
      and d.root = p_root
      and (d.txid, d.seq) > (v.after_txid, p_after_seq)
      and d.txid < v.frontier
    order by d.txid, d.seq
    limit p_limit + 1
  ), numbered as (
    select p.*, pg_catalog.row_number() over (order by p.txid, p.seq) as n
    from page p
  )
  select pg_catalog.jsonb_build_object(
    'rows', coalesce((
      select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'path', n.path, 'data', n.data, 'deleted', n.deleted,
        'seq', n.seq, 'txid', n.txid::text::bigint) order by n.n)
      from numbered n where n.n <= p_limit), '[]'::jsonb),
    'has_more', (select pg_catalog.count(*) from page) > p_limit,
    'next', case
      when (select pg_catalog.count(*) from page) > p_limit then (
        select pg_catalog.jsonb_build_array(n.txid::text::bigint, n.seq)
        from numbered n where n.n = p_limit)
      else pg_catalog.jsonb_build_array(v.frontier::text::bigint, 0)
    end,
    'frontier', v.frontier::text::bigint,
    'epoch', (select e.epoch from public.ab_change_epochs e
              where e.root = p_root),
    'too_old', v.too_old)
  into v_out
  from verdict v;
  return v_out;
end
$$;
revoke all on function public.ab_docs_feed(text, bigint, bigint, integer)
  from public, anon;
grant execute on function public.ab_docs_feed(text, bigint, bigint, integer)
  to authenticated, service_role;

-- The root-wide log feed names which logs grew; each log is then read from
-- its own integer offset, which the per-log order above keeps safe. No
-- floor: only a deleted chat's logs are hard-deleted, and its tombstone and
-- the visible-set check already tell every reader.
create or replace function public.ab_logs_feed(
  p_root text, p_after_txid bigint, p_after_id bigint, p_limit integer
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog as $$
declare
  v_out jsonb;
begin
  if p_root is null or p_root = ''
     or p_after_txid is null or p_after_txid < 0
     or p_after_id is null or p_after_id < 0
     or p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'invalid log feed request' using errcode = '22023';
  end if;
  with snap as (
    select pg_catalog.pg_current_snapshot() as s
  ), verdict as (
    select pg_catalog.pg_snapshot_xmin(snap.s) as frontier,
           p_after_txid::text::xid8 as after_txid,
           p_after_txid::text::xid8 > pg_catalog.pg_snapshot_xmax(snap.s)
             as too_old
    from snap
  ), page as (
    select l.id, l.chat_id, l.log_name, l.txid
    from public.ab_logs l, verdict v
    where not v.too_old
      and l.root = p_root
      and (l.txid, l.id) > (v.after_txid, p_after_id)
      and l.txid < v.frontier
    order by l.txid, l.id
    limit p_limit + 1
  ), numbered as (
    select p.*, pg_catalog.row_number() over (order by p.txid, p.id) as n
    from page p
  )
  select pg_catalog.jsonb_build_object(
    'rows', coalesce((
      select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'id', n.id, 'chat_id', n.chat_id, 'log_name', n.log_name,
        'txid', n.txid::text::bigint) order by n.n)
      from numbered n where n.n <= p_limit), '[]'::jsonb),
    'has_more', (select pg_catalog.count(*) from page) > p_limit,
    'next', case
      when (select pg_catalog.count(*) from page) > p_limit then (
        select pg_catalog.jsonb_build_array(n.txid::text::bigint, n.id)
        from numbered n where n.n = p_limit)
      else pg_catalog.jsonb_build_array(v.frontier::text::bigint, 0)
    end,
    'frontier', v.frontier::text::bigint,
    'epoch', (select e.epoch from public.ab_change_epochs e
              where e.root = p_root),
    'too_old', v.too_old)
  into v_out
  from verdict v;
  return v_out;
end
$$;
revoke all on function public.ab_logs_feed(text, bigint, bigint, integer)
  from public, anon;
grant execute on function public.ab_logs_feed(text, bigint, bigint, integer)
  to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- One copy per appended line (2026-10-20). Idempotent; paste and Run.
--
-- A log append is a plain INSERT that clients retry after a transient
-- error. When the first try had committed and only its reply was lost, the
-- retry wrote a second, byte-identical row (production held 8 such pairs).
-- A line carries a unique envelope id, a nonce and a signature, so an
-- identical line in the same log is only ever a retry.
--
-- The trigger skips such a retry, so every client, old or new, sees it
-- succeed (an error would park an old client's outbox, and every later
-- send to that chat behind it, for good). It first takes the locks
-- ab_order_log_id takes, in the same order, and the log lock is held to
-- commit: a retry racing its first try waits for that commit, then sees
-- the line. Its name orders it after ab_mutation_legacy_gate (a sealed
-- root still refuses the write, and no lock moves ahead of the gate) and
-- before ab_order_log_id (a skipped line draws no id). It runs as the
-- caller, so only someone who can read the line is told it is there;
-- anyone else meets the insert policy as before.
-- The unique index serves the lookup and backs the trigger: a copy that
-- still arrives is a 23505, which the client accepts once it reads the
-- identical line back. Existing copies go first (the lowest id stays; the
-- delete trigger moves each log's head and tells readers), under a lock
-- that keeps a new copy from landing before the index exists.
do $line_once$
begin
  lock table public.ab_logs in share row exclusive mode;
  delete from public.ab_logs l
  using public.ab_logs k
  where k.root = l.root and k.chat_id = l.chat_id
    and k.log_name = l.log_name and k.line = l.line and k.id < l.id;
  create unique index if not exists ab_logs_line_once
    on public.ab_logs (root, chat_id, log_name, md5(line));
end
$line_once$;

create or replace function private.ab_omit_landed_line()
returns trigger language plpgsql security invoker set search_path = pg_catalog as $$
begin
  perform pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended(
    'agentbridge-change-ledger:' || new.root, 0));
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-log-head:' || pg_catalog.jsonb_build_array(
      new.root, new.chat_id, new.log_name)::text,
    0));
  if exists (
    select 1 from public.ab_logs l
    where l.root = new.root and l.chat_id = new.chat_id
      and l.log_name = new.log_name
      and pg_catalog.md5(l.line) = pg_catalog.md5(new.line)
      and l.line = new.line
  ) then
    return null;
  end if;
  return new;
end
$$;
revoke all on function private.ab_omit_landed_line()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_omit_landed_line on public.ab_logs;
create trigger ab_omit_landed_line
before insert on public.ab_logs for each row
execute function private.ab_omit_landed_line();
