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
