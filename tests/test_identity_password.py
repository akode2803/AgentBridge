"""A bound typed writer changes passwords with one exact compare-and-swap."""

from __future__ import annotations

import base64
import os

import pytest

import test_identity_dispatch as dispatch
from agentbridge import crypto
from agentbridge.core.errors import PermissionDenied
from agentbridge.mesh import accounts as accounts_module

world = dispatch.world
identity_world = dispatch.identity_world
accounts = dispatch.accounts


def _auth(password):
    salt = os.urandom(16)
    return {"algo": "scrypt", "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(accounts_module._scrypt(password, salt)).decode()}


@pytest.fixture
def human(accounts):
    service, provider, successes = accounts
    bundle = service.keystore.load("alice")
    recovery = crypto.wrap_bundle(bundle, "recovery-code")
    doc = provider.docs.docs["users/alice.json"]
    doc["auth"] = _auth("old-pass")
    doc["keys"].update(wrapped_priv=crypto.wrap_bundle(bundle, "old-pass"), recovery=recovery)
    return service, provider, successes, bundle, recovery


def _rpcs(provider):
    return [name for name, _ in provider.calls]


def test_typed_change_swaps_hash_and_wrap_but_keeps_recovery(human):
    service, provider, successes, bundle, recovery = human
    service.change_password("old-pass", "new-pass-1")
    doc = provider.docs.docs["users/alice.json"]
    assert accounts_module._password_matches(doc, "new-pass-1")
    assert not accounts_module._password_matches(doc, "old-pass")
    assert crypto.unwrap_bundle(doc["keys"]["wrapped_priv"], "new-pass-1") == bundle
    assert doc["keys"]["recovery"] == recovery
    assert doc["display"] == "Alice"
    assert successes == ["hint"]
    assert dispatch.COMPARE_RPC in _rpcs(provider)


def test_wrong_current_password_sends_nothing(human):
    service, provider, _, _, _ = human
    with pytest.raises(PermissionDenied, match="incorrect"):
        service.change_password("wrong-pass", "new-pass-1")
    assert dispatch.COMPARE_RPC not in _rpcs(provider)


def test_concurrent_change_elsewhere_is_never_overwritten(human, monkeypatch):
    service, provider, _, _, _ = human
    original = provider._handle
    raced = {}

    def elsewhere(name, params):
        # Another device changes the password between our check and swap.
        if name == "ab_node_docs_exact" and not raced:
            raced["auth"] = _auth("other-device")
            provider.docs.docs["users/alice.json"]["auth"] = raced["auth"]
            provider.seqs["users/alice.json"] = 2
        return original(name, params)

    monkeypatch.setattr(provider, "_handle", elsewhere)
    with pytest.raises(PermissionDenied, match="incorrect"):
        service.change_password("old-pass", "new-pass-1")
    assert provider.docs.docs["users/alice.json"]["auth"] == raced["auth"]
    assert dispatch.COMPARE_RPC not in _rpcs(provider)


def test_lost_reply_replays_the_identical_material(human):
    service, provider, _, bundle, _ = human
    lost = {}
    original = provider._handle

    def lose_once(name, params):
        reply = original(name, params)
        if name == dispatch.COMPARE_RPC and not lost:
            lost["op"] = params["p_operation_id"]
            raise TimeoutError("reply lost after provider commit")
        return reply

    provider._handle = lose_once
    with pytest.raises(Exception):
        service.change_password("old-pass", "new-pass-1")
    committed = dict(provider.docs.docs["users/alice.json"])
    assert accounts_module._password_matches(committed, "new-pass-1")
    # The change committed, so the old credential is now refused outright.
    with pytest.raises(PermissionDenied):
        service.change_password("old-pass", "new-pass-2")
    assert provider.docs.docs["users/alice.json"] == committed
    # The next swap first settles the exact retained attempt by replay.
    service.change_password("new-pass-1", "new-pass-2")
    assert crypto.unwrap_bundle(
        provider.docs.docs["users/alice.json"]["keys"]["wrapped_priv"], "new-pass-2") == bundle
    executions = [op for name, op in provider.calls if name == dispatch.COMPARE_RPC]
    assert executions[:2] == [lost["op"], lost["op"]] and len(set(executions)) == 2
