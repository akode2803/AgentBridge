"""Regression fences for staged admission, exact reuse, and reclamation."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.store import local_source, source_selectors, staged_publication, staged_source
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


CHAT = 'room'
LOGICAL = 'local-source'
META = 'chats/room/meta.json'


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'store.sqlite')
    staged_source.initialize(value)
    yield value
    value.close()


def _candidate(store, expected, *, value=1):
    handle = staged_source.begin(store, expected.source_id, CHAT, expected=expected)
    staged_source.append(store, handle, {META: {'value': value}})
    staged_source.finish(store, handle)
    return handle


def _admit_first(store):
    expected = local_source.retire_for_publication(store, local_source.capture(store, LOGICAL))
    handle = _candidate(store, expected)
    admitted, index = staged_publication.admit(store, expected, handle, observed_ns=10)
    assert admitted.ready
    return admitted, index, handle


def test_missing_mapping_schema_never_reclaims_admitted_physical_rows(store):
    ready, _index, handle = _admit_first(store)
    conn = store._conn()
    # Model a damaged database. Reclamation must fail closed even if the stage
    # was previously marked abandoned by a separate interrupted cleanup.
    conn.execute("UPDATE staged_sources SET phase='abandoned' WHERE source=?", (handle.source_id,))
    conn.execute('DROP TABLE local_input_generations')
    conn.commit()
    with pytest.raises(staged_source.StageChanged, match='admission_mapping_unavailable'):
        staged_source.cleanup(store, max_rows=1)
    with pytest.raises(staged_source.StageChanged, match='admission_mapping_unavailable'):
        staged_source.retire_generation(store, ready.raw.source_id)
    assert conn.execute('SELECT phase FROM staged_sources WHERE source=?', (handle.source_id,)).fetchone() == ('abandoned',)
    assert conn.execute('SELECT count(*) FROM document_observation_records WHERE source_id=?',
                        (handle.source_id,)).fetchone() == (1,)


def test_old_index_reuse_requires_matching_exact_comparison(store):
    ready, old_index, _ = _admit_first(store)
    retired = local_source.retire_for_publication(store, ready)
    same = _candidate(store, retired)
    proof = staged_publication.identical(store, retired, same)
    assert proof and proof.expected == retired
    with pytest.raises(local_source.SourceChanged, match='unproven_source_equality'):
        staged_publication.admit(store, retired, same, observed_ns=11, reuse=old_index)
    with pytest.raises(local_source.SourceChanged, match='unproven_source_equality'):
        staged_publication.admit(
            store, retired, same, observed_ns=11, reuse=old_index,
            comparison=replace(proof, seal=object()),
        )
    different = _candidate(store, retired, value=2)
    with pytest.raises(local_source.SourceChanged, match='unproven_source_equality'):
        staged_publication.admit(store, retired, different, observed_ns=11,
                                 reuse=old_index, comparison=proof)
    assert local_source.capture(store, LOGICAL) == retired
    stable, index = staged_publication.admit(store, retired, same, observed_ns=11,
                                               reuse=old_index, comparison=proof)
    assert stable.raw == ready.raw and index == old_index and stable.ready


def test_comparison_is_stale_after_logical_revision_changes(store):
    ready, old_index, _ = _admit_first(store)
    retired = local_source.retire_for_publication(store, ready)
    candidate = _candidate(store, retired)
    proof = staged_publication.identical(store, retired, candidate)
    local_source.invalidate(store, LOGICAL)
    with pytest.raises(local_source.SourceChanged, match='source_changed_during_ingestion'):
        staged_publication.admit(store, retired, candidate, observed_ns=11,
                                 reuse=old_index, comparison=proof)
    assert not local_source.capture(store, LOGICAL).ready


def test_legacy_publishers_reject_mapped_stage_without_retiring_it(store, tmp_path):
    source_selectors.initialize(store)
    coordinator = MutationCoordinator(tmp_path / 'coordinator', 'root')
    coordinator.register_store(store)
    definition = source_selectors.definition(
        coordinator.identity, (source_selectors.Selector('doc_exact', META),),
    )
    publisher = SourcePublisher(coordinator, store, definition)
    captured = publisher.capture()
    retired = local_source.retire_for_publication(store, captured.source)
    stage = _candidate(store, retired)
    ready, _index = staged_publication.admit(store, retired, stage, observed_ns=10)
    before = publisher.capture()
    assert before.source == ready
    with pytest.raises(local_source.SourceChanged, match='staged_source_requires_staged_publication'):
        publisher.publish(before, {META: {'value': 2}}, observed_ns=11)
    with pytest.raises(local_source.SourceChanged, match='staged_source_requires_staged_publication'):
        local_source.publish(store, before.source, {META: {'value': 2}}, observed_ns=11)
    assert local_source.capture(store, definition.source) == ready
    assert store.capture_document_position(ready.raw.source_id) == ready.raw
