"""Authenticated lifecycle composition for the inactive page runtime."""
from __future__ import annotations

import threading

import pytest

from agentbridge.mesh.node_page_owner import LocalNodePageOwner
from agentbridge.node.server import LocalNodeServer
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.local_mutations import (
    LocalMutationTransport, root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient


EPOCH = "12345678-1234-5678-9234-567812345678"
ACCOUNT = "12345678-1234-5678-9234-567812345679"


def _world(tmp_path):
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_source_ledger_version": 2,
        "_scoped_source_version": 1,
        "_recovery_account": ACCOUNT,
        "ab_change_epochs": [{
            "root": "team", "epoch": EPOCH, "minimum_cursor": 0,
            "source_schema_version": 2,
        }],
        "ab_change_events": [],
        "ab_docs": [],
        "ab_logs": [],
    })
    provider = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://Example.TEST:443/",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    coordinator = MutationCoordinator(
        tmp_path / "coordinator", root_identity(provider),
    )
    transport = LocalMutationTransport(provider, coordinator)
    store = Store(tmp_path / "store.sqlite")
    return client, transport, store


def test_owner_pins_authenticated_provider_principal_before_node_open(tmp_path):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    try:
        assert owner.server is not None and owner.runtime is not None
        assert owner.runtime.server is owner.server
        assert owner.server.identity.provider_endpoint == "https://example.test"
        assert owner.server.identity.root == "supabase://team"
        assert owner.server.identity.principal == ACCOUNT
        assert owner.server.identity.machine == "desktop"
        assert owner.server.port > 0
    finally:
        owner.close()
        store.close()

    assert owner.server is None and owner.runtime is None
    owner.close()


def test_failed_authenticated_fence_leaves_no_node_owner(tmp_path):
    client, transport, store = _world(tmp_path)
    client.db["_recovery_denied"] = True

    try:
        with pytest.raises((NotImplementedError, ValueError)):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
        assert not (tmp_path / "home" / "nodes").exists()
    finally:
        store.close()


def test_startup_rechecks_location_after_authenticated_fence(tmp_path,
                                                             monkeypatch):
    _client, transport, store = _world(tmp_path)
    original = transport.recovery_fence

    def drift_after_fence():
        cut = original()
        transport._transport.root = "other"
        transport._transport.cache_key = "supabase:https://Example.TEST:443/:other"
        return cut

    monkeypatch.setattr(transport, "recovery_fence", drift_after_fence)
    try:
        with pytest.raises(ValueError, match="identity changed"):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
        assert not (tmp_path / "home" / "nodes").exists()
    finally:
        store.close()


def test_recovery_rechecks_pinned_location_after_owner_open(tmp_path):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    try:
        transport._transport.root = "other"
        transport._transport.cache_key = "supabase:https://Example.TEST:443/:other"
        with pytest.raises(ValueError, match="identity changed"):
            owner.server.recover_once(attempted_ns=1)
        assert owner.server.store.claimed_recovery_id() is None
        assert owner.server.store.status(
            node_epoch="", started_ns=0,
        ).admitted_generation == 0
    finally:
        owner.close()
        store.close()


def test_interrupt_after_server_start_releases_owner_resources(tmp_path,
                                                               monkeypatch):
    _client, transport, store = _world(tmp_path)
    opened = []
    original = LocalNodeServer.start

    def interrupted(server):
        original(server)
        opened.append(server)
        raise KeyboardInterrupt

    monkeypatch.setattr(LocalNodeServer, "start", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
        assert len(opened) == 1
        assert opened[0].port == 0
        assert opened[0]._owner is None
        assert not opened[0].state_path.exists()
    finally:
        store.close()


def test_concurrent_close_is_idempotent(tmp_path, monkeypatch):
    _client, transport, store = _world(tmp_path)
    owner = LocalNodePageOwner.open(
        tmp_path / "home", transport, store, "desktop",
    )
    server = owner.server
    original = server.stop
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(3)
    errors = []

    def delayed_stop():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("close test did not release stop")
        original()

    def close():
        start.wait()
        try:
            owner.close()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(server, "stop", delayed_stop)
    threads = [threading.Thread(target=close) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    try:
        assert entered.wait(5)
    finally:
        release.set()
    for thread in threads:
        thread.join(5)
    store.close()

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert owner.server is None and owner.runtime is None


def test_owner_rejects_rebound_mutation_coordinator_before_provider_io(
        tmp_path, monkeypatch):
    _client, transport, store = _world(tmp_path)
    transport._coordinator.identity = "changed"
    monkeypatch.setattr(
        transport, "recovery_fence",
        lambda: pytest.fail("provider read crossed changed identity"),
    )

    try:
        with pytest.raises(ValueError, match="identity changed"):
            LocalNodePageOwner.open(
                tmp_path / "home", transport, store, "desktop",
            )
    finally:
        store.close()
