-- Exact disposable-root setup for real Auth and Storage HTTP acceptance.
-- Project: AgentBridge Scratch (riagjoluhcwjvmfchbzl)
-- The two Auth users were generated with example.com addresses and auto-confirmed.

insert into public.ab_members(root, username, uid) values
(
  'typed-blob-http-20261013081948',
  'typed-blob-http-20261013081948-member',
  'bcae492c-0493-4aae-b18a-bec122b5cf34'::uuid
),
(
  'typed-blob-http-20261013081948',
  'typed-blob-http-20261013081948-reader',
  '427f340f-e266-4424-9c5f-82088c1ab76c'::uuid
);

insert into public.ab_change_epochs(root)
values ('typed-blob-http-20261013081948');

insert into public.ab_docs(root, path, data, deleted) values (
  'typed-blob-http-20261013081948',
  'chats/room/meta.json',
  jsonb_build_object('members', jsonb_build_object(
    'typed-blob-http-20261013081948-member', '{}'::jsonb,
    'typed-blob-http-20261013081948-reader', '{}'::jsonb
  )),
  false
);

select public.ab_admin_seal_legacy_database_mutations(
  'typed-blob-http-20261013081948',
  (
    select protocol_epoch
    from public.ab_mutation_roots
    where root = 'typed-blob-http-20261013081948'
  ),
  '17ea02a1-0595-46b7-9ff7-c6744d5d1faf'::uuid,
  repeat('a', 64)
) as seal;
