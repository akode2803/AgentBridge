"""Exact unchanged raw publication and composed source-admission contracts."""
from __future__ import annotations

import pytest

from agentbridge.store import document_observation, local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


SOURCE = "unchanged-source"
S = source_selectors.Selector


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    local_source.initialize(opened)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def publisher_rig(tmp_path):
    opened = Store(tmp_path / "published.sqlite")
    local_source.initialize(opened)
    source_selectors.initialize(opened)
    coordinator = MutationCoordinator(tmp_path / "home", "unchanged-root")
    coordinator.register_store(opened)
    definition = source_selectors.definition(
        coordinator.identity,
        (S("doc_prefix", "scope"),),
        build="unchanged-publication-test",
    )
    try:
        yield opened, coordinator, definition, SourcePublisher(
            coordinator, opened, definition,
        )
    finally:
        opened.close()


def _publish(store, expected, documents, *, cursor=0, skip=False, **kwargs):
    return store.publish_document_batch(
        expected,
        documents,
        cursor=cursor,
        full=True,
        retain_tombstones=False,
        skip_unchanged=skip,
        **kwargs,
    )


def test_store_default_advances_but_opt_in_preserves_exact_position(store):
    cold = store.capture_document_position(SOURCE)
    first = _publish(store, cold, {"scope/a": {"value": 1}})
    assert first.initialized and first.generation == cold.generation + 1

    default = _publish(store, first, {"scope/a": {"value": 1}})
    assert default.generation == first.generation + 1

    unchanged = _publish(
        store, default, {"scope/a": {"value": 1}}, skip=True,
    )
    assert unchanged == default


def test_local_source_default_admission_rejects_same_raw_position(store):
    expected = local_source.capture(store, SOURCE)
    published = _publish(store, expected.raw, {"scope/a": 1})
    ready = local_source.admit(store, expected, published, observed_ns=1)
    with pytest.raises(local_source.SourceChanged, match="publication_not_successor"):
        local_source.admit(store, ready, ready.raw, observed_ns=2)
    assert local_source.capture(store, SOURCE) == ready


def test_skip_unchanged_validates_shape_and_cold_source_still_initializes(store):
    cold = store.capture_document_position(SOURCE)
    initialized = _publish(store, cold, {}, skip=True)
    assert initialized.initialized
    assert initialized.generation == cold.generation + 1

    with pytest.raises(ValueError, match="skip_unchanged must be a bool"):
        store.publish_document_batch(
            initialized, {}, cursor=0, full=True,
            retain_tombstones=False, skip_unchanged=1,
        )
    for kwargs in (
        {"full": False, "retain_tombstones": False},
        {"full": True, "retain_tombstones": True},
        {"full": True, "retain_tombstones": False, "deleted_paths": ("gone",)},
    ):
        with pytest.raises(ValueError, match="complete tombstone-free"):
            store.publish_document_batch(
                initialized, {}, cursor=0, skip_unchanged=True, **kwargs,
            )


@pytest.mark.parametrize("change", ["payload", "path", "presence", "cursor"])
def test_any_complete_set_or_cursor_change_advances_generation(store, change):
    documents = {"scope/a": "same", "scope/b": "present"}
    first = _publish(store, store.capture_document_position(SOURCE), documents)
    changed = dict(documents)
    cursor = 0
    if change == "payload":
        changed["scope/a"] = "samf"
    elif change == "path":
        changed["scope/c"] = changed.pop("scope/b")
    elif change == "presence":
        changed.pop("scope/b")
    else:
        cursor = 1
    result = _publish(store, first, changed, cursor=cursor, skip=True)
    assert result.generation == first.generation + 1
    assert result.cursor == cursor
    assert store.capture_document_observation(SOURCE).documents() == changed


def test_existing_tombstone_is_not_equal_to_complete_live_set(store):
    first = store.publish_document_batch(
        store.capture_document_position(SOURCE),
        {"scope/a": 1},
        cursor=0,
        full=True,
        retain_tombstones=True,
    )
    tombstoned = store.publish_document_batch(
        first, {}, cursor=0, full=True, retain_tombstones=True,
    )
    assert store.capture_document_observation(SOURCE).records[0].deleted

    empty = _publish(store, tombstoned, {}, skip=True)
    assert empty.generation == tombstoned.generation + 1
    assert store.capture_document_observation(SOURCE).records == ()


def test_old_oversized_payload_size_mismatch_avoids_payload_fetch(store):
    huge = "x" * 4096
    first = _publish(
        store,
        store.capture_document_position(SOURCE),
        {"scope/a": "same", "scope/z": huge},
        max_bytes=8192,
    )
    statements = []
    store._conn().set_trace_callback(statements.append)
    try:
        replaced = _publish(
            store, first, {"scope/a": "same", "scope/z": "x"},
            skip=True, max_bytes=128,
        )
    finally:
        store._conn().set_trace_callback(None)
    assert replaced.generation == first.generation + 1
    assert not any(
        "SELECT payload,deleted FROM document_observation_records" in statement
        for statement in statements
    )


def test_source_publisher_identical_raw_advances_owner_only_and_updates_health(
        publisher_rig):
    store, _coordinator, definition, publisher = publisher_rig
    documents = {"scope/a": {"value": 1}}
    first = publisher.publish(publisher.capture(), documents, observed_ns=10)
    again = publisher.publish(publisher.capture(), documents, observed_ns=20)

    assert again.ready
    assert again.raw == first.raw
    assert again.revision == first.revision + 2
    assert local_source.health(store, definition.source) == {
        "ready": True,
        "writes_pending": 0,
        "last_success_ns": 20,
        "failures": 0,
        "error": "",
    }

    # The generic owner API retains its existing generation-advancing default.
    generic = local_source.publish(store, again, documents, observed_ns=30)
    assert generic.raw.generation == again.raw.generation + 1


def test_exhausted_generation_allows_exact_noop_but_rejects_changes(
        publisher_rig):
    store, _coordinator, definition, publisher = publisher_rig
    documents = {"scope/a": {"value": 1}}
    publisher.publish(publisher.capture(), documents, observed_ns=10)
    maximum = document_observation.MAX_SQLITE_INTEGER
    conn = store._conn()
    conn.execute(
        "UPDATE document_observation_sources SET generation=? WHERE source_id=?",
        (maximum, definition.source),
    )
    conn.execute(
        "UPDATE local_sources SET ready_generation=? WHERE source=?",
        (maximum, definition.source),
    )
    conn.commit()
    at_max = local_source.capture(store, definition.source)
    assert at_max.ready and at_max.raw.generation == maximum

    unchanged = publisher.publish(publisher.capture(), documents, observed_ns=20)
    assert unchanged.raw == at_max.raw
    assert unchanged.revision == at_max.revision + 2

    with pytest.raises(OverflowError, match="generation is exhausted"):
        publisher.publish(
            publisher.capture(), {"scope/a": {"value": 2}}, observed_ns=30,
        )
    assert not local_source.capture(store, definition.source).ready

    # The Store default does not opt in and remains generation advancing.
    with pytest.raises(OverflowError, match="generation is exhausted"):
        _publish(store, at_max.raw, documents)


@pytest.mark.parametrize("race", ["admission_failure", "pending_write", "raw_aba"])
def test_unchanged_attempt_races_never_restore_readiness(
        publisher_rig, monkeypatch, race):
    store, coordinator, definition, publisher = publisher_rig
    documents = {"scope/a": {"value": 1}}
    ready = publisher.publish(publisher.capture(), documents, observed_ns=10)
    captured = publisher.capture()

    if race == "admission_failure":
        monkeypatch.setattr(
            local_source,
            "admit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("admission failed")
            ),
        )
        error = RuntimeError
    else:
        original = store.publish_document_batch

        def crossed(*args, **kwargs):
            published = original(*args, **kwargs)
            if race == "pending_write":
                local_source.begin_write(store, definition.source)
            else:
                conn = store._conn()
                conn.execute(
                    "UPDATE document_observation_records SET payload=? "
                    "WHERE source_id=? AND path='scope/a'",
                    ('{"value":2}', definition.source),
                )
                conn.execute(
                    "UPDATE document_observation_records SET payload=? "
                    "WHERE source_id=? AND path='scope/a'",
                    ('{"value":1}', definition.source),
                )
                conn.commit()
            return published

        monkeypatch.setattr(store, "publish_document_batch", crossed)
        error = local_source.SourceChanged

    with pytest.raises(error):
        publisher.publish(captured, documents, observed_ns=20)
    current = local_source.capture(store, definition.source)
    assert not current.ready
    if race == "admission_failure":
        assert current.raw == ready.raw
    elif race == "pending_write":
        assert current.writes_pending == 1
    else:
        assert current.raw.generation == ready.raw.generation + 2
