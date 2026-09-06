"""Database location stays bound to the Store, not a mutable input alias."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentbridge.store.db import LogIngestionConflict, Store


def test_retargeted_symlink_cannot_relabel_an_open_database(tmp_path):
    original_path = tmp_path / "original.sqlite"
    copy_path = tmp_path / "copy.sqlite"
    alias = tmp_path / "active.sqlite"
    original = Store(original_path)
    with sqlite3.connect(copy_path) as backup:
        original._conn().backup(backup)
    original.close()
    alias.symlink_to(original_path)
    opened = Store(alias)
    copied = Store(copy_path)
    try:
        before = opened.capture_log_position("chat", "writer")
        alias.unlink()
        alias.symlink_to(copy_path)
        after = opened.capture_log_position("chat", "writer")
        assert after == before
        assert after.database_path == str(original_path.resolve())
        assert copied.capture_log_position("chat", "writer").incarnation == after.incarnation
        with pytest.raises(LogIngestionConflict):
            copied.ingest_log("chat", "writer", after, 1, [])

        def another_connection():
            try:
                return opened.capture_log_position("chat", "writer")
            finally:
                opened.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(another_connection).result() == before
    finally:
        opened.close()
        copied.close()


def test_relative_store_path_stays_bound_after_cwd_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opened = Store("cache.sqlite")
    try:
        before = opened.capture_log_position("chat", "writer")
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        opened.close()
        assert opened.capture_log_position("chat", "writer") == before
        assert not (other / "cache.sqlite").exists()
    finally:
        opened.close()
