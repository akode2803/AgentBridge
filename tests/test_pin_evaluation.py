"""R177 contracts for pure key-pin evaluation and its publication wrapper."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from agentbridge import crypto
from agentbridge.core.timekit import next_ns
from agentbridge.mesh import pins
from agentbridge.mesh.pins import (
    KeyPinStore,
    PinDecision,
    evaluate_pin,
    rekey_signing_bytes,
)


def keypair():
    bundle = crypto.generate_identity()
    return bundle, *crypto.identity_pubs(bundle)


def signed_link(name, old_bundle, old_sign, sign, agree, ns):
    return {
        "old_sign_pub": old_sign,
        "sign_pub": sign,
        "agree_pub": agree,
        "ns": ns,
        "sig": crypto.sign(
            old_bundle, rekey_signing_bytes(name, old_sign, sign, agree, ns)
        ),
    }


def test_evaluate_pin_is_pure_frozen_and_does_not_mutate_observations(monkeypatch):
    """The detached selector has no storage dependency or mutable result."""
    pin = {"sign_pub": "old-sign", "agree_pub": "old-agree", "extra": []}
    history = [{"old_sign_pub": "unrelated", "ns": 4}]
    original_pin, original_history = deepcopy(pin), deepcopy(history)

    monkeypatch.setattr(
        pins, "read_json", lambda *_args, **_kwargs: pytest.fail("unexpected I/O")
    )
    monkeypatch.setattr(
        pins, "atomic_write_json", lambda *_args, **_kwargs: pytest.fail("unexpected I/O")
    )

    decision = evaluate_pin("kim", pin, "old-sign", "old-agree", history)

    assert decision == PinDecision("keep", "old-sign", "old-agree")
    assert pin == original_pin
    assert history == original_history
    with pytest.raises(FrozenInstanceError):
        decision.action = "alert"


def test_evaluate_pin_covers_keyless_first_seen_matching_and_mismatch():
    assert evaluate_pin("kim", None, "", "") == PinDecision("keep", "", "")
    assert evaluate_pin("kim", {}, "new-sign", "new-agree") == PinDecision(
        "first_seen", "new-sign", "new-agree"
    )
    pin = {"sign_pub": "old-sign", "agree_pub": "old-agree"}
    assert evaluate_pin("kim", pin, "old-sign", "old-agree") == PinDecision(
        "keep", "old-sign", "old-agree"
    )
    assert evaluate_pin("kim", pin, "new-sign", "new-agree") == PinDecision(
        "alert", "old-sign", "old-agree"
    )
    assert evaluate_pin("kim", pin, "", "") == PinDecision(
        "alert", "old-sign", "old-agree"
    )


def test_evaluate_pin_advances_only_a_valid_ordered_multilink_history():
    a_bundle, a_sign, a_agree = keypair()
    b_bundle, b_sign, b_agree = keypair()
    _, c_sign, c_agree = keypair()
    first_ns, second_ns = next_ns(), next_ns()
    first = signed_link("kim", a_bundle, a_sign, b_sign, b_agree, first_ns)
    second = signed_link("kim", b_bundle, b_sign, c_sign, c_agree, second_ns)
    pin = {"sign_pub": a_sign, "agree_pub": a_agree}

    assert evaluate_pin("kim", pin, c_sign, c_agree, [second, first]) == PinDecision(
        "rotate", c_sign, c_agree
    )

    second["sig"] = crypto.sign(
        a_bundle, rekey_signing_bytes("kim", b_sign, c_sign, c_agree, second_ns)
    )
    assert evaluate_pin("kim", pin, c_sign, c_agree, [first, second]) == PinDecision(
        "alert", a_sign, a_agree
    )


def test_evaluate_pin_preserves_malformed_history_and_skipped_branch_behavior():
    pin = {"sign_pub": "old-sign", "agree_pub": "old-agree"}
    malformed = [{"old_sign_pub": "old-sign", "ns": "not-an-int"}]

    # Matching and keyless inputs never examine the malformed history.
    assert evaluate_pin("kim", pin, "old-sign", "old-agree", malformed).action == "keep"
    assert evaluate_pin("kim", None, "", "", malformed).action == "keep"

    # A mismatched signed-history path retains the prior conversion failure.
    with pytest.raises(ValueError):
        evaluate_pin("kim", pin, "new-sign", "new-agree", malformed)


@pytest.mark.parametrize("same_sign", [True, False])
def test_trusted_first_seen_competitor_rereads_merged_winner(tmp_path, same_sign):
    """A stale Store returns its caller pair only when the merged sign key agrees."""
    winner = KeyPinStore(tmp_path, "rootX")
    stale = KeyPinStore(tmp_path, "rootX")
    _, sign, stored_agree = keypair()
    _, alternate_sign, caller_agree = keypair()
    caller_sign = sign if same_sign else alternate_sign

    assert winner.trusted("kim", sign, stored_agree) == (sign, stored_agree)
    expected = (caller_sign, caller_agree) if same_sign else (sign, stored_agree)
    assert stale.trusted("kim", caller_sign, caller_agree) == expected
    assert bool(stale.alerts()) is not same_sign
    assert KeyPinStore(tmp_path, "rootX").trusted("kim", sign, stored_agree) == (
        sign,
        stored_agree,
    )


def test_trusted_keeps_memory_trust_when_pin_write_fails(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "rootX")
    _, first_sign, first_agree = keypair()
    _, changed_sign, changed_agree = keypair()

    def fail_write(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(pins, "atomic_write_json", fail_write)
    assert store.trusted("kim", first_sign, first_agree) == (first_sign, first_agree)
    assert store.trusted("kim", changed_sign, changed_agree) == (first_sign, first_agree)
    assert store.alerts()[0]["pinned_sign_pub"] == first_sign
