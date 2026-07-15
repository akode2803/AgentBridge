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
-- Identity: each mesh member is a Supabase AUTH user provisioned by the
-- mesh owner (python -m agentbridge.transport.supabase_admin provision).
-- The authorization claims live in app_metadata (ADMIN-set; user_metadata
-- is self-editable by the user and must never gate anything):
--   app_metadata: { "ab_member": "<username>", "ab_roots": ["mesh2"] }
--
-- The ACL for chat lanes is the chat's own meta doc
-- (chats/<id>/meta.json -> data.members object): it is maintained on every
-- membership change, written meta-FIRST at genesis (R25 ordered it so "the
-- member gate holds from here on"), and rewritten by a current member on
-- add/remove. RLS here is the coarse ACCESS gate; the authenticated event
-- fold and E2EE remain the source of truth for what a member can READ.

create or replace function public.ab_member() returns text
language sql stable as $$
  select coalesce((auth.jwt()->'app_metadata')->>'ab_member', '')
$$;

create or replace function public.ab_root_ok(p_root text) returns boolean
language sql stable as $$
  select public.ab_member() <> ''
     and coalesce((auth.jwt()->'app_metadata')->'ab_roots' ? p_root, false)
$$;

create or replace function public.ab_chat_of(p_path text) returns text
language sql immutable as $$
  select case when p_path like 'chats/%'
              then split_part(p_path, '/', 2) else '' end
$$;

-- SECURITY DEFINER: the membership lookup itself reads ab_docs — evaluated
-- inside an ab_docs policy it would recurse through RLS. Deliberately does
-- NOT filter tombstoned metas: during the deletion grace the members'
-- janitors still need access to purge the subtree.
create or replace function public.ab_is_member(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.ab_docs m
    where m.root = p_root
      and m.path = 'chats/' || p_chat || '/meta.json'
      and m.data->'members' ? public.ab_member()
  )
$$;
revoke all on function public.ab_is_member(text, text) from public;
grant execute on function public.ab_is_member(text, text) to authenticated;

-- ab_docs: global lanes (users/, status/, control/, machines/, presence/…)
-- are mesh-wide by design — any member of the root reads and writes them
-- (the app's own rules arbitrate within; E2EE seals what must be sealed).
-- chats/ lanes are members-only, with ONE insert exception: genesis — a
-- fresh meta.json for a chat with no meta yet, listing the creator among
-- its members. Chat ids commit to their genesis hash (R13.5), so squatting
-- a foreign id is not practical.
drop policy if exists ab_docs_member_select on public.ab_docs;
create policy ab_docs_member_select on public.ab_docs
for select to authenticated using (
  public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_is_member(root, public.ab_chat_of(path))
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
      and data->'members' ? public.ab_member()
    )
  )
);

drop policy if exists ab_docs_member_update on public.ab_docs;
create policy ab_docs_member_update on public.ab_docs
for update to authenticated using (
  public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_is_member(root, public.ab_chat_of(path))
  )
) with check (public.ab_root_ok(root));

drop policy if exists ab_docs_member_delete on public.ab_docs;
create policy ab_docs_member_delete on public.ab_docs
for delete to authenticated using (
  public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_is_member(root, public.ab_chat_of(path))
  )
);

-- ab_logs: members only, both ways; deletes cover the janitor's purge of a
-- deleted chat's logs. Genesis is safe by ordering: meta.json lands before
-- the first log record (R25).
drop policy if exists ab_logs_member_select on public.ab_logs;
create policy ab_logs_member_select on public.ab_logs
for select to authenticated using (
  public.ab_root_ok(root) and public.ab_is_member(root, chat_id)
);

drop policy if exists ab_logs_member_insert on public.ab_logs;
create policy ab_logs_member_insert on public.ab_logs
for insert to authenticated with check (
  public.ab_root_ok(root) and public.ab_is_member(root, chat_id)
);

drop policy if exists ab_logs_member_delete on public.ab_logs;
create policy ab_logs_member_delete on public.ab_logs
for delete to authenticated using (
  public.ab_root_ok(root) and public.ab_is_member(root, chat_id)
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
    or public.ab_is_member(split_part(name, '/', 1), split_part(name, '/', 3))
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
    or public.ab_is_member(split_part(name, '/', 1), split_part(name, '/', 3))
  )
);
