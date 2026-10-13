-- Post-HTTP acceptance verification for the disposable root.
select
  (
    select count(*)::integer
    from storage.objects
    where bucket_id = 'ab-mesh'
      and name =
        'typed-blob-http-20261013081948/blob-stage/room/'
        || 'http-0165fd6eebc64bf7ba7bc59cd73110aa'
  ) as remaining_objects,
  (
    select count(*)::integer
    from public.ab_blob_manifests
    where root = 'typed-blob-http-20261013081948'
      and chat_id = 'room'
      and blob_id = 'http-0165fd6eebc64bf7ba7bc59cd73110aa'
      and object_id = '0ce96759-01fa-4371-a0d7-911172b8fb24'::uuid
  ) as exact_manifests,
  (
    select count(*)::integer
    from public.ab_mutation_receipts
    where root = 'typed-blob-http-20261013081948'
      and protocol_epoch = '042dc09a-349f-4401-a378-42893d2f2d0c'::uuid
      and operation_kind = 'blob_manifest'
      and receipt_state = 'terminal'
  ) as applied_blob_receipts;
