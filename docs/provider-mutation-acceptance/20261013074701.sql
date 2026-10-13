-- AgentBridge Scratch migration 20261013074701
-- Project: riagjoluhcwjvmfchbzl
-- Name: agentbridge_p1b_typed_blob_manifest_rpc
-- Applied statement bytes: 13375
-- Applied statement SHA-256: e8fc9e5eda1afdeee08e001236c0f61e47b3bf5dfd2496f8c81b35f6cb498a9a
-- BEGIN APPLIED STATEMENT

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
    when 'blob_manifest' then p_table = 'ab_blob_manifests'
      and p_operation = 'INSERT'
    else false
  end
$$;
revoke all on function private.ab_mutation_kind_permits(text,text,text)
  from public, anon, authenticated, service_role;

-- P1b blob-manifest slice: immutable Storage objects are uploaded through the
-- Storage API before this receipt-bearing publication RPC runs.  PostgreSQL
-- proves the exact Storage row's owner, opaque version and bounded size; it
-- does not claim to read or hash the underlying object bytes.

create table if not exists public.ab_blob_manifests (
  root             text not null
                     references public.ab_mutation_roots(root),
  protocol_epoch   uuid not null,
  operation_id     uuid not null,
  chat_id          text not null,
  blob_id          text not null,
  actor_uid        uuid not null,
  object_version   text not null,
  sealed_bytes     bigint not null check (
                     sealed_bytes between 1 and 52428800
                   ),
  sealed_sha256    text not null check (
                     sealed_sha256 ~ '^[0-9a-f]{64}$'
                   ),
  manifest_digest  text not null check (
                     manifest_digest ~ '^[0-9a-f]{64}$'
                   ),
  created_at       timestamptz not null default now(),
  primary key (root, chat_id, blob_id),
  unique (root, protocol_epoch, operation_id),
  check (pg_catalog.octet_length(root) between 1 and 1024),
  check (pg_catalog.octet_length(chat_id) between 1 and 1024),
  check (pg_catalog.octet_length(blob_id) between 1 and 180),
  check (pg_catalog.octet_length(object_version) between 1 and 128)
);
alter table public.ab_blob_manifests enable row level security;
revoke all on table public.ab_blob_manifests
  from public, anon, authenticated, service_role;

drop trigger if exists ab_mutation_legacy_gate on public.ab_blob_manifests;
create trigger ab_mutation_legacy_gate
before insert or update or delete on public.ab_blob_manifests
for each row execute function private.ab_mutation_legacy_gate();

-- Storage metadata is provider-owned and must remain read-only to this
-- protocol.  Invalid or oversized metadata is unavailable rather than cast.
create or replace function private.ab_storage_object_size(p_metadata jsonb)
returns bigint
language sql immutable set search_path = pg_catalog as $$
  select case
    when p_metadata->>'size' ~ '^[0-9]{1,8}$'
     and (p_metadata->>'size')::numeric between 1 and 52428800
    then (p_metadata->>'size')::bigint
    else null
  end
$$;
revoke all on function private.ab_storage_object_size(jsonb)
  from public, anon, authenticated, service_role;

-- One exact predicate supports staged-object SELECT policy.  Before
-- publication, only the current uploader/current room member may read the
-- object back for verification.  After any manifest exists, owner fallback is
-- disabled and the exact published version/size plus current chat visibility
-- are required.
create or replace function public.ab_staged_blob_readable(
  p_root text, p_chat text, p_blob text, p_owner_id text,
  p_object_version text, p_metadata jsonb
) returns boolean
language plpgsql stable security definer set search_path = pg_catalog as $$
declare
  v_actor uuid := auth.uid();
  v_sealed_bytes bigint := private.ab_storage_object_size(p_metadata);
begin
  if v_actor is null or p_root is null or p_chat is null or p_blob is null
     or p_owner_id is null or p_object_version is null
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
        and m.object_version = p_object_version
        and m.sealed_bytes = v_sealed_bytes
    );
  end if;
  return p_owner_id = v_actor::text
    and public.ab_is_member(p_root, p_chat);
end
$$;
revoke all on function public.ab_staged_blob_readable(
  text,text,text,text,text,jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_staged_blob_readable(
  text,text,text,text,text,jsonb) to authenticated;

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
        version,
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
  p_object_version text, p_sealed_bytes bigint,
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
     or p_object_version is null or p_object_version = ''
     or pg_catalog.octet_length(p_object_version) > 128
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
      'blob_manifest', p_chat, p_blob, p_object_version,
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
    and o.version = p_object_version
    and o.archived_at is null
    and not coalesce(o.is_delete_marker, false)
  for share;
  if not found or v_storage_size is distinct from p_sealed_bytes then
    raise exception 'typed blob manifest object is unavailable'
      using errcode = '55000';
  end if;

  insert into public.ab_blob_manifests(
    root, protocol_epoch, operation_id, chat_id, blob_id, actor_uid,
    object_version, sealed_bytes, sealed_sha256, manifest_digest
  ) values (
    p_root, p_protocol_epoch, p_operation_id, p_chat, p_blob, v_actor,
    p_object_version, p_sealed_bytes, p_sealed_sha256, p_manifest_digest
  );
  return private.ab_mutation_finish(
    p_root, p_protocol_epoch, p_operation_id, 'blob_manifest',
    p_payload_digest, v_provider_digest,
    pg_catalog.jsonb_build_object(
      'chat_id', p_chat, 'blob_id', p_blob,
      'object_version', p_object_version, 'sealed_bytes', p_sealed_bytes
    )
  );
end
$$;
revoke all on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,text,bigint,text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.ab_mutation_blob_manifest(
  text,uuid,uuid,text,text,text,text,bigint,text,text) to authenticated;
