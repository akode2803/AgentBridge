-- Rolled-back production experiment for 20261020-production-p7-slice.sql
-- (safe experiments allowed by Aryan 2026-10-18; P7 approved for production
-- 2026-10-20). One DO block that always rolls back: it applies the slice in
-- its own transaction, checks that the real login's view of the real root
-- is unchanged and that its feeds answer, then runs the P7 cases on a
-- throwaway root under production's own policies. The observations come
-- back in the final exception; nothing persists.
do $experiment$
declare
  v_root text := 'zz-accept-p7';
  v_alice uuid := '00000000-0000-4000-8000-0000000000a1';
  v_bob uuid := '00000000-0000-4000-8000-0000000000b2';
  v_chat text := 'p7-g0123456789abcdef';
  v_out jsonb := '{}'::jsonb;
  v_n bigint;
  v_real uuid;
  v_before jsonb;
  v_after jsonb;
  v_feed jsonb;
  v_ids bigint[];
  v_plan text := '';
  v_row record;
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
    'live_docs', (select count(*) from public.ab_docs
                  where root = 'mesh2' and not deleted),
    'chat_metas', (select count(*) from public.ab_docs
                   where root = 'mesh2' and path like 'chats/%/meta.json'),
    'logs', (select count(*) from public.ab_logs where root = 'mesh2'));
  perform pg_temp.as_owner();

  -- The slice's ALTERs queue behind any open transaction on the tables and
  -- every later reader queues behind them: give up rather than stall.
  perform set_config('lock_timeout', '5s', true);
  execute $slice$
-- Production slice of docs/COMMIT_ORDERED_CURSORS.md (P7) for
-- eakewaaqbilmtswbbxix, 2026-10-20. Generated from docs/supabase_schema.sql;
-- each part below is copied verbatim from it:
--   1. the member delete policy (tombstones only), edited in place;
--   2. the commit-ordered feeds section at the end of the file.

-- Members hard-delete only tombstones (the janitor's purge): a live row's
-- hard delete never reaches another reader's delta feed, so it would force
-- every reader in the root to pull everything (P7's floor). Deleting a doc
-- is a soft delete (deleted = true); only an operator removes live rows.
drop policy if exists ab_docs_member_delete on public.ab_docs;
create policy ab_docs_member_delete on public.ab_docs
for delete to authenticated using (
  deleted
  and public.ab_root_ok(root) and (
    path not like 'chats/%'
    or public.ab_can_read_chat(root, public.ab_chat_of(path))
  )
  and path not like 'chats/%/runtime/effects/%'
);

-- ---------------------------------------------------------------------------
-- Commit-ordered feeds (P7, 2026-10-18). Idempotent; paste and Run.
-- Design: docs/COMMIT_ORDERED_CURSORS.md.
--
-- ab_docs.seq and ab_logs.id are handed out before commit, so a reader of
-- `seq > cursor` can pass a row whose transaction commits later and never
-- see it. Each row now records its writing transaction, and the two feeds
-- below serve only rows below the oldest transaction still running (the
-- frontier): a transaction below it has finished and can write nothing
-- more, so a cursor never passes a row that has not committed. Old clients
-- keep reading seq and id directly and see no change.

-- Constant defaults: no table rewrite. Existing rows are older than any
-- frontier a client stores from now on.
alter table public.ab_docs
  add column if not exists txid xid8 not null default '0';
alter table public.ab_logs
  add column if not exists txid xid8 not null default '0';

-- The top-level transaction, also inside subtransactions. A separate
-- trigger rather than ab_docs_touch, so that function keeps one definition.
create or replace function private.ab_docs_record_txid()
returns trigger language plpgsql set search_path = pg_catalog as $$
begin
  new.txid := pg_catalog.pg_current_xact_id();
  return new;
end
$$;
revoke all on function private.ab_docs_record_txid()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_txid on public.ab_docs;
create trigger ab_docs_txid
before insert or update on public.ab_docs for each row
execute function private.ab_docs_record_txid();

-- Ids within one log follow commit order, so a client's per-log offset
-- (`id > offset`) can never pass a row still in flight. The trigger takes
-- the lock ab_recovery_record_log_heads already takes after every append,
-- but before the id is drawn: a second append to the same log waits until
-- the first commits or rolls back, then draws a larger id. The identity
-- value drawn before this trigger is discarded (ids are not dense anyway).
-- Lock order: the change ledger's shared root lock first, this log lock
-- second, on every path. The trigger takes the ledger lock itself rather
-- than rely on ab_mutation_legacy_gate (absent on a project without the
-- typed protocol, where the ledger's own triggers take it only AFTER the
-- row): a log-then-ledger writer could otherwise deadlock with a
-- ledger-then-log one behind a queued exclusive page read, seal or fence.
-- Re-taking a shared lock this transaction holds costs nothing.
-- Definer: the inserting role has no right to call nextval directly. An
-- import that must keep its ids disables this trigger on purpose.
create or replace function private.ab_order_log_id()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
begin
  perform pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended(
    'agentbridge-change-ledger:' || new.root, 0));
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-log-head:' || pg_catalog.jsonb_build_array(
      new.root, new.chat_id, new.log_name)::text,
    0));
  new.id := pg_catalog.nextval(
    pg_catalog.pg_get_serial_sequence('public.ab_logs', 'id')::regclass);
  new.txid := pg_catalog.pg_current_xact_id();
  return new;
end
$$;
revoke all on function private.ab_order_log_id()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_order_log_id on public.ab_logs;
create trigger ab_order_log_id
before insert on public.ab_logs for each row
execute function private.ab_order_log_id();

create index if not exists ab_docs_feed_order
  on public.ab_docs (root, txid, seq);
create index if not exists ab_logs_feed_order
  on public.ab_logs (root, txid, id);

-- "Cursor too old": a hard-deleted doc row leaves no trace in the feed, so
-- a reader that may have missed it must pull everything. A purged tombstone
-- raises the root's floor to its own (txid, seq): a reader past it already
-- applied the delete. A live row (only an operator can hard-delete one; the
-- member delete policy allows tombstones only) raises it to the deleting
-- transaction itself, (txid, 1): no reader can learn of that delete from
-- the feed, so every cursor up to it is too old. In practice the janitor's
-- purge of tombstones older than 30 days. Its own table: ab_change_epochs
-- refuses every update. An empty delete inserts nothing.
create table if not exists public.ab_doc_floors (
  root text primary key,
  txid xid8 not null,
  seq  bigint not null
);
alter table public.ab_doc_floors enable row level security;
revoke all on table public.ab_doc_floors from public, anon, authenticated;
grant select on table public.ab_doc_floors to authenticated;
drop policy if exists ab_doc_floors_member_select on public.ab_doc_floors;
create policy ab_doc_floors_member_select on public.ab_doc_floors
for select to authenticated using ((select public.ab_root_ok(root)));

create or replace function private.ab_docs_raise_floor()
returns trigger language plpgsql security definer set search_path = pg_catalog as $$
begin
  insert into public.ab_doc_floors as f (root, txid, seq)
  select distinct on (c.root) c.root, c.txid, c.seq
  from (
    select d.root,
           case when d.deleted then d.txid
                else pg_catalog.pg_current_xact_id() end as txid,
           case when d.deleted then d.seq else 1::bigint end as seq
    from old_rows d
  ) c
  order by c.root, c.txid desc, c.seq desc
  on conflict (root) do update
    set txid = excluded.txid, seq = excluded.seq
    where (excluded.txid, excluded.seq) > (f.txid, f.seq);
  return null;
end
$$;
revoke all on function private.ab_docs_raise_floor()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_docs_floor on public.ab_docs;
create trigger ab_docs_floor
after delete on public.ab_docs
referencing old table as old_rows
for each statement execute function private.ab_docs_raise_floor();

-- The feeds. Each answer is one jsonb value computed in one statement, so
-- the frontier, the page, the floor and the epoch share one snapshot.
-- `has_more` comes from reading one row past the limit, so a server row cap
-- can never make a cut-off page look final. `next` is the last row's
-- position while has_more, else (frontier, 0): everything below the
-- frontier has been read, and that frontier is what a client stores.
-- Transaction ids travel as bigint. RLS filters rows as for direct reads.
-- `too_old`: the cursor is below the floor, or above the server's next
-- transaction id (the server went back in time: a restore); the client
-- then pulls everything. The epoch changes on a restore (P10).
create or replace function public.ab_feeds_frontier(p_root text)
returns jsonb
language sql stable security invoker set search_path = pg_catalog as $$
  select pg_catalog.jsonb_build_object(
    'frontier', pg_catalog.pg_snapshot_xmin(
      pg_catalog.pg_current_snapshot())::text::bigint,
    'epoch', (select e.epoch from public.ab_change_epochs e
              where e.root = p_root))
$$;
revoke all on function public.ab_feeds_frontier(text) from public, anon;
grant execute on function public.ab_feeds_frontier(text)
  to authenticated, service_role;

create or replace function public.ab_docs_feed(
  p_root text, p_after_txid bigint, p_after_seq bigint, p_limit integer
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog as $$
declare
  v_out jsonb;
begin
  if p_root is null or p_root = ''
     or p_after_txid is null or p_after_txid < 0
     or p_after_seq is null or p_after_seq < 0
     or p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'invalid doc feed request' using errcode = '22023';
  end if;
  with snap as (
    select pg_catalog.pg_current_snapshot() as s
  ), bounds as (
    select pg_catalog.pg_snapshot_xmin(snap.s) as frontier,
           pg_catalog.pg_snapshot_xmax(snap.s) as horizon,
           p_after_txid::text::xid8 as after_txid
    from snap
  ), doc_floor as (
    select f.txid, f.seq from public.ab_doc_floors f where f.root = p_root
  ), verdict as (
    select b.*,
           (b.after_txid > b.horizon
            or exists (select 1 from doc_floor f
                       where (b.after_txid, p_after_seq) < (f.txid, f.seq)))
             as too_old
    from bounds b
  ), page as (
    select d.path, d.data, d.deleted, d.seq, d.txid
    from public.ab_docs d, verdict v
    where not v.too_old
      and d.root = p_root
      and (d.txid, d.seq) > (v.after_txid, p_after_seq)
      and d.txid < v.frontier
    order by d.txid, d.seq
    limit p_limit + 1
  ), numbered as (
    select p.*, pg_catalog.row_number() over (order by p.txid, p.seq) as n
    from page p
  )
  select pg_catalog.jsonb_build_object(
    'rows', coalesce((
      select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'path', n.path, 'data', n.data, 'deleted', n.deleted,
        'seq', n.seq, 'txid', n.txid::text::bigint) order by n.n)
      from numbered n where n.n <= p_limit), '[]'::jsonb),
    'has_more', (select pg_catalog.count(*) from page) > p_limit,
    'next', case
      when (select pg_catalog.count(*) from page) > p_limit then (
        select pg_catalog.jsonb_build_array(n.txid::text::bigint, n.seq)
        from numbered n where n.n = p_limit)
      else pg_catalog.jsonb_build_array(v.frontier::text::bigint, 0)
    end,
    'frontier', v.frontier::text::bigint,
    'epoch', (select e.epoch from public.ab_change_epochs e
              where e.root = p_root),
    'too_old', v.too_old)
  into v_out
  from verdict v;
  return v_out;
end
$$;
revoke all on function public.ab_docs_feed(text, bigint, bigint, integer)
  from public, anon;
grant execute on function public.ab_docs_feed(text, bigint, bigint, integer)
  to authenticated, service_role;

-- The root-wide log feed names which logs grew; each log is then read from
-- its own integer offset, which the per-log order above keeps safe. No
-- floor: only a deleted chat's logs are hard-deleted, and its tombstone and
-- the visible-set check already tell every reader.
create or replace function public.ab_logs_feed(
  p_root text, p_after_txid bigint, p_after_id bigint, p_limit integer
) returns jsonb
language plpgsql stable security invoker set search_path = pg_catalog as $$
declare
  v_out jsonb;
begin
  if p_root is null or p_root = ''
     or p_after_txid is null or p_after_txid < 0
     or p_after_id is null or p_after_id < 0
     or p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'invalid log feed request' using errcode = '22023';
  end if;
  with snap as (
    select pg_catalog.pg_current_snapshot() as s
  ), verdict as (
    select pg_catalog.pg_snapshot_xmin(snap.s) as frontier,
           p_after_txid::text::xid8 as after_txid,
           p_after_txid::text::xid8 > pg_catalog.pg_snapshot_xmax(snap.s)
             as too_old
    from snap
  ), page as (
    select l.id, l.chat_id, l.log_name, l.txid
    from public.ab_logs l, verdict v
    where not v.too_old
      and l.root = p_root
      and (l.txid, l.id) > (v.after_txid, p_after_id)
      and l.txid < v.frontier
    order by l.txid, l.id
    limit p_limit + 1
  ), numbered as (
    select p.*, pg_catalog.row_number() over (order by p.txid, p.id) as n
    from page p
  )
  select pg_catalog.jsonb_build_object(
    'rows', coalesce((
      select pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'id', n.id, 'chat_id', n.chat_id, 'log_name', n.log_name,
        'txid', n.txid::text::bigint) order by n.n)
      from numbered n where n.n <= p_limit), '[]'::jsonb),
    'has_more', (select pg_catalog.count(*) from page) > p_limit,
    'next', case
      when (select pg_catalog.count(*) from page) > p_limit then (
        select pg_catalog.jsonb_build_array(n.txid::text::bigint, n.id)
        from numbered n where n.n = p_limit)
      else pg_catalog.jsonb_build_array(v.frontier::text::bigint, 0)
    end,
    'frontier', v.frontier::text::bigint,
    'epoch', (select e.epoch from public.ab_change_epochs e
              where e.root = p_root),
    'too_old', v.too_old)
  into v_out
  from verdict v;
  return v_out;
end
$$;
revoke all on function public.ab_logs_feed(text, bigint, bigint, integer)
  from public, anon;
grant execute on function public.ab_logs_feed(text, bigint, bigint, integer)
  to authenticated, service_role;

  $slice$;

  perform pg_temp.as_login(v_real);
  v_after := jsonb_build_object(
    'member', public.ab_member('mesh2') <> '',
    'roster', (select count(*) from public.ab_members where root = 'mesh2'),
    'docs', (select count(*) from public.ab_docs where root = 'mesh2'),
    'live_docs', (select count(*) from public.ab_docs
                  where root = 'mesh2' and not deleted),
    'chat_metas', (select count(*) from public.ab_docs
                   where root = 'mesh2' and path like 'chats/%/meta.json'),
    'logs', (select count(*) from public.ab_logs where root = 'mesh2'));
  v_out := v_out || jsonb_build_object('real_view_before', v_before,
    'real_view_after', v_after, 'real_view_unchanged', v_before = v_after);

  -- 1. The real login's feeds on the real root (existing rows: txid 0).
  v_feed := public.ab_feeds_frontier('mesh2');
  v_out := v_out || jsonb_build_object(
    'real_frontier_is_number', jsonb_typeof(v_feed->'frontier') = 'number',
    'real_epoch_present', jsonb_typeof(v_feed->'epoch') = 'string');
  v_feed := public.ab_docs_feed('mesh2', 0, 0, 1000);
  v_out := v_out || jsonb_build_object(
    'real_doc_page_rows', jsonb_array_length(v_feed->'rows'),
    'real_doc_page_has_more', v_feed->'has_more',
    'real_doc_page_too_old', v_feed->'too_old');
  v_feed := public.ab_logs_feed('mesh2', 0, 0, 1000);
  v_out := v_out || jsonb_build_object(
    'real_log_page_rows', jsonb_array_length(v_feed->'rows'),
    'real_log_page_has_more', v_feed->'has_more',
    'real_log_page_too_old', v_feed->'too_old');
  perform pg_temp.as_owner();

  -- Fixture, as the database owner.
  insert into public.ab_members(root, username, uid) values
    (v_root, 'alice', v_alice), (v_root, 'bob', v_bob);
  insert into public.ab_docs(root, path, data) values
    (v_root, 'chats/' || v_chat || '/meta.json', jsonb_build_object(
      'members', jsonb_build_object('alice', '{}'::jsonb, 'bob', '{}'::jsonb))),
    (v_root, 'status/live.json', '{}'), (v_root, 'status/tomb.json', '{}');
  update public.ab_docs set deleted = true, data = '{}'
  where root = v_root and path = 'status/tomb.json';

  -- 2. Rows of a transaction still running are held back, even from itself.
  perform pg_temp.as_login(v_alice);
  v_feed := public.ab_docs_feed(v_root, 0, 0, 1000);
  v_out := v_out || jsonb_build_object(
    'own_inflight_doc_rows', jsonb_array_length(v_feed->'rows'),
    'frontier_at_or_below_own_xact',
      (v_feed->'frontier')::bigint <= pg_catalog.txid_current());

  -- 3. A member hard-deletes a tombstone, never a live doc.
  delete from public.ab_docs where root = v_root and path = 'status/live.json';
  get diagnostics v_n = row_count;
  v_out := v_out || jsonb_build_object('member_live_delete_rows', v_n);
  delete from public.ab_docs where root = v_root and path = 'status/tomb.json';
  get diagnostics v_n = row_count;
  v_out := v_out || jsonb_build_object('member_tombstone_delete_rows', v_n);

  -- 4. That purge raised the floor: a cursor below it is too old.
  v_feed := public.ab_docs_feed(v_root, 0, 0, 10);
  v_out := v_out || jsonb_build_object('below_floor_too_old', v_feed->'too_old');
  perform pg_temp.as_owner();
  v_out := v_out || jsonb_build_object('floor_rows',
    (select count(*) from public.ab_doc_floors where root = v_root));

  -- 5. Two appends to one log draw increasing ids; the logs carry txid.
  insert into public.ab_logs(root, chat_id, log_name, line) values
    (v_root, v_chat, 'alice@box.jsonl', '{"id":"m1"}');
  insert into public.ab_logs(root, chat_id, log_name, line) values
    (v_root, v_chat, 'alice@box.jsonl', '{"id":"m2"}');
  select array_agg(id order by line) into v_ids from public.ab_logs
  where root = v_root and log_name = 'alice@box.jsonl';
  v_out := v_out || jsonb_build_object(
    'log_ids_follow_order', v_ids[1] < v_ids[2],
    'log_txid_is_this_xact', (select bool_and(txid = pg_catalog.pg_current_xact_id())
                              from public.ab_logs where root = v_root));

  -- 6. Existing rows keep txid 0; the new triggers and indexes exist.
  v_out := v_out || jsonb_build_object(
    'existing_doc_txids_zero', not exists (
      select 1 from public.ab_docs where root = 'mesh2' and txid <> '0'),
    'existing_log_txids_zero', not exists (
      select 1 from public.ab_logs where root = 'mesh2' and txid <> '0'),
    'triggers', (select jsonb_agg(tgname order by tgname) from pg_trigger
                 where tgname in ('ab_docs_txid', 'ab_order_log_id', 'ab_docs_floor')),
    'indexes', (select jsonb_agg(indexname order by indexname) from pg_indexes
                where indexname in ('ab_docs_feed_order', 'ab_logs_feed_order')));

  -- 7. Each feed's page query can use its index.
  set local enable_seqscan = off;
  for v_row in execute $q$explain (costs off) select d.path from public.ab_docs d
      where d.root = 'mesh2' and (d.txid, d.seq) > ('5'::xid8, 0::bigint)
        and d.txid < '900'::xid8 order by d.txid, d.seq limit 1001$q$ loop
    v_plan := v_plan || v_row."QUERY PLAN" || ' | ';
  end loop;
  for v_row in execute $q$explain (costs off) select l.id from public.ab_logs l
      where l.root = 'mesh2' and (l.txid, l.id) > ('5'::xid8, 0::bigint)
        and l.txid < '900'::xid8 order by l.txid, l.id limit 1001$q$ loop
    v_plan := v_plan || v_row."QUERY PLAN" || ' | ';
  end loop;
  v_out := v_out || jsonb_build_object(
    'doc_feed_index', v_plan like '%ab_docs_feed_order%',
    'log_feed_index', v_plan like '%ab_logs_feed_order%');

  raise exception using errcode = 'P0001', message = 'EXPERIMENT ' || v_out::text;
end
$experiment$;
