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
    when 'member_leave' then p_table = 'ab_members' and p_operation = 'DELETE'
    when 'blob_manifest' then p_table = 'ab_blob_manifests'
      and p_operation = 'INSERT'
    else false
  end
$$;
revoke all on function private.ab_mutation_kind_permits(text,text,text)
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
