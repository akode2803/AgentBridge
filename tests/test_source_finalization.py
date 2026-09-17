"""Final root-to-Store cut for already prepared local source inputs."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from agentbridge.store import local_source, mutation_coordinator, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


S = source_selectors.Selector


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    root = MutationCoordinator(tmp_path / "home", "finalization-root")
    root.register_store(store)
    definition = source_selectors.definition(
        root.identity,
        (S("doc_prefix", "chats/room"),),
        build="finalization-test",
    )
    publisher = SourcePublisher(root, store, definition)
    try:
        yield store, root, definition, publisher
    finally:
        store.close()


def _ready(rig, value=1):
    store, _root, definition, publisher = rig
    return publisher.publish(
        publisher.capture(),
        {"chats/room/state/alice.json": {"read": value}},
        observed_ns=value,
    )


def test_exact_registered_ready_owner_and_raw_position_are_returned(rig):
    store, root, definition, _publisher = rig
    ready = _ready(rig)
    with root.finalization_cut(store, definition) as (conn, position):
        assert conn.in_transaction
        assert position == ready
        assert local_source.capture_in_transaction(
            conn, store, definition.source,
        ) == ready
    assert local_source.capture(store, definition.source) == ready


def test_missing_registration_and_unready_source_fail_without_foreground_writes(
        rig):
    store, root, definition, publisher = rig
    missing = source_selectors.definition(
        root.identity,
        (S("doc_exact", "accounts/alice.json"),),
        build="missing-finalization-test",
    )
    before_source = local_source.capture(store, missing.source)
    conn = store._conn()
    before_counts = (
        conn.execute("SELECT count(*) FROM local_source_definitions").fetchone()[0],
        conn.execute("SELECT count(*) FROM local_source_selectors").fetchone()[0],
    )
    with pytest.raises(local_source.SourceChanged, match="source_not_registered"):
        with root.finalization_cut(store, missing):
            pytest.fail("unregistered source reached caller")
    assert local_source.capture(store, missing.source) == before_source
    assert before_counts == (
        conn.execute("SELECT count(*) FROM local_source_definitions").fetchone()[0],
        conn.execute("SELECT count(*) FROM local_source_selectors").fetchone()[0],
    )

    publisher.capture()  # Registration is an explicit background preparation.
    registered = local_source.capture(store, definition.source)
    assert not registered.ready
    with pytest.raises(local_source.SourceChanged, match="source_not_ready"):
        with root.finalization_cut(store, definition):
            pytest.fail("unready source reached caller")
    assert local_source.capture(store, definition.source) == registered


def test_pending_overlap_rejects_even_if_source_is_prepared_behind_root(rig):
    store, root, definition, _publisher = rig
    _ready(rig)
    intent = root.begin((S("doc_exact", "chats/room/meta.json"),))
    retired = local_source.capture(store, definition.source)
    prepared = local_source.publish(
        store,
        retired,
        {"chats/room/state/alice.json": {"read": 2}},
        observed_ns=2,
    )
    assert prepared.ready
    try:
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            with root.finalization_cut(store, definition):
                pytest.fail("overlapping pending mutation reached caller")
    finally:
        root.complete(intent)


def test_unrelated_pending_intent_is_allowed(rig):
    store, root, definition, _publisher = rig
    ready = _ready(rig)
    intent = root.begin((S("doc_exact", "accounts/bob.json"),))
    try:
        with root.finalization_cut(store, definition) as (_conn, position):
            assert position == ready
    finally:
        root.complete(intent)


@pytest.mark.parametrize("state", ["retired", "failure"])
def test_retired_or_failed_source_is_denied(rig, state):
    store, root, definition, _publisher = rig
    _ready(rig)
    if state == "retired":
        local_source.invalidate(store, definition.source)
    else:
        local_source.record_failure(store, definition.source, reason="io")
    before = local_source.capture(store, definition.source)
    with pytest.raises(local_source.SourceChanged, match="source_not_ready"):
        with root.finalization_cut(store, definition):
            pytest.fail("unavailable source reached caller")
    assert local_source.capture(store, definition.source) == before


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("root", "mutation_root_changed"),
        ("store", "registered_store_replaced"),
        ("source_epoch", "registered_store_replaced"),
        ("definition", "definition_changed"),
        ("selector", "registered_selectors_changed"),
    ],
)
def test_root_store_source_definition_and_selector_replacement_rejected(
        rig, damage, message):
    store, root, definition, _publisher = rig
    _ready(rig)
    if damage == "root":
        with sqlite3.connect(root.path) as conn:
            conn.execute(
                "UPDATE mutation_root SET epoch=? WHERE singleton=1", ("f" * 32,),
            )
    elif damage == "store":
        store._conn().execute(
            "UPDATE ingestion_identity SET incarnation=? WHERE singleton=1",
            ("f" * 32,),
        )
        store._conn().commit()
    elif damage == "source_epoch":
        store._conn().execute(
            "UPDATE local_source_schema SET epoch=? WHERE singleton=1",
            ("e" * 32,),
        )
        store._conn().commit()
    elif damage == "definition":
        store._conn().execute(
            "UPDATE local_source_definitions SET definition='[]' WHERE source=?",
            (definition.source,),
        )
        store._conn().commit()
    else:
        store._conn().execute(
            "UPDATE local_source_selectors SET value='chats/other' WHERE source=?",
            (definition.source,),
        )
        store._conn().commit()
    with pytest.raises(local_source.SourceChanged, match=message):
        with root.finalization_cut(store, definition):
            pytest.fail("replaced ownership reached caller")


@pytest.mark.parametrize("damage", ["huge_incarnation", "blob_epoch"])
def test_malformed_store_registry_is_rejected_before_value_fetch(
        rig, monkeypatch, damage):
    store, root, definition, _publisher = rig
    _ready(rig)
    with sqlite3.connect(root.path) as conn:
        if damage == "huge_incarnation":
            conn.execute(
                "UPDATE mutation_stores SET incarnation=? WHERE path=?",
                ("x" * 5000, str(store.path.resolve())),
            )
        else:
            conn.execute(
                "UPDATE mutation_stores SET source_epoch=? WHERE path=?",
                (sqlite3.Binary(b"x" * 32), str(store.path.resolve())),
            )

    statements = []
    real_connect = mutation_coordinator.sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(mutation_coordinator.sqlite3, "connect", traced_connect)
    with pytest.raises(local_source.SourceChanged,
                       match="invalid_store_registration"):
        with root.finalization_cut(store, definition):
            pytest.fail("malformed registry reached caller")
    assert not any(
        statement.startswith("SELECT incarnation,source_epoch ")
        for statement in statements
    )


def test_overbudget_corrupt_registered_selectors_fail_boundedly(rig):
    store, root, definition, _publisher = rig
    _ready(rig)
    store._conn().executemany(
        "INSERT INTO local_source_selectors VALUES(?,?,?)",
        (
            (definition.source, f"bad{i:03d}", f"scope/{i:03d}")
            for i in range(source_selectors.MAX_SELECTORS)
        ),
    )
    store._conn().commit()
    with pytest.raises(local_source.SourceChanged,
                       match="registered_selector_budget"):
        with root.finalization_cut(store, definition):
            pytest.fail("overbudget selector registry reached caller")


def test_caller_exception_rolls_back_store_writes(rig):
    store, root, definition, _publisher = rig
    ready = _ready(rig)
    with pytest.raises(RuntimeError, match="caller failed"):
        with root.finalization_cut(store, definition) as (conn, _position):
            conn.execute(
                "UPDATE document_observation_records SET payload=? "
                "WHERE source_id=?",
                ('{"read":99}', definition.source),
            )
            raise RuntimeError("caller failed")
    assert local_source.capture(store, definition.source) == ready
    assert store.capture_document_observation(
        definition.source,
    ).document("chats/room/state/alice.json") == {"read": 1}


def test_post_yield_raw_change_is_detected_and_rolled_back(rig):
    store, root, definition, _publisher = rig
    ready = _ready(rig)
    with pytest.raises(local_source.SourceChanged,
                       match="source_changed_during_finalization"):
        with root.finalization_cut(store, definition) as (conn, _position):
            conn.execute(
                "UPDATE document_observation_records SET payload=? "
                "WHERE source_id=?",
                ('{"read":2}', definition.source),
            )
    assert local_source.capture(store, definition.source) == ready
    assert store.capture_document_observation(
        definition.source,
    ).document("chats/room/state/alice.json") == {"read": 1}


def test_concurrent_begin_waits_for_final_store_commit_then_retires(rig):
    store, root, definition, _publisher = rig
    _ready(rig)
    entered = threading.Event()
    release = threading.Event()
    finalized = threading.Event()
    begun = threading.Event()
    errors = []
    intents = []

    def finalize():
        try:
            with root.finalization_cut(store, definition):
                entered.set()
                assert release.wait(10), "finalization barrier timed out"
            finalized.set()
        except BaseException as exc:  # asserted below
            errors.append(exc)

    def mutate():
        try:
            intents.append(root.begin((S("doc_exact", "chats/room/meta.json"),)))
            begun.set()
        except BaseException as exc:  # asserted below
            errors.append(exc)

    finalizer = threading.Thread(target=finalize)
    mutator = threading.Thread(target=mutate)
    finalizer.start()
    try:
        assert entered.wait(10), "finalization did not acquire its cut"
        mutator.start()
        assert not begun.wait(0.1)
    finally:
        release.set()
        finalizer.join(10)
        mutator.join(10)
    try:
        assert not errors
        assert not finalizer.is_alive() and not mutator.is_alive()
        assert finalized.is_set() and begun.is_set()
        assert not local_source.capture(store, definition.source).ready
    finally:
        if intents:
            root.complete(intents[0])
