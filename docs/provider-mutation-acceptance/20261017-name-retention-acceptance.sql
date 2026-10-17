-- Hosted acceptance for docs/NAME_RETENTION_AND_PRIVATE_POKES.md. Runs on
-- AgentBridge Scratch only, as one DO block that always rolls back: the
-- observations come back in the final exception.
--
-- Each case runs as a real `authenticated` login under the production RLS
-- policies (the CI Postgres gate has neither the policies nor Realtime):
-- leaving and deleting no longer free a name, a claim over an account or a
-- held name is refused, an account doc over another login's name is
-- refused, a revoked login loses every read, account rows survive hard
-- deletes, and the poke channel admits only active members of the root.
--
-- Scratch was built from the protocol sections and has no table policies,
-- so the block first installs the production ab_members and ab_docs
-- policies, copied verbatim from docs/supabase_schema.sql's final
-- definitions; the rollback removes them again.
do $acceptance$
declare
  v_root text := 'zz-accept-names';
  v_alice uuid := '00000000-0000-4000-8000-0000000000a1';
  v_bob uuid := '00000000-0000-4000-8000-0000000000b2';
  v_carol uuid := '00000000-0000-4000-8000-0000000000c3';
  v_chat text := 'live-g0123456789abcdef';
  v_out jsonb := '{}'::jsonb;
  v_n bigint;
  v_text text;
begin
  execute $f$
    create function pg_temp.as_login(p_uid uuid) returns void
    language plpgsql as $b$
    begin
      perform set_config('role', 'authenticated', true);
      perform set_config('request.jwt.claim.sub', p_uid::text, true);
      perform set_config('request.jwt.claims', pg_catalog.jsonb_build_object(
        'sub', p_uid, 'role', 'authenticated')::text, true);
    end
    $b$
  $f$;
  execute $f$
    create function pg_temp.as_owner() returns void
    language plpgsql as $b$
    begin
      perform set_config('role', 'postgres', true);
      perform set_config('request.jwt.claim.sub', '', true);
      perform set_config('request.jwt.claims', '', true);
    end
    $b$
  $f$;

  -- The production table policies (verbatim, see the header).
  drop policy if exists ab_members_select on public.ab_members;
  create policy ab_members_select on public.ab_members
  for select to authenticated using (public.ab_root_ok(root));
  drop policy if exists ab_members_claim on public.ab_members;
  create policy ab_members_claim on public.ab_members
  for insert to authenticated with check (uid = (select auth.uid()));
  drop policy if exists ab_docs_member_select on public.ab_docs;
  create policy ab_docs_member_select on public.ab_docs
  for select to authenticated using (
    public.ab_root_ok(root) and (
      path not like 'chats/%'
      or public.ab_can_read_chat(root, public.ab_chat_of(path))
    )
  );
  drop policy if exists ab_docs_member_insert on public.ab_docs;
  create policy ab_docs_member_insert on public.ab_docs
  for insert to authenticated with check (
    public.ab_root_ok(root) and (
      path not like 'chats/%'
      or public.ab_is_member(root, public.ab_chat_of(path))
      or (
        path = 'chats/' || public.ab_chat_of(path) || '/meta.json'
        and data->'members' ? public.ab_member(root)
      )
    )
    and path not like 'chats/%/runtime/effects/%'
  );
  drop policy if exists ab_docs_member_update on public.ab_docs;
  create policy ab_docs_member_update on public.ab_docs
  for update to authenticated using (
    public.ab_root_ok(root) and (
      path not like 'chats/%'
      or public.ab_is_member(root, public.ab_chat_of(path))
    )
    and path not like 'chats/%/runtime/effects/%'
  ) with check (
    public.ab_root_ok(root)
    and path not like 'chats/%/runtime/effects/%'
  );
  drop policy if exists ab_docs_member_delete on public.ab_docs;
  create policy ab_docs_member_delete on public.ab_docs
  for delete to authenticated using (
    public.ab_root_ok(root) and (
      path not like 'chats/%'
      or public.ab_can_read_chat(root, public.ab_chat_of(path))
    )
    and path not like 'chats/%/runtime/effects/%'
  );

  -- Fixture, as the database owner. A new root is unsealed, so the legacy
  -- gate admits these direct rows. helper is an agent: an account, no login.
  insert into public.ab_members(root, username, uid) values
    (v_root, 'alice', v_alice), (v_root, 'bob', v_bob);
  insert into public.ab_docs(root, path, data) values
    (v_root, 'chats/' || v_chat || '/meta.json', jsonb_build_object(
      'members', jsonb_build_object('alice', '{}'::jsonb, 'bob', '{}'::jsonb))),
    (v_root, 'users/alice.json', '{"kind": "human"}'),
    (v_root, 'users/helper.json', '{"kind": "agent"}');

  -- 1. A login can no longer delete or revoke its own row (no policy).
  perform pg_temp.as_login(v_alice);
  delete from public.ab_members where root = v_root and uid = v_alice;
  get diagnostics v_n = row_count;
  v_out := v_out || jsonb_build_object('self_delete_rows', v_n);
  update public.ab_members set revoked_at = now()
  where root = v_root and uid = v_alice;
  get diagnostics v_n = row_count;
  v_out := v_out || jsonb_build_object('self_update_rows', v_n);

  -- 2. A new login cannot claim an account's name or a held name; a fresh
  -- name works.
  perform pg_temp.as_login(v_carol);
  begin
    insert into public.ab_members(root, username, uid)
    values (v_root, 'helper', v_carol);
    v_out := v_out || '{"claim_agent_name": "accepted"}';
  exception when others then
    v_out := v_out || jsonb_build_object('claim_agent_name', sqlstate || ' ' || sqlerrm);
  end;
  begin
    insert into public.ab_members(root, username, uid)
    values (v_root, 'alice', v_carol);
    v_out := v_out || '{"claim_held_name": "accepted"}';
  exception when others then
    v_out := v_out || jsonb_build_object('claim_held_name', sqlstate || ' ' || sqlerrm);
  end;
  insert into public.ab_members(root, username, uid)
  values (v_root, 'carol', v_carol);
  v_out := v_out || '{"claim_fresh_name": "accepted"}';

  -- 3. An account doc may not take another login's name; the holder's own
  -- doc and a new agent's doc pass.
  begin
    insert into public.ab_docs(root, path, data)
    values (v_root, 'users/bob.json', '{"kind": "human"}');
    v_out := v_out || '{"foreign_account_doc": "accepted"}';
  exception when others then
    v_out := v_out || jsonb_build_object('foreign_account_doc', sqlstate || ' ' || sqlerrm);
  end;
  perform pg_temp.as_login(v_bob);
  insert into public.ab_docs(root, path, data)
  values (v_root, 'users/bob.json', '{"kind": "human"}');
  insert into public.ab_docs(root, path, data)
  values (v_root, 'users/bob-agent.json', '{"kind": "agent"}');
  v_out := v_out || '{"own_and_agent_docs": "accepted"}';

  -- 4. A member's hard delete of an account row is skipped; the row stays.
  delete from public.ab_docs where root = v_root and path = 'users/bob-agent.json';
  get diagnostics v_n = row_count;
  perform pg_temp.as_owner();
  select count(*) into v_text from public.ab_docs
  where root = v_root and path = 'users/bob-agent.json';
  v_out := v_out || jsonb_build_object('account_hard_delete_rows', v_n,
                                       'account_row_kept', v_text);

  -- 5. Revoked (as leave and the operator do), bob loses every read, and the
  -- row can be neither restored nor deleted.
  perform pg_temp.as_login(v_bob);
  v_out := v_out || jsonb_build_object('before_revoke', jsonb_build_object(
    'member', public.ab_member(v_root),
    'chat_docs', (select count(*) from public.ab_docs
                  where root = v_root and path like 'chats/%'),
    'poke_topic', public.ab_poke_topic_ok('ab-' || v_root)));
  perform pg_temp.as_owner();
  update public.ab_members set revoked_at = now()
  where root = v_root and uid = v_bob;
  perform pg_temp.as_login(v_bob);
  v_out := v_out || jsonb_build_object('after_revoke', jsonb_build_object(
    'member', public.ab_member(v_root),
    'root_ok', public.ab_root_ok(v_root),
    'roster_rows', (select count(*) from public.ab_members where root = v_root),
    'chat_docs', (select count(*) from public.ab_docs
                  where root = v_root and path like 'chats/%'),
    'poke_topic', public.ab_poke_topic_ok('ab-' || v_root)));
  perform pg_temp.as_owner();
  begin
    update public.ab_members set revoked_at = null
    where root = v_root and uid = v_bob;
    v_out := v_out || '{"unrevoke": "accepted"}';
  exception when others then
    v_out := v_out || jsonb_build_object('unrevoke', sqlstate || ' ' || sqlerrm);
  end;
  begin
    delete from public.ab_members where root = v_root and uid = v_bob;
    v_out := v_out || '{"owner_delete": "accepted"}';
  exception when others then
    v_out := v_out || jsonb_build_object('owner_delete', sqlstate || ' ' || sqlerrm);
  end;

  -- 6. The poke topic: an exact root only, never a prefix match.
  perform pg_temp.as_login(v_alice);
  v_out := v_out || jsonb_build_object('alice_topics', jsonb_build_object(
    'own', public.ab_poke_topic_ok('ab-' || v_root),
    'longer_root', public.ab_poke_topic_ok('ab-' || v_root || '-x'),
    'shorter_root', public.ab_poke_topic_ok('ab-zz-accept'),
    'wrong_prefix', public.ab_poke_topic_ok('xx-' || v_root),
    'bare_prefix', public.ab_poke_topic_ok('ab-')));

  -- 7. The realtime.messages policies. Realtime evaluates them per join,
  -- but Scratch has no messages partition (Realtime creates those, and
  -- postgres may not), so this checks the stored policies and evaluates
  -- their topic expression under each login, with realtime.topic() set as
  -- Realtime sets it. The live join is the HTTP check's job.
  perform pg_temp.as_owner();
  v_out := v_out || jsonb_build_object('policies', (
    select jsonb_object_agg(policyname, jsonb_build_object(
      'cmd', cmd, 'roles', roles::text,
      'expression', coalesce(qual, with_check)))
    from pg_policies where schemaname = 'realtime' and tablename = 'messages'));
  foreach v_text in array array['alice', 'bob', 'carol'] loop
    perform pg_temp.as_login(case v_text when 'alice' then v_alice
                                         when 'bob' then v_bob else v_carol end);
    perform set_config('realtime.topic', 'ab-' || v_root, true);
    v_out := v_out || jsonb_build_object('topic_ok_' || v_text,
      (select public.ab_poke_topic_ok((select realtime.topic()))));
  end loop;
  perform pg_temp.as_owner();

  raise exception using errcode = 'P0001', message = 'ACCEPTANCE ' || v_out::text;
end
$acceptance$;
