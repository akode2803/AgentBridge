-- Scratch-only scaffold correction: the clean-install schema
-- (docs/supabase_schema.sql) gives every ab_docs insert/update a globally
-- monotonic seq from public.ab_docs_ver. The Scratch scaffold lacked it, so
-- every document stayed at seq 0 and no conditional (compare) RPC could be
-- exercised. Statements copied verbatim from the schema; the backfill is
-- omitted on purpose (existing Scratch rows are disposable fixtures).
create sequence if not exists public.ab_docs_ver;

create or replace function public.ab_docs_touch() returns trigger
language plpgsql as $$
begin
  new.seq := nextval('public.ab_docs_ver');
  new.updated := now();
  return new;
end $$;

drop trigger if exists ab_docs_touch on public.ab_docs;
create trigger ab_docs_touch before insert or update on public.ab_docs
  for each row execute function public.ab_docs_touch();
