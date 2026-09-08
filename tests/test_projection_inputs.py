"""P2.1 diagnostic projection collection, explicitly barred from cache serving."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.harness.runtime.controls import publish_pause
from agentbridge.core.errors import NotAMember
from agentbridge.mesh.paths import P
from agentbridge.mesh.projection_inputs import (
    ProjectionInputCollector, ProjectionInputError,
)
from agentbridge.mesh.projection_version import (
    ProjectionVersionError, component_digest, frontier_digest,
)
from agentbridge.mesh.service import Mesh


@pytest.fixture()
def projection_mesh(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    home = tmp_path / "home"
    mesh = Mesh(
        root, "owner", "box", encrypt=True, home=home,
        store_path=tmp_path / "owner.sqlite",
    )
    mesh.accounts.create_human("owner", "correct-horse")
    mesh.accounts.create_agent("helper")
    chat = mesh.create_chat("Projection", members=[])
    message = mesh.post(chat.id, "projection body")
    mesh.outbox.flush_once()
    try:
        yield mesh, chat.id, message.id
    finally:
        mesh.close()


def _parts(collection):
    return {component.name: component.digest
            for component in collection.version.components}


def test_collector_uses_one_local_capture_and_still_refuses_cache(projection_mesh, monkeypatch):
    mesh, chat_id, _ = projection_mesh
    capture = mesh.store.capture_chat_inputs
    seen = []

    def captured(*args, **kwargs):
        seen.append((args, kwargs))
        return capture(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("collector performed split local input reads")

    monkeypatch.setattr(mesh.store, "capture_chat_inputs", captured)
    monkeypatch.setattr(mesh.store, "message_count", forbidden)
    monkeypatch.setattr(mesh.store, "messages", forbidden)
    monkeypatch.setattr(mesh.store, "log_offsets", forbidden)
    collection = ProjectionInputCollector(mesh, server_generation="test").collect(chat_id)
    assert len(seen) == 1
    assert seen[0][1]["document_paths"] == ("sync/log_cursor",)
    with pytest.raises(ProjectionInputError, match="cannot authorize cache"):
        collection.require_cache_ready()


def test_collector_is_membership_gated_content_free_and_honest(projection_mesh):
    mesh, chat_id, _message_id = projection_mesh
    collection = ProjectionInputCollector(
        mesh, server_generation="generation-a").collect(chat_id)
    assert collection.version.structurally_complete
    assert not collection.external_inputs_supplied
    assert collection.replication_frontier_supplied is False
    assert collection.runtime_process_supplied is False
    with pytest.raises(ProjectionInputError, match="cannot authorize cache"):
        collection.require_cache_ready()
    encoded = str(collection.version.to_dict())
    assert "owner" not in encoded
    assert chat_id not in encoded
    assert "projection body" not in encoded

    node = component_digest("node", "server-a")
    complete = ProjectionInputCollector(
        mesh,
        server_generation="generation-a",
        runtime_process_facts={"helper": {"alive": True}},
        replication_frontier_digest=frontier_digest({node: 0}),
    ).collect(chat_id)
    assert complete.external_inputs_supplied
    assert set(complete.coverage_gaps) == {
        "historical_identity_dependencies", "retained_lifecycle_heads",
        "global_privacy_ownership", "future_skew_activation",
        "decrypted_result_cache",
    }
    with pytest.raises(ProjectionInputError, match="cannot authorize cache"):
        complete.require_cache_ready()
    assert complete.candidate_digest() != collection.candidate_digest()


def test_collector_rejects_nonmember_cold_oversized_and_membership_race(
        projection_mesh, monkeypatch):
    mesh, chat_id, _message_id = projection_mesh
    helper = Mesh(
        mesh.tx.root, "helper", "box", encrypt=True, home=mesh.home,
        store_path=mesh.home / "helper-projection.sqlite",
    )
    try:
        with pytest.raises(NotAMember, match="not a member"):
            ProjectionInputCollector(
                helper, server_generation="generation").collect(chat_id)
    finally:
        helper.close()

    monkeypatch.setattr(
        mesh.tx, "mirror_status", lambda: {"warm": False, "state": "loading"},
        raising=False,
    )
    with pytest.raises(ProjectionInputError, match="not warm"):
        ProjectionInputCollector(mesh, server_generation="generation").collect(chat_id)
    monkeypatch.delattr(mesh.tx, "mirror_status", raising=False)

    original_cached = mesh.tx.cached_docs_bounded
    monkeypatch.setattr(
        mesh.tx, "cached_docs_bounded",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OverflowError("large")),
    )
    with pytest.raises(ProjectionInputError, match="read budget"):
        ProjectionInputCollector(mesh, server_generation="generation").collect(chat_id)
    monkeypatch.setattr(mesh.tx, "cached_docs_bounded", original_cached)

    original_terminal = mesh.messaging.pending_terminal
    monkeypatch.setattr(mesh.messaging, "pending_terminal", lambda _chat_id: True)
    with pytest.raises(NotAMember, match="not a member"):
        ProjectionInputCollector(mesh, server_generation="generation").collect(chat_id)
    monkeypatch.setattr(mesh.messaging, "pending_terminal", original_terminal)

    original_member = mesh.messaging._require_member
    calls = 0

    def changed(cid):
        nonlocal calls
        calls += 1
        snap = original_member(cid)
        return replace(snap, description="changed") if calls == 2 else snap

    monkeypatch.setattr(mesh.messaging, "_require_member", changed)
    with pytest.raises(ProjectionInputError, match="membership changed"):
        ProjectionInputCollector(mesh, server_generation="generation").collect(chat_id)


def test_every_local_projection_source_moves_its_component(projection_mesh):
    mesh, chat_id, message_id = projection_mesh
    collector = ProjectionInputCollector(mesh, server_generation="generation")
    current = collector.collect(chat_id)

    def assert_changed(name, mutate):
        nonlocal current
        before = _parts(current)
        mutate()
        current = collector.collect(chat_id)
        assert _parts(current)[name] != before[name], name

    assert_changed("messages", lambda: mesh.post(chat_id, "second"))
    assert_changed("edits", lambda: mesh.edit(chat_id, message_id, "edited"))
    assert_changed("reactions", lambda: mesh.react(chat_id, message_id, "ok"))
    assert_changed("pins", lambda: mesh.pin(chat_id, message_id, hours=1))
    assert current.next_observed_boundary_ns is not None
    assert_changed("redactions", lambda: mesh.redact(chat_id, [message_id]))
    assert_changed("viewer_state", lambda: mesh.star(chat_id, [message_id]))
    assert_changed("receipts", lambda: mesh.mark_read(chat_id))
    assert_changed(
        "directory",
        lambda: mesh.directory.patch(
            "owner", lambda doc: doc.update(display="Changed Owner")),
    )
    assert_changed("presence", lambda: mesh.presence.heartbeat(online=True))
    assert_changed(
        "runtime",
        lambda: mesh.tx.put_doc(
            f"chats/{chat_id}/runtime/test/event.json", {"v": 1}),
    )
    assert_changed(
        "pause", lambda: publish_pause(mesh, paused=True, chat_id=chat_id))
    assert_changed(
        "key_epoch", lambda: mesh.keys.rotate(chat_id, sorted(mesh.snapshot(chat_id).members)),
    )
    assert_changed("key_trust", lambda: mesh.key_pins.mark_verified("owner"))
    assert_changed(
        "replication_frontier",
        lambda: mesh.store.set_offset(chat_id, "owner@other-node", 4),
    )
    assert_changed("membership", lambda: mesh.add_members(chat_id, ["helper"]))


def test_authentication_secrets_do_not_change_directory_candidate(projection_mesh):
    mesh, chat_id, _message_id = projection_mesh
    collector = ProjectionInputCollector(mesh, server_generation="generation")
    before = _parts(collector.collect(chat_id))["directory"]
    path = P.user("owner")
    doc = mesh.tx.get_doc(path)
    doc["auth"] = {**doc["auth"], "verifier": "different-secret-verifier"}
    keys = dict(doc.get("keys") or {})
    keys["wrapped_priv"] = {"salt": "x", "nonce": "y", "ct": "z"}
    doc["keys"] = keys
    mesh.tx.put_doc(path, doc)
    after = _parts(collector.collect(chat_id))["directory"]
    assert after == before


def test_directory_collection_is_local_only_and_lifecycle_invalidates(
        projection_mesh, monkeypatch):
    mesh, chat_id, _message_id = projection_mesh
    collector = ProjectionInputCollector(mesh, server_generation="generation")
    before = _parts(collector.collect(chat_id))["directory"]
    original_cached = mesh.tx.cached_docs_bounded
    original_get = mesh.tx.get_doc
    snapshots = {
        prefix: original_cached(prefix, 10_000)
        for prefix in ("users/", "lifecycle/")
    }

    def bounded_snapshot(prefix, limit):
        if prefix in snapshots:
            return snapshots[prefix]
        return original_cached(prefix, limit)

    def reject_directory_readthrough(path, *args, **kwargs):
        if path.startswith(("users/", "lifecycle/")):
            raise AssertionError(f"unexpected directory read-through: {path}")
        return original_get(path, *args, **kwargs)

    monkeypatch.setattr(mesh.tx, "cached_docs_bounded", bounded_snapshot)
    monkeypatch.setattr(mesh.tx, "get_doc", reject_directory_readthrough)
    assert _parts(collector.collect(chat_id))["directory"] == before
    monkeypatch.setattr(mesh.tx, "get_doc", original_get)
    monkeypatch.setattr(mesh.tx, "cached_docs_bounded", original_cached)

    mesh.tx.put_doc(
        "lifecycle/owner/untrusted-change.json",
        {"record": {"id": "untrusted-change"}},
    )
    after = _parts(collector.collect(chat_id))["directory"]
    assert after != before


def test_unrelated_runtime_and_presence_do_not_invalidate_room(projection_mesh):
    mesh, chat_id, _message_id = projection_mesh
    collector = ProjectionInputCollector(mesh, server_generation="generation")
    before = _parts(collector.collect(chat_id))
    mesh.tx.put_doc("status/outsider_live.json", {
        "kind": "run-set", "agent": "outsider",
        "runs": [{"chat_id": "another-room", "updated": "2026-09-02T00:00:00Z"}],
    })
    mesh.tx.put_doc("presence/outsider@box.json", {
        "last_seen_ns": 123, "online": True,
    })
    after = _parts(collector.collect(chat_id))
    assert after["runtime"] == before["runtime"]
    assert after["presence"] == before["presence"]
    assert after["expiry"] == before["expiry"]


def test_local_key_loss_changes_key_availability_component(projection_mesh):
    mesh, chat_id, _message_id = projection_mesh
    collector = ProjectionInputCollector(mesh, server_generation="generation")
    before = _parts(collector.collect(chat_id))["key_epoch"]
    bundle = mesh.keystore.load(mesh.user)
    assert bundle is not None
    mesh.keystore.forget(mesh.user)
    try:
        after = _parts(collector.collect(chat_id))["key_epoch"]
        assert after != before
    finally:
        mesh.keystore.save(mesh.user, bundle)


def test_key_facts_do_not_read_through_cache_or_upgrade_identity(
        projection_mesh, monkeypatch):
    mesh, chat_id, _message_id = projection_mesh
    docs = mesh.tx.cached_docs_bounded(f"chats/{chat_id}/keys/", 100)
    mesh.keys._cache.clear()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("key observation must be read-only and local")

    monkeypatch.setattr(mesh.tx, "get_doc", forbidden)
    monkeypatch.setattr(mesh.keystore, "save", forbidden)
    monkeypatch.setattr("agentbridge.mesh.keyring.dpapi.available", lambda: True)
    facts = mesh.keys.projection_facts(chat_id, docs)
    assert facts["identity"]
    assert any(value["unwrap"] for value in facts["epochs"].values())
    assert mesh.keys._cache == {}


def test_supplied_frontier_must_be_a_digest(projection_mesh):
    mesh, chat_id, _message_id = projection_mesh
    with pytest.raises(ProjectionVersionError, match="SHA-256 digest"):
        ProjectionInputCollector(
            mesh, server_generation="generation",
            runtime_process_facts={}, replication_frontier_digest="not-a-digest",
        ).collect(chat_id)


def test_clock_crossing_changes_expiry_without_document_write(projection_mesh):
    mesh, chat_id, message_id = projection_mesh
    mesh.pin(chat_id, message_id, hours=1 / 3600)
    collector = ProjectionInputCollector(mesh, server_generation="generation")
    now = mesh.tx.get_doc(P.pin(chat_id, message_id))["ns"]
    before = collector.collect(chat_id, now_ns=now)
    assert before.next_observed_boundary_ns is not None
    after = collector.collect(chat_id, now_ns=before.next_observed_boundary_ns + 1)
    assert _parts(before)["expiry"] != _parts(after)["expiry"]
