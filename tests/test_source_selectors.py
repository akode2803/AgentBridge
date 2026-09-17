"""Registered local-source coverage and transactional retirement contracts."""
from __future__ import annotations

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store


S = source_selectors.Selector


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    local_source.initialize(opened)
    source_selectors.initialize(opened)
    yield opened
    opened.close()


def _register(store, *definitions):
    with local_source._writer(store) as conn:
        return tuple(
            source_selectors.register_in_transaction(conn, store, definition)
            for definition in definitions
        )


def _matching(store, *changes):
    with local_source._writer(store) as conn:
        return source_selectors.matching_in_transaction(conn, tuple(changes))


def test_account_fanout_does_not_select_unrelated_chat_sources(store):
    account_a = source_selectors.definition("root", (
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/a"),
    ), build="a")
    account_b = source_selectors.definition("root", (
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/b"),
    ), build="b")
    unrelated = source_selectors.definition("root", (
        S("doc_exact", "accounts/bob.json"),
        S("doc_prefix", "chats/c"),
    ), build="c")
    _register(store, account_a, account_b, unrelated)

    assert _matching(store, S("doc_exact", "accounts/alice.json")) == tuple(
        sorted((account_a.source, account_b.source))
    )
    assert _matching(store, S("doc_exact", "chats/a/meta.json")) == (
        account_a.source,
    )
    assert unrelated.source not in _matching(
        store, S("doc_prefix", "chats/a"),
    )


def test_exact_prefix_segment_boundaries_and_literal_wildcard_characters(store):
    exact = source_selectors.definition("root", (
        S("doc_exact", "rooms/a_%/meta.json"),
    ), build="exact")
    prefix = source_selectors.definition("root", (
        S("doc_prefix", "rooms/a_%"),
    ), build="prefix")
    neighbor = source_selectors.definition("root", (
        S("doc_prefix", "rooms/a_%2"),
    ), build="neighbor")
    _register(store, exact, prefix, neighbor)

    assert _matching(store, S("doc_exact", "rooms/a_%/meta.json")) == tuple(
        sorted((exact.source, prefix.source))
    )
    assert _matching(store, S("doc_prefix", "rooms/a_%")) == tuple(
        sorted((exact.source, prefix.source))
    )
    assert _matching(store, S("doc_exact", "rooms/aX/meta.json")) == ()
    assert _matching(store, S("doc_exact", "rooms/a_%2/meta.json")) == (
        neighbor.source,
    )


def test_subtree_change_intersects_ancestors_and_descendants_not_neighbors(store):
    ancestor = source_selectors.definition("root", (
        S("doc_prefix", "trees/a"),
    ), build="ancestor")
    same = source_selectors.definition("root", (
        S("doc_prefix", "trees/a/branch"),
    ), build="same")
    descendant = source_selectors.definition("root", (
        S("doc_exact", "trees/a/branch/leaf.json"),
    ), build="descendant")
    neighbor = source_selectors.definition("root", (
        S("doc_prefix", "trees/a/branches"),
    ), build="neighbor")
    _register(store, ancestor, same, descendant, neighbor)

    affected = _matching(store, S("doc_prefix", "trees/a/branch"))
    assert affected == tuple(sorted((ancestor.source, same.source, descendant.source)))
    assert neighbor.source not in affected


def test_log_selectors_match_only_exact_chat(store):
    first = source_selectors.definition(
        "root", (S("log_chat", "chat-one"),), build="one",
    )
    second = source_selectors.definition(
        "root", (S("log_chat", "chat-two"),), build="two",
    )
    _register(store, first, second)
    assert _matching(store, S("log_chat", "chat-one")) == (first.source,)
    assert _matching(store, S("log_chat", "chat")) == ()


def test_definition_binds_root_build_and_canonicalized_coverage():
    values = (
        S("doc_prefix", "chats/a"),
        S("doc_exact", "accounts/alice.json"),
        S("doc_prefix", "chats/a"),
    )
    canonical = source_selectors.definition("root-a", values, build="build-a")
    reordered = source_selectors.definition(
        "root-a", tuple(reversed(values)), build="build-a",
    )
    assert canonical == reordered
    assert len(canonical.selectors) == 2
    assert source_selectors.definition("root-b", values, build="build-a").source != canonical.source
    assert source_selectors.definition("root-a", values, build="build-b").source != canonical.source
    assert source_selectors.definition(
        "root-a", (S("doc_prefix", "chats/b"),), build="build-a",
    ).source != canonical.source


def test_registration_replay_is_idempotent_and_corruption_fails_closed(store):
    definition = source_selectors.definition(
        "root", (S("doc_prefix", "chats/a"),),
    )
    first = _register(store, definition)[0]
    replay = _register(store, definition)[0]
    assert replay == first

    store._conn().execute(
        "UPDATE local_source_selectors SET value='chats/corrupt' WHERE source=?",
        (definition.source,),
    )
    store._conn().commit()
    with pytest.raises(local_source.SourceChanged,
                       match="registered_selectors_changed"):
        _register(store, definition)


@pytest.mark.parametrize("damage", ["definition", "schema"])
def test_corrupt_registry_or_schema_fails_closed(store, damage):
    definition = source_selectors.definition(
        "root", (S("doc_exact", "accounts/alice.json"),),
    )
    _register(store, definition)
    if damage == "definition":
        store._conn().execute(
            "UPDATE local_source_definitions SET definition='[]' WHERE source=?",
            (definition.source,),
        )
    else:
        store._conn().execute("DROP INDEX idx_local_source_selector_match")
    store._conn().commit()
    with pytest.raises(local_source.SourceChanged):
        _register(store, definition)


def test_matching_mutation_retires_ready_generation(store):
    definition = source_selectors.definition(
        "root", (S("doc_prefix", "chats/a"),),
    )
    registered = _register(store, definition)[0]
    ready = local_source.publish(store, registered, {"doc": 1}, observed_ns=10)
    assert ready.ready

    with local_source._writer(store) as conn:
        count = source_selectors.retire_in_transaction(
            conn, store, (S("doc_exact", "chats/a/meta.json"),),
        )
    retired = local_source.capture(store, definition.source)
    assert count == 1
    assert retired.revision == ready.revision + 1
    assert retired.raw == ready.raw
    assert not retired.ready


def test_registration_budget_rejects_without_partial_row(store, monkeypatch):
    monkeypatch.setattr(source_selectors, "MAX_SOURCES", 2)
    definitions = tuple(
        source_selectors.definition(
            f"root-{index}", (S("doc_exact", f"accounts/{index}.json"),),
        )
        for index in range(3)
    )
    _register(store, *definitions[:2])
    with pytest.raises(local_source.SourceChanged,
                       match="source_registration_budget"):
        _register(store, definitions[2])
    assert store._conn().execute(
        "SELECT count(*) FROM local_source_definitions",
    ).fetchone()[0] == 2


def test_retirement_rolls_back_all_batches_on_late_revision_exhaustion(store):
    definitions = tuple(
        source_selectors.definition(
            f"root-{index}", (S("doc_exact", "accounts/shared.json"),),
        )
        for index in range(401)
    )
    _register(store, *definitions)
    ordered = sorted(definition.source for definition in definitions)
    store._conn().execute(
        "UPDATE local_sources SET revision=? WHERE source=?",
        (local_source.MAX, ordered[-1]),
    )
    store._conn().commit()

    with pytest.raises(local_source.SourceChanged,
                       match="mutation_revision_unavailable"):
        with local_source._writer(store) as conn:
            source_selectors.retire_in_transaction(
                conn, store, (S("doc_exact", "accounts/shared.json"),),
            )
    rows = dict(store._conn().execute(
        "SELECT source,revision FROM local_sources",
    ).fetchall())
    assert rows[ordered[0]] == 1
    assert rows[ordered[399]] == 1
    assert rows[ordered[-1]] == local_source.MAX


def test_matching_requires_active_transaction(store):
    with pytest.raises(ValueError, match="requires a transaction"):
        source_selectors.matching_in_transaction(
            store._conn(), (S("doc_exact", "accounts/a.json"),),
        )


def test_initialize_does_not_repair_partial_selector_schema(store):
    store._conn().execute("DROP TABLE local_source_selectors")
    store._conn().commit()
    with pytest.raises(local_source.SourceChanged, match="selector_schema_changed"):
        source_selectors.initialize(store)
