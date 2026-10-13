"""Strict read-only wire for the inactive provider mutation protocol."""

from __future__ import annotations

import copy

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.mutation_protocol import (
    DATABASE_SEAL_SCOPE,
    ProviderDatabaseSeal,
    ProviderMutationProtocolState,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

ACCOUNT = "12345678-1234-5678-9234-567812345679"
OLD_EPOCH = "12345678-1234-5678-9234-567812345670"
EPOCH = "12345678-1234-5678-9234-567812345671"
SOURCE_EPOCH = "12345678-1234-5678-9234-567812345672"
SEAL_ID = "12345678-1234-5678-9234-567812345673"


def _legacy_state() -> dict:
    return {
        "schema_version": 1,
        "account_id": ACCOUNT,
        "role": "authenticated",
        "root": "team",
        "protocol_epoch": OLD_EPOCH,
        "legacy_writes_enabled": True,
        "database_seal": None,
        "blob_writes_sealed": False,
    }


def _sealed_state() -> dict:
    state = _legacy_state()
    state.update({
        "protocol_epoch": EPOCH,
        "legacy_writes_enabled": False,
        "database_seal": {
            "seal_id": SEAL_ID,
            "plan_digest": "a" * 64,
            "sealed_from_epoch": OLD_EPOCH,
            "protocol_epoch": EPOCH,
            "source_epoch": SOURCE_EPOCH,
            "minimum_cursor": 4,
            "cursor": 9,
            "seal_scope": DATABASE_SEAL_SCOPE,
        },
    })
    return state


def _rotated_state() -> dict:
    state = _sealed_state()
    state.update({
        "schema_version": 2,
        "protocol_epoch": "12345678-1234-5678-9234-567812345674",
    })
    return state


@pytest.fixture
def tx():
    client = FakeClient()
    client.db.update({
        "_mutation_protocol_version": 1,
        "_mutation_protocol_state": _legacy_state(),
    })
    return SupabaseTransport(
        "team",
        env={"SUPABASE_URL": "https://x.test"},
        client=client,
    )


def test_legacy_and_database_sealed_states_are_strict_value_objects(tx):
    assert tx.supports_mutation_protocol is True
    legacy = tx.mutation_protocol_state()
    assert legacy == ProviderMutationProtocolState(
        1, ACCOUNT, "authenticated", "team", OLD_EPOCH, True, None, False,
    )

    tx._client.db["_mutation_protocol_state"] = _sealed_state()
    sealed = tx.mutation_protocol_state()
    assert sealed == ProviderMutationProtocolState(
        1, ACCOUNT, "authenticated", "team", EPOCH, False,
        ProviderDatabaseSeal(
            SEAL_ID, "a" * 64, OLD_EPOCH, EPOCH, SOURCE_EPOCH,
            4, 9, DATABASE_SEAL_SCOPE,
        ),
        False,
    )

    tx._client.db["_mutation_protocol_state"] = _rotated_state()
    rotated = tx.mutation_protocol_state()
    assert rotated.schema_version == 2
    assert rotated.protocol_epoch != rotated.database_seal.protocol_epoch


def test_capability_is_cached_but_each_state_read_is_fresh(tx, monkeypatch):
    original = tx._client._rpc_locked
    calls = []

    def counted(fn, params):
        calls.append(fn)
        return original(fn, params)

    monkeypatch.setattr(tx._client, "_rpc_locked", counted)
    assert tx.supports_mutation_protocol is True
    tx._client.db["_mutation_protocol_state"] = _sealed_state()
    assert tx.supports_mutation_protocol is True
    assert tx.mutation_protocol_state().protocol_epoch == EPOCH
    assert calls == ["ab_mutation_protocol_ready", "ab_mutation_protocol_state"]


def test_missing_or_non_member_capability_is_unavailable(tx):
    tx._client.db["_mutation_protocol_version"] = None
    tx._mutation_protocol_ready = None
    assert tx.supports_mutation_protocol is False
    with pytest.raises(NotImplementedError, match="mutation protocol"):
        tx.mutation_protocol_state()

    tx._client.db["_mutation_protocol_version"] = 1
    tx._client.db["_mutation_protocol_role"] = "service_role"
    tx._mutation_protocol_ready = None
    tx._mutation_protocol_reprobe = 0
    assert tx.supports_mutation_protocol is False


@pytest.mark.parametrize("mutation", [
    lambda state: state.update({"future": True}),
    lambda state: state.pop("blob_writes_sealed"),
    lambda state: state.update({"root": "other"}),
    lambda state: state.update({"account_id": "not-a-uuid"}),
    lambda state: state.update({"role": "service_role"}),
    lambda state: state.update({"schema_version": True}),
    lambda state: state.update({"schema_version": 3}),
    lambda state: state.update({"blob_writes_sealed": True}),
    lambda state: state.update({"legacy_writes_enabled": False}),
    lambda state: state.update({"database_seal": {"seal": True}}),
])
def test_state_wire_rejects_malformed_legacy_state(tx, mutation):
    state = _legacy_state()
    mutation(state)
    tx._client.db["_mutation_protocol_state"] = state
    with pytest.raises(TransportError, match="invalid mutation protocol state"):
        tx.mutation_protocol_state()


@pytest.mark.parametrize("mutation", [
    lambda seal: seal.update({"future": True}),
    lambda seal: seal.update({"seal_scope": "all-v1"}),
    lambda seal: seal.update({"protocol_epoch": OLD_EPOCH}),
    lambda seal: seal.update({"sealed_from_epoch": EPOCH}),
    lambda seal: seal.update({"plan_digest": "A" * 64}),
    lambda seal: seal.update({"minimum_cursor": 10}),
])
def test_state_wire_rejects_malformed_database_seal(tx, mutation):
    state = _sealed_state()
    mutation(state["database_seal"])
    tx._client.db["_mutation_protocol_state"] = state
    with pytest.raises(TransportError, match="invalid mutation protocol state"):
        tx.mutation_protocol_state()


def test_state_wire_versions_bind_seal_epoch_to_current_generation(tx):
    v1 = _sealed_state()
    v1["protocol_epoch"] = "12345678-1234-5678-9234-567812345674"
    tx._client.db["_mutation_protocol_state"] = v1
    with pytest.raises(TransportError, match="invalid mutation protocol state"):
        tx.mutation_protocol_state()

    v2 = _rotated_state()
    v2["protocol_epoch"] = v2["database_seal"]["protocol_epoch"]
    tx._client.db["_mutation_protocol_state"] = v2
    with pytest.raises(TransportError, match="invalid mutation protocol state"):
        tx.mutation_protocol_state()


def test_state_wire_rejects_legacy_mode_at_version_two(tx):
    state = _legacy_state()
    state["schema_version"] = 2
    tx._client.db["_mutation_protocol_state"] = state
    with pytest.raises(TransportError, match="invalid mutation protocol state"):
        tx.mutation_protocol_state()


def test_cached_wrapper_delegates_observation_without_caching(tx):
    cached = CachingTransport(tx, auto_refresh=False)
    try:
        assert cached.supports_mutation_protocol is True
        first = cached.mutation_protocol_state()
        tx._client.db["_mutation_protocol_state"] = _sealed_state()
        second = cached.mutation_protocol_state()
        assert first.legacy_writes_enabled is True
        assert second.database_seal is not None
    finally:
        cached.close()


def test_wire_detaches_fake_provider_response(tx):
    source = _sealed_state()
    tx._client.db["_mutation_protocol_state"] = source
    state = tx.mutation_protocol_state()
    source["database_seal"]["plan_digest"] = "b" * 64
    assert state.database_seal is not None
    assert state.database_seal.plan_digest == "a" * 64


def test_parser_does_not_accept_mapping_subclasses(tx):
    raw = copy.deepcopy(_legacy_state())

    class DictSubclass(dict):
        pass

    tx._client.db["_mutation_protocol_state"] = DictSubclass(raw)
    # The fake models the JSON boundary, which normalizes mappings. Exercise
    # the strict parser directly for the in-process misuse case.
    with pytest.raises(ValueError, match="invalid mutation protocol state"):
        tx._mutation_protocol_state(DictSubclass(raw))
