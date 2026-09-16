"""Final owner fences for observed pins and serialized authority inputs."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import FrozenInstanceError, replace

import pytest

from agentbridge import crypto
from agentbridge.core.timekit import next_ns
from agentbridge.mesh.authority_source import (
    AuthorityInputs,
    AuthoritySourceReceipt,
    _source,
    matches_inputs_in_transaction,
)
from agentbridge.mesh.pin_storage import MAX_PENDING_BYTES, PendingPin
from agentbridge.mesh.pin_storage import PinStoreUnavailable
from agentbridge.mesh.pins import (
    EffectivePinView,
    KeyPinStore,
    ObservedPinDecision,
    evaluate_observed_view,
    rekey_signing_bytes,
)
from agentbridge.store import document_observation as documents
from agentbridge.store.db import Store
from agentbridge.transport.authority_observation import LookupPolicy
from agentbridge.transport.mirror_observation import MirrorExpectedPosition


CHAT = "room"
META = "chats/room/meta.json"
ALICE = "users/alice.json"


def test_observed_pin_actions_are_pure_frozen_and_satisfaction_is_explicit(tmp_path):
    pins = KeyPinStore(tmp_path, "root")
    empty = pins.capture_effective_view()
    first = evaluate_observed_view(empty, "alice", "S0", "A0")
    assert first == ObservedPinDecision("first_seen", "S0", "A0", False)

    pins.pin("alice", "S0", "A0")
    pinned = pins.capture_effective_view()
    keep = evaluate_observed_view(pinned, "alice", "S0", "A0")
    alert_missing = evaluate_observed_view(pinned, "alice", "S1", "A1")
    assert keep == ObservedPinDecision("keep", "S0", "A0", True)
    assert alert_missing == ObservedPinDecision("alert", "S0", "A0", False)
    with pytest.raises(FrozenInstanceError):
        keep.satisfied = False

    # The side effect is satisfied only by the exact canonical alert identity.
    assert pins.trusted("alice", "S1", "A1") == ("S0", "A0")
    alerted = pins.capture_effective_view()
    assert evaluate_observed_view(alerted, "alice", "S1", "A1") == (
        ObservedPinDecision("alert", "S0", "A0", True)
    )
    assert evaluate_observed_view(alerted, "alice", "S2", "A2").satisfied is False


def test_accepted_pending_pin_is_part_of_the_effective_view(tmp_path):
    pins = KeyPinStore(tmp_path, "root")
    pins._storage.pending = (
        PendingPin("first_seen", "alice", None, None, "S0", "A0", "now", "[]"),
    )
    view = pins.capture_effective_view()
    assert json.loads(view.durable_json)["pins"] == {}
    assert json.loads(view.effective_json)["pins"]["alice"]["sign_pub"] == "S0"
    assert evaluate_observed_view(view, "alice", "S0", "A0") == (
        ObservedPinDecision("keep", "S0", "A0", True)
    )


def test_valid_rotation_is_selected_but_not_yet_satisfied(tmp_path):
    pins = KeyPinStore(tmp_path, "root")
    old_bundle = crypto.generate_identity()
    old_sign, old_agree = crypto.identity_pubs(old_bundle)
    new_bundle = crypto.generate_identity()
    new_sign, new_agree = crypto.identity_pubs(new_bundle)
    pins.pin("alice", old_sign, old_agree)
    ns = next_ns()
    history = [{
        "old_sign_pub": old_sign,
        "sign_pub": new_sign,
        "agree_pub": new_agree,
        "ns": ns,
        "sig": crypto.sign(
            old_bundle,
            rekey_signing_bytes("alice", old_sign, new_sign, new_agree, ns),
        ),
    }]
    assert evaluate_observed_view(
        pins.capture_effective_view(), "alice", new_sign, new_agree, history,
    ) == ObservedPinDecision("rotate", new_sign, new_agree, False)


def test_final_view_replay_exposes_pin_value_aba(tmp_path):
    pins = KeyPinStore(tmp_path, "root")
    pins.pin("alice", "K0", "A0")
    initial = pins.capture_effective_view()
    original = json.loads(initial.durable_json)
    intermediate = json.loads(initial.durable_json)
    intermediate["pins"]["alice"].update(sign_pub="K1", agree_pub="A1")
    with pins._storage.locked():
        pins._storage.write(intermediate)
    consumed = pins.resolve_observed("alice", "K1", "A1")
    with pins._storage.locked():
        pins._storage.write(original)
    final = pins.capture_effective_view()

    assert initial == final
    assert (consumed.sign_pub, consumed.changed) == ("K1", False)
    replay = evaluate_observed_view(final, "alice", "K1", "A1")
    assert replay == ObservedPinDecision("alert", "K0", "A0", False)


@pytest.mark.parametrize(
    "view",
    [
        object(),
        EffectivePinView("o", "p", "{}", True, "not-json", ()),
        EffectivePinView("o", "p", "{}", True, "x" * (MAX_PENDING_BYTES + 1), ()),
        EffectivePinView("o", "p", "{}", True, '{"pins":[],"alerts":[]}', ()),
    ],
)
def test_observed_pin_rejects_malformed_or_oversized_views(view):
    with pytest.raises((TypeError, ValueError, PinStoreUnavailable)):
        evaluate_observed_view(view, "alice", "S", "A")


def _authority(store):
    mirror = MirrorExpectedPosition("root", "cache", "nonce", 7)
    source = _source(CHAT, mirror)
    position = store.capture_document_position(source)
    pending = store.invalidate_document_observation(position)
    position = store.publish_document_batch(
        pending,
        {META: {"members": ["alice"]}, ALICE: {"name": "alice"}},
        cursor=3,
        full=True,
    )
    receipt = AuthoritySourceReceipt(CHAT, position, mirror)
    policy = LookupPolicy(
        CHAT,
        mirror,
        ((ALICE, "present"),),
        (META, "present"),
    )
    captured = store.capture_selected_documents(position, (META, ALICE))
    return AuthorityInputs(receipt, policy, captured)


def _reader(store):
    conn = documents._open_reader(store.path)
    conn.execute("BEGIN")
    return conn


def test_authority_inputs_match_exact_rows_in_the_callers_transaction(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    expected = _authority(store)
    conn = _reader(store)
    try:
        assert matches_inputs_in_transaction(conn, store, expected)
        # The helper does not parse or authorize serialized document bodies.
        assert conn.in_transaction
    finally:
        conn.close()
        store.close()


def test_authority_match_rejects_source_row_and_database_mismatch(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    expected = _authority(store)
    stale = store.invalidate_document_observation(expected.receipt.position)
    conn = _reader(store)
    try:
        assert matches_inputs_in_transaction(conn, store, expected) is False
    finally:
        conn.close()

    rebuilt = store.publish_document_batch(
        stale,
        {META: {"members": []}, ALICE: {"name": "changed"}},
        cursor=4,
        full=True,
    )
    replacement = replace(
        expected,
        receipt=replace(expected.receipt, position=rebuilt),
        documents=store.capture_selected_documents(rebuilt, (META, ALICE)),
    )
    forged_row = replace(
        replacement,
        documents=replace(
            replacement.documents,
            records=(
                replace(replacement.documents.records[0], payload_json='{"members":["mallory"]}'),
                replacement.documents.records[1],
            ),
        ),
    )
    conn = _reader(store)
    try:
        assert matches_inputs_in_transaction(conn, store, forged_row) is False
    finally:
        conn.close()

    other = Store(tmp_path / "other.sqlite")
    conn = _reader(other)
    try:
        with pytest.raises(ValueError, match="another database"):
            matches_inputs_in_transaction(conn, store, replacement)
    finally:
        conn.close()
        other.close()
        store.close()


def test_authority_match_requires_transaction_and_bounded_valid_bindings(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    expected = _authority(store)
    conn = documents._open_reader(store.path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="active transaction"):
            matches_inputs_in_transaction(conn, store, expected)
        conn.execute("BEGIN")
        with pytest.raises(TypeError, match="AuthorityInputs"):
            matches_inputs_in_transaction(conn, store, object())
        with pytest.raises(ValueError, match="policy binding"):
            matches_inputs_in_transaction(
                conn,
                store,
                replace(
                    expected,
                    policy=replace(
                        expected.policy,
                        chat_id="other",
                        meta=("chats/other/meta.json", "present"),
                    ),
                ),
            )
        with pytest.raises(ValueError, match="record policy"):
            matches_inputs_in_transaction(
                conn,
                store,
                replace(
                    expected,
                    documents=replace(
                        expected.documents,
                        records=(
                            replace(expected.documents.records[0], deleted=True),
                            expected.documents.records[1],
                        ),
                    ),
                ),
            )
        with pytest.raises(OverflowError, match="byte budget"):
            matches_inputs_in_transaction(conn, store, expected, max_bytes=1)
    finally:
        conn.close()
        store.close()
