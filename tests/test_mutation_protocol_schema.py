"""Inactive provider mutation epoch, legacy gate and seal contract."""

from pathlib import Path


SCHEMA = (Path(__file__).resolve().parents[1]
          / "docs" / "supabase_schema.sql").read_text(encoding="utf-8")


def _body(start_text: str, end_text: str) -> str:
    start = SCHEMA.index(start_text)
    return SCHEMA[start:SCHEMA.index(end_text, start)]


def _last_body(start_text: str, end_text: str) -> str:
    start = SCHEMA.rindex(start_text)
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
    gate = _last_body(
        "create or replace function private.ab_mutation_legacy_gate()",
        "revoke all on function private.ab_mutation_legacy_gate()",
    )
    assert "security definer set search_path = pg_catalog" in gate
    isolation = gate.index("current_setting('transaction_isolation')")
    lock = gate.index("pg_advisory_xact_lock_shared")
    state_create = gate.index("insert into public.ab_mutation_roots")
    state_read = gate.index(
        "select r.protocol_epoch, r.legacy_writes_enabled")
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
    seal = _last_body(
        "create or replace function "
        "public.ab_admin_seal_legacy_database_mutations(",
        "revoke all on function "
        "public.ab_admin_seal_legacy_database_mutations(",
    )
    assert "volatile security invoker" in seal
    isolation = seal.index("current_setting('transaction_isolation')")
    root_read = seal.index("select r.* into strict v_root")
    replay_read = seal.index("select s.* into v_seal")
    source_cut = seal.index("from public.ab_change_epochs e")
    receipt = seal.index("insert into public.ab_database_mutation_seals")
    disable = seal.index("legacy_writes_enabled = false")
    epoch_lock = seal.index("'agentbridge-mutation-epoch:'")
    change_lock = seal.index("'agentbridge-change-ledger:'")
    assert isolation < epoch_lock < change_lock < root_read < replay_read
    assert replay_read < source_cut < receipt < disable
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


def test_p1b_receipts_are_bounded_private_and_same_transaction_bound():
    core = SCHEMA[SCHEMA.index(
        "-- P1b core: bounded typed-mutation receipts") :]
    assert "receipt_slot between 0 and 4095" in core
    assert "ab_mutation_receipts_slot" in core
    assert "ab_mutation_receipts_applying_xid" in core
    assert "pg_try_advisory_xact_lock" in core
    begin = _body(
        "create or replace function private.ab_mutation_begin(",
        "revoke all on function private.ab_mutation_begin(",
    )
    first_occupied = begin.index("if exists (", begin.index("for v_probe"))
    try_lock = begin.index("pg_try_advisory_xact_lock", first_occupied)
    second_occupied = begin.index("if exists (", try_lock)
    release = begin.index("exception when unique_violation", second_occupied)
    assert first_occupied < try_lock < second_occupied < release
    assert "typed mutation receipt slots are contended" in core
    assert "pg_current_xact_id()" in core
    assert "private.ab_mutation_kind_permits" in core
    assert "private.ab_mutation_receipt_json" in core
    for signature in (
        "private.ab_mutation_begin(\n  text,uuid,uuid,text,text,text)",
        "private.ab_mutation_finish(\n  text,uuid,uuid,text,text,text,jsonb)",
    ):
        start = core.index("revoke all on function " + signature)
        assert "authenticated, service_role" in core[start:start + 260]


def test_p1b_exact_actor_receipt_access_and_rotation_are_narrow():
    core = SCHEMA[SCHEMA.index(
        "-- P1b core: bounded typed-mutation receipts") :]
    receipt = _body(
        "create or replace function public.ab_mutation_receipt(",
        "revoke all on function public.ab_mutation_receipt(",
    )
    ack = _body(
        "create or replace function public.ab_ack_mutation_receipt(",
        "revoke all on function public.ab_ack_mutation_receipt(",
    )
    for function in (receipt, ack):
        assert "v_actor uuid := auth.uid()" in function
        assert "r.actor_uid = v_actor" in function
        assert "ab_root_ok" not in function
    assert "grant execute on function public.ab_mutation_receipt" in core
    assert "grant execute on function public.ab_ack_mutation_receipt" in core
    rotation = _body(
        "create or replace function public.ab_admin_rotate_mutation_epoch(",
        "revoke all on function public.ab_admin_rotate_mutation_epoch(",
    )
    assert "'agentbridge-mutation-epoch:'" in rotation
    assert "r.acknowledged_at is null" in rotation
    assert "protocol_version = 2" in rotation
    assert "delete from public.ab_mutation_receipts" in rotation
    rotation_tail = core[
        core.index("revoke all on function public.ab_admin_rotate_mutation_epoch"):
        core.index("-- Authenticated observation of receipt closure")
    ]
    assert "grant execute" not in rotation_tail


def test_p1b_gate_allows_only_kind_specific_same_transaction_writes():
    gate = _last_body(
        "create or replace function private.ab_mutation_legacy_gate()",
        "revoke all on function private.ab_mutation_legacy_gate()",
    )
    state = gate.index("select r.protocol_epoch, r.legacy_writes_enabled")
    receipt = gate.index("r.applying_xid = pg_catalog.pg_current_xact_id()")
    permission = gate.index("private.ab_mutation_kind_permits")
    rejection = gate.index("legacy provider writes are sealed")
    assert state < receipt < permission < rejection
    permit = _body(
        "create or replace function private.ab_mutation_kind_permits(",
        "revoke all on function private.ab_mutation_kind_permits(",
    )
    assert "when 'doc_put'" in permit
    assert "when 'doc_create'" in permit
    assert "when 'member_leave'" in permit


def test_p1b_doc_log_rpcs_bind_normalized_requests_and_receipts():
    section = SCHEMA[SCHEMA.index(
        "-- P1b document/log slice: inactive receipt-bearing source mutation RPCs") :]
    assert "pg_catalog.sha256(" in section
    for name, kind in (
            ("doc_put", "doc_put"),
            ("doc_create", "doc_create"),
            ("doc_delete", "doc_delete"),
            ("log_append", "log_append")):
        body = _body(
            f"create or replace function public.ab_mutation_{name}(",
            f"revoke all on function public.ab_mutation_{name}(",
        )
        begin = body.index("private.ab_mutation_begin(")
        sealed = body.index("private.ab_mutation_root_sealed(")
        replay = body.index("if (v_begin->>'replay')::boolean")
        authority = min(
            value for value in (
                body.find("private.ab_mutation_lock_actor", replay),
                body.find("public.ab_is_member", replay),
            ) if value >= 0
        )
        effect = min(
            value for value in (
                body.find("insert into public.ab_docs", authority),
                body.find("update public.ab_docs", authority),
                body.find("insert into public.ab_logs", authority),
            ) if value >= 0
        )
        finish = body.index("private.ab_mutation_finish(", effect)
        assert sealed < begin < replay < authority < effect < finish
        assert f"'{kind}'" in body
        assert "private.ab_mutation_provider_digest(" in body
        tail = section[section.index(
            f"revoke all on function public.ab_mutation_{name}(") :]
        assert "from public, anon, authenticated, service_role" in tail[:320]
        assert f"grant execute on function public.ab_mutation_{name}(" in tail[:500]


def test_p1b_room_authority_lock_orders_member_then_room_then_effect():
    actor = _body(
        "create or replace function private.ab_mutation_lock_actor(",
        "revoke all on function private.ab_mutation_lock_actor(",
    )
    room = _body(
        "create or replace function private.ab_mutation_lock_chat(",
        "revoke all on function private.ab_mutation_lock_chat(",
    )
    assert "where m.root = p_root and m.uid = auth.uid()" in actor
    assert "for key share" in actor
    assert "pg_advisory_xact_lock(v_key)" in room
    assert "pg_advisory_xact_lock_shared(v_key)" in room
    assert "jsonb_build_array(p_root, p_chat)::text" in room


def test_p1b_doc_log_authority_matches_existing_lanes():
    put = _body(
        "create or replace function public.ab_mutation_doc_put(",
        "revoke all on function public.ab_mutation_doc_put(",
    )
    create = _body(
        "create or replace function public.ab_mutation_doc_create(",
        "revoke all on function public.ab_mutation_doc_create(",
    )
    delete = _body(
        "create or replace function public.ab_mutation_doc_delete(",
        "revoke all on function public.ab_mutation_doc_delete(",
    )
    append = _body(
        "create or replace function public.ab_mutation_log_append(",
        "revoke all on function public.ab_mutation_log_append(",
    )
    for body in (put, create, delete):
        assert "p_path like 'chats/%/runtime/effects/%'" in body
        assert "private.ab_mutation_path_valid(p_path, 4096, 32)" in body
        assert body.index("private.ab_mutation_lock_actor(p_root)") < \
            body.index("private.ab_mutation_lock_chat(")
    assert "p_path = 'chats/' || v_chat || '/meta.json'" in put
    assert "p_path = 'chats/' || v_chat || '/meta.json'" in create
    assert "p_path = 'chats/' || v_chat || '/meta.json'" in delete
    assert "public.ab_is_member(p_root, v_chat)" in put
    assert "not coalesce((" in create
    assert "p_data->'members' ? v_member" in create
    assert "public.ab_can_read_chat(p_root, v_chat)" in delete
    assert "public.ab_is_member(p_root, p_chat)" in append
    assert append.index("private.ab_mutation_lock_actor(p_root)") < \
        append.index("private.ab_mutation_lock_chat(p_root, p_chat, false)") < \
        append.index("public.ab_is_member(p_root, p_chat)")
    assert "returning id into v_row_id" in append
    assert "create or replace function public.ab_mutation_protocol_ready" not in \
        SCHEMA[SCHEMA.index(
            "-- P1b document/log slice: inactive receipt-bearing source mutation RPCs") :]


def test_p1b_lifecycle_rpcs_bind_receipts_before_exact_effects():
    section = SCHEMA[SCHEMA.index(
        "-- P1b lifecycle slice: inactive receipt-bearing membership") :]
    for name, kind in (
            ("member_claim", "member_claim"),
            ("member_leave", "member_leave"),
            ("chat_delete", "chat_delete")):
        body = _body(
            f"create or replace function public.ab_mutation_{name}(",
            f"revoke all on function public.ab_mutation_{name}(",
        )
        sealed = body.index("private.ab_mutation_root_sealed(")
        begin = body.index("private.ab_mutation_begin(")
        replay = body.index("if (v_begin->>'replay')::boolean")
        effect = min(
            value for value in (
                body.find("insert into public.ab_members", replay),
                body.find("delete from public.ab_members", replay),
                body.find("delete from public.ab_logs", replay),
            ) if value >= 0
        )
        finish = body.index("private.ab_mutation_finish(", effect)
        assert sealed < begin < replay < effect < finish
        assert f"'{kind}'" in body
        assert "private.ab_mutation_provider_digest(" in body
        tail = section[section.index(
            f"revoke all on function public.ab_mutation_{name}(") :]
        assert "from public, anon, authenticated, service_role" in tail[:320]
        assert f"grant execute on function public.ab_mutation_{name}(" in tail[:500]


def test_p1b_lifecycle_authority_is_self_bound_and_terminal_exact():
    claim = _body(
        "create or replace function public.ab_mutation_member_claim(",
        "revoke all on function public.ab_mutation_member_claim(",
    )
    leave = _body(
        "create or replace function public.ab_mutation_member_leave(",
        "revoke all on function public.ab_mutation_member_leave(",
    )
    delete = _body(
        "create or replace function public.ab_mutation_chat_delete(",
        "revoke all on function public.ab_mutation_chat_delete(",
    )
    assert "values (p_root, p_username, v_actor)" in claim
    assert "typed member claim conflicts with existing identity" in claim
    assert "p_uid" not in claim
    assert "where m.root = p_root and m.uid = v_actor" in leave
    assert "for update" in leave
    assert "p_uid" not in leave
    assert "and not d.deleted" in delete
    assert delete.index("private.ab_mutation_lock_actor(p_root)") < \
        delete.index("private.ab_mutation_lock_chat(p_root, p_chat, true)") < \
        delete.index("select d.data into v_meta")
    assert "v_meta @> '{\"deleted\": true}'::jsonb" in delete
    assert "v_meta->'members' is distinct from '{}'::jsonb" in delete
    assert "jsonb_typeof(v_meta->'members') is distinct from 'object'" in delete
    assert "jsonb_typeof(v_meta->'tenure') is distinct from 'object'" in delete
    assert "v_meta->'tenure' ? v_member" in delete
    assert "pg_catalog.left(d.path, pg_catalog.length(v_prefix)) = v_prefix" \
        in delete
    assert "d.path like" not in delete.lower()
    assert "create or replace function public.ab_mutation_protocol_ready" not in \
        SCHEMA[SCHEMA.index(
            "-- P1b lifecycle slice: inactive receipt-bearing membership") :]


def test_p1b_effect_rpc_binds_exact_request_and_receipt():
    body = _body(
        "create or replace function public.ab_mutation_effect_transition(",
        "revoke all on function public.ab_mutation_effect_transition(",
    )
    sealed = body.index("private.ab_mutation_root_sealed(")
    begin = body.index("private.ab_mutation_begin(")
    replay = body.index("if (v_begin->>'replay')::boolean")
    actor = body.index("private.ab_mutation_lock_actor(p_root)", replay)
    room = body.index("private.ab_mutation_lock_chat(p_root, v_chat, false)")
    exact = body.index("'agentbridge-effect-transition:'")
    actor_doc = body.index("'users/' || v_effect_actor || '.json'")
    fresh = body.index("typed effect transition already exists")
    transition = body.index("public.ab_effect_transition(", fresh)
    verify = body.index("v_existing is distinct from p_data", transition)
    finish = body.index("private.ab_mutation_finish(", verify)
    assert sealed < begin < replay < actor < room < exact < actor_doc
    assert actor_doc < fresh < transition < verify < finish
    assert "'effect_transition'" in body
    assert "private.ab_mutation_provider_digest(" in body
    assert "p_grant_ask, p_grant_decision" in body
    assert "public.ab_source_json_bytes(p_grant_ask)" in body
    assert "public.ab_source_json_bytes(p_grant_decision)" in body
    assert "for share" in body


def test_p1b_effect_rpc_is_narrow_and_does_not_advertise_readiness():
    section = SCHEMA[SCHEMA.index("-- P1b effect slice:") :]
    revoke = section.index(
        "revoke all on function public.ab_mutation_effect_transition(")
    tail = section[revoke:revoke + 620]
    assert "from public, anon, authenticated, service_role" in tail
    assert "grant execute on function public.ab_mutation_effect_transition(" \
        in tail
    assert "to authenticated" in tail
    assert "create or replace function public.ab_mutation_protocol_ready" \
        not in section


def test_p1b_blob_manifest_table_is_private_bounded_and_receipt_gated():
    section = SCHEMA[SCHEMA.index("-- P1b blob-manifest slice:") :]
    table = section[:section.index(
        "create or replace function private.ab_storage_object_size")]
    assert "create table if not exists public.ab_blob_manifests" in table
    assert "primary key (root, chat_id, blob_id)" in table
    assert "unique (root, protocol_epoch, operation_id)" in table
    assert "sealed_bytes between 1 and 52428800" in table
    assert "sealed_sha256 ~ '^[0-9a-f]{64}$'" in table
    assert "manifest_digest ~ '^[0-9a-f]{64}$'" in table
    assert "object_id        uuid not null" in table
    assert "alter table public.ab_blob_manifests enable row level security" \
        in table
    assert "revoke all on table public.ab_blob_manifests" in table
    assert (
        "before insert or update or delete on public.ab_blob_manifests"
        in table
    )
    permit = _body(
        "create or replace function private.ab_mutation_kind_permits(",
        "revoke all on function private.ab_mutation_kind_permits(",
    )
    assert "when 'blob_manifest' then p_table = 'ab_blob_manifests'" in permit
    assert "and p_operation = 'INSERT'" in permit


def test_p1b_blob_storage_policies_isolate_exact_staged_lane():
    section = SCHEMA[SCHEMA.index("-- P1b blob-manifest slice:") :]
    policy = section[section.index(
        "-- Existing policies are permissive."):
        section.index("create or replace function public.ab_mutation_blob_manifest(")]
    for name in ("select", "insert", "update", "delete"):
        assert policy.count(
            f"drop policy if exists ab_blobs_member_{name} on storage.objects"
        ) == 1
        assert policy.count(
            f"create policy ab_blobs_member_{name} on storage.objects"
        ) == 1
    assert policy.count(
        "pg_catalog.split_part(name, '/', 2) <> 'blob-stage'"
    ) >= 5
    assert policy.count(
        "pg_catalog.split_part(name, '/', 5) = ''"
    ) == 3
    assert "private.ab_storage_object_size(metadata)" not in policy
    assert "public.ab_staged_blob_readable(" in policy
    update = policy[policy.index(
        "create policy ab_blobs_member_update"):
        policy.index("drop policy if exists ab_blobs_member_delete")]
    assert update.count(
        "pg_catalog.split_part(name, '/', 2) <> 'blob-stage'"
    ) == 2
    assert ") with check (" in update
    delete = policy[policy.index(
        "create policy ab_blobs_member_delete"):]
    assert "owner_id = auth.uid()::text" in delete
    assert "public.ab_is_member(" in delete


def test_p1b_staged_read_predicate_fails_closed_after_any_manifest():
    size = _body(
        "create or replace function private.ab_storage_object_size(",
        "revoke all on function private.ab_storage_object_size(",
    )
    assert "p_metadata->>'size' ~ '^[0-9]{1,8}$'" in size
    assert "between 1 and 52428800" in size
    helper = _body(
        "create or replace function public.ab_staged_blob_readable(",
        "revoke all on function public.ab_staged_blob_readable(",
    )
    any_manifest = helper.index("if exists (")
    exact_manifest = helper.index("return public.ab_can_read_chat", any_manifest)
    owner_fallback = helper.index("return p_owner_id = v_actor::text")
    assert any_manifest < exact_manifest < owner_fallback
    assert "m.object_id = p_object_id" in helper
    assert "m.sealed_bytes = v_sealed_bytes" in helper
    assert "private.ab_storage_object_size(p_metadata)" in helper
    assert "and public.ab_is_member(p_root, p_chat)" in helper


def test_p1b_blob_manifest_rpc_binds_exact_storage_row_and_receipt():
    body = _body(
        "create or replace function public.ab_mutation_blob_manifest(",
        "revoke all on function public.ab_mutation_blob_manifest(",
    )
    sealed = body.index("private.ab_mutation_root_sealed(p_root)")
    begin = body.index("private.ab_mutation_begin(")
    replay = body.index("if (v_begin->>'replay')::boolean")
    actor = body.index("private.ab_mutation_lock_actor(p_root)", replay)
    room = body.index("private.ab_mutation_lock_chat(p_root, p_chat, false)")
    blob_lock = body.index("'agentbridge-blob-manifest:'")
    authority = body.index("from public.ab_docs d", blob_lock)
    duplicate = body.index("typed blob manifest already exists", authority)
    storage = body.index("from storage.objects o", duplicate)
    insert = body.index("insert into public.ab_blob_manifests", storage)
    finish = body.index("private.ab_mutation_finish(", insert)
    assert sealed < begin < replay < actor < room < blob_lock
    assert blob_lock < authority < duplicate < storage < insert < finish
    assert "'blob_manifest', p_chat, p_blob, p_object_id" in body
    assert "p_sealed_bytes, p_sealed_sha256, p_manifest_digest" in body
    assert "o.name = p_root || '/blob-stage/' || p_chat || '/' || p_blob" \
        in body
    assert "o.owner_id = v_actor::text" in body
    assert "o.id = p_object_id" in body
    assert "o.archived_at is null" in body
    assert "not coalesce(o.is_delete_marker, false)" in body
    assert "v_storage_size is distinct from p_sealed_bytes" in body
    assert body.count("for share") >= 2


def test_p1b_blob_manifest_rpc_is_narrow_and_not_ready():
    section = SCHEMA[SCHEMA.index("-- P1b blob-manifest slice:") :]
    revoke = section.index(
        "revoke all on function public.ab_mutation_blob_manifest(")
    tail = section[revoke:revoke + 620]
    assert "from public, anon, authenticated, service_role" in tail
    assert "grant execute on function public.ab_mutation_blob_manifest(" in tail
    assert "to authenticated" in tail
    assert "create or replace function public.ab_mutation_protocol_ready" \
        not in section


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


def test_conditional_document_put_binds_version_and_terminalizes_conflict():
    body = _last_body(
        'create or replace function public.ab_mutation_doc_compare_put(',
        'revoke all on function public.ab_mutation_doc_compare_put(')
    assert 'p_expected_source_epoch uuid, p_expected_seq bigint, p_expected_deleted boolean' in body
    replay = body.index("if (v_begin->>'replay')::boolean")
    authority = body.index('v_member := private.ab_mutation_lock_actor')
    source_lock = body.index("'agentbridge-change-ledger:'")
    epoch = body.index('if v_source_epoch = p_expected_source_epoch then')
    insert = body.index('on conflict (root, path) do nothing')
    update = body.index('and d.seq = p_expected_seq and d.deleted = p_expected_deleted')
    finish = body.index('return private.ab_mutation_finish')
    assert replay < authority < source_lock < epoch < insert < update < finish
    assert "'doc_compare_put', p_path, p_data," in body
    assert 'p_expected_source_epoch, p_expected_seq, p_expected_deleted)' in body
    assert "pg_catalog.jsonb_build_object('path', p_path, 'applied', v_applied)" in body
    assert 'v_applied boolean := false' in body
    assert "p_path like 'chats/%/runtime/effects/%'" in body
    assert 'grant execute on function public.ab_mutation_doc_compare_put(' in SCHEMA


def test_receipt_closure_metadata_is_bounded_and_not_membership_authority():
    rotation = _body(
        'create or replace function public.ab_admin_rotate_mutation_epoch(',
        'revoke all on function public.ab_admin_rotate_mutation_epoch(',
    )
    assert rotation.index('receipt_generation = 9223372036854775807') < rotation.index('delete from public.ab_mutation_receipts')
    assert 'receipt_generation = receipt_generation + 1' in rotation
    assert 'add column if not exists receipt_lifecycle_id uuid not null' in SCHEMA
    observation = _body(
        'create or replace function public.ab_mutation_epoch_state(',
        'revoke all on function public.ab_mutation_epoch_state(',
    )
    assert "auth.role() is distinct from 'authenticated'" in observation
    assert 'auth.uid() is null' in observation
    assert 'ab_root_ok' not in observation
    assert 'not r.legacy_writes_enabled' in observation
    assert 'ab_database_mutation_seals' in observation
    assert 'ab_mutation_receipts' not in observation
    assert 'ab_docs' not in observation and 'ab_members' not in observation


def test_reaction_pair_rolls_back_both_documents_and_retains_conflict_receipt():
    body = _last_body(
        'create or replace function public.ab_mutation_reaction_pair(',
        'revoke all on function public.ab_mutation_reaction_pair(')
    replay = body.index("if (v_begin->>'replay')::boolean")
    authority = body.index('v_member := private.ab_mutation_lock_actor')
    source_lock = body.index("'agentbridge-change-ledger:'")
    epoch = body.index('if v_source_epoch = p_expected_source_epoch then')
    subtransaction = body.index('    begin', epoch)
    writes = body.index('for v_index in 1..2 loop')
    conflict = body.index("using errcode = 'P0R01'")
    rollback = body.index("exception when sqlstate 'P0R01' then")
    finish = body.index('return private.ab_mutation_finish')
    assert replay < authority < source_lock < epoch < subtransaction < writes < conflict < rollback < finish
    assert body.index("'/overlays/reactions/'") < body.index("'/overlays/state/'")
    assert 'and d.seq = v_seqs[v_index] and d.deleted = v_deleted[v_index]' in body
    assert 'on conflict (root, path) do nothing' in body
    assert "'chat_id', p_chat, 'username', p_user" in body
    assert 'public.ab_source_json_bytes(p_state_data)' in body
    assert '+ public.ab_source_json_bytes(p_reaction_data) > 8355840' in body
    assert 'p_expected_state_seq, p_expected_state_deleted,' in body
    assert 'p_expected_reaction_seq, p_expected_reaction_deleted)' in body
    grant = SCHEMA[SCHEMA.index('revoke all on function public.ab_mutation_reaction_pair('):]
    assert 'from public, anon, authenticated, service_role' in grant
    assert 'to authenticated' in grant


def test_meta_append_swaps_meta_and_appends_records_atomically():
    body = _last_body(
        'create or replace function public.ab_mutation_meta_append(',
        'revoke all on function public.ab_mutation_meta_append(')
    validate = body.index("raise exception 'invalid meta append'")
    replay = body.index("if (v_begin->>'replay')::boolean")
    actor = body.index('v_member := private.ab_mutation_lock_actor')
    chat_lock = body.index('perform private.ab_mutation_lock_chat(p_root, p_chat, true)')
    observed = body.index('select d.data, d.seq, d.deleted into v_meta, v_seq, v_deleted')
    tenure = body.index("not coalesce(v_meta->'members' ? v_member\n"
                        "                     or v_meta->'tenure' ? v_member, false)")
    source_lock = body.index("'agentbridge-change-ledger:'")
    compare = body.index('if v_source_epoch = p_expected_source_epoch and v_seq = p_expected_seq\n'
                         '     and v_deleted = p_expected_deleted\n'
                         '     and public.ab_is_member(p_root, p_chat) then')
    update = body.index('and d.seq = p_expected_seq and d.deleted = p_expected_deleted')
    applied = body.index('if v_applied then')
    append = body.index('insert into public.ab_logs(root, chat_id, log_name, line)')
    finish = body.index('return private.ab_mutation_finish')
    # Tenure earns an answer; only a current member of the exact pre-swap
    # version swaps, and every other tenure answer is the same applied=false
    # (a race lost to one's own removal settles; nothing leaks about later
    # versions); records land only with an applied swap.
    assert validate < replay < actor < chat_lock < observed < tenure < source_lock
    assert source_lock < compare < update < applied < append < finish
    assert "v_member = '' or v_meta is null" in body
    assert body.count("raise exception 'meta append is unauthorized'") == 1
    assert body.count('ab_is_member') == 1
    assert "d.path = 'chats/' || p_chat || '/meta.json'" in body
    assert 'on conflict' not in body and 'insert into public.ab_docs' not in body
    assert 'p_expected_seq < 1' in body
    assert 'p_expected_deleted is distinct from false' in body
    assert 'jsonb_array_length(p_records) not between 1 and 256' in body
    assert 'with ordinality r(value, ordinal)' in body and 'order by r.ordinal' in body
    assert 'v_bytes > 8355840' in body
    assert "'meta_append', p_chat, p_data," in body
    assert 'p_log, p_records)' in body
    assert "'chat_id', p_chat, 'log_name', p_log," in body
    permits = _last_body('create or replace function private.ab_mutation_kind_permits(',
                         'revoke all on function private.ab_mutation_kind_permits(')
    clause = permits[permits.index("when 'meta_append'"):permits.index("when 'doc_create'")]
    assert "p_table = 'ab_docs' and p_operation = 'UPDATE'" in clause
    assert "p_table = 'ab_logs' and p_operation = 'INSERT'" in clause
    assert "'DELETE'" not in clause and "ab_members" not in clause
    for kinds in (SCHEMA[SCHEMA.index('create or replace function private.ab_mutation_begin('):],
                  SCHEMA[SCHEMA.rindex('add constraint ab_mutation_receipts_operation_kind_check'):],
                  SCHEMA[SCHEMA.rindex('add constraint ab_mutation_admission_legs_operation_kind_check'):]):
        assert "'meta_append'" in kinds[:1200]
    grant = SCHEMA[SCHEMA.index('revoke all on function public.ab_mutation_meta_append('):]
    assert 'from public, anon, authenticated, service_role' in grant
    assert 'to authenticated' in grant


def test_owner_admission_tables_bound_slots_and_keep_receipt_wire_separate():
    for table in ('ab_mutation_admission_groups', 'ab_mutation_admission_legs'):
        assert f'create table if not exists public.{table}' in SCHEMA
        assert f'alter table public.{table} enable row level security' in SCHEMA
        assert f'revoke all on table public.{table}' in SCHEMA
    legs = _body('create table if not exists public.ab_mutation_admission_legs',
                 'alter table public.ab_mutation_admission_groups enable')
    assert 'receipt_slot between 0 and 4095' in legs
    assert 'ordinal between 1 and 1025' in legs
    assert 'unique (root, protocol_epoch, receipt_slot)' in legs
    assert 'primary key (root, protocol_epoch, operation_id)' in legs
    assert 'check (not cancel_acknowledged or cancelled)' in legs
    assert 'on delete cascade' in legs
    assert 'admission_required boolean not null default false' in SCHEMA


def test_admission_enable_requires_explicit_empty_sealed_epoch():
    enable = _body('create or replace function public.ab_admin_enable_mutation_admission(',
                   'revoke all on function public.ab_admin_enable_mutation_admission(')
    assert 'security invoker' in enable
    assert 'pg_advisory_xact_lock(' in enable
    assert 'ab_database_mutation_seals' in enable
    assert 'admission requires an empty receipt epoch' in enable
    assert 'ab_mutation_receipts' in enable and 'ab_mutation_admission_groups' in enable
    assert 'admission_required = true' in enable
    assert 'admission_required = false' not in SCHEMA
    assert 'grant execute on function public.ab_admin_enable_mutation_admission' not in SCHEMA


def test_admission_plan_is_bounded_exact_and_replayed_before_fresh_authority():
    admit = _body('create or replace function public.ab_admit_mutation_owner(',
                  'revoke all on function public.ab_admit_mutation_owner(')
    assert 'v_count < 1 or v_count > 1025' in admit
    assert 'octet_length(p_plan::text) > 524288' in admit
    assert "v_item - array['operation_id','kind','client_digest']" in admit
    assert "v_id::text is distinct from (v_item->>'operation_id')" in admit
    assert 'v_id = any(v_ids)' in admit
    assert admit.index("'agentbridge-mutation-epoch:'") < admit.index("'agentbridge-mutation-admission:'")
    assert admit.index('v_plan is distinct from p_plan') < admit.index('ab_mutation_lock_actor')
    assert 'v_group.actor_uid is distinct from v_actor' in admit
    assert "v_count = 1 and p_plan->0->>'kind' = 'member_claim'" in admit
    # Unaffiliated reservations are bounded before capacity is evaluated, and
    # only after exact replay, which must stay available to the same owner.
    pending = admit.index('unaffiliated mutation admission is already pending')
    budget = admit.index('unaffiliated mutation admission budget exhausted')
    assert admit.index('v_plan is distinct from p_plan') < pending < budget
    assert budget < admit.index('v_occupied + v_count > 4096')
    assert 'not (l.cancelled and l.cancel_acknowledged)' in admit
    assert "l.operation_kind = 'member_claim') >= 4" in admit
    assert admit.index("'status', 'full'") < admit.index('insert into public.ab_mutation_admission_groups')
    assert 'v_occupied + v_count > 4096' in admit
    assert 'pg_try_advisory_xact_lock' not in admit


def test_gated_begin_uses_exact_admission_slot_without_changing_terminal_replay():
    begin = _body('create or replace function private.ab_mutation_begin(',
                  'revoke all on function private.ab_mutation_begin(')
    assert begin.index("'replay', true") < begin.index('if v_root.admission_required then')
    gated = begin[begin.index('if v_root.admission_required then'):begin.index('  v_base :=')]
    for expected in ('v_leg.actor_uid is distinct from v_actor',
                     'v_leg.operation_kind is distinct from p_operation_kind',
                     'v_leg.client_digest is distinct from p_payload_digest',
                     'v_leg.cancelled', "v_leg.receipt_slot, 'applying'"):
        assert expected in gated
    # Option B: admission is optional per owner. Only the shared admission
    # mutex is taken, after the operation lock and before the leg lookup; a
    # found leg is exact and never falls back to an unadmitted slot.
    assert gated.count('pg_advisory_xact_lock') == 1
    assert 'pg_advisory_xact_lock_shared(' in gated
    assert begin.index("'agentbridge-mutation-operation:'") < begin.index(
        "'agentbridge-mutation-admission:'")
    assert gated.index('pg_advisory_xact_lock_shared(') < gated.index(
        'from public.ab_mutation_admission_legs l')
    found = gated[gated.index('if found then'):]
    assert found.index("'typed mutation requires matching uncancelled admission'") < found.index(
        "return pg_catalog.jsonb_build_object('replay', false")
    allocator = begin[begin.index('  v_base :='):]
    assert allocator.index('from public.ab_mutation_admission_legs l') < allocator.index(
        'pg_try_advisory_xact_lock')
    full = allocator[allocator.rindex('select pg_catalog.count(*) into v_count'):]
    assert 'from public.ab_mutation_admission_legs l' in full and 'union' in full


def test_admission_shares_slots_with_unadmitted_writes():
    admit = _body('create or replace function public.ab_admit_mutation_owner(',
                  'revoke all on function public.ab_admit_mutation_owner(')
    collision = admit[admit.index("-- An operation that already executed unadmitted"):]
    assert 'from public.ab_mutation_receipts r' in collision[:collision.index(
        "'mutation admission operation collision'")]
    occupancy = admit[admit.index('into v_occupied'):admit.index('v_occupied + v_count > 4096')]
    assert 'ab_mutation_admission_legs' in occupancy and 'ab_mutation_receipts' in occupancy
    assert 'union' in occupancy
    selection = admit[admit.index('into v_slots'):admit.index(
        'insert into public.ab_mutation_admission_groups')]
    assert 'ab_mutation_admission_legs' in selection and 'ab_mutation_receipts' in selection
    # Exclusive mutex only: no per-slot or per-operation locks can invert order.
    assert 'pg_try_advisory_xact_lock' not in admit
    assert "'agentbridge-mutation-operation:'" not in admit


def test_cancel_is_exact_no_effect_control_and_ack_requires_cancellation():
    cancel = _body('create or replace function private.ab_mutation_admission_cancel(',
                   'revoke all on function private.ab_mutation_admission_cancel(')
    assert cancel.index("'agentbridge-mutation-epoch:'") < cancel.index("'agentbridge-mutation-operation:'")
    assert 'g.actor_uid = v_actor' in cancel
    assert 'l.owner_id = p_owner_id' in cancel
    assert 'v_leg.operation_kind is distinct from p_kind' in cancel
    assert 'v_leg.client_digest is distinct from p_client_digest' in cancel
    assert "if not v_leg.cancelled then" in cancel
    assert "v_status := 'executed'" in cancel and "v_status := 'cancelled'" in cancel
    assert 'insert into' not in cancel and 'delete from' not in cancel
    assert 'ab_mutation_lock_actor' not in cancel


def test_rotation_requires_acknowledged_effect_or_acknowledged_no_effect_for_every_leg():
    rotate = _body('create or replace function public.ab_admin_rotate_mutation_epoch(',
                   'revoke all on function public.ab_admin_rotate_mutation_epoch(')
    assert 'l.cancelled and l.cancel_acknowledged and r.operation_id is null' in rotate
    assert "not l.cancelled and r.receipt_state='terminal' and r.acknowledged_at is not null" in rotate
    assert 'is not true' in rotate  # a missing receipt must block, not SQL NULL-skip
    assert 'r.actor_uid=g.actor_uid' in rotate and 'r.payload_digest=l.client_digest' in rotate
    assert rotate.index('mutation admission legs are not settled') < rotate.index('delete from public.ab_mutation_admission_groups')
    assert rotate.index('delete from public.ab_mutation_receipts') < rotate.index('delete from public.ab_mutation_admission_groups')
