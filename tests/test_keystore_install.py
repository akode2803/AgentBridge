"""Durable, no-clobber identity installation at the local keystore boundary."""

from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from agentbridge import crypto
from agentbridge.mesh import keyring
from agentbridge.mesh.keyring import KeyStore
from agentbridge.node.security import LocalSecurityError


def _racing_install(home: str, bundle: bytes, start, result) -> None:
    start.wait()
    try:
        installed = KeyStore(Path(home)).install_if_absent_or_identical("alice", bundle)
        result.put(("ok", installed))
    except Exception as exc:
        result.put((type(exc).__name__, str(exc)))


def test_install_is_idempotent_and_private(tmp_path):
    store = KeyStore(tmp_path)
    bundle = crypto.generate_identity()
    assert store.install_if_absent_or_identical("alice", bundle) is True
    first = (tmp_path / "keys" / "alice.key").read_bytes()
    assert store.install_if_absent_or_identical("alice", bundle) is False
    assert (tmp_path / "keys" / "alice.key").read_bytes() == first
    assert store.load("alice") == bundle
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "keys").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "keys" / "alice.key").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "keys" / ".identity-install.lock").stat().st_mode) == 0o600


def test_install_rejects_conflict_corruption_and_unsafe_name(tmp_path):
    store = KeyStore(tmp_path)
    original = crypto.generate_identity()
    assert store.install_if_absent_or_identical("alice", original)
    path = tmp_path / "keys" / "alice.key"
    prior = path.read_bytes()
    with pytest.raises(ValueError, match="conflicts"):
        store.install_if_absent_or_identical("alice", crypto.generate_identity())
    assert path.read_bytes() == prior
    for name in ("../alice", "a/b", "AlicE", "a", "a\\b", "a.key", ""):
        with pytest.raises(ValueError, match="name"):
            store.install_if_absent_or_identical(name, original)
    with pytest.raises(ValueError, match="bundle"):
        store.install_if_absent_or_identical("alice", b"wrong")
    path.write_bytes(b"dpapi1:!!!")
    assert store.load("alice") is None
    with pytest.raises(LocalSecurityError, match="unreadable"):
        store.install_if_absent_or_identical("alice", original)
    assert path.read_bytes() == b"dpapi1:!!!"


def test_install_hardens_legacy_file_without_rewriting_it(tmp_path):
    store = KeyStore(tmp_path)
    bundle = crypto.generate_identity()
    store.save("alice", bundle)
    path = tmp_path / "keys" / "alice.key"
    prior = path.read_bytes()
    assert store.install_if_absent_or_identical("alice", bundle) is False
    assert path.read_bytes() == prior
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_identical_legacy_key_requires_inode_flush(tmp_path, monkeypatch):
    store = KeyStore(tmp_path)
    bundle = crypto.generate_identity()
    store.save("alice", bundle)
    path = store.dir / "alice.key"
    prior = path.read_bytes()
    original_fsync = keyring.os.fsync
    flushed_key = []

    def reject_key_flush(fd):
        if os.fstat(fd).st_ino == path.stat().st_ino:
            flushed_key.append(True)
            raise OSError("key inode flush failed")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(keyring.os, "fsync", reject_key_flush)
        with pytest.raises(OSError, match="key inode flush failed"):
            store.install_if_absent_or_identical("alice", bundle)
    assert flushed_key
    assert path.read_bytes() == prior
    assert store.install_if_absent_or_identical("alice", bundle) is False


def test_install_never_treats_read_failure_as_absence(tmp_path, monkeypatch):
    store = KeyStore(tmp_path)
    original = crypto.generate_identity()
    store.save("alice", original)
    path = store.dir / "alice.key"
    prior = path.read_bytes()

    with monkeypatch.context() as patch:
        patch.setattr(keyring.os, "read", lambda fd, size: (_ for _ in ()).throw(OSError("read failed")))
        with pytest.raises(OSError, match="read failed"):
            store.install_if_absent_or_identical("alice", crypto.generate_identity())
    assert path.read_bytes() == prior


def test_install_rejects_symlink_key_and_lock(tmp_path):
    store = KeyStore(tmp_path)
    store.dir.mkdir()
    target = tmp_path / "outside"
    target.write_bytes(b"untouched")
    path = store.dir / "alice.key"
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(LocalSecurityError):
        store.install_if_absent_or_identical("alice", crypto.generate_identity())
    assert target.read_bytes() == b"untouched"
    path.unlink()
    (store.dir / ".identity-install.lock").unlink()
    (store.dir / ".identity-install.lock").symlink_to(target)
    with pytest.raises(LocalSecurityError):
        store.install_if_absent_or_identical("alice", crypto.generate_identity())
    assert target.read_bytes() == b"untouched"


def test_install_retry_after_prepublication_failure(tmp_path, monkeypatch):
    store = KeyStore(tmp_path)
    bundle = crypto.generate_identity()
    real_link = keyring.os.link

    def fail_link(src, dst, **kwargs):
        if Path(dst).name == "alice.key":
            raise OSError("injected publication failure")
        return real_link(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(keyring.os, "link", fail_link)
        with pytest.raises(OSError, match="publication failure"):
            store.install_if_absent_or_identical("alice", bundle)
    assert not (store.dir / "alice.key").exists()
    assert not tuple(store.dir.glob(".alice.key.*"))
    assert store.install_if_absent_or_identical("alice", bundle) is True


def test_install_never_overwrites_key_appearing_at_publication(tmp_path, monkeypatch):
    store = KeyStore(tmp_path)
    proposed = crypto.generate_identity()
    competitor = crypto.generate_identity()
    real_link = keyring.os.link

    def competing_publish(src, dst, **kwargs):
        # Simulate a legacy writer that does not participate in the install
        # lock and publishes after the installer's absent check.
        Path(dst).write_text(crypto.b64e(competitor), encoding="ascii")
        return real_link(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(keyring.os, "link", competing_publish)
        with pytest.raises(LocalSecurityError, match="appeared"):
            store.install_if_absent_or_identical("alice", proposed)
    assert store.load("alice") == competitor
    assert not tuple(store.dir.glob(".alice.key.*"))


def test_install_retry_after_directory_sync_failure(tmp_path, monkeypatch):
    store = KeyStore(tmp_path)
    bundle = crypto.generate_identity()
    real_sync = keyring._sync_directory
    flushes = []

    def fail_key_dir(path):
        flushes.append(path)
        if path == store.dir:
            raise OSError("sync failed")
        real_sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(keyring, "_sync_directory", fail_key_dir)
        with pytest.raises(OSError, match="sync failed"):
            store.install_if_absent_or_identical("alice", bundle)
    assert store.load("alice") == bundle
    assert store.dir in flushes
    retried_flushes = []
    with monkeypatch.context() as patch:
        patch.setattr(keyring, "_sync_directory", lambda path: (retried_flushes.append(path), real_sync(path)))
        assert store.install_if_absent_or_identical("alice", bundle) is False
    assert store.dir in retried_flushes


def test_two_processes_cannot_install_different_bundles(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    result = ctx.Queue()
    first = crypto.generate_identity()
    second = crypto.generate_identity()
    processes = [
        ctx.Process(target=_racing_install, args=(str(tmp_path), bundle, start, result))
        for bundle in (first, second)
    ]
    for process in processes:
        process.start()
    try:
        start.set()
        outcomes = [result.get(timeout=15) for _ in processes]
    finally:
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join()
    assert [outcome[0] for outcome in outcomes].count("ok") == 1
    assert [outcome[0] for outcome in outcomes].count("ValueError") == 1
    assert KeyStore(tmp_path).load("alice") in (first, second)


def test_install_rejects_key_replaced_after_publication(tmp_path, monkeypatch):
    store = KeyStore(tmp_path)
    competitor = crypto.generate_identity()
    real_link = keyring.os.link

    def replaced_after_link(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        # A non-cooperating writer replaces the key right after publication.
        other = Path(dst).with_suffix(".other")
        other.write_text(crypto.b64e(competitor), encoding="ascii")
        os.replace(other, dst)

    with monkeypatch.context() as patch:
        patch.setattr(keyring.os, "link", replaced_after_link)
        with pytest.raises(LocalSecurityError, match="changed during install"):
            store.install_if_absent_or_identical("alice", crypto.generate_identity())
    assert store.load("alice") == competitor


def test_save_and_forget_wait_for_another_holder_of_the_install_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as Pending

    store = KeyStore(tmp_path)
    first, second = crypto.generate_identity(), crypto.generate_identity()
    assert store.install_if_absent_or_identical("alice", first) is True
    key_file = store._path("alice")
    # A separate open of the lock file contends like another process would.
    # (Read the file directly: the waiting writer holds the in-process lock.)
    with ThreadPoolExecutor(max_workers=1) as pool:
        before = key_file.read_bytes()
        with keyring._private_install_lock(store.dir / ".identity-install.lock"):
            saving = pool.submit(store.save, "alice", second)
            with pytest.raises(Pending):
                saving.result(timeout=0.3)
            assert key_file.read_bytes() == before
        saving.result(timeout=5)
        assert store.load("alice", upgrade=False) == second
        before = key_file.read_bytes()
        with keyring._private_install_lock(store.dir / ".identity-install.lock"):
            forgetting = pool.submit(store.forget, "alice")
            with pytest.raises(Pending):
                forgetting.result(timeout=0.3)
            assert key_file.read_bytes() == before
        forgetting.result(timeout=5)
    assert store.load("alice") is None


def test_install_lock_is_reentrant_for_nested_key_writes(tmp_path):
    store = KeyStore(tmp_path)
    bundle = crypto.generate_identity()
    assert store.install_if_absent_or_identical("alice", bundle) is True
    with store._install_lock():
        store.save("bob", bundle)
        assert store.forget_if_identical("bob", bundle) is True
        store.hold_recovery_code("alice", "CODE-1234")
        assert store.release_recovery_code("alice") is True
    assert keyring._INSTALL_DEPTH == {}
    assert store.load("bob") is None
    assert store.load("alice") == bundle
