"""config: resilient JSON primitives (regression home of the 8D OneDrive-lock burn)."""

import json
import os

import pytest

from agentbridge.core import config
from agentbridge.core.errors import TransportError


def test_read_json_tolerant(tmp_path):
    missing = config.read_json(tmp_path / "nope.json", default={"d": 1})
    assert missing == {"d": 1}
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{half a rec", encoding="utf-8")
    assert config.read_json(corrupt, default=None) is None


def test_atomic_write_roundtrip(tmp_path):
    p = tmp_path / "deep" / "cfg.json"  # parent dirs auto-created
    config.atomic_write_json(p, {"a": "em—dash", "n": 5})
    assert config.read_json(p) == {"a": "em—dash", "n": 5}
    # utf-8 on disk, no BOM (the PowerShell-corruption class must stay dead)
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and "em—dash".encode() in raw


def test_atomic_write_retries_transient_lock(tmp_path, monkeypatch):
    """OneDrive locks the target mid-sync -> first replaces fail, then heal."""
    p = tmp_path / "locked.json"
    real_replace = os.replace
    fails = {"left": 2}

    def flaky(src, dst):
        if fails["left"] > 0:
            fails["left"] -= 1
            raise PermissionError("locked by OneDrive")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    config.atomic_write_json(p, {"ok": True}, base_delay=0.001)
    assert config.read_json(p) == {"ok": True}
    assert fails["left"] == 0


def test_read_json_retries_transient_lock(tmp_path, monkeypatch):
    """A transient PermissionError (Windows os.replace window / OneDrive lock)
    is NOT missing data — retry, don't fall through to the default. This is
    the read-side twin of the write retry; a spurious default here reads as
    'no such chat' at the membership layer (the R13 Windows-CI burn)."""
    p = tmp_path / "meta.json"
    config.atomic_write_json(p, {"members": {"aryan": {}}})
    real_open = type(p).open
    calls = {"n": 0}

    def flaky_open(self, *a, **k):
        if str(self) == str(p):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("locked mid-replace")
        return real_open(self, *a, **k)

    monkeypatch.setattr(type(p), "open", flaky_open)
    monkeypatch.setattr(config, "_READ_DELAY", 0.001)
    got = config.read_json(p, default=None)
    assert got == {"members": {"aryan": {}}}   # healed, not defaulted
    assert calls["n"] == 3


def test_read_json_missing_is_immediate(tmp_path, monkeypatch):
    """A genuinely absent file returns default at once — no retry spin."""
    monkeypatch.setattr(config, "_READ_DELAY", 10)  # would hang if it retried
    assert config.read_json(tmp_path / "gone.json", default="x") == "x"


def test_read_json_persistent_lock_defaults(tmp_path, monkeypatch):
    """If the lock never clears, fall back to default (sync tolerance) rather
    than raise — a slow consumer heals on the next poll."""
    p = tmp_path / "stuck.json"
    config.atomic_write_json(p, {"v": 1})
    monkeypatch.setattr(config, "_READ_DELAY", 0.001)
    monkeypatch.setattr(
        type(p), "open",
        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError()),
    )
    assert config.read_json(p, default={"fallback": True}) == {"fallback": True}


def test_atomic_write_gives_up_cleanly(tmp_path, monkeypatch):
    p = tmp_path / "never.json"
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(TransportError):
        config.atomic_write_json(p, {"x": 1}, retries=3, base_delay=0.001)
    # no tmp litter left behind
    assert list(tmp_path.iterdir()) == []


def test_concurrent_io_never_fails_or_misreads(tmp_path):
    """The live shape, hardened: MULTIPLE writers rewriting one doc (a request
    handler and the sync thread both refold meta.json) while readers poll it.
    Writers must never exhaust their retries (Windows: os.replace fails while
    any same-process handle is open — the striped-lock fix) and a reader must
    never see the default for an always-present file."""
    import threading
    import time

    p = tmp_path / "meta.json"
    config.atomic_write_json(p, {"members": {"aryan": {}}, "n": 0})
    stop = threading.Event()
    misses = {"n": 0}
    write_errors = []

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                config.atomic_write_json(p, {"members": {"aryan": {}}, "n": i})
            except Exception as e:  # noqa: BLE001 — collected, asserted below
                write_errors.append(repr(e))

    def reader():
        while not stop.is_set():
            if config.read_json(p, default=None) is None:
                misses["n"] += 1

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join()
    assert write_errors == []
    assert misses["n"] == 0


def test_app_config_roundtrip(tmp_path):
    config.save_app_config({"mesh_root": "X:/synced/mesh2"}, home=tmp_path)
    assert config.load_app_config(home=tmp_path)["mesh_root"] == "X:/synced/mesh2"
    assert json.loads((tmp_path / "config.json").read_text("utf-8"))


# --- single-instance lock (R32.2: the GUI's stray-second-instance guard) ---

def test_single_instance_blocks_a_second_holder(tmp_path):
    from agentbridge.core.lock import SingleInstance

    a = SingleInstance(tmp_path / "gui-7787.lock")
    assert a.acquire() is True
    b = SingleInstance(tmp_path / "gui-7787.lock")
    assert b.acquire() is False          # a live holder blocks the second
    a.release()
    assert b.acquire() is True           # freed on release -> next one gets it
    b.release()


def test_single_instance_distinct_ports_coexist(tmp_path):
    from agentbridge.core.lock import SingleInstance

    prod = SingleInstance(tmp_path / "gui-7787.lock")
    dev = SingleInstance(tmp_path / "gui-7790.lock")
    assert prod.acquire() is True
    assert dev.acquire() is True         # a different port is a different lock
    prod.release(); dev.release()
