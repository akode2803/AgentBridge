from __future__ import annotations

import hashlib
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentbridge import crypto
from agentbridge.gui.context import GuiApp
from agentbridge.mesh.lifecycle import LifecycleUnavailable


@pytest.fixture(autouse=True)
def _fresh_verification_cache():
    crypto._reset_verify_cache_after_fork()
    yield
    crypto._reset_verify_cache_after_fork()


def _signed(payload: bytes = b"payload") -> tuple[str, str, bytes]:
    bundle = crypto.generate_identity()
    public, _agree = crypto.identity_pubs(bundle)
    return public, crypto.sign(bundle, payload), payload


def _count_uncached(monkeypatch):
    original = crypto._verify_uncached
    calls = []

    def counted(public, signature, payload):
        calls.append((public, signature, payload))
        return original(public, signature, payload)

    monkeypatch.setattr(crypto, "_verify_uncached", counted)
    return calls


def test_positive_exact_inputs_hit_but_changed_and_negative_inputs_do_not(
        monkeypatch):
    public, signature, payload = _signed()
    other_public, other_signature, _ = _signed(payload)
    calls = _count_uncached(monkeypatch)

    assert crypto.verify(public, signature, payload)
    assert crypto.verify(public, signature, payload)
    assert len(calls) == 1

    assert not crypto.verify(public, signature, payload + b"!")
    assert not crypto.verify(public, signature, payload + b"!")
    assert not crypto.verify(public, other_signature, payload)
    assert not crypto.verify(other_public, signature, payload)
    assert len(calls) == 5  # negative results never enter the cache


def test_oversized_valid_payload_uses_uncached_path(monkeypatch):
    payload = b"x" * (crypto._VERIFY_CACHE_MAX_PAYLOAD + 1)
    public, signature, payload = _signed(payload)
    calls = _count_uncached(monkeypatch)
    assert crypto.verify(public, signature, payload)
    assert crypto.verify(public, signature, payload)
    assert len(calls) == 2
    assert not crypto._verify_cache


@pytest.mark.parametrize("which,value", [
    ("public", None),
    ("public", 7),
    ("signature", []),
    ("payload", bytearray(b"payload")),
])
def test_wrong_runtime_types_preserve_uncached_behavior(which, value):
    public, signature, payload = _signed()
    inputs = {"public": public, "signature": signature, "payload": payload}
    inputs[which] = value

    def outcome(function):
        try:
            return ("return", function(
                inputs["public"], inputs["signature"], inputs["payload"]))
        except Exception as exc:  # compare the existing runtime contract
            return ("raise", type(exc), str(exc))

    assert outcome(crypto.verify) == outcome(crypto._verify_uncached)
    assert not crypto._verify_cache


def test_str_subclasses_and_malformed_base64_stay_uncached(monkeypatch):
    public, signature, payload = _signed()
    calls = _count_uncached(monkeypatch)

    class Text(str):
        pass

    assert crypto.verify(Text(public), signature, payload)
    assert crypto.verify(Text(public), signature, payload)
    assert not crypto.verify("!" * 44, signature, payload)
    assert not crypto.verify("!" * 44, signature, payload)
    assert len(calls) == 4
    assert not crypto._verify_cache


def test_lru_capacity_and_eviction(monkeypatch):
    monkeypatch.setattr(crypto, "_VERIFY_CACHE_CAPACITY", 2)
    calls = _count_uncached(monkeypatch)
    values = [_signed(f"payload-{index}".encode()) for index in range(4)]
    for value in values[:3]:
        assert crypto.verify(*value)
    assert len(crypto._verify_cache) == 2
    assert crypto.verify(*values[1])  # refresh payload-1
    assert crypto.verify(*values[3])  # evicts payload-2
    assert crypto.verify(*values[0])  # payload-0 was already evicted
    assert len(calls) == 5
    assert len(crypto._verify_cache) == 2


def test_concurrent_misses_do_not_hold_cache_lock_during_crypto(monkeypatch):
    public, signature, payload = _signed()
    original = crypto._verify_uncached
    entered = threading.Barrier(8)
    calls = 0
    calls_lock = threading.Lock()

    def checked(*args):
        nonlocal calls
        assert crypto._verify_cache_lock.acquire(timeout=0.2)
        crypto._verify_cache_lock.release()
        with calls_lock:
            calls += 1
        entered.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(crypto, "_verify_uncached", checked)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: crypto.verify(public, signature, payload), range(8)))
    assert results == [True] * 8
    assert 1 <= calls <= 8
    assert len(crypto._verify_cache) == 1
    settled = calls
    assert crypto.verify(public, signature, payload)
    assert calls == settled


def test_cache_failures_preserve_single_verification_and_positive_result(
        monkeypatch):
    public, signature, payload = _signed()
    calls = _count_uncached(monkeypatch)

    def lookup_failure(_digest):
        raise RuntimeError("injected lookup failure")

    monkeypatch.setattr(crypto, "_verify_cache_hit", lookup_failure)
    assert crypto.verify(public, signature, payload)
    assert len(calls) == 1

    crypto._reset_verify_cache_after_fork()
    monkeypatch.undo()
    calls = _count_uncached(monkeypatch)

    def insertion_failure(_digest):
        raise RuntimeError("injected insertion failure")

    monkeypatch.setattr(crypto, "_remember_positive_verification", insertion_failure)
    assert crypto.verify(public, signature, payload)
    assert len(calls) == 1


def test_secret_generation_failure_disables_only_the_memo(monkeypatch):
    public, signature, payload = _signed()
    calls = _count_uncached(monkeypatch)

    def unavailable(_count):
        raise OSError("injected randomness failure")

    monkeypatch.setattr(crypto.secrets, "token_bytes", unavailable)
    crypto._reset_verify_cache_after_fork()
    assert crypto._verify_cache_secret is None
    assert crypto.verify(public, signature, payload)
    assert crypto.verify(public, signature, payload)
    assert len(calls) == 2
    assert not crypto._verify_cache


def _fork_verify(connection, public, signature, payload, parent_secret_hash):
    child_secret = crypto._verify_cache_secret
    before = len(crypto._verify_cache)
    result = crypto.verify(public, signature, payload)
    connection.send((
        result,
        before,
        len(crypto._verify_cache),
        hashlib.sha256(child_secret or b"").digest() != parent_secret_hash,
    ))
    connection.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_child_replaces_held_lock_cache_and_secret():
    public, signature, payload = _signed()
    assert crypto.verify(public, signature, payload)
    assert len(crypto._verify_cache) == 1
    parent_secret_hash = hashlib.sha256(crypto._verify_cache_secret or b"").digest()
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_fork_verify,
        args=(child, public, signature, payload, parent_secret_hash),
    )
    crypto._verify_cache_lock.acquire()
    try:
        process.start()
        child.close()
        assert parent.poll(5), "fork child inherited a locked verification cache"
        assert parent.recv() == (True, 0, 1, True)
        process.join(5)
        assert process.exitcode == 0
    finally:
        crypto._verify_cache_lock.release()
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def _stop_background(app: GuiApp) -> None:
    mesh = app.mesh
    assert mesh is not None
    mesh.sync.stop()
    if app._sync_thread is not None:
        app._sync_thread.join(15)
    mesh.outbox.stop()
    mesh.notifier.stop()
    mesh.presence.stop()
    assert not (app._sync_thread and app._sync_thread.is_alive())
    assert not (mesh.outbox._thread and mesh.outbox._thread.is_alive())
    assert not (mesh.notifier._thread and mesh.notifier._thread.is_alive())
    assert not (mesh.presence._thread and mesh.presence._thread.is_alive())


def test_warm_verification_cache_preserves_lifecycle_unavailable(tmp_path, monkeypatch):
    root, home = tmp_path / "mesh", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    app = GuiApp(root, home=home, machine="r194-authority", encrypt=True,
                 poll_s=0.05)
    try:
        app.signup("aryan", "", "hexagon")
        mesh = app.mesh
        assert mesh is not None
        chat = mesh.create_chat("Disposable authority probe", members=[])
        mesh.post(chat.id, "probe-first")
        mesh.post(chat.id, "probe-second")
        _stop_background(app)
        messages = mesh.messages_for(chat.id)
        assert [message.body for message in messages if message.body] == [
            "probe-first", "probe-second",
        ]
        assert crypto._verify_cache

        original_unseal = mesh.sealer.unseal
        completed = 0

        def barrier(chat_id, envelope):
            nonlocal completed
            body = original_unseal(chat_id, envelope)
            completed += 1
            if completed == 1:
                def unavailable(*_args, **_kwargs):
                    raise OSError("injected retained authority I/O failure")

                monkeypatch.setattr(
                    mesh.directory.store, "observe_lifecycle_head", unavailable)
            return body

        monkeypatch.setattr(mesh.sealer, "unseal", barrier)
        with pytest.raises(LifecycleUnavailable):
            mesh.messages_for(chat.id)
        assert completed == 1
    finally:
        app.close()
