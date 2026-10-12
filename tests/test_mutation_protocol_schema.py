"""Inactive provider mutation epoch, legacy gate and seal contract."""

from pathlib import Path


SCHEMA = (Path(__file__).resolve().parents[1]
          / "docs" / "supabase_schema.sql").read_text(encoding="utf-8")


def _body(start_text: str, end_text: str) -> str:
    start = SCHEMA.index(start_text)
    return SCHEMA[start:SCHEMA.index(end_text, start)]


def test_mutation_protocol_tables_are_private_and_bounded():
    for table in (
            "ab_mutation_roots", "ab_database_mutation_seals",
            "ab_mutation_receipts"):
        assert f"create table if not exists public.{table}" in SCHEMA
        assert f"alter table public.{table} enable row level security" in SCHEMA
        assert f"revoke all on table public.{table}" in SCHEMA
    receipts = _body(
        "create table if not exists public.ab_mutation_receipts",
        "create index if not exists ab_mutation_receipts_actor",
    )
    assert "primary key (root, protocol_epoch, operation_id)" in receipts
    assert "payload_digest ~ '^[0-9a-f]{64}$'" in receipts
    assert "pg_catalog.octet_length(result::text) <= 16384" in receipts
    assert "operation_kind in (" in receipts
    assert "grant" not in receipts


def test_every_legacy_source_write_checks_gate_after_shared_root_lock():
    gate = _body(
        "create or replace function private.ab_mutation_legacy_gate()",
        "revoke all on function private.ab_mutation_legacy_gate()",
    )
    assert "security definer set search_path = pg_catalog" in gate
    isolation = gate.index("current_setting('transaction_isolation')")
    lock = gate.index("pg_advisory_xact_lock_shared")
    state_create = gate.index("insert into public.ab_mutation_roots")
    state_read = gate.index("select r.legacy_writes_enabled")
    rejection = gate.index("legacy provider writes are sealed")
    assert isolation < lock < state_create < state_read < rejection
    for table in ("ab_docs", "ab_logs", "ab_members"):
        trigger = (
            "create trigger ab_mutation_legacy_gate\n"
            f"before insert or update or delete on public.{table}"
        )
        assert trigger in SCHEMA
    assert "from public, anon, authenticated, service_role" in SCHEMA[
        SCHEMA.index("revoke all on function private.ab_mutation_legacy_gate()"):
        SCHEMA.index("drop trigger if exists ab_mutation_legacy_gate")
    ]


def test_admin_seal_is_irreversible_idempotent_and_not_api_callable():
    seal = _body(
        "create or replace function "
        "public.ab_admin_seal_legacy_database_mutations(",
        "revoke all on function "
        "public.ab_admin_seal_legacy_database_mutations(",
    )
    assert "volatile security invoker" in seal
    isolation = seal.index("current_setting('transaction_isolation')")
    lock = seal.index("pg_advisory_xact_lock(")
    root_read = seal.index("select r.* into strict v_root")
    replay_read = seal.index("select s.* into v_seal")
    source_cut = seal.index("from public.ab_change_epochs e")
    receipt = seal.index("insert into public.ab_database_mutation_seals")
    disable = seal.index("legacy_writes_enabled = false")
    assert isolation < lock < root_read < replay_read < source_cut < receipt < disable
    assert "v_seal.seal_id is distinct from p_seal_id" in seal
    assert "v_seal.plan_digest is distinct from p_plan_digest" in seal
    assert "v_seal.sealed_from_epoch is distinct from p_expected_epoch" in seal
    assert "'seal_scope', 'database-v1'" in seal
    assert "'blob_writes_sealed', false" in seal
    grant_window = SCHEMA[
        SCHEMA.index(
            "revoke all on function "
            "public.ab_admin_seal_legacy_database_mutations("):
        SCHEMA.index("create or replace function public.ab_mutation_protocol_state")
    ]
    assert "authenticated, service_role" in grant_window
    assert "grant execute" not in grant_window


def test_state_probe_is_authorized_and_marker_is_installed_last():
    state = _body(
        "create or replace function public.ab_mutation_protocol_state",
        "revoke all on function public.ab_mutation_protocol_state",
    )
    assert "stable security definer set search_path = pg_catalog" in state
    authorization = state.index("if auth.uid() is null")
    root_read = state.index("from public.ab_mutation_roots r")
    assert authorization < root_read
    assert "not public.ab_root_ok(p_root)" in state
    assert "'legacy_writes_enabled', r.legacy_writes_enabled" in state
    assert "'plan_digest', s.plan_digest" in state
    assert "'database_seal'" in state
    assert "'blob_writes_sealed', false" in state
    marker = SCHEMA.index(
        "create or replace function public.ab_mutation_protocol_ready()")
    state_grant = SCHEMA.index(
        "grant execute on function public.ab_mutation_protocol_state(text)")
    assert state_grant < marker
    assert "grant execute on function public.ab_mutation_protocol_ready()" \
        in SCHEMA[marker:]
    assert "statement_timeout" in SCHEMA[marker:]


def test_schema_install_does_not_seal_existing_or_new_roots():
    roots = _body(
        "create table if not exists public.ab_mutation_roots",
        "create table if not exists public.ab_database_mutation_seals",
    )
    assert "legacy_writes_enabled boolean not null default true" in roots
    backfill = _body(
        "insert into public.ab_mutation_roots(root)\nselect roots.root",
        "create or replace function "
        "public.ab_admin_seal_legacy_database_mutations(",
    )
    assert "on conflict (root) do nothing" in backfill
    assert "legacy_writes_enabled = false" not in backfill
