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

-- SECURITY DEFINER: these run inside policies over the very tables they
-- read — without definer they'd recurse through RLS. Owned by the schema
-- owner; search_path pinned.
create or replace function public.ab_member(p_root text) returns text
language sql stable security definer set search_path = public as $$
  select coalesce((select username from public.ab_members
                   where root = p_root and uid = (select auth.uid())), '')
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

-- A deleted room has no current members, but its former members must still be
-- able to observe the terminal meta/log evidence and perform bounded janitor
-- cleanup. This is read/delete authority only; insert/update policies continue
-- to use ab_is_member, so terminal rooms cannot receive new content.
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
          and m.data->'tenure' ? public.ab_member(p_root)
        )
      )
  )
$$;
revoke all on function public.ab_can_read_chat(text, text) from public;
grant execute on function public.ab_can_read_chat(text, text) to authenticated;

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
-- You may remove yourself; removing OTHERS is the owner's act (service
-- key), so a hostile member can't evict the mesh. Members see their
-- root's roster; outsiders see nothing.
drop policy if exists ab_members_select on public.ab_members;
create policy ab_members_select on public.ab_members
for select to authenticated using (public.ab_root_ok(root));

drop policy if exists ab_members_admit on public.ab_members;
drop policy if exists ab_members_claim on public.ab_members;
create policy ab_members_claim on public.ab_members
for insert to authenticated with check (uid = (select auth.uid()));

drop policy if exists ab_members_leave on public.ab_members;
create policy ab_members_leave on public.ab_members
for delete to authenticated using (uid = (select auth.uid()));

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

drop policy if exists ab_docs_member_delete on public.ab_docs;
create policy ab_docs_member_delete on public.ab_docs
for delete to authenticated using (
  public.ab_root_ok(root) and (
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

create or replace function private.ab_record_root_membership_change()
returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_root text;
begin
  if tg_op = 'DELETE' then
    v_root := old.root;
  else
    v_root := new.root;
  end if;
  perform pg_catalog.pg_advisory_xact_lock_shared(
    pg_catalog.hashtextextended('agentbridge-change-ledger:' || v_root, 0)
  );
  insert into public.ab_change_epochs(root) values (v_root)
  on conflict (root) do nothing;
  insert into public.ab_change_events(
    root, stream_kind, stream_id, domain
  ) values (v_root, 'root', '', 'visibility');
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;
revoke all on function private.ab_record_root_membership_change()
  from public, anon, authenticated;

drop trigger if exists ab_members_change_ledger on public.ab_members;
create trigger ab_members_change_ledger
after insert or delete on public.ab_members
for each row execute function private.ab_record_root_membership_change();

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
           1
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
  select 1
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
