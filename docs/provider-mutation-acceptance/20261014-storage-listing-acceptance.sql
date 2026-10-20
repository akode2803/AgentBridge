-- Database half of the hosted Storage listing check (BACKLOG "Hosted Storage
-- listing check"). Runs on AgentBridge Scratch only, as one DO block that
-- always rolls back: the observations come back in the final exception.
--
-- It replays the janitor's walk (SupabaseTransport.list_blobs: 100 names a
-- page, offset paging, recurse into rows without an id) through
-- storage.search -- the function Storage's list endpoint calls -- as each
-- principal under RLS, and compares the files found with the true count.
-- Not covered here: the Storage HTTP layer itself (needs a signed-in
-- session; see devtools/validate_blob_storage_http.py).
do $acceptance$
declare
  v_root text := 'zz-accept-listing';
  v_alice uuid := '00000000-0000-4000-8000-0000000000a1';
  v_bob uuid := '00000000-0000-4000-8000-0000000000b2';
  v_carol uuid := '00000000-0000-4000-8000-0000000000c3';
  v_live text := 'live-g0123456789abcdef';
  v_dead text := 'dead-gfedcba9876543210';
  v_out jsonb := '{}'::jsonb;
  v_truth jsonb;
  v_who record;
  v_folder text;
  v_res record;
  v_row jsonb;
  v_n integer;
  i integer;
begin
  execute $f$
    create function pg_temp.ab_list_all(p_folder text, out files integer,
      out pages integer, out folders integer, out max_page integer)
    language plpgsql as $b$
    declare
      v_stack text[] := array[p_folder];
      v_folder text;
      v_offset integer;
      v_n integer;
      r record;
    begin
      files := 0; pages := 0; folders := 0; max_page := 0;
      while coalesce(array_length(v_stack, 1), 0) > 0 loop
        v_folder := v_stack[array_length(v_stack, 1)];
        v_stack := v_stack[1:array_length(v_stack, 1) - 1];
        v_offset := 0;
        loop
          v_n := 0;
          for r in select * from storage.search(v_folder || '/', 'ab-mesh',
              100, 1, v_offset, '', 'name', 'asc', null, null) loop
            v_n := v_n + 1;
            if r.id is null then
              v_stack := v_stack || (v_folder || '/' || r.name);
              folders := folders + 1;
            else
              files := files + 1;
            end if;
          end loop;
          pages := pages + 1;
          max_page := greatest(max_page, v_n);
          exit when v_n < 100;
          v_offset := v_offset + 100;
        end loop;
      end loop;
    end
    $b$
  $f$;

  -- Fixture, as the database owner. A new root is unsealed, so the legacy
  -- gate admits these direct rows.
  insert into public.ab_members(root, username, uid) values
    (v_root, 'alice', v_alice), (v_root, 'bob', v_bob),
    (v_root, 'carol', v_carol);
  insert into public.ab_docs(root, path, data) values
    (v_root, 'chats/' || v_live || '/meta.json', jsonb_build_object(
      'members', jsonb_build_object(
        'alice', jsonb_build_object('role', 'admin'),
        'bob', jsonb_build_object('role', 'member')))),
    (v_root, 'chats/' || v_dead || '/meta.json', jsonb_build_object(
      'deleted', true, 'members', '{}'::jsonb,
      'final_members', jsonb_build_array('alice'),
      'tenure', jsonb_build_object('alice', '[[1, 0]]'::jsonb,
                                   'bob', '[[1, 2]]'::jsonb)));
  -- Live room: 150 files (two pages and a half), a group photo, and an
  -- unexpected nested folder.
  for i in 0..149 loop
    insert into storage.objects(bucket_id, name, owner_id, metadata) values
      ('ab-mesh', v_root || '/chats/' || v_live || '/files/f'
        || lpad(i::text, 3, '0'), v_alice::text, '{"size": 10}');
  end loop;
  insert into storage.objects(bucket_id, name, owner_id, metadata) values
    ('ab-mesh', v_root || '/chats/' || v_live || '/avatar.png',
     v_alice::text, '{"size": 10}'),
    ('ab-mesh', v_root || '/chats/' || v_live || '/odd/deep/x',
     v_alice::text, '{"size": 10}'),
    -- alice's staged upload with no manifest yet: only she may see it
    ('ab-mesh', v_root || '/blob-stage/' || v_live || '/' || md5('live-s'),
     v_alice::text, '{"size": 10}');
  -- Deleted room: 120 files, an unexpected folder, and three objects bob
  -- staged and never published.
  for i in 0..119 loop
    insert into storage.objects(bucket_id, name, owner_id, metadata) values
      ('ab-mesh', v_root || '/chats/' || v_dead || '/files/g'
        || lpad(i::text, 3, '0'), v_bob::text, '{"size": 10}');
  end loop;
  insert into storage.objects(bucket_id, name, owner_id, metadata) values
    ('ab-mesh', v_root || '/chats/' || v_dead || '/odd/y',
     v_bob::text, '{"size": 10}');
  for i in 1..3 loop
    insert into storage.objects(bucket_id, name, owner_id, metadata) values
      ('ab-mesh', v_root || '/blob-stage/' || v_dead || '/' || md5('dead-' || i),
       v_bob::text, '{"size": 10}');
  end loop;

  select jsonb_object_agg(f, (select count(*) from storage.objects o
           where o.bucket_id = 'ab-mesh' and o.name like v_root || '/' || f || '/%'))
    into v_truth
  from unnest(array['chats/' || v_live, 'blob-stage/' || v_live,
                    'chats/' || v_dead, 'blob-stage/' || v_dead]) f;
  v_out := v_out || jsonb_build_object('truth', v_truth,
    'authenticated_can_search', has_function_privilege('authenticated',
      'storage.search(text,text,integer,integer,integer,text,text,text,text,text)',
      'execute'));

  -- The janitor's three folders, walked as each principal.
  for v_who in select * from (values ('alice', v_alice), ('bob', v_bob),
                                     ('carol', v_carol)) w(name, uid) loop
    perform set_config('role', 'authenticated', true);
    perform set_config('request.jwt.claim.sub', v_who.uid::text, true);
    perform set_config('request.jwt.claims', jsonb_build_object(
      'sub', v_who.uid, 'role', 'authenticated')::text, true);
    v_row := '{}'::jsonb;
    foreach v_folder in array array['chats/' || v_live, 'blob-stage/' || v_live,
                                    'chats/' || v_dead, 'blob-stage/' || v_dead] loop
      begin
        select * into v_res from pg_temp.ab_list_all(v_root || '/' || v_folder);
        v_row := v_row || jsonb_build_object(v_folder, jsonb_build_object(
          'files', v_res.files, 'pages', v_res.pages,
          'folders', v_res.folders, 'max_page', v_res.max_page));
      exception when others then
        v_row := v_row || jsonb_build_object(v_folder, sqlstate || ' ' || sqlerrm);
      end;
    end loop;
    -- `_strict_object`: the exact-name search among 150 siblings.
    begin
      select count(*) into v_n from storage.search(
        v_root || '/chats/' || v_live || '/files/', 'ab-mesh', 100, 1, 0,
        'f105', 'name', 'asc', null, null) s where s.name = 'f105' and s.id is not null;
      v_row := v_row || jsonb_build_object('exact_f105', v_n);
      select count(*) into v_n from storage.search(
        v_root || '/chats/' || v_live || '/files/', 'ab-mesh', 100, 1, 0,
        'f999', 'name', 'asc', null, null) s;
      v_row := v_row || jsonb_build_object('exact_missing', v_n);
    exception when others then
      v_row := v_row || jsonb_build_object('exact', sqlstate || ' ' || sqlerrm);
    end;
    perform set_config('role', 'postgres', true);
    v_out := v_out || jsonb_build_object(v_who.name, v_row);
  end loop;

  raise exception using errcode = 'P0001', message = 'ACCEPTANCE ' || v_out::text;
end
$acceptance$;
