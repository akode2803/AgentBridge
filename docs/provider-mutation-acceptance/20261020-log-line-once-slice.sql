-- Production slice of the "One copy per appended line" section for
-- eakewaaqbilmtswbbxix, 2026-10-20. Copied verbatim from the end of
-- docs/supabase_schema.sql. Apply behind `set local lock_timeout = '5s'`.
-- Removes production's 7 remaining duplicate copies (each pair is
-- byte-identical, ids adjacent; the lowest id stays), builds the unique
-- index, and installs the trigger that skips a retried identical line.

-- ---------------------------------------------------------------------------
-- One copy per appended line (2026-10-20). Idempotent; paste and Run.
--
-- A log append is a plain INSERT that clients retry after a transient
-- error. When the first try had committed and only its reply was lost, the
-- retry wrote a second, byte-identical row (production held 8 such pairs).
-- A line carries a unique envelope id, a nonce and a signature, so an
-- identical line in the same log is only ever a retry.
--
-- The trigger skips such a retry, so every client, old or new, sees it
-- succeed (an error would park an old client's outbox, and every later
-- send to that chat behind it, for good). It first takes the locks
-- ab_order_log_id takes, in the same order, and the log lock is held to
-- commit: a retry racing its first try waits for that commit, then sees
-- the line. Its name orders it after ab_mutation_legacy_gate (a sealed
-- root still refuses the write, and no lock moves ahead of the gate) and
-- before ab_order_log_id (a skipped line draws no id). It runs as the
-- caller, so only someone who can read the line is told it is there;
-- anyone else meets the insert policy as before.
-- The unique index serves the lookup and backs the trigger: a copy that
-- still arrives is a 23505, which the client accepts once it reads the
-- identical line back. Existing copies go first (the lowest id stays; the
-- delete trigger moves each log's head and tells readers), under a lock
-- that keeps a new copy from landing before the index exists.
do $line_once$
begin
  lock table public.ab_logs in share row exclusive mode;
  delete from public.ab_logs l
  using public.ab_logs k
  where k.root = l.root and k.chat_id = l.chat_id
    and k.log_name = l.log_name and k.line = l.line and k.id < l.id;
  create unique index if not exists ab_logs_line_once
    on public.ab_logs (root, chat_id, log_name, md5(line));
end
$line_once$;

create or replace function private.ab_omit_landed_line()
returns trigger language plpgsql security invoker set search_path = pg_catalog as $$
begin
  perform pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended(
    'agentbridge-change-ledger:' || new.root, 0));
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'agentbridge-log-head:' || pg_catalog.jsonb_build_array(
      new.root, new.chat_id, new.log_name)::text,
    0));
  if exists (
    select 1 from public.ab_logs l
    where l.root = new.root and l.chat_id = new.chat_id
      and l.log_name = new.log_name
      and pg_catalog.md5(l.line) = pg_catalog.md5(new.line)
      and l.line = new.line
  ) then
    return null;
  end if;
  return new;
end
$$;
revoke all on function private.ab_omit_landed_line()
  from public, anon, authenticated, service_role;
drop trigger if exists ab_omit_landed_line on public.ab_logs;
create trigger ab_omit_landed_line
before insert on public.ab_logs for each row
execute function private.ab_omit_landed_line();
