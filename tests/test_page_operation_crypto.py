"""R217 encrypted bounded page-operation integration tests."""
from __future__ import annotations

import json
import time

import pytest

from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_operation import PageOperation, PageOperationLimits
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_inputs, local_source, source_selectors
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.local_mutations import root_identity


@pytest.fixture
def encrypted_page_world(tmp_path, clouds):
    provider = clouds.bare(tmp_path / "provider")
    mirror = CachingTransport(provider, auto_refresh=False)
    mesh = Mesh(mirror, "aryan", "r217-box", encrypt=True, home=tmp_path / "home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        mesh.accounts.create_human("aryan", "password")
        chat = mesh.create_chat("Encrypted page")
        first = mesh.post(chat.id, "first encrypted")
        second = mesh.post(chat.id, "second encrypted", reply_to={"id": first.id})
        mesh.messaging.edit(chat.id, first.id, "first edited")
        mesh.messaging.react(chat.id, second.id, "✅")
        mesh.outbox.flush_once()
        mirror.refresh()
        mesh.sync.sync_once([chat.id])
        root = MutationCoordinator(tmp_path / "owner", root_identity(mirror))
        root.register_store(mesh.store)
        reader = LocalPageSource(root, mesh.store, chat.id)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        yield mesh, mirror, provider, chat.id, first, second, reader, publisher
    finally:
        mesh.close()


def _target(mesh, chat):
    return f"{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"


def _prepared_inputs(mesh, mirror, reader, publisher, chat):
    mirror.refresh()
    mesh.store.refresh_terminal_observation(_target(mesh, chat))
    # Collection and admission are fixture ingestion work, outside PageOperation.
    captured = publisher.capture()
    documents = raw_documents.collect_documents(mirror, reader.definition)
    publisher.publish(captured, documents, observed_ns=time.time_ns())
    receipt = reader.capture()
    paths = tuple(path for path in documents if path.startswith(f"chats/{chat}/overlays/"))
    observed = mesh.store.capture_selected_documents(
        receipt.source.raw, paths, max_documents=max(1, len(paths)),
        max_bytes=4 * 1024 * 1024,
    )
    index = mesh.store.publish_overlay_index(
        prepare_overlay_index(observed, chat), shared_source=True,
    )
    return receipt, receipt, index


def _advance_work(operation, mesh, mirror, reader, publisher, chat, *, attempts=12):
    history = []
    authority, overlay, index = _prepared_inputs(mesh, mirror, reader, publisher, chat)
    for _ in range(attempts):
        value = operation.prepare(authority, overlay, index)
        history.append((value.status, value.reason))
        if value.status == "work" and value.reason == "overlay_proofs":
            for path, pub in value.work:
                mesh.store.verify_overlay_signature(index, path, pub)
            continue
        if value.status == "prepared":
            return value, history
        if value.status not in ("restart",):
            return value, history
        # Lifecycle and epoch progress restart this operation over the same
        # admitted source; its cumulative work ledger must survive each round.
    pytest.fail(f"page operation did not converge: {history}")


def test_cold_epoch_progress_restarts_then_encrypted_page_matches_live_messages(
        encrypted_page_world):
    mesh, mirror, _provider, chat, first, second, reader, publisher = encrypted_page_world
    mesh.keys._cache.clear()
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)

    prepared, history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert ("restart", "published") in history
    assert prepared.status == "prepared", history
    finalized = prepared.prepared.finalize()
    assert finalized.status == "page", finalized
    page = finalized.result.page
    selected = [message for message in page.messages if message.id in (first.id, second.id)]
    assert [message.id for message in selected] == [first.id, second.id]
    assert [message.body for message in selected] == [
        "first edited", "second encrypted",
    ]
    assert selected[1].reply_to["id"] == first.id
    assert selected[1].reactions == {"✅": ["aryan"]}
    assert all(not message.undecrypted for message in selected)


def test_prepare_and_finalize_do_not_call_live_fullfold_mirror_or_provider(
        encrypted_page_world, monkeypatch):
    mesh, mirror, provider, chat, _first, _second, reader, publisher = encrypted_page_world
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    # Warm pins and retain lifecycle/key progress before forbidding foreground
    # live paths.
    prepared, _history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", _history
    authority, overlay, index = _prepared_inputs(mesh, mirror, reader, publisher, chat)
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    monkeypatch.setattr(
        mesh.messaging, "messages_for",
        lambda *a, **k: pytest.fail("page operation invoked canonical full fold"),
    )
    for transport in (mirror, provider):
        for name in ("get_doc", "list_docs", "snapshot_docs", "read_log", "list_logs",
                     "capture_mirror", "capture_mirror_selection", "validate_mirror_position"):
            monkeypatch.setattr(
                transport, name,
                lambda *a, **k: pytest.fail("page operation invoked mirror/provider"),
            )
    value = operation.prepare(authority, overlay, index)
    assert (value.status, value.reason) == ("work", "overlay_proofs")
    for path, pub in value.work:
        mesh.store.verify_overlay_signature(index, path, pub)
    value = operation.prepare(authority, overlay, index)
    assert value.status == "prepared", value
    assert value.prepared.finalize().status == "page"


def test_final_epoch_cache_race_rejects_prepared_page(encrypted_page_world):
    mesh, mirror, _provider, chat, _first, _second, reader, publisher = encrypted_page_world
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    prepared, _history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", _history
    epoch = next(iter(mesh.keys._cache))[1]
    mesh.keys._cache[(chat, epoch)] = b"changed-after-prepare"

    final = prepared.prepared.finalize()
    assert final.status == "unavailable"


@pytest.mark.parametrize("race", ["pins", "source"])
def test_final_pin_or_source_race_rejects_prepared_page(
        encrypted_page_world, race):
    mesh, mirror, _provider, chat, _first, _second, reader, publisher = encrypted_page_world
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    prepared, history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", history
    if race == "pins":
        mesh.key_pins.forget("aryan")
    else:
        mirror.put_doc(P.state(chat, mesh.user), {"read_ns": 2**60})
        _prepared_inputs(mesh, mirror, reader, publisher, chat)

    final = prepared.prepared.finalize()
    assert final.status == ("restart" if race == "source" else "unavailable")
    assert final.result is None
    if race == "source":
        assert final.reason == "local_inputs_changed"


def test_unrelated_mirror_mutation_keeps_admitted_page_readable(encrypted_page_world):
    mesh, mirror, _provider, chat, first, second, reader, publisher = encrypted_page_world
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    prepared, history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", history
    admitted = reader.capture()

    mirror.put_doc("unrelated/r217.json", {"changed": True})

    assert reader.capture() == admitted
    final = prepared.prepared.finalize()
    assert final.status == "page", final
    selected = [m for m in final.result.page.messages if m.id in (first.id, second.id)]
    assert [m.body for m in selected] == ["first edited", "second encrypted"]
    assert all(not m.undecrypted for m in selected)


def test_epoch_budget_is_explicit_unavailable_not_undecrypted(encrypted_page_world):
    mesh, mirror, _provider, chat, _first, _second, reader, publisher = encrypted_page_world
    operation = PageOperation(
        mesh, chat, source_reader=reader, limit=10,
        limits=PageOperationLimits(max_epochs=0),
    )
    value, history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert value.status == "unavailable"
    assert value.reason == "operation_epoch_budget"
    assert all(status != "prepared" for status, _reason in history)


def test_signed_redaction_clear_keep_starred_and_offpage_parent_are_canonical(
        encrypted_page_world):
    mesh, mirror, _provider, chat, first, second, reader, publisher = encrypted_page_world
    mesh.messaging.redact(chat, [first.id])
    mesh.messaging.star(chat, [second.id])
    mesh.messaging.clear_chat(chat, keep_starred=True)
    mirror.refresh()

    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    prepared, history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", history
    final = prepared.prepared.finalize()
    assert final.status == "page", final
    visible = [m for m in final.result.page.messages if m.id in (first.id, second.id)]
    assert [m.id for m in visible] == [second.id]
    assert visible[0].body == "second encrypted"
    assert visible[0].reactions == {"✅": ["aryan"]}
    assert visible[0].reply_to == {"id": first.id, "deleted": True}
    assert final.result.presentation.starred == (second.id,)


def test_invalid_viewer_signature_cannot_surface_page_presentation(encrypted_page_world):
    mesh, mirror, _provider, chat, first, _second, reader, publisher = encrypted_page_world
    mesh.messaging.star(chat, [first.id])
    mesh.messaging.set_chat_flag(chat, "archived", True)
    valid = PageOperation(mesh, chat, source_reader=reader, limit=10)
    prepared, history = _advance_work(valid, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", history
    result = prepared.prepared.finalize()
    assert result.status == "page", result
    assert result.result.presentation.starred == (first.id,)
    assert json.loads(result.result.presentation.viewer_state_json)["archived"] is True

    tampered = mirror.get_doc(P.state(chat, mesh.user))
    tampered["read_ns"] = 2**60
    tampered["starred"] = [first.id]
    mirror.put_doc(P.state(chat, mesh.user), tampered)
    operation = PageOperation(mesh, chat, source_reader=reader, limit=10)
    prepared, history = _advance_work(operation, mesh, mirror, reader, publisher, chat)
    assert prepared.status == "prepared", history
    result = prepared.prepared.finalize()
    assert result.status == "page", result
    assert result.result.presentation.starred == ()
    assert json.loads(result.result.presentation.viewer_state_json) == {
        "read_ns": 0, "archived": False, "mute": False,
    }
