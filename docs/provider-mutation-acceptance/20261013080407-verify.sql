-- Exact post-migration verification query executed on AgentBridge Scratch.
select
  (select count(*) from information_schema.columns
   where table_schema = 'public'
     and table_name = 'ab_blob_manifests'
     and column_name = 'object_id'
     and data_type = 'uuid'
     and is_nullable = 'NO')::integer as object_id_column,
  (select count(*) from information_schema.columns
   where table_schema = 'public'
     and table_name = 'ab_blob_manifests'
     and column_name = 'object_version')::integer as object_version_columns,
  (select count(*) from pg_catalog.pg_proc p
   join pg_catalog.pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'ab_staged_blob_readable'
     and pg_catalog.pg_get_function_identity_arguments(p.oid) =
       'p_root text, p_chat text, p_blob text, p_owner_id text, '
       'p_object_id uuid, p_metadata jsonb')::integer as new_read_predicate,
  (select count(*) from pg_catalog.pg_proc p
   join pg_catalog.pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'ab_staged_blob_readable')::integer
     as all_read_predicates,
  (select count(*) from pg_catalog.pg_proc p
   join pg_catalog.pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'ab_mutation_blob_manifest'
     and pg_catalog.pg_get_function_identity_arguments(p.oid) =
       'p_root text, p_protocol_epoch uuid, p_operation_id uuid, '
       'p_payload_digest text, p_chat text, p_blob text, p_object_id uuid, '
       'p_sealed_bytes bigint, p_sealed_sha256 text, '
       'p_manifest_digest text')::integer as new_manifest_rpc,
  (select count(*) from pg_catalog.pg_proc p
   join pg_catalog.pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'ab_mutation_blob_manifest')::integer
     as all_manifest_rpcs,
  (select count(*) from pg_catalog.pg_policies
   where schemaname = 'storage'
     and tablename = 'objects'
     and policyname like 'ab_blobs_member_%')::integer as storage_policies;
