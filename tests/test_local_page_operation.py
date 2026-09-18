"""Canonical page operations over one admitted direct-folder source."""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from agentbridge import crypto
from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.mesh import local_page_source
from agentbridge.mesh.events import reaction_signing_bytes
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_operation import PageOperation, PageOperationLimits
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_heads, lifecycle_inputs, local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity


COUNT = 24


def _message(ident, ns):
    return {
        "id": ident, "ns": ns, "ts": "2026-01-01T00:00:00Z",
        "from": "aryan", "kind": "message", "epoch": 0,
        "nonce": "", "ct": json.dumps({"body": ident}), "sig": "",
    }


@pytest.fixture(params=[False, True], ids=["plain", "encrypted"])
def world(tmp_path, request):
    encrypt = request.param
    provider = FolderTransport(tmp_path / "provider")
    mesh = Mesh(
        provider, "aryan", "local-page-box", encrypt=encrypt,
        home=tmp_path / "mesh-home",
    )
    try:
        mesh.store.prepare_terminal_observation()
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        mesh.accounts.create_human("aryan", "password")
        chat = mesh.create_chat("Local pages")
        if encrypt:
            for index in range(COUNT):
                mesh.post(chat.id, f"m{index:03d}")
            mesh.outbox.flush_once()
            mesh.sync.sync_once([chat.id])
        else:
            meta = provider.get_doc(P.meta(chat.id))
            base = max(v["joined_ns"] for v in meta["members"].values()) + 1
            mesh.store.upsert_messages(
                chat.id, [_message(f"m{index:03d}", base + index)
                          for index in range(COUNT)],
            )
        root = MutationCoordinator(tmp_path / "owner", root_identity(provider))
        root.register_store(mesh.store)
        reader = local_page_source.LocalPageSource(root, mesh.store, chat.id)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        yield mesh, provider, root, reader, publisher, chat.id, encrypt
    finally:
        mesh.close()


def _target(mesh, chat):
    return f"{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"


def _inputs(world, *, observed_ns=None):
    mesh, provider, _root, reader, publisher, chat, _encrypted = world
    captured = publisher.capture()
    publisher.publish(
        captured, raw_documents.collect_documents(provider, reader.definition),
        observed_ns=time.time_ns() if observed_ns is None else observed_ns,
    )
    receipt = reader.capture()
    mesh.store.refresh_terminal_observation(_target(mesh, chat))
    paths = tuple(provider.list_docs(f"chats/{chat}/overlays/"))
    overlays = mesh.store.capture_selected_documents(
        receipt.source.raw, paths, max_documents=max(1, len(paths)),
        max_bytes=4 * 1024 * 1024,
    )
    index = mesh.store.publish_overlay_index(
        prepare_overlay_index(overlays, chat), shared_source=True,
    )
    return receipt, receipt, index


def _prepare(operation, inputs, mesh, *, attempts=8):
    history = []
    for _ in range(attempts):
        value = operation.prepare(*inputs)
        history.append((value.status, value.reason))
        if value.status == "work" and value.reason == "overlay_proofs":
            for path, public in value.work:
                mesh.store.verify_overlay_signature(inputs[2], path, public)
            continue
        if value.status == "restart":
            continue
        return value, history
    pytest.fail(f"local page did not converge: {history}")


def _page(world, *, limit=10):
    mesh, _provider, _root, reader, _publisher, chat, _encrypted = world
    inputs = _inputs(world)
    prepared, history = _prepare(
        PageOperation(mesh, chat, source_reader=reader, limit=limit),
        inputs, mesh,
    )
    assert prepared.status == "prepared", history
    finalized = prepared.prepared.finalize()
    assert finalized.status == "page", finalized
    return finalized.result.page, history, inputs


def test_plain_and_encrypted_local_pages_match_canonical_visible_bodies(world):
    mesh, provider, _root, _reader, _publisher, chat, encrypted = world
    provider.get_doc = lambda *_a, **_k: pytest.fail("local page read provider")
    page, history, _inputs_value = _page(world, limit=COUNT)
    bodies = [message.body for message in page.messages]
    assert bodies == [f"m{index:03d}" for index in range(COUNT)]
    if encrypted:
        assert all(not message.undecrypted for message in page.messages)


def test_local_encrypted_restart_then_resident_page(world):
    mesh, _provider, _root, reader, _publisher, chat, encrypted = world
    if not encrypted:
        pytest.skip("encrypted-only epoch progress")
    mesh.keys._cache.clear()
    inputs = _inputs(world)
    operation = PageOperation(mesh, chat, source_reader=reader, limit=5)
    first = operation.prepare(*inputs)
    assert (first.status, first.reason) == ("restart", "published")
    assert mesh.keys._cache
    second, history = _prepare(operation, inputs, mesh)
    assert second.status == "prepared", history
    assert second.prepared.finalize().status == "page"


@pytest.mark.parametrize("race", ["source", "message", "key", "membership"])
def test_delayed_local_changes_reject_prepared_finalizer(world, race):
    mesh, provider, _root, reader, publisher, chat, encrypted = world
    if race == "key" and not encrypted:
        pytest.skip("key cache exists only for encrypted page")
    inputs = _inputs(world)
    operation = PageOperation(mesh, chat, source_reader=reader, limit=5)
    prepared, history = _prepare(operation, inputs, mesh)
    assert prepared.status == "prepared", history

    if race == "source":
        state = P.state(chat, mesh.messaging.user)
        provider.put_doc(state, {
            "cleared": {"ns": time.time_ns(), "keep_starred": False},
        })
        captured = publisher.capture()
        publisher.publish(
            captured, raw_documents.collect_documents(provider, reader.definition),
            observed_ns=time.time_ns(),
        )
    elif race == "message":
        mesh.store.upsert_messages(chat, [_message("late", time.time_ns())])
    elif race == "membership":
        mesh.store.upsert_messages(chat, [{
            "id": "late-membership", "ns": time.time_ns(), "from": "aryan",
            "kind": "info", "event": {"type": "member_left"},
            "sig": "invalid-but-position-changing",
        }])
    else:
        epoch = next(iter(mesh.keys._cache))[1]
        mesh.keys._cache[(chat, epoch)] = b"changed-key"

    result = prepared.prepared.finalize()
    assert result.status == "unavailable", result


def test_continuation_rejects_delayed_older_message_instead_of_skipping(world):
    mesh, _provider, _root, reader, _publisher, chat, _encrypted = world
    first, _history, _inputs_value = _page(world, limit=5)
    operation = PageOperation(
        mesh, chat, source_reader=reader, limit=5,
        before=first.oldest_examined, expected_position=first.position,
    )
    mesh.store.upsert_messages(chat, [_message("delayed-old", 1)])
    result = operation.prepare(*_inputs(world))
    assert (result.status, result.reason) == ("unavailable", "continuation_changed")


def test_first_page_raw_work_is_independent_of_unexamined_old_history(world):
    mesh, _provider, _root, _reader, _publisher, chat, _encrypted = world
    first, _history, _inputs_value = _page(world, limit=5)
    baseline = first.raw_examined
    mesh.store.upsert_messages(
        chat, [_message(f"old-{index:04d}", index + 1) for index in range(500)],
    )
    enlarged, _history, _inputs_value = _page(world, limit=5)
    assert enlarged.raw_examined == baseline
    assert [message.id for message in enlarged.messages] == [
        message.id for message in first.messages
    ]


def test_wrong_source_index_reader_and_transport_are_rejected(world, tmp_path):
    mesh, _provider, root, reader, _publisher, chat, _encrypted = world
    receipt, _overlay, index = _inputs(world)
    wrong_index = replace(index, source=replace(index.source, source_id="wrong"))
    result = PageOperation(mesh, chat, source_reader=reader).prepare(
        receipt, receipt, wrong_index,
    )
    assert (result.status, result.reason) == ("unavailable", "invalid_inputs")
    wrong_receipt = replace(receipt, chat_id="other")
    result = PageOperation(mesh, chat, source_reader=reader).prepare(
        wrong_receipt, wrong_receipt, index,
    )
    assert result.status == "unavailable"

    other_store = Store(tmp_path / "other.sqlite")
    try:
        local_source.initialize(other_store)
        source_selectors.initialize(other_store)
        root.register_store(other_store)
        wrong_reader = local_page_source.LocalPageSource(root, other_store, chat)
        with pytest.raises(ValueError, match="local page owner"):
            PageOperation(mesh, chat, source_reader=wrong_reader)
    finally:
        other_store.close()

    original = mesh.tx
    mesh.tx = FolderTransport(tmp_path / "replacement")
    try:
        result = PageOperation(mesh, chat, source_reader=reader).prepare(
            receipt, receipt, index,
        )
        assert result.status == "unavailable"
    finally:
        mesh.tx = original


def test_finalizer_is_one_use_and_identity_stale(world):
    mesh, _provider, _root, reader, _publisher, chat, _encrypted = world
    inputs = _inputs(world)
    operation = PageOperation(mesh, chat, source_reader=reader, limit=5)
    prepared, history = _prepare(operation, inputs, mesh)
    assert prepared.status == "prepared", history
    assert prepared.prepared.finalize().status == "page"
    assert prepared.prepared.finalize().reason == "operation_superseded"

    stale, history = _prepare(operation, inputs, mesh)
    assert stale.status == "prepared", history
    mesh.messaging.user = "changed"
    try:
        result = stale.prepared.finalize()
    finally:
        mesh.messaging.user = "aryan"
    assert (result.status, result.reason) == (
        "unavailable", "operation_superseded",
    )


def test_pending_local_source_never_reads_provider_or_falls_back(world, monkeypatch):
    mesh, provider, root, reader, _publisher, chat, _encrypted = world
    inputs = _inputs(world)
    for name in ("get_doc", "list_docs", "snapshot_docs", "read_log", "list_logs"):
        monkeypatch.setattr(
            provider, name,
            lambda *_a, _name=name, **_k: pytest.fail(
                f"pending local page invoked provider {_name}"
            ),
        )
    intent = root.begin((source_selectors.Selector("doc_exact", P.meta(chat)),))
    try:
        result = PageOperation(mesh, chat, source_reader=reader).prepare(*inputs)
    finally:
        root.complete(intent)
    assert result.status == "unavailable"


def test_local_page_operation_ledger_remains_bounded(world):
    mesh, _provider, _root, reader, _publisher, chat, _encrypted = world
    result = PageOperation(
        mesh, chat, source_reader=reader,
        limits=PageOperationLimits(max_bytes=1),
    ).prepare(*_inputs(world))
    assert result.status == "unavailable"
    assert result.reason in {"operation_byte_budget", "budget_exhausted"}


def test_current_viewer_membership_removal_is_forbidden(world):
    mesh, _provider, _root, reader, _publisher, chat, _encrypted = world
    mesh.membership.leave(chat)
    mesh.sync.sync_once([chat])
    result = PageOperation(mesh, chat, source_reader=reader).prepare(*_inputs(world))
    assert (result.status, result.reason) == ("forbidden", "viewer_not_member")


def test_local_lifecycle_write_triggering_raw_key_change_rolls_back(
        tmp_path, monkeypatch):
    owner, agent, chat_id = "aryan", "claude", "local-lifecycle-page"
    provider = FolderTransport(tmp_path / "provider")
    mesh = Mesh(
        provider, owner, "local-page-box", encrypt=True,
        home=tmp_path / "mesh-home",
    )
    try:
        mesh.store.prepare_terminal_observation()
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        mesh.accounts.create_human(owner, "password")
        mesh.accounts.create_agent(agent)
        snapshot = ChatSnapshot(
            id=chat_id, kind=ChatKind.GROUP, name="Lifecycle page",
            members={owner: Member(Role.ADMIN, 1), agent: Member(Role.MEMBER, 1)},
            materialized_ns=1,
            tenure={owner: [[1, 0]], agent: [[1, 0]]},
        )
        provider.put_doc(P.meta(chat_id), snapshot.to_dict())
        message = mesh.post(chat_id, "signed page body")
        mesh.outbox.flush_once()
        mesh.sync.sync_once([chat_id])
        mapping = {message.id: "✅"}
        ns = message.ns + 1
        provider.put_doc(P.reactions(chat_id, agent), {
            "v": mapping, "ns": ns,
            "sig": crypto.sign(
                mesh.keystore.load(agent),
                reaction_signing_bytes(chat_id, agent, ns, mapping),
            ),
        })
        with mesh.store._conn() as conn:
            conn.execute(
                "DELETE FROM docs WHERE path IN (?,?)",
                (lifecycle_heads.PREFIX + owner, lifecycle_heads.PREFIX + agent),
            )
        root = MutationCoordinator(tmp_path / "owner", root_identity(provider))
        root.register_store(mesh.store)
        reader = local_page_source.LocalPageSource(root, mesh.store, chat_id)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        captured = publisher.capture()
        publisher.publish(
            captured, raw_documents.collect_documents(provider, reader.definition),
            observed_ns=time.time_ns(),
        )
        receipt = reader.capture()
        mesh.store.refresh_terminal_observation(_target(mesh, chat_id))
        overlay_paths = tuple(provider.list_docs(f"chats/{chat_id}/overlays/"))
        overlays = mesh.store.capture_selected_documents(
            receipt.source.raw, overlay_paths,
            max_documents=max(1, len(overlay_paths)), max_bytes=4 * 1024 * 1024,
        )
        index = mesh.store.publish_overlay_index(
            prepare_overlay_index(overlays, chat_id), shared_source=True,
        )
        owner_head = lifecycle_heads.PREFIX + owner
        key_path = next(path for path in provider.list_docs(f"chats/{chat_id}/keys/"))
        before = mesh.store.observe_lifecycle_head(owner)
        published = False
        original_publish = lifecycle_inputs.publish_head_in_transaction

        def publish_then_mark(*args, **kwargs):
            nonlocal published
            result = original_publish(*args, **kwargs)
            published = result
            return result

        monkeypatch.setattr(
            lifecycle_inputs, "publish_head_in_transaction", publish_then_mark,
        )
        with mesh.store._conn() as conn:
            conn.execute(
                "CREATE TRIGGER local_page_after_head AFTER INSERT ON docs "
                f"WHEN NEW.path='{owner_head}' BEGIN "
                "UPDATE document_observation_records SET payload=payload||' ' "
                f"WHERE source_id='{receipt.source.raw.source_id}' "
                f"AND path='{key_path}'; END"
            )

        result = PageOperation(
            mesh, chat_id, source_reader=reader, limit=10,
        ).prepare(receipt, receipt, index)

        assert result.status == "unavailable", result
        assert published
        assert mesh.store.observe_lifecycle_head(owner) == before
        assert mesh.store.observe_lifecycle_head(agent).payload_json is None
        assert reader.capture() == receipt
    finally:
        mesh.close()
