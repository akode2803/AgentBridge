"""Streaming raw collection admits only completed, bounded enumerations."""
from __future__ import annotations

import pytest

from agentbridge.store import source_selectors
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.raw_documents import RawCollectionUnavailable, collect_document_batches


def _cache(clouds, tmp_path):
    clouds.seed_documents(tmp_path / 'provider', {
        f'items/{i:05}.json': {'n': i} for i in range(20_111)
    })
    return clouds.cached(tmp_path / 'provider')


def _definition(transport, *selectors):
    return source_selectors.definition(root_identity(transport), tuple(selectors), build='stream-test')


def test_cache_stream_exceeds_legacy_limit_without_retaining_all_refs(clouds, tmp_path):
    cache = _cache(clouds, tmp_path)
    definition = _definition(cache, source_selectors.Selector('doc_prefix', 'items'))
    observed = []

    def consume(batch):
        assert len(batch) <= 113
        observed.extend(batch)

    assert collect_document_batches(cache, definition, consume=consume,
                                    batch_documents=113) is None
    assert len(observed) == 20_111
    assert len(set(observed)) == len(observed)


def test_batch_stats_distinguish_examined_mirror_from_selected_scope(clouds, tmp_path):
    cache = _cache(clouds, tmp_path)
    cache.put_doc('outside/value.json', {'ignored': True})
    definition = _definition(
        cache, source_selectors.Selector('doc_exact', 'items/00000.json'),
    )
    stats = {}
    batches = []

    collect_document_batches(cache, definition, consume=batches.append,
                             batch_documents=37, stats=stats)

    assert batches == [{'items/00000.json': {'n': 0}}]
    assert stats['documents_examined'] == 20_112
    assert stats['documents_selected'] == 1
    assert stats['document_batches'] == 1
    assert stats['document_bytes'] > 0


def test_cache_movement_after_callback_fails_without_completion(clouds, tmp_path):
    cache = _cache(clouds, tmp_path)
    definition = _definition(cache, source_selectors.Selector('doc_prefix', 'items'))
    called = 0

    def consume(batch):
        nonlocal called
        called += 1
        with cache._lock:
            cache._mirror_revision += 1

    with pytest.raises(RawCollectionUnavailable, match='mirror_changed'):
        collect_document_batches(cache, definition, consume=consume, batch_documents=31)
    assert called == 1


def test_cloud_document_limit_and_overlap_without_global_seen(clouds, tmp_path):
    provider = clouds.cached(tmp_path / 'provider')
    provider.put_doc('items/a.json', {'body': 'x' * 300})
    provider.put_doc('items/b.json', {'body': 'ok'})
    definition = _definition(provider, source_selectors.Selector('doc_exact', 'items/a.json'),
                             source_selectors.Selector('doc_prefix', 'items'))
    batches = []
    with pytest.raises(RawCollectionUnavailable, match='invalid_or_oversize_payload'):
        collect_document_batches(provider, definition, consume=batches.append,
                                 max_document_bytes=128)
    batches.clear()
    collect_document_batches(provider, definition, consume=batches.append,
                             batch_documents=1)
    assert {name for batch in batches for name in batch} == {'items/a.json', 'items/b.json'}
    assert len(batches) == 2


def test_document_count_failure_after_provisional_batches(clouds, tmp_path):
    cache = _cache(clouds, tmp_path)
    definition = _definition(cache, source_selectors.Selector('doc_prefix', 'items'))
    batches = []
    with pytest.raises(RawCollectionUnavailable, match='document_budget'):
        collect_document_batches(cache, definition, consume=batches.append,
                                 max_documents=137, batch_documents=100)
    assert batches and sum(map(len, batches)) <= 137
