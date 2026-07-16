"""V111 app lock — the local passphrase gate over the machine-trust model:
boot-locked when configured, API-level enforcement (authed refuses while
locked), backoff, and the account-password recovery lane."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.timeout(120)


def test_applock_unit_roundtrip(tmp_path):
    from agentbridge.gui.applock import AppLock

    lk = AppLock(tmp_path)
    assert not lk.enabled and not lk.locked
    lk.configure("hunter42", 5)
    assert lk.enabled and lk.autolock_min == 5
    assert lk.verify("hunter42") and not lk.verify("nope")
    assert not lk.locked                     # configuring never locks you out
    # a NEW process boots locked whenever the verifier exists
    lk2 = AppLock(tmp_path)
    assert lk2.enabled and lk2.locked
    lk2.set_autolock(15)
    assert lk2.autolock_min == 15 and lk2.verify("hunter42")  # hash kept
    lk2.remove()
    assert not lk2.enabled and not lk2.locked


def test_applock_backoff(tmp_path):
    from agentbridge.gui.applock import AppLock

    lk = AppLock(tmp_path)
    lk.configure("right", 0)
    assert lk.note_failure() == 0.0          # three free tries…
    assert lk.note_failure() == 0.0
    assert lk.note_failure() == 1.0          # …then 1s, 2s, 4s (capped)
    assert lk.retry_in() > 0.0
    assert lk.note_failure() == 2.0
    lk.note_success()
    assert lk.retry_in() == 0.0


def test_applock_endpoint_flow(rig):
    rig.signup()
    # not configured: state says so, manual lock refuses politely
    st = rig.get("/api/state")
    assert st["app_lock"] == {"enabled": False, "locked": False,
                              "autolock_min": 0}
    assert "not set up" in rig.post("/api/applock/lock")["error"]

    # enable (no current needed on the first set) — enabling never locks
    r = rig.post("/api/applock/set", passphrase="open sesame", autolock_min=5)
    assert r["ok"] and r["enabled"] and not r["locked"]
    assert r["autolock_min"] == 5
    assert rig.post("/api/applock/lock")["locked"] is True

    # locked: every authed endpoint refuses WITH the distinguishing flag
    # (the client must tell this apart from a sign-out), while /api/state
    # keeps answering — the lock page polls it
    r = rig.get("/api/mesh/state")
    assert r["error"] and r["locked"] is True
    st = rig.get("/api/state")
    assert st["app_lock"]["locked"] is True and st["user"] == "aryan"

    # wrong passphrase -> error; the right one opens the app again
    r = rig.post("/api/applock/unlock", passphrase="nope")
    assert "Wrong passphrase" in r["error"]
    assert rig.post("/api/applock/unlock", passphrase="open sesame")["ok"]
    assert rig.get("/api/mesh/state")["user"] == "aryan"

    # the ACCOUNT password also unlocks (forgot-the-lock recovery: it
    # already grants a full sign-out/sign-in swap, so this loses nothing)
    rig.post("/api/applock/lock")
    assert rig.post("/api/applock/unlock", passphrase="hexagon")["ok"]

    # retime without touching the passphrase; a short one is refused
    assert rig.post("/api/applock/set", autolock_min=0)["autolock_min"] == 0
    r = rig.post("/api/applock/set", passphrase="abc", current="open sesame")
    assert "at least 4" in r["error"]
    # change requires the current secret; then the new one verifies
    assert "wrong" in rig.post("/api/applock/set", passphrase="fresh-one",
                               current="bad")["error"]
    assert rig.post("/api/applock/set", passphrase="fresh-one",
                    current="open sesame")["ok"]
    rig.post("/api/applock/lock")
    assert rig.post("/api/applock/unlock", passphrase="fresh-one")["ok"]

    # disable (passphrase: "") — also gated on the current secret
    r = rig.post("/api/applock/set", passphrase="", current="fresh-one")
    assert r["ok"] and not r["enabled"] and not r["locked"]
    assert rig.get("/api/state")["app_lock"]["enabled"] is False
