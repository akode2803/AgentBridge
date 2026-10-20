-- Deleted rooms are readable, cleanable and purgeable only by their final
-- members (DECISIONS D-018); a purged room answers every root member alike
-- (chat_delete: nothing purged; meta_append: applied=false). Branch
-- docs/supabase_schema.sql at this commit; the doc-change trigger function's
-- final_members watch is not applied here (Scratch has no such function).
create or replace function public.ab_can_read_chat(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.ab_docs m
    where m.root = p_root
      and m.path = 'chats/' || p_chat || '/meta.json'
      and (
        m.data->'members' ? public.ab_member(p_root)
        or (
          m.data @> '{"deleted": true}'::jsonb
          and case
            when m.data ? 'final_members'
            then pg_catalog.jsonb_typeof(m.data->'final_members') = 'array'
                 and m.data->'final_members' ? public.ab_member(p_root)
            else m.data->'tenure' ? public.ab_member(p_root)
          end
        )
      )
  )
$$;
revoke all on function public.ab_can_read_chat(text, text) from public;
grant execute on function public.ab_can_read_chat(text, text) to authenticated;

-- Of those, the ones who are not current members: a deleted room's final
-- members. Its staged attachments are theirs to list and remove before the
-- purge (no other principal may touch another uploader's staged object).
create or replace function public.ab_chat_terminal_reader(p_root text, p_chat text)
returns boolean
language sql stable security definer set search_path = public as $$
  select public.ab_can_read_chat(p_root, p_chat)
    and not public.ab_is_member(p_root, p_chat)
$$;
revoke all on function public.ab_chat_terminal_reader(text, text) from public;
grant execute on function public.ab_chat_terminal_reader(text, text) to authenticated;

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
  -- A deleted room's final members see every staged object it holds, so
  -- their janitor can list and remove them all before the purge.
  if public.ab_chat_terminal_reader(p_root, p_chat) then
    return true;
  end if;
  if p_owner_id is null or p_object_id is null or v_sealed_bytes is null then
    return false;
  end if;
  if exists (
    select 1 from public.ab_blob_manifests m
    where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob
  ) then
    return public.ab_can_read_chat(p_root, p_chat) and exists (
      select 1 from public.ab_blob_manifests m
      where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob
        and m.object_id = p_object_id
        and m.sealed_bytes = v_sealed_bytes
    );
  end if;
  return p_owner_id = v_actor::text
    and public.ab_is_member(p_root, p_chat);
end
$$;
revoke all on function public.ab_staged_blob_readable(
  text,text,text,text,uuid,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_staged_blob_readable(
  text,text,text,text,uuid,jsonb) to authenticated;

drop policy if exists ab_blobs_member_delete on storage.objects;
create policy ab_blobs_member_delete on storage.objects
for delete to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and (
    (
      pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
      and (
        pg_catalog.split_part(name, '/', 2) <> 'chats'
        or public.ab_can_read_chat(
          pg_catalog.split_part(name, '/', 1),
          pg_catalog.split_part(name, '/', 3)
        )
      )
    )
    or (
      pg_catalog.split_part(name, '/', 2) = 'blob-stage'
      and pg_catalog.split_part(name, '/', 3) <> ''
      and pg_catalog.split_part(name, '/', 4) <> ''
      and pg_catalog.split_part(name, '/', 5) = ''
      and (
        (
          owner_id = auth.uid()::text
          and public.ab_is_member(
            pg_catalog.split_part(name, '/', 1),
            pg_catalog.split_part(name, '/', 3)
          )
        )
        -- A deleted room's final members clear its staged objects (D-018).
        or public.ab_chat_terminal_reader(
          pg_catalog.split_part(name, '/', 1),
          pg_catalog.split_part(name, '/', 3)
        )
      )
    )
  )
);

create or replace function public.ab_mutation_chat_delete(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_meta jsonb;
  v_purged boolean;
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
  select d.data, d.deleted into v_meta, v_purged
  from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || p_chat || '/meta.json'
  for update;
  if v_purged then
    -- Already purged (another final member's janitor got there first):
    -- nothing is left to purge or to authorize against, so every root member
    -- gets the same empty answer and a lost race settles.
    return private.ab_mutation_finish(
      p_root, p_protocol_epoch, p_operation_id, 'chat_delete',
      p_payload_digest, v_provider_digest,
      pg_catalog.jsonb_build_object(
        'chat_id', p_chat, 'logs_deleted', 0, 'documents_tombstoned', 0)
    );
  end if;
  -- Only a deleted room's final members (D-018) purge it.
  if v_meta is null
     or not coalesce(v_meta @> '{"deleted": true}'::jsonb, false)
     or pg_catalog.jsonb_typeof(v_meta->'members') is distinct from 'object'
     or v_meta->'members' is distinct from '{}'::jsonb
     or not public.ab_can_read_chat(p_root, p_chat) then
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
  -- Tenure (current or former member) earns an answer; see the header. So
  -- does every root member once the room is purged: nothing is left to tell
  -- former members from others, and a refusal would reveal the purge to them.
  if v_actor is null or v_member = '' or v_meta is null
     or not (v_deleted or coalesce(v_meta->'members' ? v_member
                                   or v_meta->'tenure' ? v_member, false)) then
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
