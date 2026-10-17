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
