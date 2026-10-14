-- Correction to 20261014064104: only a current member of the exact version
-- swaps; everyone else in the chat's tenure gets the same terminal
-- applied=false whatever version they send, so the answer tells a former
-- member nothing about the chat since they left (DECISIONS D-017).
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
  -- Tenure (current or former member) earns an answer; see the header.
  if v_actor is null or v_member = '' or v_meta is null
     or not coalesce(v_meta->'members' ? v_member
                     or v_meta->'tenure' ? v_member, false) then
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
