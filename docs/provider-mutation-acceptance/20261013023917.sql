-- AgentBridge Scratch migration 20261013023917
-- Project: riagjoluhcwjvmfchbzl
-- Name: agentbridge_p1b_typed_doc_log_rpcs
-- Applied statement bytes: 10923
-- Applied statement SHA-256: 3ed6526a3e2abe91a1fd006199aeb7b353bef789430255020ec18aba6e80d3b2
-- BEGIN APPLIED STATEMENT

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

  v_member := public.ab_member(p_root);
  v_chat := public.ab_chat_of(p_path);
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

  v_member := public.ab_member(p_root);
  v_chat := public.ab_chat_of(p_path);
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

  v_member := public.ab_member(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_actor is null or v_member = ''
     or p_path like 'chats/%/runtime/effects/%'
     or (v_chat <> '' and not public.ab_can_read_chat(p_root, v_chat)) then
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

  if v_actor is null or public.ab_member(p_root) = ''
     or not public.ab_is_member(p_root, p_chat) then
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
-- END APPLIED STATEMENT
