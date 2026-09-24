"""Bounded local raw pin manifest and metadata-only exact message anchors."""
from __future__ import annotations

import pytest

from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.store import document_observation as docs, local_source, page_metadata, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher


CHAT = 'room'


def _pin(index, *, body='pin'):
    return (f'chats/{CHAT}/overlays/pins/m{index:04d}.json',
            {'by': 'alice', 'ns': index + 1, 'body': body})


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    local_source.initialize(store)
    source_selectors.initialize(store)
    store.prepare_page_input_index()
    coordinator = MutationCoordinator(tmp_path / 'home', 'page-metadata-root')
    coordinator.register_store(store)
    reader = LocalPageSource(coordinator, store, CHAT)
    publisher = SourcePublisher(coordinator, store, reader.definition)
    try:
        yield store, coordinator, reader, publisher
    finally:
        store.close()


def _ready(rig, pins):
    store, _coordinator, reader, publisher = rig
    docs_in = {f'chats/{CHAT}/meta.json': {'id': CHAT}, **pins}
    publisher.publish(publisher.capture(), docs_in, observed_ns=1)
    receipt = reader.capture()
    # The complete state/reaction index can be empty; its ready position still
    # binds the same physical source generation as raw pin documents.
    index = store.publish_overlay_index(prepare_overlay_index(
        docs.DocumentObservation(receipt.source.raw, ()), CHAT,
    ), shared_source=True)
    return receipt, index


def test_manifest_complete_empty_and_exact_generation_bound(rig):
    _store, _coordinator, reader, _publisher = rig
    receipt, index = _ready(rig, dict([_pin(1), _pin(3)]))
    manifest = reader.capture_pin_manifest(receipt, index)
    assert manifest.position.overlays == index
    assert manifest.documents.position == receipt.source.raw
    assert [rec.path for rec in manifest.documents.records] == [_pin(1)[0], _pin(3)[0]]
    assert manifest.documents.document(_pin(3)[0]) == _pin(3)[1]
    empty_receipt, empty_index = _ready(rig, {})
    empty = reader.capture_pin_manifest(empty_receipt, empty_index)
    assert empty.documents.records == ()
    assert empty.documents.position == empty_receipt.source.raw


def test_sixty_fifth_pin_rejects_before_any_payload_copy(rig, monkeypatch):
    _store, _coordinator, reader, _publisher = rig
    receipt, index = _ready(rig, dict(_pin(i) for i in range(65)))
    monkeypatch.setattr(docs, '_capture_selected',
                        lambda *_args, **_kwargs: pytest.fail('payload copied'))
    with pytest.raises(page_metadata.PageMetadataUnavailable, match='pin_manifest_exceeds_limit'):
        reader.capture_pin_manifest(receipt, index)


def test_exactly_sixty_four_pins_is_complete(rig):
    _store, _coordinator, reader, _publisher = rig
    receipt, index = _ready(rig, dict(_pin(i) for i in range(64)))
    captured = reader.capture_pin_manifest(receipt, index)
    assert len(captured.documents.records) == 64
    assert captured.documents.records[-1].path == _pin(63)[0]


def test_malformed_pin_path_rejected_even_if_source_row_were_present():
    for name in (None, f'chats/{CHAT}/overlays/pins/bad.txt',
                 f'chats/{CHAT}/overlays/pins/.json',
                 f'chats/{CHAT}/overlays/pins/nested/m1.json'):
        with pytest.raises(page_metadata.PageMetadataUnavailable, match='malformed_pin_path'):
            page_metadata._pin_path(name, CHAT)


def test_oversize_pin_inputs_fail_closed(rig):
    _store, _coordinator, reader, _publisher = rig
    pins = {_pin(1)[0]: _pin(1, body='x' * (1024 * 1024))[1]}
    receipt, index = _ready(rig, pins)
    with pytest.raises(page_metadata.PageMetadataUnavailable, match='pin_manifest_byte_budget'):
        reader.capture_pin_manifest(receipt, index)


def test_late_source_change_and_pending_write_reject_old_receipt(rig):
    _store, coordinator, reader, publisher = rig
    receipt, index = _ready(rig, dict([_pin(1)]))
    intent = coordinator.begin((source_selectors.Selector('doc_prefix',
                                f'chats/{CHAT}/overlays/pins'),))
    try:
        with pytest.raises(local_source.SourceChanged):
            reader.capture_pin_manifest(receipt, index)
        with pytest.raises(local_source.SourceChanged):
            reader.capture_message_anchor(receipt, index, 'old')
    finally:
        coordinator.complete(intent)
    publisher.publish(publisher.capture(), {f'chats/{CHAT}/meta.json': {'id': CHAT}},
                      observed_ns=2)
    with pytest.raises(local_source.SourceChanged):
        reader.capture_pin_manifest(receipt, index)
    with pytest.raises(local_source.SourceChanged):
        reader.capture_message_anchor(receipt, index, 'old')


def test_old_anchor_uses_exact_primary_key_and_never_materializes_body(rig, monkeypatch):
    store, _coordinator, reader, _publisher = rig
    receipt, index = _ready(rig, {})
    records = [dict(id=f'm{i:04d}', ns=i + 1, sender='alice', kind='message',
                    **{'from': 'alice'}, body='x' * 512) for i in range(600)]
    store.upsert_messages(CHAT, records)
    monkeypatch.setattr(docs, '_capture_selected',
                        lambda *_args, **_kwargs: pytest.fail('payload copied'))
    with reader._read(receipt) as (conn, _):
        plan = conn.execute(
            'EXPLAIN QUERY PLAN SELECT ns,sender,id,kind FROM messages '
            'INDEXED BY sqlite_autoindex_messages_1 WHERE chat_id=? AND id=?',
            (CHAT, 'm0000'),
        ).fetchall()
    assert any('sqlite_autoindex_messages_1' in str(row) for row in plan)
    old = reader.capture_message_anchor(receipt, index, 'm0000')
    assert (old.key.ns, old.key.sender, old.key.id) == (1, 'alice', 'm0000')
    assert old.position.messages.chat_id == CHAT and old.position.overlays == index
    assert reader.capture_message_anchor(receipt, index, 'unknown').key is None
    with pytest.raises(ValueError, match='message id'):
        reader.capture_message_anchor(receipt, index, '')
