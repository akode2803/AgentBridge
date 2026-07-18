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
                   where root = p_root and uid = auth.uid()), '')
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
for insert to authenticated with check (uid = auth.uid());

drop policy if exists ab_members_leave on public.ab_members;
create policy ab_members_leave on public.ab_members
for delete to authenticated using (uid = auth.uid());

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
      and data->'members' ? public.ab_member(root)
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
