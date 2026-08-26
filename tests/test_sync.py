"""SyncEngine: parallel catch-up, membership gating, incremental offsets,
and the R30 change-feed fast path (one query per tick on cloud drivers)."""

from agentbridge.mesh.sync import SyncEngine
from agentbridge.store.db import Store
from agentbridge.core.latency import sink_for_store
from agentbridge.transport.base import Transport, Watcher
from agentbridge.transport.folder import FolderTransport


def seed(tx, chat_id, sender, n, start=1):
    for i in range(n):
        # `from` must be the log owner — the sync ingestion sanity check
        # (R13.5) drops records that claim another identity
        tx.append_log(chat_id, f"{sender}@m",
                      {"id": f"{chat_id}-m{start + i}", "ns": start + i, "from": sender})


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


def test_sync_observation_is_once_and_names_discovery_lane(tmp_path):
    tx = FolderTransport(tmp_path / "mesh2")
    seed(tx, "c1", "ann", 1)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    assert eng.sync_chat("c1", lane="hint") == 1
    assert eng.sync_chat("c1", lane="poll") == 0
    rows = [row for row in sink_for_store(store).read()
            if row["stage"] == "sync_observed"]
    assert len(rows) == 1 and rows[0]["lane"] == "hint"
    observed_ns, observed_mono, observed_clock = store.message_observation(
        "c1", "c1-m1")
    assert observed_ns > 0 and observed_mono > 0 and observed_clock
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


def test_ingestion_drops_records_claiming_another_identity(tmp_path):
    """R13.5: a per-device log is single-writer, so sync drops any record whose
    `from` isn't the log's owner — a client can't smuggle records attributed
    to someone else through its own log."""
    tx = FolderTransport(tmp_path / "mesh2")
    # eve's log carries one honest record and one spoofed as "ann"
    tx.append_log("c1", "eve@m", {"id": "ok", "ns": 1, "from": "eve"})
    tx.append_log("c1", "eve@m", {"id": "spoof", "ns": 2, "from": "ann"})
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    eng.sync_chat("c1")
    ids = {r.get("id") for r in store.messages("c1")}
    assert ids == {"ok"}   # the spoofed record never entered the store


# --------------------------------------------------- the change feed (R30)

class FeedTransport(Transport):
    """In-memory transport with a global monotonic change feed — the shape a
    cloud driver (supabase) exposes. Counts the reads the feed is meant to
    replace so tests can prove the one-query idle tick."""

    scheme = "feed"
    has_change_feed = True

    def __init__(self) -> None:
        self.rows: list[tuple[int, str, str, dict]] = []  # (id, chat, log, rec)
        self._seq = 0
        self.docs: dict[str, dict] = {}
        self.fail_feed = False
        self.calls = {"list_logs": 0, "changed_logs": 0, "read_log": 0,
                      "list_chat_ids": 0}

    def append_log(self, chat_id, log_name, record):
        self._seq += 1
        self.rows.append((self._seq, chat_id, log_name, dict(record)))

    def read_log(self, chat_id, log_name, offset=0):
        self.calls["read_log"] += 1
        out, new_offset = [], int(offset)
        for rid, c, log, rec in self.rows:
            if c == chat_id and log == log_name and rid > offset:
                out.append(rec)
                new_offset = rid
        return out, new_offset

    def list_logs(self, chat_id):
        self.calls["list_logs"] += 1
        heads: dict[str, int] = {}
        for rid, c, log, _rec in self.rows:
            if c == chat_id:
                heads[log] = max(heads.get(log, 0), rid)
        return sorted(heads.items())

    def list_chat_ids(self):
        self.calls["list_chat_ids"] += 1
        return sorted({c for _rid, c, _log, _rec in self.rows})

    def changed_logs(self, cursor):
        self.calls["changed_logs"] += 1
        if self.fail_feed:
            raise ConnectionError("cloud unreachable")
        pairs, seen, new_cursor = [], set(), int(cursor)
        for rid, c, log, _rec in self.rows:
            if rid > cursor:
                new_cursor = max(new_cursor, rid)
                if (c, log) not in seen:
                    seen.add((c, log))
                    pairs.append((c, log))
        return pairs, new_cursor

    def get_doc(self, path, default=None): return self.docs.get(path, default)
    def put_doc(self, path, data): self.docs[path] = data
    def delete_doc(self, path): self.docs.pop(path, None)
    def list_docs(self, prefix): return sorted(
        p for p in self.docs if p.startswith(prefix) and p.endswith(".json"))
    def delete_chat(self, chat_id):
        self.rows = [r for r in self.rows if r[1] != chat_id]
    def put_blob(self, path, data): ...
    def put_blob_from(self, local_src, path): ...
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


class ScriptedWatcher(Watcher):
    def __init__(self, steps):
        self.steps = list(steps)

    def wait(self, timeout):
        if not self.steps:
            return False
        hinted, action = self.steps.pop(0)
        if action is not None:
            action()
        return hinted


class NotingFeedTransport(FeedTransport):
    def __init__(self):
        super().__init__()
        self.notes: list[tuple[bool, bool]] = []
        self.watch_steps = []

    def watch(self):
        return ScriptedWatcher(self.watch_steps)

    def note_log_poll(self, *, changed: bool, hinted: bool) -> None:
        self.notes.append((changed, hinted))


def feed_seed(tx, chat_id, sender, n):
    for i in range(n):
        tx.append_log(chat_id, f"{sender}@m",
                      {"id": f"{chat_id}-m{i}", "ns": i + 1, "from": sender})


def test_feed_first_tick_catches_up_then_idles_on_one_query(tmp_path):
    tx = FeedTransport()
    for c in range(3):
        feed_seed(tx, f"chat{c}", "ann", 4)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    assert eng.sync_once() == 12          # boot tick: full catch-up
    tx.calls = {k: 0 for k in tx.calls}
    assert eng.sync_once() == 0           # idle tick
    assert tx.calls["changed_logs"] == 1  # ONE feed query…
    assert tx.calls["list_logs"] == 0     # …no per-chat listing
    assert tx.calls["read_log"] == 0      # …and nothing read
    store.close()


def test_feed_tick_reads_only_the_changed_log(tmp_path):
    tx = FeedTransport()
    for c in range(3):
        feed_seed(tx, f"chat{c}", "ann", 4)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    eng.sync_once()
    tx.append_log("chat1", "ann@m", {"id": "new", "ns": 99, "from": "ann"})
    tx.calls = {k: 0 for k in tx.calls}
    assert eng.sync_once() == 1
    assert tx.calls["read_log"] == 1      # exactly the changed log
    assert tx.calls["list_logs"] == 0
    store.close()


def test_feed_cursor_survives_a_restart(tmp_path):
    tx = FeedTransport()
    feed_seed(tx, "c1", "ann", 5)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    eng.sync_once()
    # a NEW engine over the same store (a process restart)
    eng2 = SyncEngine(tx, store)
    assert eng2.sync_once() == 0          # nothing double-ingested
    assert store.message_count("c1") == 5
    store.close()


def test_feed_membership_gate_skips_foreign_chats(tmp_path):
    tx = FeedTransport()
    feed_seed(tx, "mine", "ann", 3)
    feed_seed(tx, "theirs", "sue", 3)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store, is_member=lambda c: c == "mine")
    assert eng.sync_once() == 3
    assert store.message_count("theirs") == 0   # never even read
    # …and rows that keep landing there stay unread
    tx.append_log("theirs", "sue@m", {"id": "x", "ns": 9, "from": "sue"})
    tx.calls = {k: 0 for k in tx.calls}
    assert eng.sync_once() == 0
    assert tx.calls["read_log"] == 0
    store.close()


def test_feed_join_recovers_history_below_the_cursor(tmp_path):
    """Joining a chat whose history predates the cursor: the membership diff
    triggers one full scan for it, so nothing below the cursor is lost."""
    tx = FeedTransport()
    feed_seed(tx, "mine", "ann", 2)
    feed_seed(tx, "old", "sue", 4)      # history I can't see yet
    store = Store(tmp_path / "cache.sqlite")
    member = {"mine"}
    eng = SyncEngine(tx, store, is_member=lambda c: c in member)
    eng.sync_once()                     # cursor now PAST old's rows
    assert store.message_count("old") == 0
    member.add("old")                   # I get added to the chat
    assert eng.sync_once() == 4         # full catch-up despite the cursor
    assert store.message_count("old") == 4
    store.close()


def test_run_loop_reports_unhinted_log_changes_after_wait(tmp_path):
    """V101: if a timed safety poll (not a watcher poke) finds messages, the
    metered transport wrapper must hear about it so its hint watchdog can
    shorten the degraded-realtime cadence."""
    import threading

    tx = NotingFeedTransport()
    feed_seed(tx, "c1", "ann", 1)       # startup catch-up: not a hint sample
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    tx.watch_steps = [
        (False, lambda: tx.append_log(
            "c1", "ann@m", {"id": "later", "ns": 99, "from": "ann"}
        )),
    ]
    seen = []

    def on_new(n):
        seen.append(n)
        if len(seen) >= 2:
            eng.stop()

    t = threading.Thread(
        target=lambda: eng.run(poll_s=0.01, on_new=on_new), daemon=True
    )
    t.start()
    t.join(5.0)
    assert not t.is_alive()
    assert seen == [1, 1]
    assert tx.notes == [(True, False)]
    store.close()


def test_feed_failure_keeps_the_cursor_and_recovers(tmp_path):
    tx = FeedTransport()
    feed_seed(tx, "c1", "ann", 2)
    store = Store(tmp_path / "cache.sqlite")
    eng = SyncEngine(tx, store)
    eng.sync_once()
    tx.append_log("c1", "ann@m", {"id": "late", "ns": 9, "from": "ann"})
    tx.fail_feed = True
    assert eng.sync_once() == 0         # blip: nothing lost, nothing crashed
    tx.fail_feed = False
    assert eng.sync_once() == 1         # same cursor retried -> caught up
    store.close()
