-- Scratch-only cleanup executed before the object-ID correction migration.
-- The draft manifest and receipt were created from SQL-only Storage metadata;
-- no product or user data is addressed here.
begin;
alter table public.ab_blob_manifests
  disable trigger ab_mutation_legacy_gate;
delete from public.ab_blob_manifests
where root = 'typed-blob-accept-20261013130855';
alter table public.ab_blob_manifests
  enable trigger ab_mutation_legacy_gate;
delete from public.ab_mutation_receipts
where root = 'typed-blob-accept-20261013130855'
  and operation_kind = 'blob_manifest';
commit;
