-- Behavioural acceptance of agentbridge_p1b_terminal_readers_review against
-- AgentBridge Scratch: the staged-object read rule still answers every case
-- the same with the deleted-room branch checked last, and doc_delete never
-- tombstones a chat's meta (live or deleted), so the purge still finds it.
-- One DO block ending in a P0001 raise carrying the observations, so every
-- fixture row (members, docs, logs, Storage rows, manifests, receipts)
-- rolls back with it.
do $accept$
declare
  v_root constant text := 'typed-terminal-review-accept';
  v_final constant uuid := 'a1b2c3d4-0000-4000-8000-000000000011';
  v_uploader constant uuid := 'a1b2c3d4-0000-4000-8000-000000000012';
  v_departed constant uuid := 'a1b2c3d4-0000-4000-8000-000000000013';
  v_dead_staged text := 'typed-terminal-review-accept/blob-stage/room/f2-x';
  v_published text := 'typed-terminal-review-accept/blob-stage/live/f2-pub';
  v_own text := 'typed-terminal-review-accept/blob-stage/live/f2-own';
  v_initial uuid;
  v_sealed jsonb;
  v_epoch uuid;
  v_object uuid;
  v_out jsonb := '{}'::jsonb;
  v_sees jsonb := '{}'::jsonb;
  v_who record;
begin
  execute $f$
    create function pg_temp.as_principal(p_uid uuid) returns void
    language plpgsql as $b$
    begin
      perform set_config('role', 'authenticated', true);
      perform set_config('request.jwt.claim.role', 'authenticated', true);
      perform set_config('request.jwt.claim.sub', p_uid::text, true);
    end $b$
  $f$;
  execute $f$
    create function pg_temp.doc_delete(p_root text, p_epoch uuid, p_path text)
    returns jsonb language plpgsql as $b$
    declare s text; m text;
    begin
      return pg_catalog.jsonb_build_object('ok', public.ab_mutation_doc_delete(
        p_root, p_epoch, gen_random_uuid(), repeat('e', 64), p_path)->'result');
    exception when others then
      get stacked diagnostics s = returned_sqlstate, m = message_text;
      return pg_catalog.jsonb_build_object('error', s, 'message', m);
    end $b$
  $f$;

  -- 1. A deleted room (final members final+uploader; departed left before),
  -- a live room of final+uploader, and three staged objects by the uploader:
  -- one in the dead room, one published and one unpublished in the live room.
  insert into public.ab_members(root, username, uid) values
    (v_root, v_root || '-final', v_final),
    (v_root, v_root || '-uploader', v_uploader),
    (v_root, v_root || '-departed', v_departed);
  insert into public.ab_change_epochs(root) values (v_root);
  select protocol_epoch into v_initial from public.ab_mutation_roots where root = v_root;
  insert into public.ab_docs(root, path, data) values
    (v_root, 'chats/room/meta.json', jsonb_build_object(
       'deleted', true, 'members', '{}'::jsonb,
       'tenure', jsonb_build_object(v_root || '-final', '[[1, 4]]'::jsonb,
                                    v_root || '-uploader', '[[1, 4]]'::jsonb,
                                    v_root || '-departed', '[[1, 2]]'::jsonb),
       'final_members', jsonb_build_array(v_root || '-final', v_root || '-uploader'))),
    (v_root, 'chats/room/note.json', '{}'::jsonb),
    (v_root, 'chats/live/meta.json', jsonb_build_object(
       'members', jsonb_build_object(v_root || '-final', '{}'::jsonb,
                                     v_root || '-uploader', '{}'::jsonb))),
    (v_root, 'chats/live/note.json', '{}'::jsonb);
  insert into public.ab_logs(root, chat_id, log_name, line)
    values (v_root, 'room', 'writer', '{}');
  insert into storage.objects(bucket_id, name, owner_id, metadata) values
    ('ab-mesh', v_dead_staged, v_uploader::text, '{"size": 1}'::jsonb),
    ('ab-mesh', v_published, v_uploader::text, '{"size": 1}'::jsonb),
    ('ab-mesh', v_own, v_uploader::text, '{"size": 1}'::jsonb);
  select id into v_object from storage.objects
  where bucket_id = 'ab-mesh' and name = v_published;

  -- 2. Seal; the uploader publishes one live object through the typed RPC.
  v_sealed := public.ab_admin_seal_legacy_database_mutations(
    v_root, v_initial, 'b1b2c3d4-0000-4000-8000-000000000019'::uuid, repeat('a', 64));
  v_epoch := (v_sealed->>'protocol_epoch')::uuid;
  perform pg_temp.as_principal(v_uploader);
  v_out := v_out || jsonb_build_object('manifest', public.ab_mutation_blob_manifest(
    v_root, v_epoch, gen_random_uuid(), repeat('b', 64), 'live', 'f2-pub',
    v_object, 1, repeat('c', 64), repeat('d', 64))->'result');

  -- 3. Who sees which staged object.
  for v_who in select * from (values ('final', v_final), ('uploader', v_uploader),
                                     ('departed', v_departed)) as t(label, uid) loop
    perform pg_temp.as_principal(v_who.uid);
    v_sees := v_sees || jsonb_build_object(v_who.label, jsonb_build_object(
      'dead_staged', (select count(*) from storage.objects
                      where bucket_id = 'ab-mesh' and name = v_dead_staged),
      'live_published', (select count(*) from storage.objects
                         where bucket_id = 'ab-mesh' and name = v_published),
      'live_unpublished', (select count(*) from storage.objects
                           where bucket_id = 'ab-mesh' and name = v_own)));
  end loop;
  v_out := v_out || jsonb_build_object('sees', v_sees);

  -- 4. No principal tombstones a chat meta through doc_delete; other docs
  -- still go; the final member's purge then finds the meta and the logs.
  perform pg_temp.as_principal(v_final);
  v_out := v_out || jsonb_build_object(
    'delete_dead_meta', pg_temp.doc_delete(v_root, v_epoch, 'chats/room/meta.json'),
    'delete_live_meta', pg_temp.doc_delete(v_root, v_epoch, 'chats/live/meta.json'),
    'delete_live_note', pg_temp.doc_delete(v_root, v_epoch, 'chats/live/note.json'),
    'purge_final', public.ab_mutation_chat_delete(
      v_root, v_epoch, gen_random_uuid(), repeat('f', 64), 'room')->'result');
  perform set_config('role', 'postgres', true);
  v_out := v_out || jsonb_build_object(
    'live_meta_live_after', (select not deleted from public.ab_docs
                             where root = v_root and path = 'chats/live/meta.json'),
    'postgresql', current_setting('server_version'));

  raise exception using errcode = 'P0001', message = 'ACCEPTANCE ' || v_out::text;
end
$accept$;
