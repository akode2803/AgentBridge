"""SyncEngine: parallel catch-up, membership gating, incremental offsets."""

from agentbridge.mesh.sync import SyncEngine
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport


def seed(tx, chat_id, sender, n, start=1):
    for i in range(n):
        tx.append_log(chat_id, f"{sender}@m", {"id": f"{chat_id}-m{start + i}", "ns": start + i})


def test_parallel_catchup_across_chats(tmp_path):
    tx = FolderTransport(tmp_path / "mesh2")
    for c in range(6):
        seed(tx, f"chat{c}", "ann", 10)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store, workers=4)
    assert eng.sync_once() == 60
    assert store.message_count("chat3") == 10
    # second pass: offsets say nothing changed
    assert eng.sync_once() == 0
    store.close()


def test_membership_gate_never_fetches_foreign_chats(tmp_path):
    """Requirement: the mesh fetches ONLY what this identity needs."""
    tx = FolderTransport(tmp_path / "mesh2")
    seed(tx, "mine", "ann", 3)
    seed(tx, "theirs", "sue", 3)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store, is_member=lambda c: c == "mine")
    assert eng.my_chat_ids() == ["mine"]
    assert eng.sync_once() == 3
    assert store.message_count("theirs") == 0  # never even read
    store.close()


def test_incremental_appends_only_new(tmp_path):
    tx = FolderTransport(tmp_path / "mesh2")
    seed(tx, "c1", "ann", 5)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    assert eng.sync_chat("c1") == 5
    seed(tx, "c1", "ann", 2, start=6)
    seed(tx, "c1", "bob", 1, start=100)  # a second per-device log appears
    assert eng.sync_chat("c1") == 3
    assert store.message_count("c1") == 8
    store.close()


def test_shrunken_log_heals_via_dedup(tmp_path):
    tx = FolderTransport(tmp_path / "mesh2")
    seed(tx, "c1", "ann", 5)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    eng.sync_chat("c1")

    # sync conflict rewrites the file with only the first record
    p = tx.local_path("chats/c1/msgs/ann@m.jsonl")
    first_line = p.read_bytes().split(b"\n")[0] + b"\n"
    p.write_bytes(first_line)

    assert eng.sync_chat("c1") == 0  # re-read all, everything already cached
    assert store.message_count("c1") == 5  # nothing lost locally
    store.close()


def test_run_loop_stops_cleanly(tmp_path):
    import threading
    import time

    tx = FolderTransport(tmp_path / "mesh2")
    seed(tx, "c1", "ann", 2)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    seen = []
    t = threading.Thread(
        target=lambda: eng.run(poll_s=0.05, on_new=seen.append), daemon=True
    )
    t.start()
    deadline = time.time() + 5.0
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    eng.stop()
    t.join(5.0)
    assert not t.is_alive() and seen and seen[0] == 2
    store.close()
