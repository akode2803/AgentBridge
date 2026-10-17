begin;
-- Existing product prerequisites absent from the older scratch scaffold.
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
    'doc_put','doc_compare_put','reaction_pair','doc_create','doc_delete','log_append',
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
       'doc_put','doc_compare_put','reaction_pair','doc_create','doc_delete','log_append','effect_transition',
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
    select l.*, g.actor_uid into v_leg from public.ab_mutation_admission_legs l
      join public.ab_mutation_admission_groups g using(root, protocol_epoch, owner_id)
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.operation_id = p_operation_id;
    if not found or v_leg.actor_uid is distinct from v_actor
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
-- Effect-free immutable owner admission. Epoch lock always precedes the root
-- admission mutex. Execution/cancellation use epoch then operation lock and
-- never acquire the admission mutex, so the lock order cannot invert.
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
         'doc_put','doc_compare_put','reaction_pair','doc_create','doc_delete','log_append',
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
  if exists (select 1 from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.operation_id = any(v_ids)) then
    raise exception 'mutation admission operation collision' using errcode = '55000';
  end if;
  select pg_catalog.count(*) into v_occupied from public.ab_mutation_admission_legs l
    where l.root = p_root and l.protocol_epoch = p_protocol_epoch;
  if v_occupied + v_count > 4096 then
    -- No control row is needed: no source effect is eligible without positive
    -- durable admission. Slots, including cancelled legs, never free in-epoch.
    return pg_catalog.jsonb_build_object('schema_version', 1, 'root', p_root,
      'actor_id', v_actor, 'protocol_epoch', p_protocol_epoch, 'owner_id', p_owner_id,
      'status', 'full', 'plan', p_plan);
  end if;
  select pg_catalog.array_agg(candidate order by candidate) into v_slots from (
    select slot as candidate from pg_catalog.generate_series(0,4095) as slot
    where not exists (select 1 from public.ab_mutation_admission_legs l
      where l.root = p_root and l.protocol_epoch = p_protocol_epoch and l.receipt_slot = slot)
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

do $$
declare
  r text := 'admission-acceptance-' || replace(gen_random_uuid()::text,'-','');
  a uuid := gen_random_uuid(); e uuid; s jsonb;
begin
  insert into public.ab_members(root,username,uid) values(r,'admission-tester',a);
  insert into public.ab_change_epochs(root) values(r);
  select protocol_epoch into e from public.ab_mutation_roots where root=r;
  s := public.ab_admin_seal_legacy_database_mutations(r,e,gen_random_uuid(),repeat('a',64));
  e := (s->>'protocol_epoch')::uuid;
  perform public.ab_admin_enable_mutation_admission(r,e);
  perform set_config('ab.test.root',r,true);
  perform set_config('ab.test.epoch',e::text,true);
  perform set_config('ab.test.actor',a::text,true);
  perform set_config('request.jwt.claim.sub',a::text,true);
  perform set_config('request.jwt.claim.role','authenticated',true);
end $$;
set local role authenticated;
do $$
declare
  r text := current_setting('ab.test.root'); e uuid := current_setting('ab.test.epoch')::uuid;
  owner uuid := gen_random_uuid(); op uuid := gen_random_uuid(); other uuid := gen_random_uuid();
  plan jsonb; a jsonb; b jsonb; receipt jsonb; cancelled jsonb; rejected boolean := false;
begin
  plan := jsonb_build_array(jsonb_build_object('operation_id',op,'kind','doc_put','client_digest',repeat('a',64)),
                           jsonb_build_object('operation_id',other,'kind','doc_put','client_digest',repeat('b',64)));
  a := public.ab_admit_mutation_owner(r,e,owner,plan);
  b := public.ab_admit_mutation_owner(r,e,owner,plan);
  if a is distinct from b or a->>'status' <> 'admitted' then raise exception 'admission replay failed'; end if;
  begin
    perform public.ab_mutation_doc_put(r,e,gen_random_uuid(),repeat('c',64),'unadmitted.json','{}');
  exception when sqlstate '55000' then rejected := true;
  end;
  if not rejected then raise exception 'unadmitted effect allowed'; end if;
  receipt := public.ab_mutation_doc_put(r,e,op,repeat('a',64),'admitted.json','{"value":1}');
  if public.ab_cancel_mutation_admission(r,e,owner,op,'doc_put',repeat('a',64))->>'status' <> 'executed' then
    raise exception 'cancel lost committed effect'; end if;
  cancelled := public.ab_cancel_mutation_admission(r,e,owner,other,'doc_put',repeat('b',64));
  if cancelled->>'status' <> 'cancelled' or cancelled is distinct from public.ab_cancel_mutation_admission(r,e,owner,other,'doc_put',repeat('b',64)) then
    raise exception 'cancel replay failed'; end if;
  rejected := false;
  begin
    perform public.ab_mutation_doc_put(r,e,other,repeat('b',64),'cancelled.json','{}');
  exception when sqlstate '55000' then rejected := true;
  end;
  if not rejected then raise exception 'cancelled effect allowed'; end if;
  perform public.ab_ack_mutation_receipt(r,e,op,repeat('a',64),receipt->>'provider_digest');
  perform set_config('ab.test.owner',owner::text,true);
  perform set_config('ab.test.cancelled',other::text,true);
end $$;
reset role;
do $$
declare rejected boolean := false;
begin
  begin
    perform public.ab_admin_rotate_mutation_epoch(current_setting('ab.test.root'),current_setting('ab.test.epoch')::uuid,gen_random_uuid());
  exception when sqlstate '55000' then rejected := true;
  end;
  if not rejected then raise exception 'rotation forgot unacknowledged cancellation'; end if;
end $$;
set local role authenticated;
select public.ab_ack_mutation_admission_cancel(current_setting('ab.test.root'),current_setting('ab.test.epoch')::uuid,
  current_setting('ab.test.owner')::uuid,current_setting('ab.test.cancelled')::uuid,'doc_put',repeat('b',64));
reset role;
do $$
declare rotated jsonb;
begin
  rotated := public.ab_admin_rotate_mutation_epoch(current_setting('ab.test.root'),current_setting('ab.test.epoch')::uuid,gen_random_uuid());
  if rotated->>'protocol_epoch' = current_setting('ab.test.epoch') then raise exception 'rotation did not advance'; end if;
  if exists(select 1 from public.ab_docs where root=current_setting('ab.test.root') and path in ('unadmitted.json','cancelled.json')) then
    raise exception 'cancelled/unadmitted source appeared'; end if;
  if has_function_privilege('anon','public.ab_admit_mutation_owner(text,uuid,uuid,jsonb)','execute') then
    raise exception 'anonymous admission access'; end if;
  if has_function_privilege('authenticated','public.ab_admin_enable_mutation_admission(text,uuid)','execute') then
    raise exception 'authenticated admin enable access'; end if;
end $$;
select 'provider admission rollback-only acceptance passed' as result;

rollback;
