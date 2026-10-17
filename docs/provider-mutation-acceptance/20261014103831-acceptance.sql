-- Behavioural acceptance of ab_mutation_meta_append, executed against
-- AgentBridge Scratch after the uniform-answer correction 20261014103831
-- (the 20261014064104 schedule, now expecting a former member to get the same
-- applied=false at the current version as at a stale one, with nothing
-- written; a root member never in the chat is still refused at both). One DO
-- block: it ends by raising SQLSTATE P0001 whose message carries the
-- observations as JSON,
-- so every fixture row, receipt and log line rolls back with it. The
-- two-session lock race is covered by the PostgreSQL CI gate
-- (devtools/validate_mutation_protocol_postgres.py), not here.
do $accept$
declare
  v_root constant text := 'typed-meta-append-accept-20261014103831';
  v_actor constant uuid := '839ab9b1-8bf9-4903-9541-f458d819aa42';
  v_peer constant uuid := '2e334d98-5a73-4e8c-81a8-822e5ea1f99b';
  v_outsider constant uuid := 'aeb009b0-ba91-4e58-a0fb-024bef7ed8d4';
  v_stranger constant uuid := '0dd01759-e76c-4d3a-a321-09386b47a85c';
  v_member text := v_root || '-member';
  v_peer_name text := v_root || '-peer';
  v_log text := v_root || '-member|m1';
  v_peer_log text := v_root || '-peer|m1';
  v_path constant text := 'chats/room/meta.json';
  v_leave_op constant uuid := '5a1d3f4e-8c2b-4e0f-9a51-6b7c8d9e0f12';
  v_stale_op constant uuid := '6b2e4a5f-9d3c-4f1a-8b62-7c8d9e0f1a23';
  v_delete_op constant uuid := '7c3f5b6a-ae4d-4a2b-9c73-8d9e0f1a2b34';
  v_left jsonb;
  v_gone jsonb;
  v_initial uuid;
  v_sealed jsonb;
  v_epoch uuid;
  v_source uuid;
  v_seq bigint;
  v_start jsonb;
  v_after jsonb;
  v_meta jsonb;
  v_big jsonb;
  v_stale jsonb;
  v_leave jsonb;
  v_delete jsonb;
  v_r jsonb;
  v_out jsonb := '{}'::jsonb;
  v_rec2 constant jsonb := '{"id":"e2","ns":2,"kind":"info","event":{"type":"pill"}}';
  v_rec3 constant jsonb := '{"id":"e3","ns":3,"kind":"info","event":{"type":"member_left"}}';
  v_rec4 constant jsonb := '{"id":"e4","ns":4,"kind":"info","event":{"type":"chat_deleted"}}';
begin
  -- A per-call wrapper: the exception block is a subtransaction, so a refused
  -- call rolls back its own partial work and reports SQLSTATE plus message.
  execute $f$
    create function pg_temp.append(
      p_root text, p_epoch uuid, p_op uuid, p_digest text, p_data jsonb,
      p_source uuid, p_seq bigint, p_deleted boolean, p_log text, p_records jsonb
    ) returns jsonb language plpgsql as $b$
    declare s text; m text;
    begin
      return pg_catalog.jsonb_build_object('ok', public.ab_mutation_meta_append(
        p_root, p_epoch, p_op, p_digest, 'room', p_data, p_source, p_seq,
        p_deleted, p_log, p_records));
    exception when others then
      get stacked diagnostics s = returned_sqlstate, m = message_text;
      return pg_catalog.jsonb_build_object('error', s, 'message', m);
    end $b$
  $f$;
  execute $f$
    create function pg_temp.state(p_root text) returns jsonb
    language sql as $b$
      select pg_catalog.jsonb_build_object(
        'seq', d.seq, 'deleted', d.deleted, 'data', d.data,
        'lines', coalesce((select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(
                   l.log_name, l.line::jsonb) order by l.id)
                 from public.ab_logs l where l.root = p_root and l.chat_id = 'room'),
                 '[]'::jsonb),
        'receipts', (select pg_catalog.count(*) from public.ab_mutation_receipts r
                     where r.root = p_root and r.operation_kind = 'meta_append'))
      from public.ab_docs d where d.root = p_root and d.path = 'chats/room/meta.json'
    $b$
  $f$;

  -- 1. Disposable root, chat meta (actor + peer), database-v1 seal.
  insert into public.ab_members(root, username, uid) values
    (v_root, v_member, v_actor),
    (v_root, v_peer_name, v_peer),
    (v_root, v_root || '-outsider', v_outsider);
  insert into public.ab_change_epochs(root) values (v_root);
  select protocol_epoch into v_initial from public.ab_mutation_roots where root = v_root;
  insert into public.ab_docs(root, path, data) values (v_root, v_path,
    jsonb_build_object('members', jsonb_build_object(
      v_member, '{}'::jsonb, v_peer_name, jsonb_build_object('role', 'admin')),
      'tenure', jsonb_build_object(v_member, '[[1]]'::jsonb, v_peer_name, '[[1]]'::jsonb)));
  v_sealed := public.ab_admin_seal_legacy_database_mutations(
    v_root, v_initial, '014590f8-b111-43d5-9e9b-508a93fc4d21'::uuid, repeat('a', 64));
  v_epoch := (v_sealed->>'protocol_epoch')::uuid;
  v_source := (v_sealed->>'source_epoch')::uuid;
  v_start := pg_temp.state(v_root);
  v_seq := (v_start->>'seq')::bigint;
  v_meta := v_start->'data';
  select jsonb_agg(v_rec2) into v_big from generate_series(1, 257);
  -- The fold keeps everyone who ever joined in tenure.
  v_left := jsonb_build_object(
    'members', jsonb_build_object(v_peer_name, v_meta->'members'->v_peer_name),
    'tenure', jsonb_build_object(v_member, '[[1, 3]]'::jsonb, v_peer_name, '[[1]]'::jsonb));
  v_gone := jsonb_build_object('members', '{}'::jsonb, 'deleted', true,
    'tenure', jsonb_build_object(v_member, '[[1, 3]]'::jsonb, v_peer_name, '[[1, 4]]'::jsonb));

  -- 2. anon cannot execute the RPC at all.
  perform set_config('role', 'anon', true);
  v_out := v_out || jsonb_build_object('anon', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('b', 64), v_meta, v_source,
    v_seq, false, v_log, jsonb_build_array(v_rec2))->>'error');

  -- 3. Invalid requests from a chat member: refused 22023, nothing written.
  perform set_config('role', 'authenticated', true);
  perform set_config('request.jwt.claim.role', 'authenticated', true);
  perform set_config('request.jwt.claim.sub', v_actor::text, true);
  v_out := v_out || jsonb_build_object('invalid', jsonb_build_array(
    pg_temp.append(v_root, v_epoch, gen_random_uuid(), repeat('b', 64), v_meta,
      v_source, 0, false, v_log, jsonb_build_array(v_rec2)),
    pg_temp.append(v_root, v_epoch, gen_random_uuid(), repeat('b', 64), v_meta,
      v_source, v_seq, false, v_log, '[]'::jsonb),
    pg_temp.append(v_root, v_epoch, gen_random_uuid(), repeat('b', 64), v_meta,
      v_source, v_seq, true, v_log, jsonb_build_array(v_rec2)),
    pg_temp.append(v_root, v_epoch, gen_random_uuid(), repeat('b', 64), v_meta,
      v_source, v_seq, false, v_log, jsonb_build_array(v_rec2, 'not-an-object'::text)),
    pg_temp.append(v_root, v_epoch, gen_random_uuid(), repeat('b', 64), v_meta,
      v_source, v_seq, false, v_log, v_big)));

  -- 4. A stale version is a terminal applied=false receipt that replays
  -- exactly; a stale source epoch also applies nothing.
  v_stale := pg_temp.append(v_root, v_epoch, v_stale_op, repeat('c', 64),
    v_left,
    v_source, v_seq + 1, false, v_log, jsonb_build_array(v_rec2));
  v_out := v_out || jsonb_build_object(
    'stale', v_stale,
    'stale_replay_equal', pg_temp.append(v_root, v_epoch, v_stale_op, repeat('c', 64),
      v_left,
      v_source, v_seq + 1, false, v_log, jsonb_build_array(v_rec2)) = v_stale,
    'wrong_source_epoch', pg_temp.append(v_root, v_epoch, gen_random_uuid(),
      repeat('d', 64), v_meta, gen_random_uuid(), v_seq, false, v_log,
      jsonb_build_array(v_rec2)));

  -- 5. A root member outside the chat, and a principal outside the root.
  perform set_config('request.jwt.claim.sub', v_outsider::text, true);
  v_out := v_out || jsonb_build_object('outsider', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('e', 64), v_meta, v_source,
    v_seq, false, v_root || '-outsider|m1', jsonb_build_array(v_rec2)));
  perform set_config('request.jwt.claim.sub', v_stranger::text, true);
  v_out := v_out || jsonb_build_object('stranger', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('e', 64), v_meta, v_source,
    v_seq, false, v_log, jsonb_build_array(v_rec2)));

  perform set_config('role', 'postgres', true);
  v_after := pg_temp.state(v_root);
  v_out := v_out || jsonb_build_object(
    'refusals_changed_nothing',
    v_after->'seq' = v_start->'seq' and v_after->'data' = v_start->'data'
      and v_after->'lines' = '[]'::jsonb,
    'receipts_before_leave', v_after->'receipts');

  -- 6. The member leaves: the meta swap and both records land together, in
  -- array order, authorised by the pre-swap meta.
  perform set_config('role', 'authenticated', true);
  perform set_config('request.jwt.claim.sub', v_actor::text, true);
  v_leave := pg_temp.append(v_root, v_epoch, v_leave_op, repeat('f', 64),
    v_left,
    v_source, v_seq, false, v_log, jsonb_build_array(v_rec2, v_rec3));
  perform set_config('role', 'postgres', true);
  v_after := pg_temp.state(v_root);
  v_out := v_out || jsonb_build_object(
    'leave', v_leave,
    'leave_meta_swapped', v_after->'data' = v_left,
    'leave_seq_advanced', (v_after->>'seq')::bigint > v_seq,
    'leave_lines_in_order', v_after->'lines' = jsonb_build_array(
      jsonb_build_array(v_log, v_rec2), jsonb_build_array(v_log, v_rec3)));

  -- 7. Same operation, different records: the provider digest binds them.
  -- Exact replay by the former member returns the same receipt and appends
  -- nothing; fresh work by them at the CURRENT version gets the same
  -- applied=false as at a stale one (the answer reveals no version).
  perform set_config('role', 'authenticated', true);
  perform set_config('request.jwt.claim.sub', v_actor::text, true);
  v_r := pg_temp.append(v_root, v_epoch, v_leave_op, repeat('f', 64),
    v_left,
    v_source, v_seq, false, v_log, jsonb_build_array(v_rec3));
  v_out := v_out || jsonb_build_object('digest_collision', v_r);
  v_out := v_out || jsonb_build_object(
    'replay_after_leave_equal', pg_temp.append(v_root, v_epoch, v_leave_op,
      repeat('f', 64), v_left,
      v_source, v_seq, false, v_log, jsonb_build_array(v_rec2, v_rec3)) = v_leave,
    'fresh_after_leave', pg_temp.append(v_root, v_epoch, gen_random_uuid(),
      repeat('0', 64), v_meta, v_source, (v_after->>'seq')::bigint, false, v_log,
      jsonb_build_array(v_rec2))->'ok'->'result');
  -- A race lost to my own departure: the version moved on and I am in
  -- tenure, so the terminal answer is applied=false (nothing written).
  v_out := v_out || jsonb_build_object('lost_race_after_leave', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('3', 64), v_meta, v_source,
    v_seq, false, v_log, jsonb_build_array(v_rec2))->'ok'->'result');
  -- A root member never in the chat gets no answer at either version.
  perform set_config('request.jwt.claim.sub', v_outsider::text, true);
  v_out := v_out || jsonb_build_object('outsider_stale', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('4', 64), v_meta, v_source,
    v_seq, false, v_root || '-outsider|m1', jsonb_build_array(v_rec2)),
    'outsider_current', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('6', 64), v_meta, v_source,
    (v_after->>'seq')::bigint, false, v_root || '-outsider|m1',
    jsonb_build_array(v_rec2)));
  perform set_config('role', 'postgres', true);
  v_out := v_out || jsonb_build_object('replay_appended_nothing',
    pg_temp.state(v_root)->'lines' = v_after->'lines');

  -- 8. The remaining admin deletes for everyone: members empty, the event
  -- lands in their log; afterwards nobody can swap the meta, and anyone in
  -- tenure gets applied=false at any version, the current one included.
  perform set_config('role', 'authenticated', true);
  perform set_config('request.jwt.claim.sub', v_peer::text, true);
  v_delete := pg_temp.append(v_root, v_epoch, v_delete_op, repeat('1', 64),
    v_gone, v_source, (v_after->>'seq')::bigint, false, v_peer_log,
    jsonb_build_array(v_rec4));
  perform set_config('role', 'postgres', true);
  v_start := v_after;
  v_after := pg_temp.state(v_root);
  perform set_config('role', 'authenticated', true);
  v_r := pg_temp.append(v_root, v_epoch, gen_random_uuid(), repeat('2', 64),
    jsonb_build_object('members', jsonb_build_object(v_peer_name, '{}'::jsonb)),
    v_source, (v_after->>'seq')::bigint, false, v_peer_log, jsonb_build_array(v_rec2));
  perform set_config('request.jwt.claim.sub', v_actor::text, true);
  v_out := v_out || jsonb_build_object('lost_race_after_delete', pg_temp.append(
    v_root, v_epoch, gen_random_uuid(), repeat('5', 64), v_left, v_source,
    (v_start->>'seq')::bigint, false, v_log, jsonb_build_array(v_rec2))->'ok'->'result');
  perform set_config('role', 'postgres', true);
  v_out := v_out || jsonb_build_object(
    'delete', v_delete,
    'delete_meta', v_after->'data',
    'delete_row_deleted_flag', v_after->'deleted',
    'delete_line_last', v_after->'lines'->-1 = jsonb_build_array(v_peer_log, v_rec4),
    'line_count', jsonb_array_length(v_after->'lines'),
    'fresh_after_delete_exact_version', v_r->'ok'->'result',
    'nothing_written_after_delete',
      pg_temp.state(v_root) - 'receipts' = v_after - 'receipts',
    'receipts_after_delete', pg_temp.state(v_root)->'receipts',
    'meta_append_receipts', v_after->'receipts',
    'postgresql', current_setting('server_version'));

  raise exception using errcode = 'P0001', message = 'ACCEPTANCE ' || v_out::text;
end
$accept$;
