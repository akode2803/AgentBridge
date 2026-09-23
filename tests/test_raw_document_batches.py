"""Streaming raw collection admits only completed, bounded enumerations."""
from __future__ import annotations

import pytest

from agentbridge.store import source_selectors
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.raw_documents import RawCollectionUnavailable, collect_document_batches
from agentbridge.transport.supabase import SupabaseTransport


def _cache():
    provider = SupabaseTransport('mesh', env={'SUPABASE_URL': 'https://batch-test.invalid'},
                                 client=object())
    cache = CachingTransport(provider, auto_refresh=False)
    with cache._lock:
        cache._docs = {f'items/{i:05}.json': {'n': i} for i in range(20_111)}
        cache._warm = True
        cache._mirror_revision = 1
        cache._mirror_provenance = 'provider_observed'
    return cache


def _definition(transport, *selectors):
    return source_selectors.definition(root_identity(transport), tuple(selectors), build='stream-test')


def test_cache_stream_exceeds_legacy_limit_without_retaining_all_refs():
    cache = _cache()
    definition = _definition(cache, source_selectors.Selector('doc_prefix', 'items'))
    observed = []

    def consume(batch):
        assert len(batch) <= 113
        observed.extend(batch)

    assert collect_document_batches(cache, definition, consume=consume,
                                    batch_documents=113) is None
    assert len(observed) == 20_111
    assert len(set(observed)) == len(observed)


def test_cache_movement_after_callback_fails_without_completion():
    cache = _cache()
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


def test_folder_document_limit_and_overlap_without_global_seen(tmp_path):
    folder = FolderTransport(tmp_path / 'provider')
    folder.put_doc('items/a.json', {'body': 'x' * 300})
    folder.put_doc('items/b.json', {'body': 'ok'})
    definition = _definition(folder, source_selectors.Selector('doc_exact', 'items/a.json'),
                             source_selectors.Selector('doc_prefix', 'items'))
    batches = []
    with pytest.raises(RawCollectionUnavailable, match='document_byte_budget'):
        collect_document_batches(folder, definition, consume=batches.append,
                                 max_document_bytes=128)
    batches.clear()
    collect_document_batches(folder, definition, consume=batches.append,
                             batch_documents=1)
    assert {name for batch in batches for name in batch} == {'items/a.json', 'items/b.json'}
    assert len(batches) == 2


def test_document_count_failure_after_provisional_batches():
    cache = _cache()
    definition = _definition(cache, source_selectors.Selector('doc_prefix', 'items'))
    batches = []
    with pytest.raises(RawCollectionUnavailable, match='document_budget'):
        collect_document_batches(cache, definition, consume=batches.append,
                                 max_documents=137, batch_documents=100)
    assert batches and sum(map(len, batches)) <= 137
