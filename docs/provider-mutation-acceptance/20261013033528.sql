-- AgentBridge Scratch migration 20261013033528
-- Project: riagjoluhcwjvmfchbzl
-- Name: agentbridge_p1b_room_lock_lifecycle_rpcs
-- Applied statement bytes: 17877
-- Applied statement SHA-256: ef3599007152e351741a4179734996c55b429ad4f2a8a1d111d1f5e334d57f6c
-- BEGIN APPLIED STATEMENT

-- Fresh typed authority remains stable until the effect commits.  Root-member
-- rows are locked before room locks; room source writes share one lock while
-- meta authority changes and terminal cleanup take it exclusively.  Every
-- waiter rechecks authority only after acquiring the room lock.
create or replace function private.ab_mutation_lock_actor(p_root text)
returns text
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_member text;
begin
  select m.username into v_member
  from public.ab_members m
  where m.root = p_root and m.uid = auth.uid()
  for key share;
  return coalesce(v_member, '');
end
$$;
revoke all on function private.ab_mutation_lock_actor(text)
  from public, anon, authenticated, service_role;

create or replace function private.ab_mutation_lock_chat(
  p_root text, p_chat text, p_exclusive boolean
) returns void
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_key bigint;
begin
  v_key := pg_catalog.hashtextextended(
    'agentbridge-chat-mutation:'
    || pg_catalog.jsonb_build_array(p_root, p_chat)::text, 0
  );
  if p_exclusive then
    perform pg_catalog.pg_advisory_xact_lock(v_key);
  else
    perform pg_catalog.pg_advisory_xact_lock_shared(v_key);
  end if;
end
$$;
revoke all on function private.ab_mutation_lock_chat(text,text,boolean)
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

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_chat <> '' then
    perform private.ab_mutation_lock_chat(
      p_root, v_chat, p_path = 'chats/' || v_chat || '/meta.json'
    );
  end if;
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

  v_member := private.ab_mutation_lock_actor(p_root);
  v_chat := public.ab_chat_of(p_path);
  if v_chat <> '' then
    perform private.ab_mutation_lock_chat(
      p_root, v_chat, p_path = 'chats/' || v_chat || '/meta.json'
    );
  end if;
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
  v_member text;
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

  v_member := private.ab_mutation_lock_actor(p_root);
  if v_actor is null or v_member = '' then
    raise exception 'typed log append is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, p_chat, false);
  if not public.ab_is_member(p_root, p_chat) then
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

-- P1b lifecycle slice: inactive receipt-bearing membership and terminal-chat
-- mutation RPCs.  Bootstrap remains an operator action: these entry points
-- require an already sealed root, and no readiness marker is installed here.

create or replace function public.ab_mutation_member_claim(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_username text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_provider_digest text;
  v_begin jsonb;
begin
  if p_username is null or p_username = ''
     or pg_catalog.octet_length(p_username) > 1024 then
    raise exception 'invalid typed member claim' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('member_claim', p_username)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'member_claim',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  if v_actor is null then
    raise exception 'typed member claim is unauthorized'
      using errcode = '42501';
  end if;
  begin
    insert into public.ab_members(root, username, uid)
    values (p_root, p_username, v_actor);
  exception when unique_violation then
    raise exception 'typed member claim conflicts with existing identity'
      using errcode = '55000';
  end;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'member_claim',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('username', p_username, 'claimed', true)
  );
end
$$;
revoke all on function public.ab_mutation_member_claim(
  text,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_member_claim(
  text,uuid,uuid,text,text) to authenticated;

create or replace function public.ab_mutation_member_leave(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_username text;
  v_provider_digest text;
  v_begin jsonb;
begin
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('member_leave')
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'member_leave',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  if v_actor is null then
    raise exception 'typed member leave is unauthorized'
      using errcode = '42501';
  end if;
  select m.username into v_username
  from public.ab_members m
  where m.root = p_root and m.uid = v_actor
  for update;
  if v_username is null then
    raise exception 'typed member leave is unauthorized'
      using errcode = '42501';
  end if;
  delete from public.ab_members m
  where m.root = p_root and m.uid = v_actor;
  if not found then
    raise exception 'typed member leave authority changed'
      using errcode = '55000';
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'member_leave',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object('username', v_username, 'left', true)
  );
end
$$;
revoke all on function public.ab_mutation_member_leave(
  text,uuid,uuid,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_member_leave(
  text,uuid,uuid,text) to authenticated;

create or replace function public.ab_mutation_chat_delete(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_meta jsonb;
  v_prefix text;
  v_provider_digest text;
  v_begin jsonb;
  v_logs_deleted bigint;
  v_docs_tombstoned bigint;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1) then
    raise exception 'invalid typed chat delete' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array('chat_delete', p_chat)
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'chat_delete',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  if v_actor is null or v_member = '' then
    raise exception 'typed chat delete is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, p_chat, true);
  select d.data into v_meta
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || p_chat || '/meta.json'
    and not d.deleted
  for update;
  if v_meta is null
     or not coalesce(v_meta @> '{"deleted": true}'::jsonb, false)
     or pg_catalog.jsonb_typeof(v_meta->'members') is distinct from 'object'
     or v_meta->'members' is distinct from '{}'::jsonb
     or pg_catalog.jsonb_typeof(v_meta->'tenure') is distinct from 'object'
     or not coalesce(v_meta->'tenure' ? v_member, false) then
    raise exception 'typed chat delete is unauthorized'
      using errcode = '42501';
  end if;

  delete from public.ab_logs l
  where l.root = p_root and l.chat_id = p_chat;
  get diagnostics v_logs_deleted = row_count;

  -- Prefix comparison is deliberate: LIKE would treat '%' and '_' inside a
  -- valid chat ID as wildcards and could retire a different room's subtree.
  v_prefix := 'chats/' || p_chat || '/';
  update public.ab_docs d
  set data = '{}'::jsonb, deleted = true
  where d.root = p_root
    and pg_catalog.left(d.path, pg_catalog.length(v_prefix)) = v_prefix
    and not d.deleted;
  get diagnostics v_docs_tombstoned = row_count;
  if v_docs_tombstoned < 1 then
    raise exception 'typed chat delete authority changed'
      using errcode = '55000';
  end if;
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'chat_delete',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object(
      'chat_id', p_chat, 'logs_deleted', v_logs_deleted,
      'documents_tombstoned', v_docs_tombstoned
    )
  );
end
$$;
revoke all on function public.ab_mutation_chat_delete(
  text,uuid,uuid,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_chat_delete(
  text,uuid,uuid,text,text) to authenticated;
