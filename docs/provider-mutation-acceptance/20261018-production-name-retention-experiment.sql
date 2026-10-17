-- Rolled-back production experiment for
-- 20261018-production-name-retention-slice.sql (Aryan allowed safe
-- experiments on production, 2026-10-18). One DO block that always rolls
-- back: it applies the slice in its own transaction, checks that the real
-- login's view of the real root is unchanged, then runs the Scratch
-- acceptance cases (20261017-name-retention-acceptance.sql, cases 1-7,
-- verbatim) on a throwaway root under production's own policies. The
-- observations come back in the final exception; nothing persists.
do $experiment$
declare
  v_root text := 'zz-accept-names';
  v_alice uuid := '00000000-0000-4000-8000-0000000000a1';
  v_bob uuid := '00000000-0000-4000-8000-0000000000b2';
  v_carol uuid := '00000000-0000-4000-8000-0000000000c3';
  v_chat text := 'live-g0123456789abcdef';
  v_out jsonb := '{}'::jsonb;
  v_n bigint;
  v_text text;
  v_real uuid;
  v_before jsonb;
  v_after jsonb;
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

  select uid into strict v_real from public.ab_members
  where root = 'mesh2' and username = 'aryan';
  if exists (select 1 from public.ab_docs where root = v_root)
     or exists (select 1 from public.ab_members where root = v_root) then
    raise exception 'fixture root % is not empty', v_root;
  end if;

  -- The real login's view of the real root, as counts only.
  perform pg_temp.as_login(v_real);
  v_before := jsonb_build_object(
    'member', public.ab_member('mesh2') <> '',
    'roster', (select count(*) from public.ab_members where root = 'mesh2'),
    'docs', (select count(*) from public.ab_docs where root = 'mesh2'),
    'chat_metas', (select count(*) from public.ab_docs
                   where root = 'mesh2' and path like 'chats/%/meta.json'),
    'logs', (select count(*) from public.ab_logs where root = 'mesh2'),
    'events', (select count(*) from public.ab_change_events where root = 'mesh2'),
    'blobs', (select count(*) from storage.objects
              where bucket_id = 'ab-mesh' and name like 'mesh2/%'),
    'poke_topic', null);
  perform pg_temp.as_owner();

  execute $slice$
-- Production slice of docs/NAME_RETENTION_AND_PRIVATE_POKES.md for a
-- project that has the legacy schema but not the typed mutation protocol
-- (production eakewaaqbilmtswbbxix on 2026-10-18). Generated from
-- docs/supabase_schema.sql; each part below is copied verbatim from it:
--   1. the revoked_at column and ab_member's revoked filter;
--   2. dropping the legacy self-delete policy;
--   3. the end section (keep-names, one-owner and keep-accounts triggers,
--      the poke topic check and the realtime.messages policies).
-- The typed protocol's parts (lock_actor, member_leave, kind_permits) do
-- not exist there, so they are not in the slice. Idempotent.
-- After applying: turn "Allow public access" off in the Realtime settings
-- only once every machine runs v0.24.378 or later.

-- 1.
-- A name is never freed (docs/NAME_RETENTION_AND_PRIVATE_POKES.md): leaving
-- sets revoked_at instead of deleting the row, so the name stays taken and
-- the login keeps no access. Null means active.
alter table public.ab_members add column if not exists revoked_at timestamptz;

-- SECURITY DEFINER: these run inside policies over the very tables they
-- read — without definer they'd recurse through RLS. Owned by the schema
-- owner; search_path pinned.
create or replace function public.ab_member(p_root text) returns text
language sql stable security definer set search_path = public as $$
  select coalesce((select username from public.ab_members
                   where root = p_root and uid = (select auth.uid())
                     and revoked_at is null), '')
$$;
revoke all on function public.ab_member(text) from public;
grant execute on function public.ab_member(text) to authenticated;

-- 2.
-- The legacy self-delete freed the name for the next claimer; it is gone.
drop policy if exists ab_members_leave on public.ab_members;

-- 3.
-- ---------------------------------------------------------------------------
-- Name retention and the private poke channel (2026-10-17). Idempotent;
-- paste and Run. Design: docs/NAME_RETENTION_AND_PRIVATE_POKES.md.
--
-- A name is held by an ab_members row (a login), by a users/<name>.json row
-- (an account, human or agent), or by both. Rows of both kinds are kept
-- forever, and the two tables agree on who owns a name, so no login can
-- inherit chat access by claiming a name someone else used or uses.

alter table public.ab_members add column if not exists revoked_at timestamptz;
-- Declared since R84, but a database built another way may lack it, and
-- the trigger below compares it.
alter table public.ab_members
  add column if not exists added_at timestamptz not null default now();

-- Every role, the service role included: no delete; username, uid and
-- added_at are fixed; revoked_at only goes from null to a time. The database
-- owner can still remove a row by disabling this trigger on purpose.
create or replace function private.ab_members_keep_names()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'a claimed name is never freed; revoke it instead'
      using errcode = '55000';
  end if;
  if new.username is distinct from old.username
     or new.uid is distinct from old.uid
     or new.added_at is distinct from old.added_at then
    raise exception 'member identity is immutable' using errcode = '22023';
  end if;
  if old.revoked_at is not null
     and new.revoked_at is distinct from old.revoked_at then
    raise exception 'a revoked name stays revoked' using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function private.ab_members_keep_names()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_members_keep_names on public.ab_members;
create trigger ab_members_keep_names
before update or delete on public.ab_members for each row
execute function private.ab_members_keep_names();

-- One transaction lock per (root, name), taken by both triggers below
-- before they look, so a claim and an account creation for the same name
-- cannot both pass under READ COMMITTED.
create or replace function private.ab_lock_name(p_root text, p_name text)
returns void language sql volatile set search_path = pg_catalog as $$
  select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-name:' || pg_catalog.jsonb_build_array(p_root, p_name)::text,
    0))
$$;
revoke all on function private.ab_lock_name(text, text)
  from public, anon, authenticated, service_role;

-- A login may not claim a name that belongs to an account (an agent, or a
-- human whose doc exists), live or tombstoned. A row this login or name
-- already has falls through to the unique constraints, which refuse it as
-- before, so "already yours" stays recognisable. Definer: a not-yet-member
-- cannot read ab_docs. The service role and the database owner are exempt
-- (the operator's seed; the legacy service-key fleet).
create or replace function private.ab_members_one_owner()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
begin
  perform private.ab_lock_name(new.root, new.username);
  if coalesce(auth.role(), '') in ('', 'service_role') then
    return new;
  end if;
  if exists (
    select 1 from public.ab_members m
    where m.root = new.root and (m.uid = new.uid or m.username = new.username)
  ) then
    return new;
  end if;
  if exists (
    select 1 from public.ab_docs d
    where d.root = new.root and d.path = 'users/' || new.username || '.json'
  ) then
    raise exception 'name belongs to an existing account'
      using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function private.ab_members_one_owner()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_members_one_owner on public.ab_members;
create trigger ab_members_one_owner
before insert on public.ab_members for each row
execute function private.ab_members_one_owner();

-- An account doc may not be created for a name another login holds. A
-- human's own doc passes (their login holds the name), and so does a new
-- agent's (no login holds it). An upsert fires this too, which is cheap.
create or replace function private.ab_docs_one_owner()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
declare
  v_name text;
begin
  if new.path !~ '^users/[^/]+\.json$' then
    return new;
  end if;
  v_name := pg_catalog.substr(new.path, 7, pg_catalog.length(new.path) - 11);
  perform private.ab_lock_name(new.root, v_name);
  if coalesce(auth.role(), '') in ('', 'service_role') then
    return new;
  end if;
  if exists (
    select 1 from public.ab_members m
    where m.root = new.root and m.username = v_name
      and m.uid is distinct from auth.uid()
  ) then
    raise exception 'name is claimed by another login'
      using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function private.ab_docs_one_owner()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_one_owner on public.ab_docs;
create trigger ab_docs_one_owner
before insert on public.ab_docs for each row
when (new.path like 'users/%')
execute function private.ab_docs_one_owner();

-- Account rows outlive everything: a hard delete of a users/ row is skipped
-- (the row stays, no error), for every role. Skipping rather than raising
-- keeps an older client's bulk tombstone purge working for every other row.
-- Soft deletes (deleted = true) are updates and still work.
create or replace function private.ab_docs_keep_accounts()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  return null;
end
$$;
revoke all on function private.ab_docs_keep_accounts()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_keep_accounts on public.ab_docs;
create trigger ab_docs_keep_accounts
before delete on public.ab_docs for each row
when (old.path like 'users/%')
execute function private.ab_docs_keep_accounts();

-- The poke channel ab-<root> is private: only active members of the root
-- may join (receive) or send. Roots may contain '-', so the topic is never
-- split; the root is everything after the 'ab-' prefix. Supabase also needs
-- "Allow public access" turned off in the Realtime settings.
create or replace function public.ab_poke_topic_ok(p_topic text)
returns boolean language sql stable set search_path = pg_catalog as $$
  select coalesce(
    pg_catalog.left(p_topic, 3) = 'ab-'
    and pg_catalog.length(p_topic) > 3
    and public.ab_root_ok(pg_catalog.substr(p_topic, 4)),
    false)
$$;
revoke all on function public.ab_poke_topic_ok(text) from public, anon;
grant execute on function public.ab_poke_topic_ok(text) to authenticated;

do $realtime$
begin
  -- Plain PostgreSQL (the CI gate) has no Realtime schema.
  if pg_catalog.to_regclass('realtime.messages') is null then
    return;
  end if;
  execute 'drop policy if exists ab_poke_receive on realtime.messages';
  execute $policy$
    create policy ab_poke_receive on realtime.messages
    for select to authenticated using (
      realtime.messages.extension = 'broadcast'
      and (select public.ab_poke_topic_ok((select realtime.topic())))
    )
  $policy$;
  execute 'drop policy if exists ab_poke_send on realtime.messages';
  execute $policy$
    create policy ab_poke_send on realtime.messages
    for insert to authenticated with check (
      realtime.messages.extension = 'broadcast'
      and (select public.ab_poke_topic_ok((select realtime.topic())))
    )
  $policy$;
end
$realtime$;
$slice$;
  v_out := v_out || jsonb_build_object('slice_applied', true);

  -- The real login's view of the real root, as counts only.
  perform pg_temp.as_login(v_real);
  v_after := jsonb_build_object(
    'member', public.ab_member('mesh2') <> '',
    'roster', (select count(*) from public.ab_members where root = 'mesh2'),
    'docs', (select count(*) from public.ab_docs where root = 'mesh2'),
    'chat_metas', (select count(*) from public.ab_docs
                   where root = 'mesh2' and path like 'chats/%/meta.json'),
    'logs', (select count(*) from public.ab_logs where root = 'mesh2'),
    'events', (select count(*) from public.ab_change_events where root = 'mesh2'),
    'blobs', (select count(*) from storage.objects
              where bucket_id = 'ab-mesh' and name like 'mesh2/%'),
    'poke_topic', public.ab_poke_topic_ok('ab-mesh2'));
  perform pg_temp.as_owner();

  v_out := v_out || jsonb_build_object('real_view_before', v_before,
    'real_view_after', v_after,
    'real_view_unchanged', (v_before - 'poke_topic') = (v_after - 'poke_topic'));

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

  raise exception using errcode = 'P0001', message = 'EXPERIMENT ' || v_out::text;
end
$experiment$;
