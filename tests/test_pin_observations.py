"""R211 effective pin observation and final-lock contracts."""
from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from agentbridge import crypto
from agentbridge.core.timekit import next_ns
from agentbridge.mesh import pin_storage, pins as pins_module
from agentbridge.mesh.pins import (
    KeyPinStore,
    PinResolution,
    rekey_signing_bytes,
)


def _pair():
    bundle = crypto.generate_identity()
    return bundle, *crypto.identity_pubs(bundle)


def _rotation(name, old_bundle, old_sign, new_sign, new_agree):
    ns = next_ns()
    return [{
        "old_sign_pub": old_sign,
        "sign_pub": new_sign,
        "agree_pub": new_agree,
        "ns": ns,
        "sig": crypto.sign(old_bundle, rekey_signing_bytes(
            name, old_sign, new_sign, new_agree, ns,
        )),
    }]


def _fail_write(*_args, **_kwargs):
    raise OSError("fixture disk unavailable")


def test_resolve_observed_preserves_canonical_cases_and_changed_idempotence(tmp_path):
    store = KeyPinStore(tmp_path, "root")
    old_bundle, old_sign, old_agree = _pair()
    _new_bundle, new_sign, new_agree = _pair()
    _seen_bundle, seen_sign, seen_agree = _pair()

    assert store.resolve_observed("keyless", "", "") == PinResolution("", "", False)
    assert store.resolve_observed("kim", old_sign, old_agree) == PinResolution(
        old_sign, old_agree, True,
    )
    assert store.resolve_observed("kim", old_sign, old_agree) == PinResolution(
        old_sign, old_agree, False,
    )

    history = _rotation("kim", old_bundle, old_sign, new_sign, new_agree)
    assert store.resolve_observed(
        "kim", new_sign, new_agree, history,
    ) == PinResolution(new_sign, new_agree, True)
    assert store.resolve_observed(
        "kim", new_sign, new_agree, history,
    ) == PinResolution(new_sign, new_agree, False)

    assert store.resolve_observed("kim", seen_sign, seen_agree) == PinResolution(
        new_sign, new_agree, True,
    )
    assert store.resolve_observed("kim", seen_sign, seen_agree) == PinResolution(
        new_sign, new_agree, False,
    )
    assert len(store.alerts()) == 1


def test_resolve_observed_ignores_stable_allowed_top_level_metadata(tmp_path):
    store = KeyPinStore(tmp_path, "root")
    _bundle, sign, agree = _pair()
    store.resolve_observed("kim", sign, agree)
    document = json.loads(store.path.read_text())
    document["future_metadata"] = {"nested": [1, 2, 3]}
    store.path.write_text(json.dumps(document))

    refreshed = KeyPinStore(tmp_path, "root")
    assert refreshed.resolve_observed("kim", sign, agree) == PinResolution(
        sign, agree, False,
    )
    assert json.loads(refreshed.path.read_text())["future_metadata"] == {
        "nested": [1, 2, 3],
    }


def test_effective_view_includes_accepted_pending_without_flush_or_write(
        tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    _bundle, sign, agree = _pair()
    original = pin_storage.atomic_write_json
    monkeypatch.setattr(pin_storage, "atomic_write_json", _fail_write)

    resolution = store.resolve_observed("kim", sign, agree)
    assert resolution == PinResolution(sign, agree, True)
    assert not store.path.exists() and store._storage.pending

    pending = store._storage.pending
    monkeypatch.setattr(
        pin_storage, "atomic_write_json",
        lambda *_args, **_kwargs: pytest.fail("capture attempted a pin write"),
    )
    view = store.capture_effective_view()
    assert view.present is False
    assert view.pending == pending
    assert '"kim"' not in view.durable_json
    assert '"kim"' in view.effective_json
    assert not store.path.exists() and store._storage.pending == pending

    fresh = KeyPinStore(tmp_path, "root")
    fresh_view = fresh.capture_effective_view()
    assert fresh_view.effective_json == fresh_view.durable_json
    assert fresh_view.pending == () and fresh_view != view

    monkeypatch.setattr(pin_storage, "atomic_write_json", original)
    flushed = store.resolve_observed("kim", sign, agree)
    assert flushed == PinResolution(sign, agree, True)
    assert store._storage.pending == () and store.path.exists()
    assert store.capture_effective_view().effective_json == view.effective_json


def test_locked_matching_view_rejects_old_owner_and_mutated_views_without_writes(
        tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    _bundle, sign, agree = _pair()
    store.resolve_observed("kim", sign, agree)
    old = store.capture_effective_view()

    monkeypatch.setattr(
        pin_storage, "atomic_write_json",
        lambda *_args, **_kwargs: pytest.fail("matching attempted a pin write"),
    )
    with store.locked_matching_view(old) as matched:
        assert matched is True

    sibling = KeyPinStore(tmp_path, "root")
    sibling_view = sibling.capture_effective_view()
    assert sibling_view.owner != old.owner
    with store.locked_matching_view(sibling_view) as matched:
        assert matched is False

    monkeypatch.undo()
    _other_bundle, other_sign, other_agree = _pair()
    store.resolve_observed("lee", other_sign, other_agree)
    with store.locked_matching_view(old) as matched:
        assert matched is False


@pytest.mark.parametrize("mutate", [
    lambda view: replace(view, owner=object()),
    lambda view: replace(view, present=1),
    lambda view: replace(view, pending=[]),
    lambda view: replace(view, pending=(object(),)),
])
def test_locked_matching_view_rejects_malformed_expected_before_lock(
        tmp_path, monkeypatch, mutate):
    store = KeyPinStore(tmp_path, "root")
    malformed = mutate(store.capture_effective_view())
    monkeypatch.setattr(
        store._storage, "locked",
        lambda: pytest.fail("malformed view reached pin lock"),
    )
    with pytest.raises((TypeError, ValueError, pin_storage.PinStoreUnavailable)):
        with store.locked_matching_view(malformed):
            pytest.fail("malformed view was accepted")

    with pytest.raises(TypeError, match="expected EffectivePinView"):
        with store.locked_matching_view(object()):
            pytest.fail("wrong token type was accepted")


def test_matching_charges_and_compares_detached_pending_copy_on_caller_mutation(
        tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    _bundle, sign, agree = _pair()
    monkeypatch.setattr(pin_storage, "atomic_write_json", _fail_write)
    store.resolve_observed("kim", sign, agree)
    captured = store.capture_effective_view()
    caller_operation = replace(captured.pending[0])
    expected = replace(captured, pending=(caller_operation,))
    original_validate = pins_module._validate_operation

    def copy_then_mutate(operation):
        copied = original_validate(operation)
        assert operation is caller_operation
        object.__setattr__(operation, "target_sign", "x" * 100_000)
        return copied

    monkeypatch.setattr(pins_module, "_validate_operation", copy_then_mutate)
    with store.locked_matching_view(expected) as matched:
        assert matched is True
    assert caller_operation.target_sign == "x" * 100_000
    assert store._storage.pending[0].target_sign == sign


def test_locked_matching_view_holds_both_locks_and_releases_after_failure(tmp_path):
    store = KeyPinStore(tmp_path, "root")
    view = store.capture_effective_view()
    entered = threading.Event()
    finished = threading.Event()
    errors = []

    def writer():
        try:
            entered.set()
            sibling = KeyPinStore(tmp_path, "root")
            sibling.resolve_observed("lee", *_pair()[1:])
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with pytest.raises(RuntimeError, match="caller failed"):
        with store.locked_matching_view(view) as matched:
            assert matched is True
            assert not store._lock.acquire(blocking=False)
            thread = threading.Thread(target=writer)
            thread.start()
            assert entered.wait(2)
            assert not finished.wait(0.1)
            raise RuntimeError("caller failed")

    thread.join(5)
    assert not thread.is_alive() and finished.is_set() and not errors
    assert store._lock.acquire(blocking=False)
    store._lock.release()
    with store._storage.locked():
        pass
