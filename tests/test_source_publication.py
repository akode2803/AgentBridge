"""Three-phase local source publication and admission fencing."""
from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


S = source_selectors.Selector


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    coordinator = MutationCoordinator(tmp_path / "home", "mesh-root")
    coordinator.register_store(store)
    definition = source_selectors.definition(
        coordinator.identity,
        (
            S("doc_exact", "accounts/alice.json"),
            S("doc_prefix", "chats/room/state"),
        ),
        build="publication-test",
    )
    publisher = SourcePublisher(coordinator, store, definition)
    try:
        yield store, coordinator, definition, publisher
    finally:
        store.close()


def _documents(value=1):
    return {
        "accounts/alice.json": {"value": value},
        "chats/room/state/alice.json": {"read": value},
    }


def test_complete_publication_and_explicit_absence(rig):
    store, _coordinator, definition, publisher = rig
    captured = publisher.capture()
    ready = publisher.publish(captured, _documents(), observed_ns=10)
    assert ready.ready
    assert store.capture_document_observation(definition.source).documents() == _documents()

    absence = publisher.capture()
    empty = publisher.publish(absence, {}, observed_ns=11)
    assert empty.ready and empty.raw.generation == ready.raw.generation + 1
    assert store.capture_document_observation(definition.source).documents() == {}


@pytest.mark.parametrize("failure", ["scope", "rows", "bytes"])
def test_invalid_or_oversized_collection_retires_old_ready_position(rig, failure):
    store, _coordinator, definition, publisher = rig
    ready = publisher.publish(publisher.capture(), _documents(), observed_ns=10)
    captured = publisher.capture()
    if failure == "scope":
        documents = {"accounts/bob.json": {"value": 2}}
        kwargs = {}
        error = ValueError
    elif failure == "rows":
        documents = _documents(2)
        kwargs = {"max_documents": 1}
        error = OverflowError
    else:
        documents = _documents(2)
        kwargs = {"max_bytes": 1}
        error = OverflowError

    with pytest.raises(error):
        publisher.publish(captured, documents, observed_ns=11, **kwargs)
    pending = local_source.capture(store, definition.source)
    assert not pending.ready and pending.revision == ready.revision + 1


def test_stale_scan_after_definite_local_mutation_cannot_retire_newer_state(rig):
    store, coordinator, definition, publisher = rig
    ready = publisher.publish(publisher.capture(), _documents(), observed_ns=10)
    stale = publisher.capture()
    intent = coordinator.begin((S("doc_exact", "accounts/alice.json"),))
    coordinator.complete(intent)
    changed = local_source.capture(store, definition.source)
    assert not changed.ready and changed.revision == ready.revision + 2

    with pytest.raises(local_source.SourceChanged,
                       match="source_changed_before_publication"):
        publisher.publish(stale, _documents(2), observed_ns=11)
    assert local_source.capture(store, definition.source) == changed


def test_pending_mutation_blocks_capture_and_foreign_epoch_rejects_publish(rig):
    store, coordinator, definition, publisher = rig
    captured = publisher.capture()
    intent = coordinator.begin((S("doc_exact", "accounts/alice.json"),))
    with pytest.raises(local_source.SourceChanged,
                       match="source_mutation_pending"):
        publisher.capture()
    coordinator.complete(intent)

    forged = replace(captured, coordinator_epoch="0" * 32)
    before = local_source.capture(store, definition.source)
    with pytest.raises(ValueError, match="foreign collection position"):
        publisher.publish(forged, _documents(), observed_ns=12)
    assert local_source.capture(store, definition.source) == before


def test_mutation_after_raw_commit_cannot_admit_even_after_completion(
        rig, monkeypatch):
    store, coordinator, definition, publisher = rig
    captured = publisher.capture()
    original = store.publish_document_batch

    def publish_then_mutate(*args, **kwargs):
        published = original(*args, **kwargs)
        intent = coordinator.begin((S("doc_exact", "accounts/alice.json"),))
        coordinator.complete(intent)
        return published

    monkeypatch.setattr(store, "publish_document_batch", publish_then_mutate)
    with pytest.raises(local_source.SourceChanged,
                       match="source_changed_during_ingestion"):
        publisher.publish(captured, _documents(), observed_ns=10)
    position = local_source.capture(store, definition.source)
    assert position.raw.initialized and not position.ready


def test_two_publishers_from_same_scan_only_winner_is_admitted(rig):
    store, _coordinator, definition, publisher = rig
    first = publisher.capture()
    second = publisher.capture()
    winner = publisher.publish(first, _documents(1), observed_ns=10)
    assert winner.ready

    with pytest.raises(local_source.SourceChanged,
                       match="source_changed_before_publication"):
        publisher.publish(second, _documents(2), observed_ns=11)
    assert local_source.capture(store, definition.source) == winner
    assert store.capture_document_observation(definition.source).document(
        "accounts/alice.json",
    ) == {"value": 1}


@pytest.mark.parametrize("failure", ["before_raw", "after_raw"])
def test_publication_exception_before_or_after_raw_commit_stays_unavailable(
        rig, monkeypatch, failure):
    store, _coordinator, definition, publisher = rig
    ready = publisher.publish(publisher.capture(), _documents(), observed_ns=10)
    captured = publisher.capture()
    if failure == "before_raw":
        def fail_raw(*_args, **_kwargs):
            raise RuntimeError("raw publication failed")

        monkeypatch.setattr(store, "publish_document_batch", fail_raw)
    else:
        def fail_admission(*_args, **_kwargs):
            raise RuntimeError("admission failed")

        monkeypatch.setattr(local_source, "admit", fail_admission)

    with pytest.raises(RuntimeError):
        publisher.publish(captured, _documents(2), observed_ns=11)
    pending = local_source.capture(store, definition.source)
    assert not pending.ready and pending.revision == ready.revision + 1
    if failure == "before_raw":
        assert pending.raw == ready.raw
    else:
        assert pending.raw.generation == ready.raw.generation + 1


def test_mutation_begin_runs_while_raw_publication_is_held_outside_root_gate(
        rig, monkeypatch):
    store, coordinator, definition, publisher = rig
    captured = publisher.capture()
    entered = threading.Event()
    release = threading.Event()
    original = store.publish_document_batch
    outcome = []

    def held_raw(*args, **kwargs):
        entered.set()
        assert release.wait(10), "raw publication barrier timed out"
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "publish_document_batch", held_raw)

    def publication():
        try:
            outcome.append(publisher.publish(captured, _documents(), observed_ns=10))
        except BaseException as exc:  # asserted below
            outcome.append(exc)

    thread = threading.Thread(target=publication)
    thread.start()
    try:
        assert entered.wait(10), "publisher never left the retirement gate"
        # This returns while raw publication/serialization is held. If the
        # publisher retained the root gate, begin would block or time out.
        intent = coordinator.begin((S("doc_exact", "accounts/alice.json"),))
        coordinator.complete(intent)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], local_source.SourceChanged)
    assert not local_source.capture(store, definition.source).ready
