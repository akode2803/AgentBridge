"""Bound plaintext handoff copies without trusting or pruning arbitrary files."""
import errno
import hashlib
import os

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
