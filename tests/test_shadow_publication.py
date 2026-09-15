"""One-shot adapter outcomes, stale owners and uncertain commits."""

from dataclasses import replace

import pytest

from agentbridge.mesh.shadow_publication import (
    PublishedShadow, ShadowPublicationUnavailable, publish_shadow_once,
    retire_published_shadow,
)
from agentbridge.store.db import Store
from agentbridge.store.shadow_slot import ShadowConflict, ShadowSource
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


class SnapshotProvider(FolderTransport):
    """In-memory snapshot provider; inherited filesystem methods must not run."""

    def __init__(self):
        self.root = "diagnostic-root"
        self.cache_key = "diagnostic-cache"
        self.docs = {"chats/c/meta.json": {"name": "room"}}
        self.calls = 0

    def snapshot_docs(self):
        self.calls += 1
        return dict(self.docs), 4

    def list_chat_ids(self):
        self.calls += 1
        return ["c"]


@pytest.fixture
def environment(tmp_path):
    provider = SnapshotProvider()
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    store = Store(tmp_path / "viewer.sqlite")
    yield provider, mirror, store
    store.close()


def publish(mirror, store):
    return publish_shadow_once(mirror, store, store.inspect_shadow_position())


def test_real_capture_metadata_parity_and_explicit_retirement(environment, monkeypatch):
    provider, mirror, store = environment
    calls = provider.calls
    captured = mirror.capture_mirror()
    def forbidden(*args, **kwargs):
        raise AssertionError("adapter must not capture chat inputs or inspect fresh position")
    expected = store.inspect_shadow_position()
    monkeypatch.setattr(store, "capture_chat_inputs", forbidden)
    monkeypatch.setattr(store, "inspect_shadow_position", forbidden)
    receipt = publish_shadow_once(mirror, store, expected)
    assert type(receipt) is PublishedShadow
    assert (receipt.revision, receipt.provider_cursor, receipt.provenance) == (
        captured.revision, captured.provider_cursor, captured.provenance)
    observed = store.capture_shadow(receipt.position).snapshot
    assert observed.records == tuple((r.path, r.payload_json) for r in captured.records)
    assert observed.chat_ids == captured.chat_ids
    assert provider.calls == calls
    retired = retire_published_shadow(store, receipt)
    assert retired.publisher_nonce is None and not retired.initialized


def test_cold_and_stricter_slot_budget_do_not_displace_previous(environment):
    provider, mirror, store = environment
    first = publish(mirror, store)
    cold = CachingTransport(provider, auto_refresh=False)
    unavailable = publish_shadow_once(cold, store, first.position)
    assert type(unavailable) is ShadowPublicationUnavailable
    assert (unavailable.phase, unavailable.reason, unavailable.acquired_position) == ("capture", "cold", None)
    cut = mirror.capture_mirror()
    budget = sum(len(s.encode()) for s in (
        cut.root_identity, cut.cache_identity, cut.instance_nonce, *cut.chat_ids,
        *(s for r in cut.records for s in (r.path, r.payload_json))))
    assert type(mirror.capture_mirror(max_bytes=budget)) is type(cut)
    over = publish_shadow_once(mirror, store, first.position, max_bytes=budget)
    assert (over.phase, over.reason, over.acquired_position) == ("preflight", "slot_limit_exceeded", None)
    assert store.inspect_shadow_position() == first.position


def test_same_expected_token_has_one_winner_without_reclaim(environment):
    _, mirror, store = environment
    expected = store.inspect_shadow_position()
    winner = publish_shadow_once(mirror, store, expected)
    loser = publish_shadow_once(mirror, store, expected)
    assert (loser.phase, loser.reason) == ("acquire", "acquire_conflict")
    assert store.inspect_shadow_position() == winner.position


def test_takeover_after_acquisition_fences_publication_and_retirement(environment, monkeypatch):
    _, mirror, store = environment
    first = publish(mirror, store)
    acquire = store.acquire_shadow
    replacement = []
    def displace(*args, **kwargs):
        owned = acquire(*args, **kwargs)
        replacement.append(acquire(owned, "competitor", owned.source))
        return owned
    monkeypatch.setattr(store, "acquire_shadow", displace)
    lost = publish_shadow_once(mirror, store, first.position)
    assert (lost.phase, lost.reason) == ("publish", "publish_conflict")
    assert lost.acquired_position.epoch < replacement[0].epoch
    assert store.inspect_shadow_position() == replacement[0]
    with pytest.raises(ShadowConflict):
        retire_published_shadow(store, first)
    with pytest.raises((ValueError, TypeError)):
        retire_published_shadow(store, lost)


def test_mutation_after_capture_publishes_historical_cut(environment, monkeypatch):
    provider, mirror, store = environment
    capture = mirror.capture_mirror
    old = capture()
    def mutate(**kwargs):
        result = capture(**kwargs)
        provider.docs = {"chats/c/meta.json": {"name": "changed"}}
        mirror.refresh()
        return result
    monkeypatch.setattr(mirror, "capture_mirror", mutate)
    receipt = publish(mirror, store)
    assert receipt.revision == old.revision
    assert capture().revision > receipt.revision
    assert store.capture_shadow(receipt.position).snapshot.records == tuple(
        (r.path, r.payload_json) for r in old.records)


@pytest.mark.parametrize("phase", ["acquire", "publish"])
def test_real_sqlite_busy_has_correct_phase_and_no_retry(environment, monkeypatch, phase):
    _, mirror, store = environment
    locker = Store(store.path)
    store._conn().execute("PRAGMA busy_timeout=1")
    expected = store.inspect_shadow_position()
    acquire = store.acquire_shadow
    acquired = []
    if phase == "acquire":
        locker._conn().execute("BEGIN IMMEDIATE")
    else:
        def lock_after_acquire(*args, **kwargs):
            p = acquire(*args, **kwargs)
            acquired.append(p)
            locker._conn().execute("BEGIN IMMEDIATE")
            return p
        monkeypatch.setattr(store, "acquire_shadow", lock_after_acquire)
    try:
        result = publish_shadow_once(mirror, store, expected)
        assert (result.phase, result.reason) == (phase, "sqlite_unavailable")
        assert result.acquired_position == (acquired[0] if acquired else None)
        assert not store._conn().in_transaction
    finally:
        locker._conn().rollback()
        locker.close()
    assert store.inspect_shadow_position() == (acquired[0] if acquired else expected)


def test_commit_then_error_propagates_without_cleanup_or_readback(environment, monkeypatch):
    _, mirror, store = environment
    expected = store.inspect_shadow_position()
    real_publish = store.publish_shadow
    committed = []
    def commit_then_fail(*args, **kwargs):
        committed.append(real_publish(*args, **kwargs))
        raise RuntimeError("unknown acknowledgement outcome")
    def forbidden(*args, **kwargs):
        raise AssertionError("no speculative cleanup/readback")
    monkeypatch.setattr(store, "publish_shadow", commit_then_fail)
    monkeypatch.setattr(store, "retire_shadow", forbidden)
    monkeypatch.setattr(store, "inspect_shadow_position", forbidden)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        publish_shadow_once(mirror, store, expected)
    assert store.capture_shadow(committed[0]).snapshot is not None


def test_invalid_budget_and_full_token_rejected_before_capture(environment, monkeypatch):
    _, mirror, store = environment
    expected = store.inspect_shadow_position()
    def forbidden(*args, **kwargs):
        raise AssertionError("source access before validation")
    monkeypatch.setattr(mirror, "capture_mirror", forbidden)
    for bad in (replace(expected, epoch=True), replace(expected, incarnation=None)):
        with pytest.raises(ValueError):
            publish_shadow_once(mirror, store, bad)
    with pytest.raises(ValueError):
        publish_shadow_once(mirror, store, expected, max_bytes=True)
    with pytest.raises(ShadowConflict):
        publish_shadow_once(mirror, store, replace(expected, database_path="other"))


def test_new_calls_change_epoch_and_nonce_and_retain_bootstrap(environment, monkeypatch):
    _, mirror, store = environment
    capture = mirror.capture_mirror
    monkeypatch.setattr(mirror, "capture_mirror", lambda **kw: replace(capture(**kw), provenance="bootstrap_unverified"))
    one = publish(mirror, store)
    two = publish(mirror, store)
    assert two.position.epoch > one.position.epoch
    assert two.position.publisher_nonce != one.position.publisher_nonce
    assert two.provenance == "bootstrap_unverified"
    assert two.position.source == ShadowSource(mirror.capture_mirror().root_identity,
                                               mirror.capture_mirror().cache_identity,
                                               mirror.capture_mirror().instance_nonce)


def test_retirement_revalidates_tampered_receipt_initialization(environment):
    _, mirror, store = environment
    receipt = publish(mirror, store)
    uninitialized = store.acquire_shadow(receipt.position, "next", receipt.position.source)
    object.__setattr__(receipt, "position", uninitialized)
    with pytest.raises(ValueError):
        retire_published_shadow(store, receipt)
    assert store.inspect_shadow_position() == uninitialized


def test_unsupported_transport_preserves_existing_slot(environment):
    provider, mirror, store = environment
    receipt = publish(mirror, store)
    result = publish_shadow_once(provider, store, receipt.position)
    assert (result.phase, result.reason) == ("capture", "unsupported")
    assert store.inspect_shadow_position() == receipt.position
