"""Membership coordinator reads from one admitted local raw-source cut."""
from __future__ import annotations

import json
import time

import pytest

from agentbridge import crypto
from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.mesh import events, local_page_source
from agentbridge.mesh.membership_coordinator import run_membership_round
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_heads, lifecycle_inputs, local_source, source_selectors
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity


CHAT = "local-coordinator"
OUTER = "claude"


@pytest.fixture
def world(tmp_path):
    provider = FolderTransport(tmp_path / "provider")
    mesh = Mesh(provider, "aryan", "local-box", home=tmp_path / "mesh-home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human("aryan", "aryan-pass")
        mesh.accounts.create_human("fable", "fable-pass")
        chat = mesh.membership.create_chat("Local coordinator", members=["fable"])
        mesh.sync.sync_once([chat.id])
        mesh.store.prepare_membership_suffix_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        root = MutationCoordinator(tmp_path / "owner", root_identity(provider))
        root.register_store(mesh.store)
        reader = local_page_source.LocalPageSource(root, mesh.store, chat.id)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        yield mesh, provider, root, reader, publisher, chat.id
    finally:
        mesh.close()


def _target(mesh, chat):
    return f"{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"


def _publish(world, *, observed_ns=None):
    mesh, provider, _root, reader, publisher, chat = world
    captured = publisher.capture()
    documents = raw_documents.collect_documents(provider, reader.definition)
    publisher.publish(captured, documents,
                      observed_ns=time.time_ns() if observed_ns is None else observed_ns)
    mesh.store.refresh_terminal_observation(_target(mesh, chat))
    return reader.capture()


def _rename(mesh, chat, name="Renamed"):
    event = {
        "id": f"rename-{time.time_ns()}", "ns": time.time_ns(),
        "from": "aryan", "kind": "info",
        "event": {"type": events.EV_RENAMED, "name": name, "by": "aryan"},
    }
    event["sig"] = crypto.sign(
        mesh.keystore.load("aryan"), events.signing_bytes(chat, event),
    )
    mesh.store.upsert_messages(chat, [event])


def _round(world, receipt):
    mesh, _provider, _root, reader, _publisher, _chat = world
    return run_membership_round(mesh, receipt, source_reader=reader)


def test_signed_suffix_matches_canonical_without_provider_or_full_fold(world, monkeypatch):
    mesh, provider, _root, _reader, _publisher, chat = world
    _rename(mesh, chat)
    expected = mesh.messaging.snapshot(chat).to_dict()
    receipt = _publish(world)
    monkeypatch.setattr(
        mesh.messaging, "snapshot",
        lambda *_a, **_k: pytest.fail("local round invoked canonical full fold"),
    )
    for method in ("get_doc", "list_docs", "snapshot_docs"):
        monkeypatch.setattr(
            provider, method,
            lambda *_a, _method=method, **_k: pytest.fail(
                f"local round invoked provider {_method}"
            ),
        )

    result = _round(world, receipt)

    assert result.status == "candidate", result
    assert json.loads(result.candidate.snapshot_json) == expected
    assert result.candidate.receipt == receipt
    assert result.candidate.policy is None


def test_current_signed_member_removal_is_applied(world):
    mesh, _provider, _root, _reader, _publisher, chat = world
    mesh.membership.remove_member(chat, "fable")
    mesh.sync.sync_once([chat])
    receipt = _publish(world)

    result = _round(world, receipt)

    assert result.status == "candidate", result
    assert "fable" not in json.loads(result.candidate.snapshot_json)["members"]


def test_missing_required_account_denies_suffix_event_without_provider_readthrough(world, monkeypatch):
    mesh, provider, _root, _reader, _publisher, chat = world
    _rename(mesh, chat)
    provider.delete_doc(P.user("aryan"))
    receipt = _publish(world)
    monkeypatch.setattr(
        provider, "get_doc",
        lambda *_a, **_k: pytest.fail("local account absence read provider"),
    )

    result = _round(world, receipt)

    assert result.status == "candidate", result
    assert json.loads(result.candidate.snapshot_json)["name"] == "Local coordinator"


def test_local_recursive_lifecycle_publishes_retained_head_then_restarts(tmp_path):
    provider = FolderTransport(tmp_path / "provider")
    mesh = Mesh(provider, "aryan", "local-box", home=tmp_path / "mesh-home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human("aryan", "aryan-pass")
        mesh.accounts.create_agent(OUTER)
        snapshot = ChatSnapshot(
            id=CHAT, kind=ChatKind.GROUP, name="Lifecycle",
            members={"aryan": Member(Role.ADMIN, 1), OUTER: Member(Role.MEMBER, 1)},
            materialized_ns=1,
            tenure={"aryan": [[1, 0]], OUTER: [[1, 0]]},
        )
        provider.put_doc(P.meta(CHAT), snapshot.to_dict())
        event = {
            "id": "remove-agent", "ns": 2, "from": OUTER, "kind": "info",
            "event": {"type": events.EV_MEMBER_REMOVED, "who": OUTER},
        }
        event["sig"] = crypto.sign(
            mesh.keystore.load(OUTER), events.signing_bytes(CHAT, event),
        )
        mesh.store.upsert_messages(CHAT, [event])
        with mesh.store._conn() as conn:
            conn.execute("DELETE FROM docs WHERE path=?", (lifecycle_heads.PREFIX + "aryan",))
        mesh.store.prepare_membership_suffix_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        root = MutationCoordinator(tmp_path / "owner", root_identity(provider))
        root.register_store(mesh.store)
        reader = local_page_source.LocalPageSource(root, mesh.store, CHAT)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        captured = publisher.capture()
        publisher.publish(captured, raw_documents.collect_documents(provider, reader.definition),
                          observed_ns=time.time_ns())
        mesh.store.refresh_terminal_observation(_target(mesh, CHAT))

        result = run_membership_round(mesh, reader.capture(), source_reader=reader)

        assert (result.status, result.reason) == ("restart", "retained_head_progress")
        assert mesh.store.observe_lifecycle_head("aryan").payload_json is not None
        assert mesh.store.observe_lifecycle_head(OUTER).payload_json is None
    finally:
        mesh.close()


@pytest.mark.parametrize(
    ("seam", "reason"),
    [
        ("source", "inputs_unavailable"),
        ("message", "membership_inputs_changed"),
        ("terminal", "terminal_inputs_changed"),
    ],
)
def test_changes_between_capture_and_final_are_rejected(
        world, monkeypatch, seam, reason):
    mesh, _provider, _root, reader, _publisher, chat = world
    receipt = _publish(world)
    from agentbridge.mesh import membership_coordinator as module
    original = module._Round.final

    def mutate_before_final(round_, snapshot, proposal, *, page=None):
        if seam == "source":
            mesh.store.invalidate_document_observation(receipt.source.raw)
        elif seam == "message":
            with mesh.store._conn() as conn:
                conn.execute(
                    "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                    (chat, "late", time.time_ns(), "aryan", "message", '{}'),
                )
        else:
            with mesh.store._conn() as conn:
                conn.execute(
                    "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                    ("append", _target(mesh, chat), "{}", time.time_ns()),
                )
        return original(round_, snapshot, proposal, page=page)

    monkeypatch.setattr(module._Round, "final", mutate_before_final)

    result = _round(world, receipt)

    assert (result.status, result.reason) == ("unavailable", reason)


def test_wrong_transport_root_is_blocked(world, tmp_path):
    mesh, _provider, root, reader, _publisher, _chat = world
    receipt = _publish(world)
    other = FolderTransport(tmp_path / "other-provider")
    original = mesh.tx
    mesh.tx = other
    try:
        result = run_membership_round(mesh, receipt, source_reader=reader)
    finally:
        mesh.tx = original

    assert (result.status, result.reason) == ("unavailable", "invalid_inputs")
    assert root.identity != root_identity(other)


def test_transport_owner_replacement_between_capture_and_final_is_rejected(world, monkeypatch):
    mesh, _provider, _root, reader, _publisher, _chat = world
    receipt = _publish(world)
    from agentbridge.mesh import membership_coordinator as module
    original = module._Round.final
    replacement = FolderTransport(mesh.home / "replacement-provider")

    def replace_before_final(round_, snapshot, proposal, *, page=None):
        previous = mesh.tx
        mesh.tx = replacement
        try:
            return original(round_, snapshot, proposal, page=page)
        finally:
            mesh.tx = previous

    monkeypatch.setattr(module._Round, "final", replace_before_final)
    result = _round(world, receipt)

    assert (result.status, result.reason) == (
        "unavailable", "local_source_owner_changed",
    )


def test_local_lifecycle_postwrite_source_change_rolls_back(tmp_path):
    provider = FolderTransport(tmp_path / "provider")
    mesh = Mesh(provider, "aryan", "local-box", home=tmp_path / "mesh-home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human("aryan", "aryan-pass")
        mesh.accounts.create_agent(OUTER)
        snapshot = ChatSnapshot(
            id=CHAT, kind=ChatKind.GROUP, name="Lifecycle",
            members={"aryan": Member(Role.ADMIN, 1), OUTER: Member(Role.MEMBER, 1)},
            materialized_ns=1,
            tenure={"aryan": [[1, 0]], OUTER: [[1, 0]]},
        )
        provider.put_doc(P.meta(CHAT), snapshot.to_dict())
        event = {
            "id": "remove-agent", "ns": 2, "from": OUTER, "kind": "info",
            "event": {"type": events.EV_MEMBER_REMOVED, "who": OUTER},
        }
        event["sig"] = crypto.sign(
            mesh.keystore.load(OUTER), events.signing_bytes(CHAT, event),
        )
        mesh.store.upsert_messages(CHAT, [event])
        with mesh.store._conn() as conn:
            conn.execute("DELETE FROM docs WHERE path=?", (lifecycle_heads.PREFIX + "aryan",))
        mesh.store.prepare_membership_suffix_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        root = MutationCoordinator(tmp_path / "owner", root_identity(provider))
        root.register_store(mesh.store)
        reader = local_page_source.LocalPageSource(root, mesh.store, CHAT)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        captured = publisher.capture()
        publisher.publish(captured, raw_documents.collect_documents(provider, reader.definition),
                          observed_ns=time.time_ns())
        receipt = reader.capture()
        mesh.store.refresh_terminal_observation(_target(mesh, CHAT))
        head = lifecycle_heads.PREFIX + "aryan"
        source = receipt.source.raw.source_id
        with mesh.store._conn() as conn:
            conn.execute(
                "CREATE TRIGGER local_postwrite AFTER INSERT ON docs "
                f"WHEN NEW.path='{head}' BEGIN "
                "UPDATE document_observation_sources SET generation=generation+1 "
                f"WHERE source_id='{source}'; END"
            )

        result = run_membership_round(mesh, receipt, source_reader=reader)

        assert result.status == "unavailable", result
        assert mesh.store.observe_lifecycle_head("aryan").payload_json is None
    finally:
        mesh.close()


def test_folder_and_honest_cache_sources_produce_same_candidate(tmp_path):
    results = []
    for mode in ("folder", "cache"):
        base = tmp_path / mode
        provider = FolderTransport(base / "provider")
        tx = provider if mode == "folder" else CachingTransport(provider, auto_refresh=False)
        if mode == "cache":
            tx.refresh()
        mesh = Mesh(tx, "aryan", "local-box", home=base / "mesh-home")
        try:
            mesh.store.prepare_terminal_observation()
            mesh.accounts.create_human("aryan", "aryan-pass")
            mesh.accounts.create_human("fable", "fable-pass")
            if mode == "cache":
                tx.refresh()
            chat = mesh.membership.create_chat("Parity", members=["fable"])
            if mode == "cache":
                tx.refresh()
            mesh.sync.sync_once([chat.id])
            mesh.store.prepare_membership_suffix_index()
            lifecycle_inputs.prepare(mesh.store._conn())
            local_source.initialize(mesh.store)
            source_selectors.initialize(mesh.store)
            root = MutationCoordinator(base / "owner", root_identity(tx))
            root.register_store(mesh.store)
            reader = local_page_source.LocalPageSource(root, mesh.store, chat.id)
            publisher = SourcePublisher(root, mesh.store, reader.definition)
            position = publisher.capture()
            publisher.publish(position, raw_documents.collect_documents(tx, reader.definition),
                              observed_ns=time.time_ns())
            mesh.store.refresh_terminal_observation(_target(mesh, chat.id))

            result = run_membership_round(mesh, reader.capture(), source_reader=reader)
            assert result.status == "candidate", result
            value = json.loads(result.candidate.snapshot_json)
            assert value == mesh.messaging.snapshot(chat.id).to_dict()
            results.append((value["name"], tuple(sorted(value["members"]))))
        finally:
            mesh.close()
    assert results[0] == results[1]


def test_cached_folder_owner_is_pinned_and_distinct_between_roots(tmp_path):
    first_inner = FolderTransport(tmp_path / "first")
    second_inner = FolderTransport(tmp_path / "second")
    first = CachingTransport(first_inner, auto_refresh=False)
    second = CachingTransport(second_inner, auto_refresh=False)
    try:
        first.refresh()
        second.refresh()
        first_observed = first.capture_mirror()
        second_observed = second.capture_mirror()
        assert first_observed.root_identity == str(first_inner.root)
        assert second_observed.root_identity == str(second_inner.root)
        assert first_observed.root_identity != second_observed.root_identity
        assert root_identity(first) == root_identity(first_inner)
        first_inner.root = tmp_path / "moved"
        with pytest.raises(ValueError, match="cache identity changed"):
            root_identity(first)
    finally:
        first.close()
        second.close()


def test_unknown_provider_path_identity_is_not_stringified(tmp_path):
    class ExplosivePath:
        def __str__(self):
            raise AssertionError("unknown provider identity was coerced")

    class UnknownFolder(FolderTransport):
        pass

    unknown = UnknownFolder(tmp_path / "unknown")
    unknown.root = ExplosivePath()
    cached = CachingTransport(unknown, auto_refresh=False)
    try:
        assert cached._mirror_root_identity is None
        with pytest.raises(ValueError, match="unsupported nested transport owner"):
            root_identity(cached)
    finally:
        cached.close()
