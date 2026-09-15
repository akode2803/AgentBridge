"""R180 strict durable trust-file and pending-operation contracts."""

from __future__ import annotations

import json

import pytest

from agentbridge import crypto
from agentbridge.core.timekit import next_ns
from agentbridge.mesh import pin_storage
from agentbridge.mesh.pin_storage import PinStoreUnavailable
from agentbridge.mesh.pins import KeyPinStore, rekey_signing_bytes


def _pair():
    bundle = crypto.generate_identity()
    return crypto.identity_pubs(bundle)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())


def _rotation(name, old_bundle, old_sign, new_sign, new_agree):
    ns = next_ns()
    return [{
        "old_sign_pub": old_sign, "sign_pub": new_sign, "agree_pub": new_agree,
        "ns": ns,
        "sig": crypto.sign(old_bundle, rekey_signing_bytes(
            name, old_sign, new_sign, new_agree, ns,
        )),
    }]


@pytest.mark.parametrize("raw,reason", [
    (b"{", "invalid_file"),
    (b'{"pins":[],"alerts":[]}', "invalid_file"),
    (b'{"pins":{},"alerts":{}}', "invalid_file"),
    (b'{"pins":{"kim":{"sign_pub":true,"agree_pub":"a","pinned":"t"}}}', "invalid_file"),
    (b'{"pins":{},"alerts":[{"name":"kim"}]}', "invalid_file"),
    (b'{"pins":{},"pins":{}}', "invalid_file"),
])
def test_strict_reader_rejects_malformed_authority_without_tofu(tmp_path, raw, reason):
    seed = KeyPinStore(tmp_path, "root")
    _write(seed.path, raw)
    with pytest.raises(PinStoreUnavailable) as raised:
        KeyPinStore(tmp_path, "root")
    assert raised.value.reason == reason
    assert seed.path.read_bytes() == raw


def test_valid_empty_file_marks_presence_and_later_disappearance_is_global(tmp_path):
    seed = KeyPinStore(tmp_path, "root")
    _write(seed.path, {})
    store = KeyPinStore(tmp_path, "root")
    seed.path.unlink()
    for call in (
        lambda: store.fingerprint("kim"), lambda: store.verified("kim"),
        lambda: store.alerts(), lambda: store.projection_facts(("kim",)),
        lambda: store.trusted("kim", *_pair()), lambda: store.pin("kim", *_pair()),
    ):
        with pytest.raises(PinStoreUnavailable, match="file_disappeared"):
            call()


def test_strict_reader_bounds_before_decode_and_preserves_unknown_legacy_fields(tmp_path):
    seed = KeyPinStore(tmp_path, "root")
    raw = {"pins": {}, "alerts": [], "unknown": {"keep": [1, 2, 3]}}
    _write(seed.path, raw)
    store = KeyPinStore(tmp_path, "root")
    sign, agree = _pair()
    store.trusted("kim", sign, agree)
    assert json.loads(seed.path.read_text())["unknown"] == raw["unknown"]

    _write(seed.path, b"x" * (16 * 1024 * 1024 + 1))
    with pytest.raises(PinStoreUnavailable) as raised:
        KeyPinStore(tmp_path, "root")
    assert raised.value.reason == "file_too_large"


@pytest.mark.parametrize("raw", [
    b'{"unknown":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}",
    b'{"unknown":' + b"[" * 2_000,
    b'{"pins":{"kim":{"sign_pub":"\\ud800","agree_pub":"a","pinned":"t"}},"alerts":[]}',
])
def test_deep_or_non_utf8_authority_never_leaks_parser_or_unicode_errors(tmp_path, raw):
    seed = KeyPinStore(tmp_path, "root")
    _write(seed.path, raw)
    with pytest.raises(PinStoreUnavailable):
        KeyPinStore(tmp_path, "root")


def test_failed_first_sight_stays_trusted_in_memory_then_replays(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    sign, agree = _pair()
    original = pin_storage.atomic_write_json
    monkeypatch.setattr(pin_storage, "atomic_write_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    assert store.trusted("kim", sign, agree) == (sign, agree)
    assert not store.path.exists()
    monkeypatch.setattr(pin_storage, "atomic_write_json", original)
    assert store.trusted("kim", sign, agree) == (sign, agree)
    assert KeyPinStore(tmp_path, "root").trusted("kim", sign, agree) == (sign, agree)


def test_pending_ack_replay_keeps_newer_ack_and_duplicate_flushes_once(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    sign, agree = _pair()
    seen_sign, seen_agree = _pair()
    store.trusted("kim", sign, agree)
    store.trusted("kim", seen_sign, seen_agree)
    original = pin_storage.atomic_write_json
    monkeypatch.setattr(pin_storage, "atomic_write_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    store.ack("kim")
    store.ack("kim")
    monkeypatch.setattr(pin_storage, "atomic_write_json", original)
    store.ack("kim")
    alerts = KeyPinStore(tmp_path, "root").alerts()
    assert len(alerts) == 1 and alerts[0]["ack"] is True


def test_repeated_known_mismatch_matches_alert_identity_without_new_conflict(tmp_path):
    store = KeyPinStore(tmp_path, "root")
    pinned, seen = _pair(), _pair()
    store.trusted("kim", *pinned)
    assert store.trusted("kim", *seen) == pinned
    assert store.trusted("kim", *seen) == pinned
    alerts = store.alerts()
    assert len(alerts) == 1
    assert (alerts[0]["name"], alerts[0]["seen_sign_pub"]) == ("kim", seen[0])


def test_successful_candidate_flushes_applied_pending_prefix_before_later_rotation(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    first_bundle = crypto.generate_identity()
    first = crypto.identity_pubs(first_bundle)
    second_bundle = crypto.generate_identity()
    second = crypto.identity_pubs(second_bundle)
    third = _pair()
    original = pin_storage.atomic_write_json
    monkeypatch.setattr(pin_storage, "atomic_write_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    assert store.trusted("kim", *first) == first
    assert store.trusted("kim", *second, _rotation("kim", first_bundle, first[0], *second)) == second
    monkeypatch.setattr(pin_storage, "atomic_write_json", original)
    assert store.trusted("kim", *second, _rotation("kim", first_bundle, first[0], *second)) == second
    assert store.trusted("kim", *third, _rotation("kim", second_bundle, second[0], *third)) == third


def test_pending_operations_replay_in_decision_order_not_global_coalescing(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    pair = _pair()
    original = pin_storage.atomic_write_json
    monkeypatch.setattr(pin_storage, "atomic_write_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    assert store.trusted("kim", *pair) == pair
    store.forget("kim")
    assert store.trusted("kim", *pair) == pair
    monkeypatch.setattr(pin_storage, "atomic_write_json", original)
    assert store.trusted("kim", *pair) == pair
    assert KeyPinStore(tmp_path, "root").trusted("kim", *pair) == pair


def test_low_pending_and_output_caps_reject_before_authority_mutation(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "pending")
    pair = _pair()
    original_write = pin_storage.atomic_write_json
    monkeypatch.setattr(pin_storage, "MAX_PENDING_OPS", 0)
    monkeypatch.setattr(pin_storage, "atomic_write_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(PinStoreUnavailable) as raised:
        store.trusted("kim", *pair)
    assert raised.value.reason == "pending_exhausted"
    assert not store.path.exists()

    output = KeyPinStore(tmp_path, "output")
    monkeypatch.setattr(pin_storage, "MAX_PENDING_OPS", 1024)
    monkeypatch.setattr(pin_storage, "atomic_write_json", original_write)
    monkeypatch.setattr(pin_storage, "MAX_FILE_BYTES", 1)
    with pytest.raises(PinStoreUnavailable) as raised:
        output.trusted("kim", *pair)
    assert raised.value.reason == "output_too_large"
    assert not output.path.exists()


def test_rotation_preserves_unknown_fields_but_clears_verified_and_alerts_are_deeply_detached(tmp_path):
    store = KeyPinStore(tmp_path, "root")
    old_bundle = crypto.generate_identity()
    old = crypto.identity_pubs(old_bundle)
    new = _pair()
    seen = _pair()
    store.trusted("kim", *old)
    doc = json.loads(store.path.read_text())
    doc["root_unknown"] = {"nested": [1]}
    doc["pins"]["kim"].update({"verified": "yes", "pin_unknown": {"safe": True}})
    _write(store.path, doc)
    refreshed = KeyPinStore(tmp_path, "root")
    assert refreshed.trusted("kim", *new, _rotation("kim", old_bundle, old[0], *new)) == new
    refreshed.trusted("kim", *seen)
    persisted = json.loads(refreshed.path.read_text())
    assert persisted["root_unknown"] == {"nested": [1]}
    assert persisted["pins"]["kim"]["pin_unknown"] == {"safe": True}
    assert "verified" not in persisted["pins"]["kim"]
    persisted["alerts"][0]["nested_unknown"] = {"values": [1]}
    _write(refreshed.path, persisted)
    observed = KeyPinStore(tmp_path, "root").alerts()
    observed[0]["nested_unknown"]["values"].append(2)
    assert KeyPinStore(tmp_path, "root").alerts()[0]["nested_unknown"] == {"values": [1]}
