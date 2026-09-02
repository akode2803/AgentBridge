"""P2.0 content-free, distributed-compatible projection input contracts."""

from __future__ import annotations

import json

import pytest

from agentbridge.mesh.projection_version import (
    REQUIRED_COMPONENTS, ProjectionInputVersion, ProjectionScope,
    ProjectionVersionError, component_digest, frontier_digest,
    invalidation_scopes, projection_binding,
)


def _components(**changes):
    values = {
        name: component_digest(name, {"high_water": index})
        for index, name in enumerate(sorted(REQUIRED_COMPONENTS), 1)
    }
    values.update(changes)
    return values


def _version(**changes):
    values = {
        "viewer_binding": projection_binding("viewer", "member-a"),
        "room_binding": projection_binding("room", "room-a"),
        "server_generation": "generation-a",
        "fold_mode": "viewer",
        "tail_limit": 200,
        "components": _components(),
    }
    values.update(changes)
    return ProjectionInputVersion.build(**values)


def test_input_version_is_canonical_content_free_and_deterministic():
    components = _components()
    first = _version(components=components)
    second = _version(components=dict(reversed(list(components.items()))))
    assert first == second
    assert first.structurally_complete
    assert first.candidate_digest() == second.candidate_digest()
    encoded = json.dumps(first.to_dict(), sort_keys=True)
    assert "member-a" not in encoded and "room-a" not in encoded
    assert set(first.to_dict()) == {
        "schema_version", "viewer_binding", "room_binding",
        "server_generation", "fold_mode", "tail_limit", "components",
    }


@pytest.mark.parametrize("field,replacement", [
    ("viewer_binding", projection_binding("viewer", "member-b")),
    ("room_binding", projection_binding("room", "room-b")),
    ("server_generation", "generation-b"),
    ("fold_mode", "breadcrumbs"),
    ("tail_limit", 500),
])
def test_every_projection_dimension_changes_candidate_digest(field, replacement):
    baseline = _version()
    changed = _version(**{field: replacement})
    assert changed.candidate_digest() != baseline.candidate_digest()


def test_component_change_and_incomplete_inputs_fail_closed():
    baseline = _version()
    changed_components = _components(
        messages=component_digest("messages", {"high_water": 99}))
    assert _version(components=changed_components).candidate_digest() != \
        baseline.candidate_digest()

    incomplete = _version(components={
        name: digest for name, digest in _components().items()
        if name != "membership"
    })
    assert not incomplete.structurally_complete
    with pytest.raises(ProjectionVersionError, match="membership"):
        incomplete.candidate_digest()


def test_per_origin_frontier_digest_detects_order_gaps_and_second_node():
    node_a = component_digest("node", "a")
    node_b = component_digest("node", "b")
    one = frontier_digest({node_a: 4}, {node_a: [(6, 7)]})
    two = frontier_digest({node_a: 4, node_b: 1}, {node_a: [(6, 7)]})
    reordered = frontier_digest(
        {node_b: 1, node_a: 4}, {node_a: [(6, 7)]})
    assert two == reordered and one != two
    with pytest.raises(ProjectionVersionError, match="beyond sequence"):
        frontier_digest({node_a: 4}, {node_a: [(4, 7)]})
    with pytest.raises(ProjectionVersionError, match="named origin"):
        frontier_digest({node_a: 4}, {node_b: [(2, 3)]})


def test_mutation_scope_map_is_explicit_and_unknown_fails_broad():
    assert invalidation_scopes("message_append") == frozenset({
        ProjectionScope.MESSAGES, ProjectionScope.SUMMARY,
        ProjectionScope.UNREAD, ProjectionScope.RECEIPTS,
        ProjectionScope.NOTIFICATIONS,
    })
    assert invalidation_scopes("membership") == frozenset({ProjectionScope.ALL})
    assert invalidation_scopes("future-unrecognized-mutation") == \
        frozenset({ProjectionScope.ALL})


def test_invalid_bindings_components_and_modes_are_rejected():
    with pytest.raises(ProjectionVersionError, match="binding kind"):
        projection_binding("server", "value")
    with pytest.raises(ProjectionVersionError, match="component name"):
        component_digest("Bad Name", 1)
    with pytest.raises(ProjectionVersionError, match="fold mode"):
        _version(fold_mode="authority")
    with pytest.raises(ProjectionVersionError, match="tail limit"):
        _version(tail_limit=0)
    with pytest.raises(ProjectionVersionError, match="canonical JSON"):
        component_digest("messages", float("nan"))
