"""Lifecycle progress discovered only while assembling a bounded page."""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from agentbridge import crypto
from agentbridge.mesh.events import reaction_signing_bytes
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.page_operation import PageOperation, PageOperationLimits
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_heads, lifecycle_inputs, local_source, source_selectors
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity


OWNER = "aryan"
AGENT = "claude"


@pytest.fixture
def page_lifecycle_world(tmp_path):
    provider = FolderTransport(tmp_path / "provider")
    provider.cache_key = "r217-page-lifecycle-cache"
    mirror = CachingTransport(provider, auto_refresh=False)
    mesh = Mesh(mirror, OWNER, "r217-page-box", encrypt=True, home=tmp_path / "home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.store.prepare_membership_suffix_index()
        mesh.store.prepare_page_input_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        local_source.initialize(mesh.store)
        source_selectors.initialize(mesh.store)
        mesh.accounts.create_human(OWNER, "password")
        mesh.accounts.create_agent(AGENT)
        chat = mesh.create_chat("Page lifecycle", members=[AGENT])
        message = mesh.post(chat.id, "signed page body")
        mesh.outbox.flush_once()
        mirror.refresh()
        mesh.sync.sync_once([chat.id])
        materialized = mirror.get_doc(P.meta(chat.id))["materialized_ns"]
        assert mesh.store.capture_membership_suffix(chat.id, materialized).rows == ()

        # The reaction is the first page-time need for the agent's signing key.
        # Membership is already fully materialized, so advancing its empty
        # suffix does not resolve either lifecycle subject.
        mapping = {message.id: "✅"}
        ns = message.ns + 1
        mirror.put_doc(P.reactions(chat.id, AGENT), {
            "v": mapping,
            "ns": ns,
            "sig": crypto.sign(
                mesh.keystore.load(AGENT),
                reaction_signing_bytes(chat.id, AGENT, ns, mapping),
            ),
        })
        mirror.refresh()

        # Force the agent evaluation to stop at its owner's first postorder
        # retained-head proposal. Account creation populated both rows.
        with mesh.store._conn() as conn:
            conn.execute(
                "DELETE FROM docs WHERE path IN (?,?)",
                (lifecycle_heads.PREFIX + OWNER, lifecycle_heads.PREFIX + AGENT),
            )
        root = MutationCoordinator(tmp_path / "owner", root_identity(mirror))
        root.register_store(mesh.store)
        reader = LocalPageSource(root, mesh.store, chat.id)
        publisher = SourcePublisher(root, mesh.store, reader.definition)
        yield mesh, mirror, chat.id, reader, publisher
    finally:
        mesh.close()


def _inputs(mesh, mirror, reader, publisher, chat):
    target = f"{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"
    mesh.store.refresh_terminal_observation(target)
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


def test_page_actor_lifecycle_progress_restarts_without_returning_page(
        page_lifecycle_world):
    mesh, mirror, chat, reader, publisher = page_lifecycle_world
    inputs = _inputs(mesh, mirror, reader, publisher, chat)
    result = PageOperation(mesh, chat, source_reader=reader, limit=10).prepare(*inputs)

    assert (result.status, result.reason) == ("restart", "retained_head_progress")
    assert result.prepared is None and result.result is None
    assert mesh.store.observe_lifecycle_head(OWNER).payload_json is not None
    assert mesh.store.observe_lifecycle_head(AGENT).payload_json is None


def test_page_input_mutation_after_lifecycle_cas_rolls_back(
        page_lifecycle_world, monkeypatch):
    mesh, mirror, chat, reader, publisher = page_lifecycle_world
    inputs = _inputs(mesh, mirror, reader, publisher, chat)
    source = inputs[2].source.source_id
    owner_head = lifecycle_heads.PREFIX + OWNER
    before = mesh.store.observe_lifecycle_head(OWNER)
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
            "CREATE TRIGGER r217_page_after_head AFTER INSERT ON docs "
            f"WHEN NEW.path='{owner_head}' BEGIN "
            "INSERT INTO overlay_index_proofs(source,path,pub,valid) VALUES"
            f"('{source}','triggered/path','triggered-key',1); END"
        )

    result = PageOperation(mesh, chat, source_reader=reader, limit=10).prepare(*inputs)

    assert result.status == "unavailable", result
    assert published
    assert mesh.store.observe_lifecycle_head(OWNER) == before
    assert mesh.store.observe_lifecycle_head(AGENT).payload_json is None
    with mesh.store._conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM overlay_index_proofs WHERE source=? AND path='triggered/path'",
            (source,),
        ).fetchone() is None


def test_operation_round_budget_survives_lifecycle_progress(page_lifecycle_world):
    mesh, mirror, chat, reader, publisher = page_lifecycle_world
    inputs = _inputs(mesh, mirror, reader, publisher, chat)
    operation = PageOperation(
        mesh, chat, source_reader=reader, limit=10,
        limits=replace(PageOperationLimits(), max_rounds=1),
    )

    first = operation.prepare(*inputs)
    assert (first.status, first.reason) == ("restart", "retained_head_progress")
    second = operation.prepare(*inputs)
    assert (second.status, second.reason) == ("unavailable", "operation_round_budget")
