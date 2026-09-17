"""Admitted local raw-input owner for future canonical page requests."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentbridge.mesh import local_page_source
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.store import lifecycle_inputs, local_source, source_selectors
from agentbridge.store.db import DocumentObservationConflict, Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


CHAT = "room"
META = f"chats/{CHAT}/meta.json"
STATE = f"chats/{CHAT}/overlays/state/alice.json"
USER = "users/alice.json"
LIFECYCLE = "lifecycle/alice/0001.json"
S = source_selectors.Selector


def _documents(value=1):
    return {
        META: {"id": CHAT, "members": ["alice"], "value": value},
        USER: {"name": "alice", "value": value},
        LIFECYCLE: {"id": "life-1", "subject": "alice", "value": value},
        STATE: {"ns": value, "hidden": [], "starred": []},
    }


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    lifecycle_inputs.prepare(store._conn())
    store.prepare_page_input_index()
    root = MutationCoordinator(tmp_path / "home", "local-page-root")
    root.register_store(store)
    source = local_page_source.LocalPageSource(root, store, CHAT)
    publisher = SourcePublisher(root, store, source.definition)
    try:
        yield store, root, source, publisher
    finally:
        store.close()


def _publish(rig, documents=None, *, observed_ns=1):
    _store, _root, _source, publisher = rig
    return publisher.publish(
        publisher.capture(),
        _documents() if documents is None else documents,
        observed_ns=observed_ns,
    )


def test_foreground_capture_requires_existing_registration_and_never_repairs(rig):
    store, _root, source, _publisher = rig
    conn = store._conn()
    before = (
        conn.execute("SELECT count(*) FROM local_source_definitions").fetchone()[0],
        conn.execute("SELECT count(*) FROM local_source_selectors").fetchone()[0],
        local_source.capture(store, source.definition.source),
    )
    with pytest.raises(local_source.SourceChanged, match="source_not_registered"):
        source.capture()
    after = (
        conn.execute("SELECT count(*) FROM local_source_definitions").fetchone()[0],
        conn.execute("SELECT count(*) FROM local_source_selectors").fetchone()[0],
        local_source.capture(store, source.definition.source),
    )
    assert after == before


def test_authority_capture_is_exact_and_absence_is_explicit(rig):
    _store, _root, source, _publisher = rig
    _publish(rig)
    receipt = source.capture()
    captured = source.capture_authority(receipt, ("alice", "missing"))
    assert captured.receipt == receipt
    assert captured.documents.decoded_records() == [
        (META, _documents()[META], False),
        (USER, _documents()[USER], False),
        ("users/missing.json", None, True),
    ]

    _publish(rig, {}, observed_ns=2)
    empty_receipt = source.capture()
    empty = source.capture_authority(empty_receipt, ("alice",))
    assert empty.documents.decoded_records() == [
        (META, None, True),
        (USER, None, True),
    ]


def test_out_of_scope_or_invalid_authority_selection_cannot_fake_absence(rig):
    _store, _root, source, publisher = rig
    captured = publisher.capture()
    with pytest.raises(ValueError, match="outside declared source scope"):
        publisher.publish(
            captured, {"runtime/session.json": {}}, observed_ns=1,
        )
    assert not local_source.capture(
        source.store, source.definition.source,
    ).ready

    _publish(rig)
    receipt = source.capture()
    for names in (("../secret",), ("alice", "alice"), tuple(str(i) for i in range(129))):
        with pytest.raises(ValueError, match="authority account selection|invalid authority"):
            source.capture_authority(receipt, names)


def test_unknown_overlay_namespace_is_outside_complete_source_scope(rig):
    _store, _root, _source, publisher = rig
    captured = publisher.capture()
    with pytest.raises(ValueError, match="outside declared source scope"):
        publisher.publish(
            captured,
            {f"chats/{CHAT}/overlays/future/alice.json": {"value": 1}},
            observed_ns=1,
        )


def test_lifecycle_subject_capture_and_budgets_use_same_raw_position(rig):
    _store, _root, source, _publisher = rig
    ready = _publish(rig)
    receipt = source.capture()
    selected = source.capture_subject(receipt, "alice")
    assert selected.position == ready.raw
    assert [record.path for record in selected.records] == [LIFECYCLE]
    assert selected.records[0].decoded() == _documents()[LIFECYCLE]
    missing = source.capture_subject(receipt, "missing")
    assert missing.records == ()
    with pytest.raises(OverflowError, match="byte budget"):
        source.capture_subject(receipt, "alice", max_bytes=1)
    with pytest.raises(ValueError, match="budget"):
        source.capture_subject(receipt, "alice", max_records=257)


def test_page_capture_binds_overlay_index_chat_and_shared_raw_source(rig):
    store, _root, source, _publisher = rig
    ready = _publish(rig)
    receipt = source.capture()
    overlays = store.capture_selected_documents(
        ready.raw, (STATE,), max_documents=1, max_bytes=4096,
    )
    index = store.publish_overlay_index(
        prepare_overlay_index(overlays, CHAT), shared_source=True,
    )
    store.upsert_messages(CHAT, [{
        "id": "m1", "ns": 1, "from": "alice", "kind": "message", "body": "hi",
    }])
    page = source.capture_page(receipt, index, raw_limit=1)
    assert page.position.overlays.source == ready.raw
    assert [row.key.id for row in page.rows] == ["m1"]

    with pytest.raises(ValueError, match="another local source"):
        source.capture_page(receipt, replace(index, chat_id="other"), raw_limit=1)
    with pytest.raises(ValueError, match="another local source"):
        source.capture_page(
            receipt,
            replace(index, source=replace(index.source, source_id="other")),
            raw_limit=1,
        )


@pytest.mark.parametrize("field", ["chat", "path", "epoch", "source", "store"])
def test_receipt_rejects_wrong_chat_root_store_epoch_and_source(
        rig, tmp_path, field):
    store, root, source, _publisher = rig
    _publish(rig)
    receipt = source.capture()
    target = source
    forged = receipt
    other = None
    if field == "chat":
        forged = replace(receipt, chat_id="other")
    elif field == "path":
        forged = replace(receipt, coordinator_path=str(tmp_path / "other.sqlite"))
    elif field == "epoch":
        forged = replace(receipt, coordinator_epoch="0" * 32)
    elif field == "source":
        forged = replace(
            receipt,
            source=replace(receipt.source, raw=replace(
                receipt.source.raw, source_id="other-source",
            )),
        )
    else:
        other = Store(tmp_path / "other-store.sqlite")
        local_source.initialize(other)
        source_selectors.initialize(other)
        root.register_store(other)
        target = local_page_source.LocalPageSource(root, other, CHAT)
    try:
        with pytest.raises((
            ValueError, local_source.SourceChanged, DocumentObservationConflict,
        )):
            target.capture_authority(forged)
    finally:
        if other is not None:
            other.close()
    assert local_source.capture(store, source.definition.source).ready


def test_failed_changed_and_identical_publications_fence_receipts(rig):
    _store, _root, source, publisher = rig
    first = _publish(rig)
    old = source.capture()

    with pytest.raises(ValueError, match="outside declared source scope"):
        publisher.publish(
            publisher.capture(), {"outside/doc.json": {}}, observed_ns=2,
        )
    with pytest.raises(local_source.SourceChanged):
        source.capture_authority(old)

    changed = _publish(rig, _documents(2), observed_ns=3)
    changed_receipt = source.capture()
    assert changed.raw.generation == first.raw.generation + 1
    with pytest.raises(local_source.SourceChanged):
        source.capture_authority(old)

    identical = _publish(rig, _documents(2), observed_ns=4)
    newest = source.capture()
    assert identical.raw == changed.raw
    with pytest.raises(local_source.SourceChanged, match="local_inputs_changed"):
        source.capture_authority(changed_receipt)
    assert source.capture_authority(newest).documents.document(META) == _documents(2)[META]


def test_pending_local_mutation_and_finalization_reject_changed_owner(rig):
    store, root, source, _publisher = rig
    _publish(rig)
    receipt = source.capture()
    intent = root.begin((S("doc_exact", META),))
    try:
        with pytest.raises(local_source.SourceChanged):
            source.capture_authority(receipt)
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            with source.finalization(receipt):
                pytest.fail("pending mutation reached finalization")
    finally:
        root.complete(intent)
    assert not local_source.capture(store, source.definition.source).ready


def test_capture_paths_have_no_transport_or_provider_dependency(rig):
    _store, _root, source, _publisher = rig
    _publish(rig)
    receipt = source.capture()

    class ForbiddenTransport:
        def __getattribute__(self, name):
            raise AssertionError(f"foreground transport access: {name}")

    forbidden = ForbiddenTransport()
    assert forbidden is not source  # No transport is accepted or retained by this owner.
    assert source.capture_authority(receipt, ("alice",)).documents.document(USER) == _documents()[USER]
    assert source.capture_subject(receipt, "alice").records[0].path == LIFECYCLE
