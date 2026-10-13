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
