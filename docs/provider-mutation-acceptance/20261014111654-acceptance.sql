-- Behavioural acceptance of the deleted-room rules (DECISIONS D-018), executed
-- against AgentBridge Scratch after agentbridge_p1b_terminal_readers. One DO
-- block: it ends by raising SQLSTATE P0001 whose message carries the
-- observations as JSON, so every fixture row (members, docs, logs, Storage
-- object rows, receipts) rolls back with it. Scratch blocks direct DELETEs on
-- storage.objects (a statement trigger), so the delete policy is evaluated by
-- running its exact pg_policies expression for each principal.
do $accept$
declare
  v_root constant text := 'typed-terminal-readers-accept';
  v_final constant uuid := 'a1b2c3d4-0000-4000-8000-000000000001';
  v_uploader constant uuid := 'a1b2c3d4-0000-4000-8000-000000000002';
  v_departed constant uuid := 'a1b2c3d4-0000-4000-8000-000000000003';
  v_outsider constant uuid := 'a1b2c3d4-0000-4000-8000-000000000004';
  v_stranger constant uuid := 'a1b2c3d4-0000-4000-8000-000000000005';
  v_staged text := 'typed-terminal-readers-accept/blob-stage/room/f2-x';
  v_file text := 'typed-terminal-readers-accept/chats/room/files/f-1';
  v_live text := 'typed-terminal-readers-accept/blob-stage/live/f2-y';
  v_qual text;
  v_initial uuid;
  v_sealed jsonb;
  v_epoch uuid;
  v_source uuid;
  v_out jsonb := '{}'::jsonb;
  v_reads jsonb := '{}'::jsonb;
  v_sees jsonb := '{}'::jsonb;
  v_deletes jsonb := '{}'::jsonb;
  v_n bigint;
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
    create function pg_temp.purge(p_root text, p_epoch uuid) returns jsonb
    language plpgsql as $b$
    declare s text; m text;
    begin
      return pg_catalog.jsonb_build_object('ok', public.ab_mutation_chat_delete(
        p_root, p_epoch, gen_random_uuid(), repeat('c', 64), 'room')->'result');
    exception when others then
      get stacked diagnostics s = returned_sqlstate, m = message_text;
      return pg_catalog.jsonb_build_object('error', s, 'message', m);
    end $b$
  $f$;
  execute $f$
    create function pg_temp.append(p_root text, p_epoch uuid, p_source uuid,
                                   p_log text) returns jsonb
    language plpgsql as $b$
    declare s text; m text;
    begin
      return pg_catalog.jsonb_build_object('ok', public.ab_mutation_meta_append(
        p_root, p_epoch, gen_random_uuid(), repeat('d', 64), 'room',
        '{"members":{}}'::jsonb, p_source, 1, false, p_log,
        '[{"id":"e1"}]'::jsonb)->'result');
    exception when others then
      get stacked diagnostics s = returned_sqlstate, m = message_text;
      return pg_catalog.jsonb_build_object('error', s, 'message', m);
    end $b$
  $f$;

  -- 1. Disposable root: a group deleted with final members final+uploader
  -- (departed left earlier), an older-client deletion without final_members,
  -- a live group, three Storage object rows uploaded by the uploader.
  insert into public.ab_members(root, username, uid) values
    (v_root, v_root || '-final', v_final),
    (v_root, v_root || '-uploader', v_uploader),
    (v_root, v_root || '-departed', v_departed),
    (v_root, v_root || '-outsider', v_outsider);
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
    (v_root, 'chats/legacy/meta.json', jsonb_build_object(
       'deleted', true, 'members', '{}'::jsonb,
       'tenure', jsonb_build_object(v_root || '-final', '[[1, 4]]'::jsonb,
                                    v_root || '-departed', '[[1, 2]]'::jsonb))),
    (v_root, 'chats/live/meta.json', jsonb_build_object(
       'members', jsonb_build_object(v_root || '-final', '{}'::jsonb,
                                     v_root || '-uploader', '{}'::jsonb)));
  insert into public.ab_logs(root, chat_id, log_name, line)
    values (v_root, 'room', 'writer', '{}');
  insert into storage.objects(bucket_id, name, owner_id, metadata) values
    ('ab-mesh', v_staged, v_uploader::text, '{"size": 1}'::jsonb),
    ('ab-mesh', v_file, v_uploader::text, '{"size": 1}'::jsonb),
    ('ab-mesh', v_live, v_uploader::text, '{"size": 1}'::jsonb);
  select qual into v_qual from pg_policies
  where schemaname = 'storage' and tablename = 'objects'
    and policyname = 'ab_blobs_member_delete';

  -- 2. Who reads the deleted room (and the older deletion), who sees and may
  -- delete each object.
  for v_who in select * from (values ('final', v_final), ('uploader', v_uploader),
                                     ('departed', v_departed), ('outsider', v_outsider))
                 as t(label, uid) loop
    perform pg_temp.as_principal(v_who.uid);
    v_reads := v_reads || jsonb_build_object(v_who.label, jsonb_build_object(
      'room', public.ab_can_read_chat(v_root, 'room'),
      'legacy', public.ab_can_read_chat(v_root, 'legacy')));
    v_sees := v_sees || jsonb_build_object(v_who.label, jsonb_build_object(
      'staged', (select count(*) from storage.objects where bucket_id = 'ab-mesh' and name = v_staged),
      'file', (select count(*) from storage.objects where bucket_id = 'ab-mesh' and name = v_file),
      'live', (select count(*) from storage.objects where bucket_id = 'ab-mesh' and name = v_live)));
    perform set_config('role', 'postgres', true);  -- evaluate the delete policy alone
    execute format('select jsonb_build_object(%L, (select count(*) from storage.objects '
                   'where bucket_id = %L and name = %L and (%s)), %L, (select count(*) '
                   'from storage.objects where bucket_id = %L and name = %L and (%s)), '
                   '%L, (select count(*) from storage.objects where bucket_id = %L '
                   'and name = %L and (%s)))',
                   'staged', 'ab-mesh', v_staged, v_qual,
                   'file', 'ab-mesh', v_file, v_qual,
                   'live', 'ab-mesh', v_live, v_qual)
      into v_out;
    v_deletes := v_deletes || jsonb_build_object(v_who.label, v_out);
  end loop;
  v_out := jsonb_build_object('reads', v_reads, 'sees', v_sees,
                              'delete_policy', v_deletes);

  -- 3. Seal, then purge: a departed member and a stranger are refused, a
  -- final member purges, everyone in the root then gets the same empty answer.
  perform set_config('role', 'postgres', true);
  v_sealed := public.ab_admin_seal_legacy_database_mutations(
    v_root, v_initial, 'b1b2c3d4-0000-4000-8000-000000000009'::uuid, repeat('a', 64));
  v_epoch := (v_sealed->>'protocol_epoch')::uuid;
  v_source := (v_sealed->>'source_epoch')::uuid;
  perform pg_temp.as_principal(v_departed);
  v_out := v_out || jsonb_build_object('purge_departed', pg_temp.purge(v_root, v_epoch));
  perform pg_temp.as_principal(v_stranger);
  v_out := v_out || jsonb_build_object('purge_stranger', pg_temp.purge(v_root, v_epoch));
  perform pg_temp.as_principal(v_final);
  v_out := v_out || jsonb_build_object('purge_final', pg_temp.purge(v_root, v_epoch),
    'reads_after_purge', public.ab_can_read_chat(v_root, 'room'));
  perform pg_temp.as_principal(v_uploader);
  v_out := v_out || jsonb_build_object('purge_again_uploader', pg_temp.purge(v_root, v_epoch));
  perform pg_temp.as_principal(v_outsider);
  v_out := v_out || jsonb_build_object('purge_again_outsider', pg_temp.purge(v_root, v_epoch));

  -- 4. A purged room answers a meta append alike for a former member and an
  -- outsider; a stranger is still refused; nothing is written.
  perform pg_temp.as_principal(v_departed);
  v_out := v_out || jsonb_build_object('append_departed',
    pg_temp.append(v_root, v_epoch, v_source, v_root || '-departed|m1'));
  perform pg_temp.as_principal(v_outsider);
  v_out := v_out || jsonb_build_object('append_outsider',
    pg_temp.append(v_root, v_epoch, v_source, v_root || '-outsider|m1'));
  perform pg_temp.as_principal(v_stranger);
  v_out := v_out || jsonb_build_object('append_stranger',
    pg_temp.append(v_root, v_epoch, v_source, 'stranger|m1'));
  perform set_config('role', 'postgres', true);
  select count(*) into v_n from public.ab_logs where root = v_root and chat_id = 'room';
  v_out := v_out || jsonb_build_object(
    'room_log_lines_after', v_n,
    'room_docs_live_after', (select count(*) from public.ab_docs
                             where root = v_root and path like 'chats/room/%' and not deleted),
    'postgresql', current_setting('server_version'));

  raise exception using errcode = 'P0001', message = 'ACCEPTANCE ' || v_out::text;
end
$accept$;
