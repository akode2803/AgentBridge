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
