-- Production slice of the "One copy per appended line" section for
-- eakewaaqbilmtswbbxix, 2026-10-20. Copied verbatim from the end of
-- docs/supabase_schema.sql. Apply behind `set local lock_timeout = '5s'`.
-- Removes production's 7 remaining duplicate copies (each pair is
-- byte-identical, ids adjacent; the lowest id stays), then builds the index.

-- ---------------------------------------------------------------------------
-- One copy per appended line (2026-10-20). Idempotent; paste and Run.
--
-- A log append is a plain INSERT that the client retries after a transient
-- error. When the first try had committed and only its reply was lost, the
-- retry wrote a second, byte-identical row (production held 8 such pairs).
-- A line carries a unique envelope id, a nonce and a signature, so an
-- identical line in the same log is only ever a retry: the index makes the
-- retry a unique violation (23505), which the client accepts once it reads
-- the identical line back. Existing copies go first (the lowest id stays;
-- the delete trigger moves each log's head and tells readers), under a lock
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
