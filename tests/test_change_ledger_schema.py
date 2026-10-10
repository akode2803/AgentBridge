from pathlib import Path


SCHEMA = (Path(__file__).parents[1] / "docs" / "supabase_schema.sql").read_text(
    encoding="utf-8",
)


def test_change_ledger_is_append_only_rls_filtered_and_replay_indexed():
    assert "create table if not exists public.ab_change_events" in SCHEMA
    assert "on public.ab_change_events (root, id)" in SCHEMA
    assert "alter table public.ab_change_events enable row level security" in SCHEMA
    assert "grant select on table public.ab_change_events to authenticated" in SCHEMA
    assert "ab_change_events_member_select" in SCHEMA
    assert "public.ab_can_read_chat(root, stream_id)" in SCHEMA
    assert "after insert on public.ab_logs" in SCHEMA
    assert "after insert or update on public.ab_docs" in SCHEMA
    assert "revoke all on sequence public.ab_change_events_id_seq" in SCHEMA
    assert "ab_change_events_page" in SCHEMA
    assert "pg_advisory_xact_lock_shared" in SCHEMA
    assert "pg_advisory_xact_lock(" in SCHEMA


def test_presence_heartbeats_are_excluded_before_ledger_work():
    start = SCHEMA.index("create or replace function private.ab_record_doc_change")
    end = SCHEMA.index("drop trigger if exists ab_docs_change_ledger", start)
    body = SCHEMA[start:end]
    presence_guard = body.index("if new.path like 'presence/%'")
    writer_lock = body.index("pg_advisory_xact_lock_shared")
    epoch_write = body.index("insert into public.ab_change_epochs")
    event_write = body.index("insert into public.ab_change_events")
    assert presence_guard < writer_lock < epoch_write < event_write


def test_commit_barrier_precedes_every_event_allocation_and_page_read():
    trigger_starts = (
        "create or replace function private.ab_record_doc_change",
        "create or replace function private.ab_record_log_change",
        "create or replace function private.ab_record_root_membership_change",
    )
    for start_text in trigger_starts:
        start = SCHEMA.index(start_text)
        end = SCHEMA.index("drop trigger if exists", start)
        body = SCHEMA[start:end]
        assert body.index("pg_advisory_xact_lock_shared") \
            < body.index("insert into public.ab_change_events")

    start = SCHEMA.index("create or replace function public.ab_change_events_page")
    end = SCHEMA.index("revoke all on function public.ab_change_events_page", start)
    body = SCHEMA[start:end]
    assert "language plpgsql volatile security invoker" in body
    assert body.index("pg_advisory_xact_lock(") \
        < body.index("from public.ab_change_events")


def test_membership_edges_emit_root_visibility_without_a_chat_identity():
    assert "ab_members_change_ledger" in SCHEMA
    assert "after insert or delete on public.ab_members" in SCHEMA
    assert "values (v_root, 'root', '', 'visibility')" in SCHEMA
    assert "old.data->'members' is distinct from new.data->'members'" in SCHEMA
    assert "values (new.root, 'root', '', 'visibility')" in SCHEMA


def test_epoch_floor_and_realtime_publication_are_idempotent():
    assert "minimum_cursor bigint not null default 0" in SCHEMA
    assert "on conflict (root) do nothing" in SCHEMA
    assert "pg_catalog.pg_publication_tables" in SCHEMA
    assert "alter publication supabase_realtime add table public.ab_change_events" in SCHEMA


def test_local_node_source_ledger_binds_exact_identity_to_the_commit_fence():
    assert "add column if not exists source_key text" in SCHEMA
    doc_start = SCHEMA.index("create or replace function private.ab_record_doc_change")
    doc_end = SCHEMA.index("drop trigger if exists ab_docs_change_ledger", doc_start)
    doc_body = SCHEMA[doc_start:doc_end]
    assert "source_key, doc_head" in doc_body
    assert "new.path, new.seq" in doc_body

    log_start = SCHEMA.index("create or replace function private.ab_record_log_change")
    log_end = SCHEMA.index("drop trigger if exists ab_logs_change_ledger", log_start)
    log_body = SCHEMA[log_start:log_end]
    assert "source_key, log_head" in log_body
    assert "new.log_name, new.id" in log_body

    for function in ("ab_node_source_ledger_fence", "ab_node_source_events_page"):
        start = SCHEMA.index(f"create or replace function public.{function}")
        end = SCHEMA.index(f"revoke all on function public.{function}", start)
        body = SCHEMA[start:end]
        assert "language plpgsql volatile security invoker" in body
        assert body.index("pg_advisory_xact_lock(") \
            < body.index("public.ab_change_events")
    fence_start = SCHEMA.index(
        "create or replace function public.ab_node_source_ledger_fence",
    )
    fence_end = SCHEMA.index(
        "revoke all on function public.ab_node_source_ledger_fence", fence_start,
    )
    fence_body = SCHEMA[fence_start:fence_end]
    assert "left join lateral" in fence_body
    assert "order by e.id desc" in fence_body
    assert "pg_catalog.max(e.id)" not in fence_body
    assert "e.source_key" in SCHEMA
    assert "pg_catalog.octet_length(stream_id) <= 1024" in SCHEMA
    assert "pg_catalog.octet_length(source_key) <= 4096" in SCHEMA
    assert "from public, anon, authenticated" in SCHEMA
