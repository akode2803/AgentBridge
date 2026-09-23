"""Epoch recovery from one admitted local raw-source cut."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from agentbridge import crypto
from agentbridge.mesh import epoch_inputs, local_page_source
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.keyring import ChatKeyService, KeyStore
from agentbridge.mesh.paths import P
from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.source_publication import SourcePublisher
from agentbridge.transport import raw_documents
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.local_mutations import root_identity


CHAT, VIEWER, EPOCH = "local-epoch-room", "aryan", 7
PATH = P.keys(CHAT, EPOCH)


@pytest.fixture
def world(tmp_path):
    provider = FolderTransport(tmp_path / "provider")
    store = Store(tmp_path / "store.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    root = MutationCoordinator(tmp_path / "owner", root_identity(provider))
    root.register_store(store)
    reader = local_page_source.LocalPageSource(root, store, CHAT)
    publisher = SourcePublisher(root, store, reader.definition)
    keystore = KeyStore(tmp_path / "keys")
    bundle = crypto.generate_identity()
    keystore.save(VIEWER, bundle)
    service = ChatKeyService(provider, Directory(provider), keystore, VIEWER)
    key = crypto.new_chat_key()
    _sign, agree = crypto.identity_pubs(bundle)
    valid = {
        "epoch": EPOCH,
        "wrapped": {VIEWER: crypto.wrap_key_for(agree, key)},
    }
    try:
        yield service, provider, store, root, reader, publisher, bundle, key, valid
    finally:
        store.close()


def _publish(world, documents):
    _service, provider, _store, _root, reader, publisher, *_rest = world
    for path in provider.list_docs(""):
        provider.delete_doc(path)
    for path, value in documents.items():
        provider.put_doc(path, value)
    captured = publisher.capture()
    publisher.publish(
        captured, raw_documents.collect_documents(provider, reader.definition),
        observed_ns=1,
    )
    return reader.capture()


def _capture(world, documents, **kwargs):
    service, _provider, _store, _root, reader, _publisher, *_rest = world
    receipt = _publish(world, documents)
    charges = []
    observed = epoch_inputs.capture_epoch(
        service, CHAT, EPOCH, source_reader=reader, receipt=receipt,
        charge_source=charges.append, **kwargs,
    )
    return receipt, observed, charges


@pytest.mark.parametrize(
    ("documents", "shape"),
    [
        ({}, "absent"),
        ({PATH: None}, "document_not_dict"),
        ({PATH: {"wrapped": []}}, "wrapped_not_dict"),
        ({PATH: {"wrapped": {VIEWER: {"eph": "e"}}}}, "invalid_fields"),
    ],
)
def test_missing_and_malformed_local_wraps_are_bounded_missing(
        world, documents, shape):
    service, provider, _store, _root, reader, *_rest = world
    _receipt, observed, charges = _capture(world, documents)
    assert observed.wrap.shape == shape
    assert charges == [observed.wrap.source_bytes]
    provider.get_doc = lambda *_a, **_k: pytest.fail("local epoch read provider")
    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    ) == "missing"


def test_valid_local_wrap_recovers_then_resident_bypasses_source(world):
    service, provider, _store, _root, reader, _publisher, _bundle, key, valid = world
    _receipt, observed, charges = _capture(world, {PATH: valid})
    provider.get_doc = lambda *_a, **_k: pytest.fail("local epoch read provider")
    with epoch_inputs.locked_matching_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    ) as matched:
        assert matched
    assert epoch_inputs.matches_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    )
    publish_charges = []
    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=publish_charges.append,
    ) == "published"
    assert service._cache[(CHAT, EPOCH)] == key
    assert charges == [observed.wrap.source_bytes]
    assert publish_charges == [observed.wrap.source_bytes] * 2

    resident = epoch_inputs.capture_epoch(service, CHAT, EPOCH)
    assert resident.resident == key and resident.wrap is None
    assert epoch_inputs.publish_epoch(service, resident) == "resident"


def test_large_raw_document_charges_full_bytes_not_selected_wrap(world):
    service, _provider, _store, _root, reader, _publisher, _bundle, key, valid = world
    document = dict(valid)
    document["unrelated"] = "x" * 70_000
    _receipt, observed, charges = _capture(world, {PATH: document})
    assert observed.wrap.source_bytes > 64 * 1024
    assert observed.wrap.captured_bytes < 64 * 1024
    final_charges = []
    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=final_charges.append,
    ) == "published"
    assert service._cache[(CHAT, EPOCH)] == key
    assert charges == [observed.wrap.source_bytes]
    assert final_charges == [observed.wrap.source_bytes] * 2


def test_source_mutation_during_unwrap_rejects_cache_publication(world, monkeypatch):
    service, _provider, _store, _root, reader, publisher, _bundle, _key, valid = world
    _receipt, observed, _charges = _capture(world, {PATH: valid})
    original = crypto.unwrap_key_with

    def mutate_then_unwrap(*args, **kwargs):
        changed = dict(valid)
        changed["extra"] = "new source"
        publisher.publish(publisher.capture(), {PATH: changed}, observed_ns=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(crypto, "unwrap_key_with", mutate_then_unwrap)
    with pytest.raises(local_source.SourceChanged, match="local_inputs_changed"):
        epoch_inputs.publish_epoch(
            service, observed, source_reader=reader,
            charge_source=lambda _size: None,
        )
    assert (CHAT, EPOCH) not in service._cache


def test_new_cached_winner_and_identity_change_reject_old_observation(world):
    service, _provider, _store, _root, reader, _publisher, _bundle, _key, valid = world
    _receipt, observed, _charges = _capture(world, {PATH: valid})
    service._cache[(CHAT, EPOCH)] = b"newer-winner"
    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    ) == "conflict"
    assert service._cache[(CHAT, EPOCH)] == b"newer-winner"

    service._cache.clear()
    observed = epoch_inputs.capture_epoch(
        service, CHAT, EPOCH, source_reader=reader, receipt=world[4].capture(),
        charge_source=lambda _size: None,
    )
    service.keystore.save(VIEWER, crypto.generate_identity())
    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    ) == "conflict"
    assert (CHAT, EPOCH) not in service._cache


def test_local_observation_requires_exact_reader_transport_and_root(world, tmp_path):
    service, _provider, store, _root, reader, _publisher, *_rest, valid = world
    receipt, observed, _charges = _capture(world, {PATH: valid})
    with pytest.raises(ValueError, match="source reader"):
        epoch_inputs.publish_epoch(service, observed)
    with pytest.raises(ValueError, match="source reader"):
        epoch_inputs.capture_epoch(
            service, CHAT, EPOCH, receipt=receipt,
            charge_source=lambda _size: None,
        )

    original = service.tx
    service.tx = FolderTransport(tmp_path / "replacement")
    try:
        with pytest.raises(ValueError, match="transport changed|source transport mismatch"):
            epoch_inputs.publish_epoch(
                service, observed, source_reader=reader,
                charge_source=lambda _size: None,
            )
    finally:
        service.tx = original

    wrong_root = MutationCoordinator(tmp_path / "wrong-owner", "wrong-root")
    wrong_root.register_store(store)
    wrong_reader = local_page_source.LocalPageSource(wrong_root, store, CHAT)
    with pytest.raises(ValueError, match="source transport mismatch"):
        epoch_inputs.capture_epoch(
            service, CHAT, EPOCH, source_reader=wrong_reader, receipt=receipt,
            charge_source=lambda _size: None,
        )


def test_dpapi_upgrade_restarts_before_local_cache_publication(world, monkeypatch):
    service, _provider, _store, _root, reader, _publisher, bundle, key, valid = world
    service.keystore._path(VIEWER).write_text(crypto.b64e(bundle), encoding="utf-8")
    _receipt, observed, _charges = _capture(world, {PATH: valid})
    monkeypatch.setattr(epoch_inputs.dpapi, "available", lambda: True)
    monkeypatch.setattr(epoch_inputs.dpapi, "protect", lambda value: b"protected")
    monkeypatch.setattr(epoch_inputs.dpapi, "unprotect", lambda _raw: bundle)

    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    ) == "identity_progress"
    assert (CHAT, EPOCH) not in service._cache
    recaptured = epoch_inputs.capture_epoch(
        service, CHAT, EPOCH, source_reader=reader, receipt=reader.capture(),
        charge_source=lambda _size: None,
    )
    assert epoch_inputs.publish_epoch(
        service, recaptured, source_reader=reader,
        charge_source=lambda _size: None,
    ) == "published"
    assert service._cache[(CHAT, EPOCH)] == key


def test_identity_and_unwrap_work_run_outside_final_sql_interval(world, monkeypatch):
    service, _provider, store, _root, reader, _publisher, _bundle, _key, valid = world
    original_read = epoch_inputs._read_identity
    original_unwrap = crypto.unwrap_key_with

    def assert_store_unlocked(callback, *args):
        probe = sqlite3.connect(store.path, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        finally:
            probe.close()
        return callback(*args)

    monkeypatch.setattr(
        epoch_inputs, "_read_identity",
        lambda *args: assert_store_unlocked(original_read, *args),
    )
    monkeypatch.setattr(
        crypto, "unwrap_key_with",
        lambda *args: assert_store_unlocked(original_unwrap, *args),
    )
    _receipt, observed, _charges = _capture(world, {PATH: valid})
    assert epoch_inputs.publish_epoch(
        service, observed, source_reader=reader,
        charge_source=lambda _size: None,
    ) == "published"


def test_finalization_exit_failure_removes_just_installed_key(world, monkeypatch):
    service, _provider, _store, _root, reader, _publisher, _bundle, _key, valid = world
    _receipt, observed, _charges = _capture(world, {PATH: valid})
    original = reader.finalization

    @contextmanager
    def fail_after_yield(receipt):
        with original(receipt) as conn:
            yield conn
        raise local_source.SourceChanged("exit failure")

    monkeypatch.setattr(reader, "finalization", fail_after_yield)
    with pytest.raises(local_source.SourceChanged, match="exit failure"):
        epoch_inputs.publish_epoch(
            service, observed, source_reader=reader,
            charge_source=lambda _size: None,
        )
    assert (CHAT, EPOCH) not in service._cache
