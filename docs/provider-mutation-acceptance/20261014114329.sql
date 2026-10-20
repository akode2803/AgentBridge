-- Review follow-up to agentbridge_p1b_terminal_readers (DECISIONS D-018):
-- a chat's meta is never tombstoned by doc_delete (only chat_delete ends a
-- room, so a purge always removes its logs), and the staged-object read
-- predicate checks the deleted-room branch last, so a live room's common
-- reads skip it. Branch docs/supabase_schema.sql at this commit.
create or replace function public.ab_staged_blob_readable(
  p_root text, p_chat text, p_blob text, p_owner_id text,
  p_object_id uuid, p_metadata jsonb
) returns boolean
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_sealed_bytes bigint := private.ab_storage_object_size(p_metadata);
begin
  if v_actor is null or p_root is null or p_chat is null or p_blob is null then
    return false;
  end if;
  if p_owner_id is not null and p_object_id is not null
     and v_sealed_bytes is not null then
    if exists (
      select 1 from public.ab_blob_manifests m
      where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob
    ) then
      if public.ab_can_read_chat(p_root, p_chat) and exists (
        select 1 from public.ab_blob_manifests m
        where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob
          and m.object_id = p_object_id
          and m.sealed_bytes = v_sealed_bytes
      ) then
        return true;
      end if;
    elsif p_owner_id = v_actor::text
          and public.ab_is_member(p_root, p_chat) then
      return true;
    end if;
  end if;
  -- A deleted room's final members see every staged object it holds, so
  -- their janitor can list and remove them all before the purge. Checked
  -- last, so a live room's common reads never pay for it.
  return public.ab_chat_terminal_reader(p_root, p_chat);
end
$$;
revoke all on function public.ab_staged_blob_readable(
  text,text,text,text,uuid,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_staged_blob_readable(
  text,text,text,text,uuid,jsonb) to authenticated;

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
  -- A chat's meta is never tombstoned directly: only chat_delete ends a
  -- room, so a purge always removes its logs too.
  if v_actor is null or v_member = ''
     or p_path like 'chats/%/runtime/effects/%'
     or (v_chat <> '' and (p_path = 'chats/' || v_chat || '/meta.json'
                           or not public.ab_can_read_chat(p_root, v_chat))) then
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
