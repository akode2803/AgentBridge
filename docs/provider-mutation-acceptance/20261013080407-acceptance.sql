-- Exact object-ID acceptance SQL executed against AgentBridge Scratch.
-- Run numbered sections independently. Section 7b is expected to fail with
-- SQLSTATE 55000 and "typed blob manifest object is unavailable".

-- 1. Disposable root and database-v1 seal.
insert into public.ab_members(root,username,uid) values
('typed-blob-id-accept-20261013080407',
 'typed-blob-id-accept-20261013080407-member',
 'c3ebd32c-33e1-41a2-8264-b77fbc722bd5'::uuid),
('typed-blob-id-accept-20261013080407',
 'typed-blob-id-accept-20261013080407-reader',
 'f69f1707-7740-4fa0-a658-682cda21b686'::uuid),
('typed-blob-id-accept-20261013080407',
 'typed-blob-id-accept-20261013080407-outsider',
 'b21ccbab-5a45-4913-897c-e01aa560b698'::uuid);
insert into public.ab_change_epochs(root)
values ('typed-blob-id-accept-20261013080407');
insert into public.ab_docs(root,path,data,deleted) values (
  'typed-blob-id-accept-20261013080407',
  'chats/room/meta.json',
  jsonb_build_object('members',jsonb_build_object(
    'typed-blob-id-accept-20261013080407-member','{}'::jsonb,
    'typed-blob-id-accept-20261013080407-reader','{}'::jsonb
  )),
  false
);
select public.ab_admin_seal_legacy_database_mutations(
  'typed-blob-id-accept-20261013080407',
  (select protocol_epoch from public.ab_mutation_roots
   where root = 'typed-blob-id-accept-20261013080407'),
  '9770a456-dfab-46f8-9f0e-a69f84e6b3bb'::uuid,
  repeat('a',64)
);

-- 2. Authenticated insert into the staged lane.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
insert into storage.objects(
  id,bucket_id,name,owner_id,metadata,version
) values (
  '2dbee154-2317-4408-8981-b12ffdd97621'::uuid,
  'ab-mesh',
  'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary',
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5',
  '{"size":37}'::jsonb,
  'internal-not-client-contract'
);
commit;

-- 3. Owner, reader and outsider visibility before publication.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
select 'owner' as principal,count(*)::integer as visible
from storage.objects
where bucket_id = 'ab-mesh'
  and name =
    'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary';
commit;
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'f69f1707-7740-4fa0-a658-682cda21b686';
set local "request.jwt.claim.role" = 'authenticated';
select 'reader' as principal,count(*)::integer as visible
from storage.objects
where bucket_id = 'ab-mesh'
  and name =
    'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary';
commit;
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'b21ccbab-5a45-4913-897c-e01aa560b698';
set local "request.jwt.claim.role" = 'authenticated';
select 'outsider' as principal,count(*)::integer as visible
from storage.objects
where bucket_id = 'ab-mesh'
  and name =
    'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary';
commit;

-- 4. First publication and exact replay. The sealed epoch returned by section
-- 1 was a38dbc04-92d0-480c-b6ac-980f1042a4a7.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
select public.ab_mutation_blob_manifest(
  'typed-blob-id-accept-20261013080407',
  'a38dbc04-92d0-480c-b6ac-980f1042a4a7'::uuid,
  '11015bc8-9cea-4054-b7b3-834dd8bf1528'::uuid,
  repeat('1',64),'room','blob-primary',
  '2dbee154-2317-4408-8981-b12ffdd97621'::uuid,
  37,repeat('2',64),repeat('3',64)
) as first_receipt;
select public.ab_mutation_blob_manifest(
  'typed-blob-id-accept-20261013080407',
  'a38dbc04-92d0-480c-b6ac-980f1042a4a7'::uuid,
  '11015bc8-9cea-4054-b7b3-834dd8bf1528'::uuid,
  repeat('1',64),'room','blob-primary',
  '2dbee154-2317-4408-8981-b12ffdd97621'::uuid,
  37,repeat('2',64),repeat('3',64)
) as replay_receipt;
commit;

-- 5. The exact section-3 statements were executed again after publication.
-- Observed counts were owner=1, reader=1, outsider=0.

-- 6. Authenticated staged-object update denial.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
with changed as (
  update storage.objects set version = 'forbidden'
  where bucket_id = 'ab-mesh'
    and name =
      'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary'
  returning 1
)
select count(*)::integer as authenticated_update_rows from changed;
commit;

-- 7a. A second object used to test an incorrect API-visible object ID.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
insert into storage.objects(id,bucket_id,name,owner_id,metadata,version) values (
  '3f599cd4-b227-420f-9018-9cd6a2c4d642'::uuid,
  'ab-mesh',
  'typed-blob-id-accept-20261013080407/blob-stage/room/wrong-id',
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5',
  '{"size":12}'::jsonb,
  'internal-not-client-contract'
);
commit;

-- 7b. Expected rejection; the requested ID differs from the stored row ID.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
select public.ab_mutation_blob_manifest(
  'typed-blob-id-accept-20261013080407',
  'a38dbc04-92d0-480c-b6ac-980f1042a4a7'::uuid,
  'd7380325-a0ae-4f10-b35e-45bc2562a844'::uuid,
  repeat('4',64),'room','wrong-id',
  '38763d3d-3bc6-41b6-ae79-256224eb86ef'::uuid,
  12,repeat('5',64),repeat('6',64)
);
commit;

-- 8. Administrative SQL-only replacement simulation. This changes only the
-- fixture row identity; real object deletion/re-upload remains an HTTP test.
update storage.objects
set id = 'd4ff495f-d028-42fb-a27f-7c4c17c0245a'::uuid
where bucket_id = 'ab-mesh'
  and name =
    'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary';
select
  (select count(*) from public.ab_mutation_receipts
   where root = 'typed-blob-id-accept-20261013080407'
     and operation_id =
       'd7380325-a0ae-4f10-b35e-45bc2562a844'::uuid)::integer
     as rejected_receipts,
  (select count(*) from public.ab_blob_manifests
   where root = 'typed-blob-id-accept-20261013080407')::integer as manifests,
  (select count(*) from public.ab_mutation_receipts
   where root = 'typed-blob-id-accept-20261013080407'
     and operation_kind = 'blob_manifest')::integer as blob_receipts;

-- 9. Owner and reader visibility after the replacement simulation.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
select 'owner' as principal,count(*)::integer as visible_after_replacement
from storage.objects
where bucket_id = 'ab-mesh'
  and name =
    'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary';
commit;
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'f69f1707-7740-4fa0-a658-682cda21b686';
set local "request.jwt.claim.role" = 'authenticated';
select 'reader' as principal,count(*)::integer as visible_after_replacement
from storage.objects
where bucket_id = 'ab-mesh'
  and name =
    'typed-blob-id-accept-20261013080407/blob-stage/room/blob-primary';
commit;

-- 10. Exact replay after replacement.
begin;
set local role authenticated;
set local "request.jwt.claim.sub" =
  'c3ebd32c-33e1-41a2-8264-b77fbc722bd5';
set local "request.jwt.claim.role" = 'authenticated';
select public.ab_mutation_blob_manifest(
  'typed-blob-id-accept-20261013080407',
  'a38dbc04-92d0-480c-b6ac-980f1042a4a7'::uuid,
  '11015bc8-9cea-4054-b7b3-834dd8bf1528'::uuid,
  repeat('1',64),'room','blob-primary',
  '2dbee154-2317-4408-8981-b12ffdd97621'::uuid,
  37,repeat('2',64),repeat('3',64)
) as replay_after_replacement;
commit;
