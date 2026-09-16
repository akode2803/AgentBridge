"""R216 bounded epoch observation, matching, and cache-publication tests."""
from __future__ import annotations

import os
import threading

import pytest

from agentbridge import crypto
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.epoch_inputs import (
    EpochInputsUnavailable, capture_epoch, locked_matching_epoch, matches_epoch,
    publish_epoch,
)
from agentbridge.mesh.keyring import ChatKeyService, KeyStore
from agentbridge.mesh.paths import P
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT, VIEWER, EPOCH = "r216-room", "aryan", 7


@pytest.fixture
def epoch_world(tmp_path):
    root = tmp_path / "provider"
    root.mkdir()
    provider = FolderTransport(root)
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror._mirror_root_identity = "r216-root"
    mirror._mirror_cache_identity = "r216-cache"
    mirror.refresh()
    store = KeyStore(tmp_path / "home")
    bundle = crypto.generate_identity()
    store.save(VIEWER, bundle)
    service = ChatKeyService(mirror, Directory(mirror), store, VIEWER)
    key = crypto.new_chat_key()
    _sign, agree = crypto.identity_pubs(bundle)
    path = P.keys(CHAT, EPOCH)
    provider.put_doc(path, {
        "epoch": EPOCH, "wrapped": {VIEWER: crypto.wrap_key_for(agree, key)},
    })
    mirror.refresh()
    yield service, mirror, provider, bundle, key, path
    mirror.close()


def test_resident_capture_bypasses_transport_and_identity(epoch_world, monkeypatch):
    service, _mirror, _provider, _bundle, key, _path = epoch_world
    service._cache[(CHAT, EPOCH)] = key
    monkeypatch.setattr(
        "agentbridge.mesh.epoch_inputs.wraps.capture_key_wrap",
        lambda *a, **k: pytest.fail("resident capture touched transport"),
    )
    monkeypatch.setattr(
        "agentbridge.mesh.epoch_inputs._read_identity",
        lambda *a, **k: pytest.fail("resident capture touched identity"),
    )
    observed = capture_epoch(service, CHAT, EPOCH)
    assert observed.resident == key and observed.wrap is None
    assert publish_epoch(service, observed) == "resident"


def test_cold_capture_publishes_then_resident_survives_wrap_and_identity_removal(
        epoch_world):
    service, mirror, _provider, _bundle, key, path = epoch_world
    observed = capture_epoch(service, CHAT, EPOCH)
    assert observed.resident is None and observed.wrap.shape == "valid"
    assert publish_epoch(service, observed) == "published"
    assert service._cache[(CHAT, EPOCH)] == key

    mirror.delete_doc(path)
    service.keystore.forget(VIEWER)
    resident = capture_epoch(service, CHAT, EPOCH)
    assert resident.resident == key
    assert publish_epoch(service, resident) == "resident"


@pytest.mark.parametrize("mutation", ["wrap", "identity", "cache"])
def test_changed_inputs_deny_cold_publication(epoch_world, mutation):
    service, mirror, _provider, _bundle, _key, path = epoch_world
    observed = capture_epoch(service, CHAT, EPOCH)
    if mutation == "wrap":
        mirror.delete_doc(path)
    elif mutation == "identity":
        service.keystore.save(VIEWER, crypto.generate_identity())
    else:
        service._cache[(CHAT, EPOCH)] = b"concurrent"
    assert not matches_epoch(service, observed)
    assert publish_epoch(service, observed) == "conflict"


def test_missing_negative_and_online_unknown_are_distinct(epoch_world):
    service, mirror, _provider, _bundle, _key, _path = epoch_world
    missing_epoch = EPOCH + 1
    unknown = capture_epoch(service, CHAT, missing_epoch)
    assert unknown.wrap.mode == "online_readthrough_required"
    assert publish_epoch(service, unknown) == "readthrough"
    with mirror._lock:
        mirror._neg.add(P.keys(CHAT, missing_epoch))
    negative = capture_epoch(service, CHAT, missing_epoch)
    assert negative.wrap.mode == "known_negative"
    assert publish_epoch(service, negative) == "missing"


def test_identity_budget_nonregular_and_unicode_are_bounded(epoch_world):
    service, _mirror, _provider, _bundle, _key, _path = epoch_world
    identity = service.keystore._path(VIEWER)
    identity.write_bytes(b"x" * 65536)
    with pytest.raises(EpochInputsUnavailable, match="identity_byte_budget"):
        capture_epoch(service, CHAT, EPOCH)

    identity.write_bytes(b"\xff")
    observed = capture_epoch(service, CHAT, EPOCH)
    with pytest.raises(UnicodeDecodeError):
        publish_epoch(service, observed)

    identity.unlink()
    os.mkfifo(identity)
    with pytest.raises(EpochInputsUnavailable, match="identity_not_regular"):
        capture_epoch(service, CHAT, EPOCH)


def test_capture_never_performs_dpapi_upgrade(epoch_world, monkeypatch):
    service, _mirror, _provider, _bundle, _key, _path = epoch_world
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.available", lambda: True)
    monkeypatch.setattr(
        "agentbridge.mesh.epoch_inputs.dpapi.protect",
        lambda value: pytest.fail("observation attempted DPAPI upgrade"),
    )
    observed = capture_epoch(service, CHAT, EPOCH)
    assert observed.identity is not None and observed.resident is None


def test_dpapi_upgrade_returns_progress_without_mirror_lock_then_next_attempt_publishes(
        epoch_world, monkeypatch):
    service, mirror, _provider, bundle, key, _path = epoch_world
    observed = capture_epoch(service, CHAT, EPOCH)
    calls = []

    def protect(value):
        assert value == bundle
        assert mirror._lock.acquire(blocking=False)
        mirror._lock.release()
        calls.append("protect")
        return b"protected-bundle"

    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.available", lambda: True)
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.protect", protect)
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.unprotect", lambda raw: bundle)

    assert publish_epoch(service, observed) == "identity_progress"
    assert calls == ["protect"]
    assert (CHAT, EPOCH) not in service._cache
    assert service.keystore._path(VIEWER).read_text().startswith(KeyStore._WRAPPED)

    recaptured = capture_epoch(service, CHAT, EPOCH)
    assert publish_epoch(service, recaptured) == "published"
    assert service._cache[(CHAT, EPOCH)] == key


def test_dpapi_upgrade_progress_precedes_failed_wrap_crypto(epoch_world, monkeypatch):
    service, _mirror, _provider, bundle, _key, _path = epoch_world
    observed = capture_epoch(service, CHAT, EPOCH)
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.available", lambda: True)
    monkeypatch.setattr(
        "agentbridge.mesh.epoch_inputs.dpapi.protect", lambda value: b"protected",
    )
    monkeypatch.setattr(
        crypto, "unwrap_key_with",
        lambda *args: pytest.fail("unwrap ran before identity restart"),
    )
    assert observed.identity.raw == crypto.b64e(bundle).encode()
    assert publish_epoch(service, observed) == "identity_progress"


def test_dpapi_protect_none_plain_fallback_unwraps_without_restart(
        epoch_world, monkeypatch):
    service, _mirror, _provider, bundle, key, _path = epoch_world
    before = service.keystore._path(VIEWER).read_bytes()
    observed = capture_epoch(service, CHAT, EPOCH)
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.available", lambda: True)
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.protect", lambda value: None)

    assert publish_epoch(service, observed) == "published"
    assert service.keystore._path(VIEWER).read_bytes() == before
    assert service._cache[(CHAT, EPOCH)] == key
    assert crypto.b64d(before.decode()) == bundle


def test_dpapi_encoded_upgrade_budget_rejects_before_write(epoch_world, monkeypatch):
    service, mirror, _provider, _bundle, _key, _path = epoch_world
    identity = service.keystore._path(VIEWER)
    before = identity.read_bytes()
    observed = capture_epoch(service, CHAT, EPOCH)

    def oversized(_value):
        assert mirror._lock.acquire(blocking=False)
        mirror._lock.release()
        return b"x" * 65536

    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.available", lambda: True)
    monkeypatch.setattr("agentbridge.mesh.epoch_inputs.dpapi.protect", oversized)
    with pytest.raises(EpochInputsUnavailable, match="identity_byte_budget"):
        publish_epoch(service, observed)
    assert identity.read_bytes() == before
    assert (CHAT, EPOCH) not in service._cache


def test_resident_publish_never_attempts_dpapi(epoch_world, monkeypatch):
    service, _mirror, _provider, _bundle, key, _path = epoch_world
    service._cache[(CHAT, EPOCH)] = key
    observed = capture_epoch(service, CHAT, EPOCH)
    monkeypatch.setattr(
        "agentbridge.mesh.epoch_inputs.dpapi.available",
        lambda: pytest.fail("resident path consulted DPAPI"),
    )
    assert publish_epoch(service, observed) == "resident"


def test_locked_match_excludes_other_keystore_instance_writer(epoch_world):
    service, _mirror, _provider, _bundle, _key, _path = epoch_world
    observed = capture_epoch(service, CHAT, EPOCH)
    other = KeyStore(service.keystore.dir.parent)
    entered = threading.Event()
    finished = threading.Event()

    def writer():
        entered.set()
        other.forget(VIEWER)
        finished.set()

    with locked_matching_epoch(service, observed) as matched:
        assert matched
        thread = threading.Thread(target=writer)
        thread.start()
        assert entered.wait(1)
        assert not finished.wait(0.05)
    thread.join(2)
    assert finished.is_set()


def test_live_my_key_does_not_overwrite_concurrent_resident_winner(
        epoch_world, monkeypatch):
    service, _mirror, _provider, _bundle, old_key, _path = epoch_world
    entered = threading.Event()
    release = threading.Event()
    original_unwrap = crypto.unwrap_key_with

    def paused_unwrap(*args, **kwargs):
        recovered = original_unwrap(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return recovered

    monkeypatch.setattr(crypto, "unwrap_key_with", paused_unwrap)
    returned = []
    thread = threading.Thread(
        target=lambda: returned.append(service.my_key(CHAT, EPOCH)),
    )
    thread.start()
    assert entered.wait(2)
    new_key = crypto.new_chat_key()
    with service._cache_lock:
        service._cache[(CHAT, EPOCH)] = new_key
    release.set()
    thread.join(2)

    assert old_key != new_key
    assert returned == [new_key]
    assert service._cache[(CHAT, EPOCH)] == new_key
