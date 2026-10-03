"""Bound plaintext handoff copies without trusting or pruning arbitrary files."""
import errno
import hashlib
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from agentbridge.gui import api_files


RAW = b'verified attachment'
DIGEST = hashlib.sha256(RAW).hexdigest()


def _select(target):
    return api_files._cache_handoff_target(target, RAW, DIGEST)[0]


def _deny_target(monkeypatch, target):
    replace = api_files.os.replace

    def denied(source, dest):
        if dest == target:
            raise PermissionError(errno.EACCES, 'held by handler')
        return replace(source, dest)

    monkeypatch.setattr(api_files.os, 'replace', denied)
    return replace


def test_locked_stale_target_has_one_fallback_preserved_for_late_reader(tmp_path, monkeypatch):
    target = tmp_path / 'report.pdf'
    api_files._write_private(target, b'x' * len(RAW))
    replace = _deny_target(monkeypatch, target)
    selected = {_select(target) for _ in range(20)}
    assert len(selected) == 1 and target not in selected
    fallback = selected.pop()
    assert fallback.read_bytes() == RAW and fallback.suffix == '.pdf'
    assert set(tmp_path.iterdir()) == {target, fallback}
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) == 2 * len(RAW)

    monkeypatch.setattr(api_files.os, 'replace', replace)
    assert {_select(target) for _ in range(5)} == {target}
    assert target.read_bytes() == RAW
    # OS launch may return before a handler opens the path. Even after the old
    # target unlocks, a late reader must still find its one retained fallback.
    assert fallback.read_bytes() == RAW
    assert set(tmp_path.iterdir()) == {target, fallback}


def test_same_length_tampered_fallback_is_not_reused_or_overwritten(tmp_path, monkeypatch):
    target = tmp_path / 'report.pdf'
    api_files._write_private(target, b'x' * len(RAW))
    _deny_target(monkeypatch, target)
    fallback = _select(target)
    fallback.write_bytes(b'y' * len(RAW))
    for _ in range(3):
        with pytest.raises(FileExistsError):
            _select(target)
    assert fallback.read_bytes() == b'y' * len(RAW)
    assert target.read_bytes() == b'x' * len(RAW)
    assert set(tmp_path.iterdir()) == {target, fallback}


@pytest.mark.parametrize('collision', ('symlink', 'directory', 'hardlink'))
def test_fallback_collisions_never_follow_overwrite_or_remove_other_files(
        tmp_path, monkeypatch, collision):
    target = tmp_path / 'report.pdf'
    api_files._write_private(target, b'x' * len(RAW))
    _deny_target(monkeypatch, target)
    fallback = _select(target)
    fallback.unlink()
    unrelated = tmp_path / 'unrelated.pdf'
    api_files._write_private(unrelated, RAW)
    if collision == 'symlink':
        try:
            fallback.symlink_to(unrelated)
        except OSError:
            pytest.skip('symlink creation unavailable')
    elif collision == 'directory':
        fallback.mkdir()
    else:
        os.link(unrelated, fallback)
    with pytest.raises(FileExistsError):
        _select(target)
    assert unrelated.read_bytes() == RAW
    assert set(tmp_path.iterdir()) == {target, fallback, unrelated}
    assert (fallback.is_symlink() if collision == 'symlink' else fallback.exists())


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permissions; Windows uses app-home ACLs')
def test_nonprivate_cache_is_replaced_and_nonprivate_fallback_is_not_reused(tmp_path, monkeypatch):
    target = tmp_path / 'report.pdf'
    target.write_bytes(RAW)
    target.chmod(0o644)
    assert _select(target) == target
    assert target.stat().st_mode & 0o777 == 0o600
    target.write_bytes(b'x' * len(RAW))
    _deny_target(monkeypatch, target)
    fallback = _select(target)
    fallback.chmod(0o644)
    with pytest.raises(FileExistsError):
        _select(target)
    assert fallback.stat().st_mode & 0o777 == 0o644


def test_cleanup_preserves_a_replacement_with_different_identity(tmp_path):
    target = tmp_path / 'owned.pdf'
    api_files._write_private(target, RAW)
    identity = api_files._verified_cache_identity(target, len(RAW), DIGEST)
    assert identity is not None
    other = tmp_path / 'other.pdf'
    api_files._write_private(other, RAW)
    os.replace(other, target)
    api_files._unlink_cache_identity(target, identity)
    assert target.read_bytes() == RAW


@pytest.mark.parametrize('changed_during_read', (False, True))
def test_windows_stat_creation_time_and_fstat_change_time_are_distinct(
        tmp_path, monkeypatch, changed_during_read):
    target = tmp_path / 'report.pdf'
    api_files._write_private(target, RAW)
    real_lstat = type(target).lstat
    real_fstat = api_files.os.fstat
    calls = []

    def windows_info(info, *, ctime):
        values = {name: getattr(info, name) for name in (
            'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode', 'st_nlink', 'st_uid',
        )}
        return SimpleNamespace(**values, st_ctime_ns=ctime, st_birthtime_ns=100)

    def lstat(path):
        return windows_info(real_lstat(path), ctime=100)

    def fstat(fd):
        calls.append(fd)
        ctime = 201 if changed_during_read and len(calls) > 1 else 200
        return windows_info(real_fstat(fd), ctime=ctime)

    monkeypatch.setattr(type(target), 'lstat', lstat)
    monkeypatch.setattr(api_files.os, 'fstat', fstat)
    identity = api_files._verified_cache_identity(target, len(RAW), DIGEST)
    assert (identity is None) == changed_during_read


@pytest.mark.skipif(os.name != 'nt' or sys.version_info < (3, 12),
                    reason='native Python 3.12+ Windows stat/fstat contract')
def test_windows_path_and_descriptor_generation_match_after_modification(tmp_path):
    target = tmp_path / 'report.pdf'
    api_files._write_private(target, RAW)
    os.utime(target, (1_600_000_000, 1_600_000_000))
    with target.open('rb') as handle:
        path_info, fd_info = target.lstat(), os.fstat(handle.fileno())
        observations = {
            'path_ctime': path_info.st_ctime_ns, 'fd_ctime': fd_info.st_ctime_ns,
            'path_birth': path_info.st_birthtime_ns, 'fd_birth': fd_info.st_birthtime_ns,
            'path_identity': api_files._cache_identity(path_info),
            'fd_identity': api_files._cache_identity(fd_info),
        }
        assert observations['path_identity'] == observations['fd_identity'], observations
    assert api_files._verified_cache_identity(target, len(RAW), DIGEST) is not None


@pytest.mark.skipif(os.name == 'nt', reason='Windows denies swapping this open descriptor')
@pytest.mark.parametrize('write_fails', (False, True))
@pytest.mark.parametrize('unlink_on_error', (False, True))
def test_private_write_detects_replacement_and_never_cleans_its_inode(
        tmp_path, monkeypatch, write_fails, unlink_on_error):
    target = tmp_path / 'saved.pdf'
    fdopen = api_files.os.fdopen

    class SwappedFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def flush(self):
            self.handle.flush()

        def write(self, data):
            written = self.handle.write(data)
            self.handle.flush()
            target.unlink()
            target.write_bytes(b'unrelated replacement')
            if write_fails:
                raise OSError('write failed after replacement')
            return written

    monkeypatch.setattr(api_files.os, 'fdopen',
                        lambda fd, mode, **kw: SwappedFile(fdopen(fd, mode, **kw)))
    with pytest.raises(OSError, match='replacement|replaced'):
        api_files._write_private(target, RAW, unlink_on_error=unlink_on_error)
    assert target.read_bytes() == b'unrelated replacement'


def test_shared_write_failure_never_checks_then_unlinks_a_mutable_name(tmp_path, monkeypatch):
    checks = []

    class SwappingPath(type(tmp_path)):
        def stat(self, **options):
            original = super().stat(**options)
            checks.append(self)
            Path(self).unlink()
            Path(self).write_bytes(b'unrelated replacement')
            return original

    target = SwappingPath(tmp_path) / 'failed.txt'
    fdopen = api_files.os.fdopen

    class FailingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def write(self, data):
            self.handle.write(data[:3])
            raise OSError('simulated disk failure')

    monkeypatch.setattr(api_files.os, 'fdopen',
                        lambda fd, mode, **kw: FailingFile(fdopen(fd, mode, **kw)))
    with pytest.raises(OSError, match='simulated disk failure'):
        api_files._write_private(target, RAW, unlink_on_error=False)
    assert checks == []  # No check/unlink window for a shared directory writer.
    assert Path(target).read_bytes() == b''  # Only the owned descriptor was scrubbed.


@pytest.mark.skipif(os.name != 'nt', reason='Windows native delete-sharing behavior')
def test_windows_release_keeps_bounded_fallback_for_existing_and_late_readers(tmp_path):
    target = tmp_path / 'report.pdf'
    api_files._write_private(target, b'x' * len(RAW))
    with target.open('rb'):
        fallback = _select(target)
        assert fallback != target
        held_fallback = fallback.open('rb')
        assert {_select(target) for _ in range(5)} == {fallback}
    try:
        assert _select(target) == target
        assert held_fallback.read() == RAW
        assert set(tmp_path.iterdir()) == {target, fallback}
    finally:
        held_fallback.close()
    assert _select(target) == target
    assert fallback.read_bytes() == RAW
    assert set(tmp_path.iterdir()) == {target, fallback}
