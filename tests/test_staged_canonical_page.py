"""Canonical encrypted/plain pages consume atomically admitted staged inputs."""
from __future__ import annotations

import time

import pytest

from test_local_page_operation import world as world, _prepare, _target, COUNT
from agentbridge.mesh.page_operation import PageOperation
from agentbridge.store import local_source, staged_source, staged_publication
from agentbridge.transport.raw_documents import collect_document_batches


@pytest.mark.parametrize('extra_overlays', [0, 20_031])
def test_staged_canonical_page_cost_ignores_unrelated_overlay_history(world, extra_overlays, record_property):
    mesh, provider, root, reader, publisher, chat, encrypted = world
    directory = provider.root / 'chats' / chat / 'overlays' / 'edits'
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(extra_overlays):
        (directory / f'old-{n:05}.json').write_bytes(b'{}')
    captured = publisher.capture()
    with root.publication_gate(mesh.store, reader.definition):
        retired = local_source.retire_for_publication(mesh.store, captured.source)
    stage = staged_source.begin(mesh.store, reader.definition.source, chat, expected=retired)
    collect_document_batches(provider, reader.definition,
                             consume=lambda batch: staged_source.append(mesh.store, stage, batch))
    staged_source.finish(mesh.store, stage)
    with root.publication_gate(mesh.store, reader.definition):
        _source, index = staged_publication.admit(mesh.store, retired, stage, observed_ns=time.time_ns())
    receipt = reader.capture()
    mesh.store.refresh_terminal_observation(_target(mesh, chat))
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    wall, cpu = time.perf_counter(), time.process_time()
    prepared, history = _prepare(operation, (receipt, receipt, index), mesh)
    assert prepared.status == 'prepared', history
    finalized = prepared.prepared.finalize()
    elapsed_wall, elapsed_cpu = time.perf_counter() - wall, time.process_time() - cpu
    assert finalized.status == 'page', finalized
    page = finalized.result.page
    assert [message.body for message in page.messages] == [f'm{n:03d}' for n in range(COUNT - 10, COUNT)]
    if encrypted:
        assert all(not message.undecrypted for message in page.messages)
    record_property('extra_overlay_documents', extra_overlays)
    record_property('first_page_wall_s', elapsed_wall)
    record_property('first_page_cpu_s', elapsed_cpu)
    record_property('canonical_messages', len(page.messages))
    record_property('raw_rows_examined', page.raw_examined)
    assert page.raw_examined == 10
