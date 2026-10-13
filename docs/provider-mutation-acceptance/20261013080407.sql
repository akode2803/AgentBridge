-- Scratch-only correction for the unpublished typed blob-manifest draft.
-- The product clean-install schema already uses storage.objects.id.
-- This migration refuses to reinterpret any published draft receipt because
-- its old client/provider digests bind the internal Storage version field.

begin;

drop policy if exists ab_blobs_member_select on storage.objects;
drop policy if exists ab_blobs_member_insert on storage.objects;
drop policy if exists ab_blobs_member_update on storage.objects;
drop policy if exists ab_blobs_member_delete on storage.objects;

revoke all on function public.ab_staged_blob_readable(
  text,text,text,text,text,jsonb)
  from public, anon, authenticated, service_role;
drop function public.ab_staged_blob_readable(
  text,text,text,text,text,jsonb);

revoke all on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,text,bigint,text,text)
  from public, anon, authenticated, service_role;
drop function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,text,bigint,text,text);

do $$
begin
  if exists (select 1 from public.ab_blob_manifests)
     or exists (
       select 1 from public.ab_mutation_receipts
       where operation_kind = 'blob_manifest'
     ) then
    raise exception
      'draft blob manifests and receipts require explicit fixture cleanup'
      using errcode = '55000';
  end if;
end
$$;

alter table public.ab_blob_manifests
  add column object_id uuid not null;
alter table public.ab_blob_manifests
  drop column object_version;

create or replace function public.ab_staged_blob_readable(
  p_root text, p_chat text, p_blob text, p_owner_id text,
  p_object_id uuid, p_metadata jsonb
) returns boolean
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_sealed_bytes bigint := private.ab_storage_object_size(p_metadata);
begin
  if v_actor is null or p_root is null or p_chat is null or p_blob is null
     or p_owner_id is null or p_object_id is null
     or v_sealed_bytes is null then
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

-- Existing policies are permissive.  Replace rather than supplement them so
-- the legacy non-chat branch cannot accidentally admit blob-stage objects.
-- Staged keys have exactly four non-empty segments:
--   <root>/blob-stage/<chat-id>/<blob-id>
drop policy if exists ab_blobs_member_select on storage.objects;
create policy ab_blobs_member_select on storage.objects
for select to authenticated using (
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
      and public.ab_staged_blob_readable(
        pg_catalog.split_part(name, '/', 1),
        pg_catalog.split_part(name, '/', 3),
        pg_catalog.split_part(name, '/', 4),
        owner_id,
        id,
        metadata
      )
    )
  )
);

drop policy if exists ab_blobs_member_insert on storage.objects;
create policy ab_blobs_member_insert on storage.objects
for insert to authenticated with check (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and (
    (
      pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
      and (
        pg_catalog.split_part(name, '/', 2) <> 'chats'
        or public.ab_is_member(
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
      and owner_id = auth.uid()::text
      and public.ab_is_member(
        pg_catalog.split_part(name, '/', 1),
        pg_catalog.split_part(name, '/', 3)
      )
    )
  )
);

drop policy if exists ab_blobs_member_update on storage.objects;
create policy ab_blobs_member_update on storage.objects
for update to authenticated using (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
  and (
    pg_catalog.split_part(name, '/', 2) <> 'chats'
    or public.ab_is_member(
      pg_catalog.split_part(name, '/', 1),
      pg_catalog.split_part(name, '/', 3)
    )
  )
) with check (
  bucket_id = 'ab-mesh'
  and public.ab_root_ok(pg_catalog.split_part(name, '/', 1))
  and pg_catalog.split_part(name, '/', 2) <> 'blob-stage'
  and (
    pg_catalog.split_part(name, '/', 2) <> 'chats'
    or public.ab_is_member(
      pg_catalog.split_part(name, '/', 1),
      pg_catalog.split_part(name, '/', 3)
    )
  )
);

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
      and owner_id = auth.uid()::text
      and public.ab_is_member(
        pg_catalog.split_part(name, '/', 1),
        pg_catalog.split_part(name, '/', 3)
      )
    )
  )
);

create or replace function public.ab_mutation_blob_manifest(
  p_root text, p_protocol_epoch uuid, p_operation_id uuid,
  p_payload_digest text, p_chat text, p_blob text,
  p_object_id uuid, p_sealed_bytes bigint,
  p_sealed_sha256 text, p_manifest_digest text
) returns jsonb
language plpgsql volatile security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_member text;
  v_provider_digest text;
  v_begin jsonb;
  v_storage_size bigint;
begin
  if not private.ab_mutation_path_valid(p_chat, 1024, 1)
     or p_blob is null
     or p_blob !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$'
     or p_object_id is null
     or p_sealed_bytes is null
     or p_sealed_bytes not between 1 and 52428800
     or p_sealed_sha256 is null
     or p_sealed_sha256 !~ '^[0-9a-f]{64}$'
     or p_manifest_digest is null
     or p_manifest_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid typed blob manifest' using errcode = '22023';
  end if;
  v_provider_digest := private.ab_mutation_provider_digest(
    pg_catalog.jsonb_build_array(
      'blob_manifest', p_chat, p_blob, p_object_id,
      p_sealed_bytes, p_sealed_sha256, p_manifest_digest
    )
  );
  if not private.ab_mutation_root_sealed(p_root) then
    raise exception 'typed mutation root is not sealed' using errcode = '55000';
  end if;
  v_begin := private.ab_mutation_begin(
    p_root, p_protocol_epoch, p_operation_id, 'blob_manifest',
    p_payload_digest, v_provider_digest
  );
  if (v_begin->>'replay')::boolean then
    return v_begin->'receipt';
  end if;

  v_member := private.ab_mutation_lock_actor(p_root);
  if v_actor is null or v_member = '' then
    raise exception 'typed blob manifest is unauthorized'
      using errcode = '42501';
  end if;
  perform private.ab_mutation_lock_chat(p_root, p_chat, false);
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'agentbridge-blob-manifest:'
      || pg_catalog.jsonb_build_array(p_root, p_chat, p_blob)::text, 0
    )
  );

  perform 1 from public.ab_docs d
  where d.root = p_root
    and d.path = 'chats/' || p_chat || '/meta.json'
    and not d.deleted
    and pg_catalog.jsonb_typeof(d.data->'members') = 'object'
    and d.data->'members' ? v_member
  for share;
  if not found then
    raise exception 'typed blob manifest is unauthorized'
      using errcode = '42501';
  end if;

  perform 1 from public.ab_blob_manifests m
  where m.root = p_root and m.chat_id = p_chat and m.blob_id = p_blob;
  if found then
    raise exception 'typed blob manifest already exists'
      using errcode = '55000';
  end if;

  select private.ab_storage_object_size(o.metadata) into v_storage_size
  from storage.objects o
  where o.bucket_id = 'ab-mesh'
    and o.name = p_root || '/blob-stage/' || p_chat || '/' || p_blob
    and o.owner_id = v_actor::text
    and o.id = p_object_id
    and o.archived_at is null
    and not coalesce(o.is_delete_marker, false)
  for share;
  if not found or v_storage_size is distinct from p_sealed_bytes then
    raise exception 'typed blob manifest object is unavailable'
      using errcode = '55000';
  end if;

  insert into public.ab_blob_manifests(
    root, protocol_epoch, operation_id, chat_id, blob_id, actor_uid,
    object_id, sealed_bytes, sealed_sha256, manifest_digest
  ) values (
    p_root, p_protocol_epoch, p_operation_id, p_chat, p_blob, v_actor,
    p_object_id, p_sealed_bytes, p_sealed_sha256, p_manifest_digest
  );
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'blob_manifest',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object(
      'chat_id', p_chat, 'blob_id', p_blob,
      'object_id', p_object_id, 'sealed_bytes', p_sealed_bytes
    )
  );
end
$$;
revoke all on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,uuid,bigint,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,uuid,bigint,text,text) to authenticated;

commit;
