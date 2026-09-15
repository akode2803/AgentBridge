"""R180 real-process coordination and nested pin-unavailability tests."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from agentbridge import crypto
from agentbridge.core.timekit import next_ns
from agentbridge.mesh.pin_storage import PinFileCoordinator, PinStoreUnavailable
from agentbridge.mesh.pins import KeyPinStore, rekey_signing_bytes
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


def _pair():
    bundle = crypto.generate_identity()
    return crypto.identity_pubs(bundle)


def _rotation(name, old_bundle, old_sign, new_sign, new_agree):
    ns = next_ns()
    return [{
        "old_sign_pub": old_sign, "sign_pub": new_sign, "agree_pub": new_agree,
        "ns": ns,
        "sig": crypto.sign(old_bundle, rekey_signing_bytes(
            name, old_sign, new_sign, new_agree, ns,
        )),
    }]


def _first_sight_worker(home, root, name, sign, agree, start, results):
    try:
        store = KeyPinStore(Path(home), root)
        if not start.wait(5):
            raise RuntimeError("start barrier timed out")
        results.put(("ok", name, store.trusted(name, sign, agree)))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _join(process):
    process.join(10)
    assert process.exitcode == 0


def _lock_attempt_worker(path, timeout, results):
    from agentbridge.mesh.pin_storage import PinFileCoordinator

    try:
        with PinFileCoordinator(Path(path), lock_timeout_s=timeout).locked():
            results.put(("acquired", ""))
    except PinStoreUnavailable as exc:
        results.put(("unavailable", exc.reason))


def _lock_holder_dies(path, acquired):
    from agentbridge.mesh.pin_storage import PinFileCoordinator

    with PinFileCoordinator(Path(path)).locked():
        acquired.set()
        os._exit(0)


def _stale_rotation_worker(home, root, sign, agree, history, loaded, release, results):
    try:
        store = KeyPinStore(Path(home), root)
        loaded.set()
        if not release.wait(5):
            raise RuntimeError("release barrier timed out")
        results.put(("ok", store.trusted("kim", sign, agree, history)))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def test_spawned_disjoint_first_sights_merge_under_real_sibling_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    first, second = _pair(), _pair()
    workers = [
        context.Process(target=_first_sight_worker,
                        args=(str(tmp_path), "root", name, *pair, start, results))
        for name, pair in (("kim", first), ("lee", second))
    ]
    for worker in workers:
        worker.start()
    start.set()
    observed = [results.get(timeout=10) for _ in workers]
    for worker in workers:
        _join(worker)
    assert sorted((state, name) for state, name, _pair_value in observed) == [
        ("ok", "kim"), ("ok", "lee"),
    ]
    fresh = KeyPinStore(tmp_path, "root")
    assert fresh.trusted("kim", *first) == first
    assert fresh.trusted("lee", *second) == second


def test_real_lock_contention_times_out_and_holder_death_releases_sibling_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    seed = KeyPinStore(tmp_path, "root")
    results = context.Queue()
    coordinator = PinFileCoordinator(seed.path)
    with coordinator.locked():
        waiter = context.Process(target=_lock_attempt_worker,
                                 args=(str(seed.path), 0.05, results))
        waiter.start()
        assert results.get(timeout=5) == ("unavailable", "lock_timeout")
        _join(waiter)

    acquired = context.Event()
    holder = context.Process(target=_lock_holder_dies, args=(str(seed.path), acquired))
    holder.start()
    assert acquired.wait(5)
    _join(holder)
    assert not coordinator.lock_path.is_dir()
    with coordinator.locked():
        pass
    assert coordinator.lock_path.exists()


def test_stale_process_rotation_from_k0_cannot_overwrite_completed_k2(tmp_path):
    context = multiprocessing.get_context("spawn")
    root = "root"
    owner = KeyPinStore(tmp_path, root)
    first_bundle = crypto.generate_identity()
    first = crypto.identity_pubs(first_bundle)
    k1, k2 = _pair(), _pair()
    owner.trusted("kim", *first)
    loaded, release, results = context.Event(), context.Event(), context.Queue()
    stale = context.Process(
        target=_stale_rotation_worker,
        args=(str(tmp_path), root, *k1, _rotation("kim", first_bundle, first[0], *k1),
              loaded, release, results),
    )
    stale.start()
    assert loaded.wait(5)
    assert owner.trusted("kim", *k2, _rotation("kim", first_bundle, first[0], *k2)) == k2
    release.set()
    assert results.get(timeout=10) == ("ok", k2)
    _join(stale)
    assert KeyPinStore(tmp_path, root).trusted("kim", *k2) == k2


def test_conflicting_pending_branch_blocks_every_public_operation(tmp_path, monkeypatch):
    store = KeyPinStore(tmp_path, "root")
    first, replacement = _pair(), _pair()
    original = __import__("agentbridge.mesh.pin_storage", fromlist=["atomic_write_json"]).atomic_write_json
    monkeypatch.setattr("agentbridge.mesh.pin_storage.atomic_write_json",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    assert store.trusted("kim", *first) == first
    monkeypatch.setattr("agentbridge.mesh.pin_storage.atomic_write_json", original)
    competitor = KeyPinStore(tmp_path, "root")
    assert competitor.trusted("kim", *replacement) == replacement
    with pytest.raises(PinStoreUnavailable, match="pin_conflict"):
        store.trusted("kim", *first)
    for call in (
        lambda: store.fingerprint("kim"), lambda: store.verified("kim"),
        lambda: store.alerts(), lambda: store.projection_facts(("kim",)),
        lambda: store.mark_verified("kim"), lambda: store.ack("kim"),
        lambda: store.forget("kim"), lambda: store.pin("other", *_pair()),
    ):
        with pytest.raises(PinStoreUnavailable, match="pin_conflict"):
            call()


def test_nested_lifecycle_actor_pin_unavailable_never_falls_back_to_raw_fields(tmp_path, monkeypatch):
    mesh = Mesh(FolderTransport(tmp_path / "mesh"), "aryan", "machine", home=tmp_path / "home")
    try:
        mesh.accounts.create_human("aryan", "password")
        mesh.accounts.create_agent("claude")
        original = mesh.key_pins.trusted

        def unavailable_for_actor(name, *args, **kwargs):
            if name == "aryan":
                raise PinStoreUnavailable("lock_timeout")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(mesh.key_pins, "trusted", unavailable_for_actor)
        with pytest.raises(PinStoreUnavailable, match="lock_timeout"):
            mesh.directory.get("claude")
        with pytest.raises(PinStoreUnavailable, match="lock_timeout"):
            mesh.directory.owner_of("claude")
    finally:
        mesh.close()
