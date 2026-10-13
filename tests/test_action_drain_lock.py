import hashlib
import sqlite3
import subprocess
import sys
import pytest
import test_user_state_actions as cases

world = cases.world


def test_external_process_lock_contention_precedes_enqueue_and_crash_releases(world):
    coordinator, provider, reopen = world
    queue = reopen()
    stripe = hashlib.sha256(cases.PATH.encode()).digest()[0]
    path = coordinator.path.parent / "action-drains" / str(stripe)
    script = 'from agentbridge.core.lock import SingleInstance; import sys; lock=SingleInstance(sys.argv[1],fail_open=False); assert lock.acquire(); print("ready",flush=True); sys.stdin.read()'
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="busy"):
            cases._write(queue, "star", {"msg_ids": ["busy"]})
        with sqlite3.connect(coordinator.path) as conn:
            assert conn.execute("SELECT count(*) FROM user_state_actions").fetchone() == (0,)
        assert not provider.effects
    finally:
        process.kill()
        process.wait(timeout=5)
    document, changed = cases._write(reopen(), "star", {"msg_ids": ["after-crash"]})
    assert changed and document["starred"] == ["after-crash"]
