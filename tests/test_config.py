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


def test_atomic_write_gives_up_cleanly(tmp_path, monkeypatch):
    p = tmp_path / "never.json"
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(TransportError):
        config.atomic_write_json(p, {"x": 1}, retries=3, base_delay=0.001)
    # no tmp litter left behind
    assert list(tmp_path.iterdir()) == []


def test_app_config_roundtrip(tmp_path):
    config.save_app_config({"mesh_root": "X:/synced/mesh2"}, home=tmp_path)
    assert config.load_app_config(home=tmp_path)["mesh_root"] == "X:/synced/mesh2"
    assert json.loads((tmp_path / "config.json").read_text("utf-8"))
