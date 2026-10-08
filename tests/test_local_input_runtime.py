"""Composition and lifecycle coverage for opt-in local input ingestion."""
from __future__ import annotations

import hashlib
import threading
from dataclasses import replace

import pytest

from agentbridge.mesh import local_input_runtime
from agentbridge.store import overlay_index, staged_publication, staged_source
from agentbridge.mesh.sealer import PlainSealer
from agentbridge.mesh.service import Mesh
from agentbridge.store import local_source
from agentbridge.store.db import Store
from agentbridge.transport.local_mutations import (
    LocalMutationTransport,
    owned_transport,
)
from agentbridge.transport.raw_documents import RawCollectionUnavailable


CHAT = "room"
META = f"chats/{CHAT}/meta.json"
STATE = f"chats/{CHAT}/overlays/state/alice.json"


def _documents(value=1):
    return {
        META: {"id": CHAT, "members": ["alice"], "value": value},
        "users/alice.json": {"name": "alice", "value": value},
        "lifecycle/alice/0001.json": {
            "id": "life-1", "subject": "alice", "value": value,
        },
        STATE: {"ns": value, "hidden": [], "starred": []},
    }


@pytest.mark.parametrize(('error', 'reason'), [
    (RawCollectionUnavailable('mirror_changed'), 'mirror_changed'),
    (RawCollectionUnavailable('mirror_pending'), 'mirror_pending'),
    (RawCollectionUnavailable('unsafe_cached_value'), 'unsafe_cached_value'),
    (RawCollectionUnavailable('invalid_or_oversize_payload'), 'invalid_payload'),
    (RawCollectionUnavailable('document_budget'), 'budget_exhausted'),
    (RawCollectionUnavailable('private_detail'), 'inputs_unavailable'),
    (local_source.SourceChanged('source_mutation_pending'), 'source_mutation_pending'),
    (local_source.SourceChanged('source_changed_before_collection'), 'collection_superseded'),
    (local_source.SourceChanged('collection_superseded'), 'collection_superseded'),
    (local_source.SourceChanged('source_changed_during_comparison'), 'comparison_superseded'),
    (local_source.SourceChanged('source_changed_during_ingestion'), 'admission_superseded'),
    (local_source.SourceChanged('ingestion_superseded'), 'finalization_superseded'),
    (local_source.SourceChanged('private_detail'), 'source_changed'),
    (overlay_index.OverlayIndexUnavailable('private_detail'), 'index_pending'),
    (staged_source.StageChanged('private_detail'), 'source_changed'),
    (OverflowError('private_detail'), 'budget_exhausted'),
    (OSError('private_detail'), 'storage_error'),
    (ValueError('private_detail'), 'invalid_payload'),
    (RuntimeError('local input ingestion stopped'), 'abort'),
    (RuntimeError('private_detail'), 'other'),
])
def test_reconciliation_failure_reason_is_fixed_and_content_free(error, reason):
    assert local_input_runtime._reconciliation_failure_reason(error) == reason


@pytest.fixture
def rig(clouds, tmp_path):
    provider = clouds.cached(tmp_path / "provider")
    for path, value in _documents().items():
        provider.put_doc(path, value)
    mesh = Mesh(
        provider, "alice", "machine", home=tmp_path / "home",
        store_path=tmp_path / "store.sqlite", local_inputs=True,
    )
    try:
        yield mesh, provider
    finally:
        mesh.close()


def test_opt_in_initializes_schema_preserves_namespace_and_owns_all_services(clouds, tmp_path):
    root = clouds.root(tmp_path / "provider")
    home = tmp_path / "home"
    legacy_tag = hashlib.sha1(clouds.cached(root).cache_key.encode()).hexdigest()[:12]
    legacy_path = home / "cache" / f"alice@machine-{legacy_tag}.sqlite"
    seeded = Store(legacy_path)
    seeded.cache_doc("sentinel.json", {"legacy": True})
    seeded.close()

    plain = Mesh(clouds.cached(root), "plain", "machine", home=home)
    assert plain.local_inputs is None
    plain.close()

    provider = clouds.cached(root)
    mesh = Mesh(provider, "alice", "machine", home=home, local_inputs=True)
    try:
        assert mesh.store.path == legacy_path
        assert mesh.store.cached_doc("sentinel.json") == {"legacy": True}
        assert type(mesh.tx) is LocalMutationTransport
        assert mesh.local_inputs.transport is mesh.tx
        tables = {row[0] for row in mesh.store._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {
            "local_source_schema", "local_sources", "local_source_writes",
            "local_source_definitions", "local_source_selectors",
        } <= tables
        services = (
            mesh.directory, mesh.keys, mesh.privacy, mesh.attachments,
            mesh.messaging, mesh.membership, mesh.accounts, mesh.presence,
            mesh.sync, mesh.applink.registry, mesh.applink.control,
        )
        assert all(service.tx is mesh.tx for service in services)
    finally:
        mesh.close()


def test_borrowed_owner_is_reused_and_explicit_sealer_is_rejected(clouds, tmp_path):
    provider = clouds.cached(tmp_path / "provider")
    owner = owned_transport(provider, tmp_path / "owner-home")
    mesh = Mesh(
        owner, "alice", "machine", home=tmp_path / "mesh-home",
        store_path=tmp_path / "borrowed.sqlite",
    )
    try:
        assert mesh.tx is owner
        assert mesh.local_inputs.transport is owner
    finally:
        mesh.close()

    with pytest.raises(ValueError, match="Mesh-owned sealer"):
        Mesh(
            clouds.cached(tmp_path / "other-provider"), "alice", "machine",
            home=tmp_path / "other-home", local_inputs=True,
            sealer=PlainSealer(),
        )


def test_independent_cloud_caches_match_admission_and_stable_poll(clouds, tmp_path):
    root = tmp_path / "provider"
    provider = clouds.cached(root)
    for path, value in _documents().items():
        provider.put_doc(path, value)
    direct = Mesh(
        clouds.cached(root), "alice", "direct", home=tmp_path / "direct-home",
        store_path=tmp_path / "direct.sqlite", local_inputs=True,
    )
    cached_transport = clouds.cached(root)
    cached_transport.refresh()
    cached = Mesh(
        cached_transport, "alice", "cached", home=tmp_path / "cached-home",
        store_path=tmp_path / "cached.sqlite", local_inputs=True,
    )
    try:
        assert direct.local_inputs.ingest(CHAT) is True
        direct_reader, direct_receipt, _direct_index = direct.local_inputs.inputs(CHAT)
        direct_records = direct_reader.capture_authority(
            direct_receipt, ("alice",),
        ).documents.records

        assert cached.tx._transport is cached_transport
        assert cached.local_inputs.ingest(CHAT) is True
        cached_reader, cached_receipt, first_index = cached.local_inputs.inputs(CHAT)
        cached_records = cached_reader.capture_authority(
            cached_receipt, ("alice",),
        ).documents.records
        assert cached_records == direct_records

        assert cached.local_inputs.ingest(CHAT) is False
        _reader, stable_receipt, stable_index = cached.local_inputs.inputs(CHAT)
        assert stable_receipt.source.raw == cached_receipt.source.raw
        assert stable_index.build == first_index.build
    finally:
        cached.close()
        direct.close()


def test_ingest_inputs_health_unchanged_and_restart_reuse_persisted_index(clouds, rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT) is True
    reader, receipt, index = runtime.inputs(CHAT)
    first_health = runtime.health(CHAT)
    assert first_health["ready"] and first_health["failures"] == 0
    assert runtime.ingest(CHAT) is False
    _reader2, receipt2, index2 = runtime.inputs(CHAT)
    assert receipt2.source.raw == receipt.source.raw
    assert index2.build == index.build

    def forbidden(*_args, **_kwargs):
        raise AssertionError("foreground inputs consulted the provider")

    for name in ("get_doc", "get_docs", "list_docs", "snapshot_docs"):
        monkeypatch.setattr(provider, name, forbidden)
    _reader3, receipt3, index3 = runtime.inputs(CHAT)
    assert receipt3.source.raw == receipt.source.raw
    assert index3 == index2
    assert reader.chat == CHAT
    persisted_health = runtime.health(CHAT)

    store_path, home, root = mesh.store.path, mesh.home, provider.root
    mesh.close()
    reopened = Mesh(
        clouds.cached(root), "alice", "machine", home=home,
        store_path=store_path, local_inputs=True,
    )
    try:
        _reader4, receipt4, index4 = reopened.local_inputs.inputs(CHAT)
        assert receipt4 == receipt3
        assert index4 == index3
        assert reopened.local_inputs.health(CHAT) == persisted_health
    finally:
        reopened.close()
    # The fixture's final close remains safe and idempotent.


def test_owned_write_invalidates_before_provider_call_and_failed_write_stays_pending(
        rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    original = provider.put_doc
    observed = []

    def inspect_then_write(path, value):
        observed.append(runtime.health(CHAT))
        return original(path, value)

    monkeypatch.setattr(provider, "put_doc", inspect_then_write)
    mesh.tx.put_doc(META, _documents(2)[META])
    assert len(observed) == 1 and not observed[0]["ready"]
    assert not runtime.health(CHAT)["ready"]
    assert runtime.health(CHAT)["writes_pending"] == 0

    def fail_write(_path, _value):
        raise OSError("ambiguous provider failure")

    monkeypatch.setattr(provider, "put_doc", fail_write)
    with pytest.raises(OSError, match="ambiguous provider failure"):
        mesh.tx.put_doc(META, _documents(3)[META])
    failed = runtime.health(CHAT)
    assert not failed["ready"]
    with pytest.raises(local_source.SourceChanged, match="source_mutation_pending"):
        runtime.ingest(CHAT)
    root = runtime.coordinator._transaction()
    with root as conn:
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (1,)


def test_confirmed_local_message_reuses_ready_snapshot_before_full_reconciliation(
        rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    provider.put_doc(META, {
        'id': CHAT, 'kind': 'group',
        'members': {'alice': {'role': 'admin'}},
    })
    assert runtime.ingest(CHAT)
    before = runtime.inputs(CHAT)[1].source
    before_health = runtime.health(CHAT)
    env = mesh.post(CHAT, 'local append fast path')

    # The optimistic row alone does not bypass invalidate-before-write. The
    # definite provider return may restore only the exact ready source captured
    # before that mutation; a complete collector remains queued afterward.
    monkeypatch.setattr(runtime, 'ingest',
                        lambda _chat: pytest.fail('success path performed full ingestion'))
    assert mesh.outbox.flush_once() == 1
    after = runtime.inputs(CHAT)[1].source
    assert after.ready and after.raw == before.raw
    assert after.revision == before.revision + 3
    assert runtime.health(CHAT)['last_success_ns'] == before_health['last_success_ns']
    assert runtime.schedule._states[CHAT].due > 0
    assert provider.read_log(CHAT, 'alice@machine')[0][-1]['id'] == env.id
    stored = mesh.store._conn().execute(
        'SELECT state FROM local_send_status WHERE chat_id=? AND message_id=?',
        (CHAT, env.id),
    ).fetchone()
    assert stored == ('sent',)


def test_confirmed_local_message_does_not_admit_across_source_transition(rig):
    mesh, provider = rig
    runtime = mesh.local_inputs
    provider.put_doc(META, {
        'id': CHAT, 'kind': 'group',
        'members': {'alice': {'role': 'admin'}},
    })
    assert runtime.ingest(CHAT)
    source_id = runtime.inputs(CHAT)[1].source.source_id
    prepare = runtime.prepare_local_append
    admit = runtime.admit_local_append

    def transition_then_admit(admission, record):
        local_source.invalidate(mesh.store, source_id)
        return admit(admission, record)

    mesh.messaging.set_local_append_admitter(
        prepare, transition_then_admit, runtime.local_append_settled,
    )
    mesh.post(CHAT, 'local append loses its exact source CAS')
    assert mesh.outbox.flush_once() == 1
    assert not runtime.health(CHAT)['ready']
    assert runtime.schedule._states[CHAT].due > 0


def test_failed_provider_append_never_admits_local_snapshot(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    provider.put_doc(META, {
        'id': CHAT, 'kind': 'group',
        'members': {'alice': {'role': 'admin'}},
    })
    assert runtime.ingest(CHAT)

    def unavailable(*_args, **_kwargs):
        raise OSError('provider append unavailable')

    monkeypatch.setattr(provider, 'append_log', unavailable)
    mesh.post(CHAT, 'provider failure keeps source unavailable')
    assert mesh.outbox.flush_once() == 0
    assert not runtime.health(CHAT)['ready']
    with runtime.coordinator._transaction() as conn:
        assert conn.execute(
            'SELECT count(*) FROM mutation_intents'
        ).fetchone() == (1,)


def test_local_append_settlement_always_queues_full_reconciliation(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    queued = []

    def failed_page(_chat):
        raise RuntimeError('page preparation unavailable')

    monkeypatch.setattr(runtime, 'request_page', failed_page)
    monkeypatch.setattr(
        runtime, 'request',
        lambda chat, **kwargs: queued.append((chat, kwargs)) or True,
    )
    with pytest.raises(RuntimeError, match='page preparation unavailable'):
        runtime.local_append_settled(CHAT, {'kind': 'message'})
    assert queued == [(CHAT, {'activity': True, 'settled': True})]


def test_unsafe_collection_retires_readiness_and_persists_bounded_health(rig):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    before = runtime.health(CHAT)
    with provider._lock:
        provider._authority_unsafe.add(META)
    with runtime._lock:
        runtime._mirror_tokens.clear()

    with pytest.raises(RawCollectionUnavailable, match="unsafe_cached_value"):
        runtime.ingest(CHAT)
    failed = runtime.health(CHAT)
    assert failed == {
        "ready": False,
        "writes_pending": 0,
        "last_success_ns": before["last_success_ns"],
        "failures": 1,
        "error": "unavailable",
    }


def test_stale_failure_cannot_retire_newer_publication(rig):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    reader = runtime.reader(CHAT)
    stale = reader.capture().source
    mesh.tx.put_doc(META, _documents(2)[META])
    assert runtime.ingest(CHAT)
    winner = runtime.inputs(CHAT)[1].source

    with runtime.coordinator.publication_gate(mesh.store, reader.definition):
        recorded = local_source.record_failure(
            mesh.store, reader.definition.source,
            reason="unavailable", expected=stale,
        )
    assert recorded is False
    assert local_source.capture(mesh.store, reader.definition.source) == winner
    assert runtime.health(CHAT)["ready"]


def test_mutation_between_collection_claim_and_stage_begin_is_retryable(
        rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    provider.put_doc(META, _documents(2)[META])
    original = staged_source.begin
    events = []
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'f' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )

    def mutate_then_begin(*args, **kwargs):
        local_source.invalidate(mesh.store, runtime.reader(CHAT).definition.source)
        return original(*args, **kwargs)

    monkeypatch.setattr(staged_source, 'begin', mutate_then_begin)
    with pytest.raises(local_source.SourceChanged, match='collection_superseded'):
        runtime.ingest(CHAT)
    assert runtime.health(CHAT)['ready'] is False
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['error_type'] == 'SourceChanged'
    assert profile['reason'] == 'collection_superseded'


def test_unchanged_ingest_reuses_admitted_source_without_creating_stage(
        rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    _reader, before, before_index = runtime.inputs(CHAT)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('unchanged source created a stage')

    monkeypatch.setattr(staged_source, 'begin', forbidden)
    assert runtime.ingest(CHAT) is False
    _reader, after, after_index = runtime.inputs(CHAT)
    assert after.source.raw == before.source.raw
    assert after.source.revision == before.source.revision
    assert after_index == before_index


def test_mirror_movement_during_unchanged_comparison_cannot_admit(
        rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    with runtime._lock:
        runtime._mirror_tokens.clear()
    original = local_input_runtime.document_observation._serialize_live_documents
    crossed = False

    def move_mirror(documents, **limits):
        nonlocal crossed
        if not crossed:
            crossed = True
            provider.put_doc(META, _documents(2)[META])
        return original(documents, **limits)

    monkeypatch.setattr(
        local_input_runtime.document_observation,
        '_serialize_live_documents', move_mirror,
    )
    with pytest.raises(RawCollectionUnavailable, match='mirror_changed'):
        runtime.ingest(CHAT)
    assert crossed
    assert runtime.health(CHAT)['ready'] is False


def test_ingestion_emits_one_compact_reconciliation_profile(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    provider.put_doc('outside/value.json', {'ignored': True})
    events = []
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )

    assert runtime.ingest(CHAT)

    profiles = [fields for phase, fields in events
                if phase == 'source_reconciliation']
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile['status'] == 'ok' and profile['duration_ms'] == 2.0
    assert profile['documents_examined'] == len(provider._docs)
    assert profile['documents_selected'] >= 1
    assert profile['document_batches'] >= 1
    assert profile['collect_ms'] == 2.0
    assert profile['stage_write_ms'] >= 2.0
    assert profile['compare_ms'] == profile['admit_ms'] == 2.0


def test_unchanged_profile_has_comparison_without_candidate_stage(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    with runtime._lock:
        runtime._mirror_tokens.clear()  # model restart / unavailable journal
    events = []
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'c' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )

    assert runtime.ingest(CHAT) is False
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['collect_ms'] == profile['compare_ms'] == 2.0
    assert profile['documents_selected'] == len(_documents())
    assert not {'stage_open_ms', 'stage_write_ms', 'seal_ms', 'cleanup_ms'} & profile.keys()


def test_journal_unchanged_updates_health_without_scan_or_generation(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    before = local_source.capture(mesh.store, runtime.reader(CHAT).definition.source)
    events = []
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'f' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )
    monkeypatch.setattr(
        local_input_runtime, 'collect_document_batches',
        lambda *_args, **_kwargs: pytest.fail('unchanged journal path scanned mirror'),
    )

    assert runtime.ingest(CHAT) is False
    after = local_source.capture(mesh.store, runtime.reader(CHAT).definition.source)
    assert after == before and after.raw == before.raw
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['change_check_ms'] == 2.0
    assert 'collect_ms' not in profile and 'stage_open_ms' not in profile


def test_journal_known_change_skips_admitted_precomparison(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    provider.put_doc(STATE, {"ns": 2, "hidden": [], "starred": []})
    monkeypatch.setattr(
        staged_publication, 'identical_admitted',
        lambda *_args, **_kwargs: pytest.fail('known change repeated comparison'),
    )
    assert runtime.ingest(CHAT) is True
    assert runtime.inputs(CHAT)[1].source.ready


def test_journal_unrelated_change_avoids_global_mirror_scan(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    before = runtime.inputs(CHAT)[1].source
    provider.put_doc('outside/value.json', {'ignored': True})
    monkeypatch.setattr(
        local_input_runtime, 'collect_document_batches',
        lambda *_args, **_kwargs: pytest.fail('unrelated change scanned mirror'),
    )
    assert runtime.ingest(CHAT) is False
    assert runtime.inputs(CHAT)[1].source == before


def test_mirror_change_after_journal_query_is_ingested_on_next_attempt(
        rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    original = staged_publication.observe_unchanged
    crossed = []

    def change_before_health_cas(*args, **kwargs):
        provider.put_doc(STATE, {"ns": 2, "hidden": [], "starred": []})
        crossed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        staged_publication, 'observe_unchanged', change_before_health_cas,
    )
    assert runtime.ingest(CHAT) is False
    assert crossed == [True]

    monkeypatch.setattr(staged_publication, 'observe_unchanged', original)
    assert runtime.ingest(CHAT) is True
    reader, receipt, _index = runtime.inputs(CHAT)
    assert reader.capture_viewer_state(receipt, 'alice').decoded() == {
        "ns": 2, "hidden": [], "starred": [],
    }


def test_mirror_tokens_are_lru_bounded(rig):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    token = next(iter(runtime._mirror_tokens.values()))
    first = None
    for number in range(local_input_runtime._MAX_MIRROR_TOKENS + 1):
        source = replace(token.source, logical_source=f"source-{number}")
        runtime._retain_mirror_token(source, token.mirror)
        first = first or source.source_id
    assert len(runtime._mirror_tokens) == local_input_runtime._MAX_MIRROR_TOKENS
    assert first not in runtime._mirror_tokens
    assert f"source-{local_input_runtime._MAX_MIRROR_TOKENS}" in runtime._mirror_tokens


def test_changed_profile_counts_comparison_and_fallback_collections(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    with runtime._lock:
        runtime._mirror_tokens.clear()  # exercise PR54 compare-then-fallback
    provider.put_doc(STATE, {"ns": 2, "hidden": [], "starred": []})
    events = []
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'd' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )

    assert runtime.ingest(CHAT) is True
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['documents_examined'] == 2 * len(provider._docs)
    assert profile['documents_selected'] == 2 * len(_documents())
    # The comparison's mismatching batch raises before its callback completes;
    # the staged fallback contributes the one completed batch.
    assert profile['document_batches'] == 1
    assert profile['collect_ms'] == 4.0
    assert profile['compare_ms'] == 4.0


def test_failed_comparison_keeps_partial_collection_counters(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    assert runtime.ingest(CHAT)
    with runtime._lock:
        runtime._mirror_tokens.clear()
    events = []
    original = local_input_runtime.collect_document_batches
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'e' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )

    def interrupted(transport, definition, *, consume, **limits):
        def fail_after_batch(batch):
            consume(batch)
            raise RawCollectionUnavailable('interrupted_profile')
        return original(
            transport, definition, consume=fail_after_batch,
            batch_documents=1, **limits,
        )

    monkeypatch.setattr(local_input_runtime, 'collect_document_batches', interrupted)
    with pytest.raises(RawCollectionUnavailable, match='interrupted_profile'):
        runtime.ingest(CHAT)
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['status'] == 'error'
    assert profile['reason'] == 'inputs_unavailable'
    assert profile['compare_ms'] == profile['collect_ms'] == 2.0
    assert profile['documents_examined'] == 1
    assert profile['documents_selected'] == 1
    assert profile['document_batches'] == 0


def test_reconciliation_profile_survives_cleanup_failure(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    events = []
    original_abort = staged_source.abort
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'a' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )

    def abort_then_fail(*args, **kwargs):
        original_abort(*args, **kwargs)
        raise OSError('private cleanup failure')

    monkeypatch.setattr(staged_source, 'abort', abort_then_fail)
    with pytest.raises(OSError, match='private cleanup failure'):
        runtime.ingest(CHAT)
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['status'] == 'error'
    assert profile['error_type'] == 'OSError'
    assert profile['reason'] == 'storage_error'
    assert profile['sample_ref'] == 'a' * 16
    assert profile['cleanup_ms'] == 2.0


def test_reconciliation_profile_omits_cleanup_before_stage_exists(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    events = []
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'queue_clock', lambda: 1.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'elapsed_ms', lambda _start: 2.0)
    monkeypatch.setattr(local_input_runtime.delivery_trace, 'sampling_reference',
                        lambda: 'b' * 16)
    monkeypatch.setattr(
        local_input_runtime.delivery_trace, 'emit',
        lambda phase, **fields: events.append((phase, fields)),
    )
    monkeypatch.setattr(
        local_input_runtime.local_source, 'claim_collection',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            local_source.SourceChanged('source_changed_before_collection')),
    )

    with pytest.raises(local_source.SourceChanged):
        runtime.ingest(CHAT)
    profile = next(fields for phase, fields in events
                   if phase == 'source_reconciliation')
    assert profile['status'] == 'error'
    assert profile['sample_ref'] == 'b' * 16
    assert 'cleanup_ms' not in profile


def test_request_run_due_failure_finishes_scheduler_lease(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    clock = iter((10.0, 11.0, 12.0))
    monkeypatch.setattr(local_input_runtime.time, "monotonic", lambda: next(clock, 12.0))
    assert runtime.request(CHAT, selected=True, activity=True)

    def fail(_chat):
        raise RuntimeError("ingestion failed")

    monkeypatch.setattr(runtime, "ingest", fail)
    assert runtime.run_due() is True
    state = runtime.schedule._states[CHAT]
    assert runtime.schedule._running is None
    assert state.failures == 1


def test_inputs_checks_index_inside_atomic_capture(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    original = overlay_index._ready

    def index_rebuilt(conn, database_path, expected):
        assert conn.in_transaction
        conn.execute('UPDATE overlay_index_ready SET generation=generation+1 WHERE source=?',
                     (expected.source.source_id,))
        return original(conn, database_path, expected)

    monkeypatch.setattr(overlay_index, "_ready", index_rebuilt)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="changed"):
        runtime.inputs(CHAT)


def test_mesh_start_stop_and_watcher_fallback_are_bounded(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    started = threading.Event()

    def unavailable_watch():
        started.set()
        raise OSError("watch unavailable")

    monkeypatch.setattr(mesh.tx, "watch", unavailable_watch)
    runtime.start()
    assert started.wait(1)
    runtime.start()
    runtime.stop()
    assert runtime._thread is not None and not runtime._thread.is_alive()


def test_watcher_is_closed_during_shutdown(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs

    class Watcher:
        def __init__(self):
            self.closed = threading.Event()

        def wait(self, timeout):
            runtime._stop.wait(timeout)
            return False

        def close(self):
            self.closed.set()

    watcher = Watcher()
    monkeypatch.setattr(mesh.tx, "watch", lambda: watcher)
    runtime.start()
    runtime.stop()
    assert watcher.closed.wait(1)


def test_stop_quiesces_manual_ingest_and_permanently_closes_runtime(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    failures = []
    original = local_input_runtime.collect_document_batches

    def blocked_collect(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(local_input_runtime, "collect_document_batches", blocked_collect)
    def interrupted_ingest():
        try:
            runtime.ingest(CHAT)
        except RuntimeError as exc:
            failures.append(exc)

    ingest = threading.Thread(target=interrupted_ingest)
    ingest.start()
    assert entered.wait(1)
    shutdown = threading.Thread(target=lambda: (runtime.stop(), stopped.set()))
    shutdown.start()
    assert not stopped.wait(0.05)
    release.set()
    ingest.join(2)
    shutdown.join(2)
    assert stopped.is_set()
    assert len(failures) == 1 and str(failures[0]) == 'local input ingestion stopped'
    assert runtime.health(CHAT)['ready'] is False
    assert runtime.request(CHAT, selected=True, activity=True) is False
    with pytest.raises(RuntimeError, match="closed"):
        runtime.start()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.ingest(CHAT)


def test_mesh_close_timeout_leaves_store_open(rig):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    real_lock = runtime._worker_lock

    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == 5
            return False

        def release(self):
            raise AssertionError("unacquired lock released")

    runtime._worker_lock = BusyLock()
    with pytest.raises(RuntimeError, match="Store must remain open"):
        mesh.close()
    assert mesh.store._conn().execute("SELECT 1").fetchone() == (1,)
    runtime._worker_lock = real_lock


@pytest.mark.parametrize('outcomes, expected_calls, expected_result', [
    ([True] * 10, 4, True), ([False], 1, False), ([True, False], 2, True),
])
def test_preparation_burst_caps_work_and_keeps_partial_progress(
        rig, monkeypatch, outcomes, expected_calls, expected_result):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    calls = []
    values = iter(outcomes)
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: 10.0)
    def prepare():
        calls.append(True)
        return next(values)
    monkeypatch.setattr(runtime, 'prepare_one', prepare)
    assert runtime._prepare_burst() is expected_result
    assert len(calls) == expected_calls


@pytest.mark.parametrize('elapsed', [0.020, 0.100])
def test_preparation_burst_yields_after_time_budget_even_with_more_jobs(rig, monkeypatch, elapsed):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    now, calls = [10.0], []
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: now[0])
    def slow_quantum():
        calls.append(True)
        now[0] += elapsed
        return True
    monkeypatch.setattr(runtime, 'prepare_one', slow_quantum)
    assert runtime._prepare_burst() is True
    assert calls == [True]  # One quantum may exceed the cooperative budget.


def test_preparation_burst_stops_between_quanta(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    calls = []
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: 10.0)
    def stop_after_one():
        calls.append(True)
        runtime._stop.set()
        return True
    monkeypatch.setattr(runtime, 'prepare_one', stop_after_one)
    assert runtime._prepare_burst() is True
    assert calls == [True]
    assert runtime._prepare_burst() is False
    assert calls == [True]


def test_worker_preparation_backlog_progress_keeps_every_collection_turn(rig, monkeypatch):
    from types import SimpleNamespace
    mesh, _provider = rig
    runtime = mesh.local_inputs
    queue = iter(range(12))
    events, rounds = [], []
    monkeypatch.setattr(runtime.transport, 'watch', lambda: None)
    monkeypatch.setattr(runtime, 'discover', lambda: None)
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: 10.0)
    def prepare():
        events.append(('prepare', next(queue)))
        return True
    def collect():
        events.append(('ingest', len(rounds)))
        rounds.append(True)
        if len(rounds) == 2:
            runtime._stop.set()
        return True
    monkeypatch.setattr(runtime, 'prepare_one', prepare)
    monkeypatch.setattr(runtime, 'run_due', collect)
    monkeypatch.setattr(runtime, 'presence', SimpleNamespace(run_due=lambda: events.append(('presence',)) or False))
    monkeypatch.setattr(runtime, 'auxiliary', SimpleNamespace(run_due=lambda: events.append(('aux',)) or False))
    runtime._run()
    assert events == ([('prepare', i) for i in range(4)] + [('ingest', 0), ('presence',), ('aux',)]
                      + [('prepare', i) for i in range(4, 8)] + [('ingest', 1), ('presence',), ('aux',)])
    # Restore real owners before fixture shutdown.
    monkeypatch.undo()


@pytest.mark.parametrize('error,blocked', [
    (local_source.SourceChanged('source_mutation_pending'), True),
    (local_source.SourceChanged('source_mutation_pending', 'extra'), False),
    (local_source.SourceChanged('source_changed'), False),
    (local_source.SourceChanged('source_mutation_pending_extra'), False),
    (RuntimeError('source_mutation_pending'), False),
    (OSError('provider unavailable'), False),
    (RawCollectionUnavailable('mirror_pending'), False),
])
def test_run_due_only_exact_pending_intent_uses_owned_retry(rig, monkeypatch, error, blocked):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    now = [10.0]
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: now[0])
    runtime.request(CHAT, selected=True, activity=True)
    now[0] = 11.0
    def fail(_chat):
        raise error
    monkeypatch.setattr(runtime, 'ingest', fail)
    assert runtime.run_due() is True
    state = runtime.schedule._states[CHAT]
    assert state.failures == (0 if blocked else 1)
    assert state.due - now[0] == pytest.approx(0.35 if blocked else 4.0)


def test_persistent_mutation_pending_never_collects_or_publishes(rig, monkeypatch):
    mesh, provider = rig
    runtime = mesh.local_inputs
    runtime.ingest(CHAT)
    def fail_write(*_args):
        raise OSError('ambiguous write')
    monkeypatch.setattr(provider, 'put_doc', fail_write)
    with pytest.raises(OSError):
        mesh.tx.put_doc(META, _documents(2)[META])
    monkeypatch.setattr(local_input_runtime, 'collect_document_batches',
                        lambda *_a, **_kw: pytest.fail('pending intent read provider'))
    now = [10.0]
    monkeypatch.setattr(local_input_runtime.time, 'monotonic', lambda: now[0])
    runtime.request(CHAT, selected=True, activity=True)
    attempts = 0
    for tick in range(1, 201):
        now[0] = 10.0 + tick / 100
        runtime.request(CHAT, selected=True, activity=True)
        attempts += runtime.run_due()
        assert not runtime.health(CHAT)['ready']
    assert 5 <= attempts <= 6
    assert runtime.schedule._states[CHAT].failures == 0
    with runtime.coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone() == (1,)


def test_route_selection_controls_preparation_priority_without_queuing_or_io(rig, monkeypatch):
    mesh, _provider = rig
    runtime = mesh.local_inputs
    prep = runtime._page_preparation
    assert runtime.request('room', selected=True)
    assert prep._selected == 'room'
    assert not prep._terminals and not prep._proofs
    assert runtime.request('background')
    assert prep._selected == 'room'
    assert runtime.request('next-room', selected=True)
    assert prep._selected == 'next-room'
    monkeypatch.setattr(runtime.schedule, 'request', lambda *_a, **_kw: False)
    assert not runtime.request('denied', selected=True)
    assert prep._selected == 'next-room'
    runtime.clear_selection()
    assert prep._selected is None
    assert not prep._terminals and not prep._proofs
