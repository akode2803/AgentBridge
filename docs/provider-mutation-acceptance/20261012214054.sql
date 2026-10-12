-- Exact non-secret SQL captured from disposable Supabase migration
-- 20261012214054_agentbridge_p1a_mutation_protocol_acceptance.
-- Evidence only: docs/supabase_schema.sql remains the source.
-- BEGIN APPLIED STATEMENT

create schema if not exists private;

create table public.ab_docs (
  root text not null,
  path text not null,
  data jsonb not null,
  primary key (root, path)
);
create table public.ab_logs (
  id bigint generated always as identity primary key,
  root text not null,
  chat_id text not null,
  log_name text not null,
  line text not null
);
create table public.ab_members (
  root text not null,
  username text not null,
  uid uuid not null,
  primary key (root, username),
  unique (root, uid)
);
create table public.ab_change_epochs (
  root text primary key,
  epoch uuid not null default gen_random_uuid(),
  minimum_cursor bigint not null default 0,
  source_schema_version integer not null default 2
);
create table public.ab_change_events (
  id bigint generated always as identity primary key,
  root text not null
);

alter table public.ab_members enable row level security;
create or replace function public.ab_root_ok(p_root text) returns boolean
language sql stable security definer set search_path = pg_catalog as $$
  select exists (
    select 1 from public.ab_members m
    where m.root = p_root and m.uid = auth.uid()
  )
$$;
revoke all on function public.ab_root_ok(text) from public;
grant execute on function public.ab_root_ok(text) to authenticated;

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
    'doc_put','doc_create','doc_delete','log_append','effect_transition',
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

-- END APPLIED STATEMENT
