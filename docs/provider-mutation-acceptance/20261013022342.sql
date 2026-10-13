-- Exact non-secret SQL captured from disposable Supabase migration
-- 20261013022342_agentbridge_p1b_core_slot_bound.
-- Evidence only: docs/supabase_schema.sql remains the source.
-- BEGIN APPLIED STATEMENT

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
    when 'member_leave' then p_table = 'ab_members' and p_operation = 'DELETE'
    when 'blob_manifest' then p_table = 'ab_docs'
      and p_operation in ('INSERT', 'UPDATE')
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
begin
  if p_root is null or p_root = ''
     or pg_catalog.octet_length(p_root) > 1024
     or p_protocol_epoch is null or p_operation_id is null
     or p_operation_kind is null or p_operation_kind not in (
       'doc_put','doc_create','doc_delete','log_append','effect_transition',
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

  v_base := (pg_catalog.hashtextextended(p_operation_id::text, 0) & 4095)::integer;
  for v_probe in 0..4095 loop
    v_slot := (v_base + v_probe) % 4096;
    -- Do not accumulate one heavyweight transaction lock per immutable
    -- occupied slot in a near-full epoch.  The first read skips settled rows;
    -- the second closes the race after the nonblocking candidate lock.
    if exists (
      select 1 from public.ab_mutation_receipts r
      where r.root = p_root and r.protocol_epoch = p_protocol_epoch
        and r.receipt_slot = v_slot
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

  select pg_catalog.count(*) into v_count
  from public.ab_mutation_receipts r
  where r.root = p_root and r.protocol_epoch = p_protocol_epoch;
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
  delete from public.ab_mutation_receipts r
  where r.root = p_root and r.protocol_epoch = p_expected_epoch;
  v_next_epoch := pg_catalog.gen_random_uuid();
  update public.ab_mutation_roots
  set previous_protocol_epoch = p_expected_epoch,
      protocol_epoch = v_next_epoch,
      protocol_version = 2,
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
-- END APPLIED STATEMENT
