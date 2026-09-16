"""Inactive bounded membership coordinator integration contracts."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

import pytest

from agentbridge import crypto
from agentbridge.mesh import authority_source, events, membership_coordinator as coordinator
from agentbridge.mesh.membership_coordinator import CoordinatorLimits, run_membership_round
from agentbridge.mesh.paths import P
from agentbridge.mesh.pins import rekey_signing_bytes
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_inputs, membership_suffix, terminal_observation
from agentbridge.transport import authority_observation
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


@pytest.fixture
def world(tmp_path):
    provider = FolderTransport(tmp_path / "mesh")
    provider.cache_key = "coordinator-cache"
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror._mirror_root_identity = "coordinator-root"
    mirror._mirror_cache_identity = "coordinator-cache"
    mesh = Mesh(mirror, "aryan", "coordinator-box", home=tmp_path / "home")
    try:
        mesh.store.prepare_terminal_observation()
        mesh.accounts.create_human("aryan", "aryan-pass")
        mesh.accounts.create_human("fable", "fable-pass")
        mirror.refresh()
        chat = mesh.membership.create_chat("Coordinator", members=["fable"])
        mirror.refresh()
        mesh.sync.sync_once([chat.id])
        mesh.store.prepare_membership_suffix_index()
        lifecycle_inputs.prepare(mesh.store._conn())
        yield mesh, mirror, provider, chat.id
    finally:
        mesh.close()


def _target(mesh, chat):
    return f"{chat}|{P.log_name(mesh.messaging.user, mesh.messaging.machine)}"


def _receipt(mesh, mirror, chat):
    mirror.refresh()
    mesh.store.refresh_terminal_observation(_target(mesh, chat))
    return authority_source.publish_authority_source(mirror, mesh.store, chat)


def _newer_rename(mesh, mirror, chat, name="After"):
    _signed_rename(mesh, chat, "aryan", mesh.keystore.load("aryan"), name)


def _signed_rename(mesh, chat, actor, bundle, name):
    event = {
        "id": f"coordinator-{actor}-{time.time_ns()}",
        "ns": time.time_ns(),
        "from": actor,
        "kind": "info",
        "event": {"type": events.EV_RENAMED, "name": name, "by": actor},
    }
    event["sig"] = crypto.sign(bundle, events.signing_bytes(chat, event))
    mesh.store.upsert_messages(chat, [event])


def test_no_suffix_shortcut_returns_candidate_without_full_fold_or_provider(world, monkeypatch):
    mesh, mirror, provider, chat = world
    receipt = _receipt(mesh, mirror, chat)
    calls = getattr(provider, "_calls", None)
    monkeypatch.setattr(
        mesh.messaging,
        "snapshot",
        lambda *_args: pytest.fail("coordinator invoked canonical full fold"),
    )
    result = run_membership_round(mesh, receipt)
    assert result.status == "candidate" and result.candidate is not None
    assert json.loads(result.candidate.snapshot_json)["name"] == "Coordinator"
    assert result.candidate.suffix.rows == ()
    if calls is not None:
        assert provider._calls == calls


def test_signed_suffix_candidate_matches_canonical_snapshot(world, monkeypatch):
    mesh, mirror, provider, chat = world
    _newer_rename(mesh, mirror, chat)
    receipt = _receipt(mesh, mirror, chat)
    expected = mesh.messaging.snapshot(chat).to_dict()
    monkeypatch.setattr(
        mesh.messaging,
        "snapshot",
        lambda *_args: pytest.fail("coordinator invoked canonical full fold"),
    )
    for name in ("get_doc", "list_docs", "snapshot_docs"):
        monkeypatch.setattr(
            provider,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"coordinator invoked provider {_name}"
            ),
        )

    result = run_membership_round(mesh, receipt)
    assert result.status == "candidate", result
    assert json.loads(result.candidate.snapshot_json) == expected
    assert json.loads(result.candidate.snapshot_json)["name"] == "After"


def test_online_unknown_account_returns_exact_readthrough_without_provider(world):
    mesh, mirror, provider, chat = world
    _newer_rename(mesh, mirror, chat)
    with mirror._lock:
        mirror._docs.pop(P.user("aryan"), None)
        mirror._neg.discard(P.user("aryan"))
        mirror._mirror_revision += 1
    mesh.store.refresh_terminal_observation(_target(mesh, chat))
    receipt = authority_source.publish_authority_source(mirror, mesh.store, chat)
    calls = getattr(provider, "_calls", None)

    result = run_membership_round(mesh, receipt)
    assert (result.status, result.work_path, result.reason) == (
        "readthrough", P.user("aryan"), "account_readthrough_required",
    )
    if calls is not None:
        assert provider._calls == calls


def test_first_sight_pin_progress_restarts_before_candidate(world):
    mesh, mirror, _provider, chat = world
    _newer_rename(mesh, mirror, chat)
    mesh.key_pins.forget("aryan")
    receipt = _receipt(mesh, mirror, chat)
    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("restart", "pin_progress")
    assert mesh.key_pins.fingerprint("aryan")


def test_changed_key_alert_progress_restarts(world):
    mesh, mirror, _provider, chat = world
    changed = crypto.generate_identity()
    sign, agree = crypto.identity_pubs(changed)
    account = mirror.get_doc(P.user("aryan"))
    account["keys"] = {"sign_pub": sign, "agree_pub": agree}
    mirror.put_doc(P.user("aryan"), account)
    _signed_rename(mesh, chat, "aryan", changed, "Changed")
    receipt = _receipt(mesh, mirror, chat)

    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("restart", "pin_progress")
    assert any(
        alert["name"] == "aryan" and alert["seen_sign_pub"] == sign
        for alert in mesh.key_pins.alerts()
    )


def test_valid_key_rotation_progress_restarts(world):
    mesh, mirror, _provider, chat = world
    old_bundle = mesh.keystore.load("aryan")
    old_sign, _old_agree = crypto.identity_pubs(old_bundle)
    changed = crypto.generate_identity()
    sign, agree = crypto.identity_pubs(changed)
    ns = time.time_ns()
    history = [{
        "old_sign_pub": old_sign,
        "sign_pub": sign,
        "agree_pub": agree,
        "ns": ns,
        "sig": crypto.sign(
            old_bundle,
            rekey_signing_bytes("aryan", old_sign, sign, agree, ns),
        ),
    }]
    account = mirror.get_doc(P.user("aryan"))
    account["keys"] = {"sign_pub": sign, "agree_pub": agree, "history": history}
    mirror.put_doc(P.user("aryan"), account)
    _signed_rename(mesh, chat, "aryan", changed, "Rotated")
    receipt = _receipt(mesh, mirror, chat)

    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("restart", "pin_progress")
    assert mesh.key_pins.fingerprint("aryan", sign, agree)
    assert not any(alert["name"] == "aryan" for alert in mesh.key_pins.alerts())


def test_invalid_initial_clock_precedes_pin_and_transaction_side_effects(
    world, monkeypatch,
):
    mesh, mirror, _provider, chat = world
    receipt = _receipt(mesh, mirror, chat)
    monkeypatch.setattr(coordinator.time, "time_ns", lambda: -1)
    monkeypatch.setattr(
        mesh.key_pins,
        "resolve_observed",
        lambda *_a, **_k: pytest.fail("invalid clock reached pin mutation"),
    )
    monkeypatch.setattr(
        authority_source,
        "matches_inputs_in_transaction",
        lambda *_a, **_k: pytest.fail("invalid clock reached final CAS"),
    )

    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("unavailable", "invalid_clock")
    assert mesh.key_pins._lock.acquire(blocking=False)
    mesh.key_pins._lock.release()
    conn = mesh.store._conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()


def test_memory_error_is_named_and_releases_all_owner_resources(world, monkeypatch):
    mesh, mirror, _provider, chat = world
    receipt = _receipt(mesh, mirror, chat)
    monkeypatch.setattr(
        events,
        "advance",
        lambda *_a, **_k: (_ for _ in ()).throw(MemoryError("fixture")),
    )

    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("unavailable", "resource_unavailable")
    assert mesh.key_pins._lock.acquire(blocking=False)
    mesh.key_pins._lock.release()
    assert mirror._lock.acquire(blocking=False)
    mirror._lock.release()
    conn = mesh.store._conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.rollback()


def test_consumed_pin_pair_aba_is_rejected_by_final_replay(world, monkeypatch):
    mesh, mirror, _provider, chat = world
    changed = crypto.generate_identity()
    sign, agree = crypto.identity_pubs(changed)
    account = mirror.get_doc(P.user("aryan"))
    account["keys"] = {"sign_pub": sign, "agree_pub": agree}
    mirror.put_doc(P.user("aryan"), account)
    _signed_rename(mesh, chat, "aryan", changed, "Intermediate trust")
    receipt = _receipt(mesh, mirror, chat)

    storage = mesh.key_pins._storage
    original = mesh.key_pins.resolve_observed
    with storage.locked() as (durable, _present):
        baseline = json.loads(json.dumps(durable))
    intermediate = json.loads(json.dumps(baseline))
    intermediate["pins"]["aryan"].update(sign_pub=sign, agree_pub=agree)

    def consume_intermediate(name, published_sign, published_agree, history=None):
        with storage.locked():
            storage.write(intermediate)
        try:
            resolution = original(name, published_sign, published_agree, history)
            assert (resolution.sign_pub, resolution.changed) == (sign, False)
            return resolution
        finally:
            with storage.locked():
                storage.write(baseline)

    monkeypatch.setattr(mesh.key_pins, "resolve_observed", consume_intermediate)
    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("unavailable", "pin_inputs_changed")
    assert mesh.key_pins.capture_effective_view().durable_json == json.dumps(
        baseline, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (CoordinatorLimits(max_steps=0), "step_budget_exhausted"),
        (CoordinatorLimits(max_bytes=0), "budget_exhausted"),
        (CoordinatorLimits(max_suffix_events=0), "inputs_unavailable"),
    ],
)
def test_budget_exhaustion_is_unavailable(world, limits, reason):
    mesh, mirror, _provider, chat = world
    if limits.max_suffix_events == 0:
        _newer_rename(mesh, mirror, chat)
    receipt = _receipt(mesh, mirror, chat)
    result = run_membership_round(mesh, receipt, limits=limits)
    assert result.status == "unavailable"
    assert result.reason == reason


@pytest.mark.parametrize(
    ("seam", "reason"),
    [
        ("source", "authority_inputs_changed"),
        ("message", "membership_inputs_changed"),
        ("pin", "pin_inputs_changed"),
        ("policy", "lookup_policy_changed"),
        ("terminal", "terminal_inputs_changed"),
    ],
)
def test_final_dependency_races_discard_candidate(world, monkeypatch, seam, reason):
    mesh, mirror, _provider, chat = world
    receipt = _receipt(mesh, mirror, chat)
    if seam == "source":
        original = authority_source.matches_inputs_in_transaction

        def change_source(conn, store, expected, **kwargs):
            conn.execute(
                "UPDATE document_observation_sources SET generation=generation+1 WHERE source_id=?",
                (receipt.position.source_id,),
            )
            return original(conn, store, expected, **kwargs)

        monkeypatch.setattr(authority_source, "matches_inputs_in_transaction", change_source)
    elif seam == "message":
        original = membership_suffix.matches_position

        def change_message(conn, path, expected):
            conn.execute(
                "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                (chat, "race", time.time_ns(), "aryan", "message", '{"race":true}'),
            )
            return original(conn, path, expected)

        monkeypatch.setattr(membership_suffix, "matches_position", change_message)
    elif seam == "terminal":
        original = terminal_observation.matches

        def change_terminal(conn, path, expected):
            conn.execute(
                "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                ("append", _target(mesh, chat), "{}", time.time_ns()),
            )
            return original(conn, path, expected)

        monkeypatch.setattr(terminal_observation, "matches", change_terminal)
    elif seam == "policy":
        original = authority_observation.matches_lookup_policy
        calls = 0

        def change_at_final(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                with mirror._lock:
                    mirror._docs.pop(P.meta(chat), None)
            return original(*args, **kwargs)

        monkeypatch.setattr(authority_observation, "matches_lookup_policy", change_at_final)
    else:
        @contextmanager
        def changed(_view):
            mesh.key_pins.pin("race", "sign", "agree")
            yield False

        monkeypatch.setattr(mesh.key_pins, "locked_matching_view", changed)

    result = run_membership_round(mesh, receipt)
    assert (result.status, result.reason) == ("unavailable", reason)
