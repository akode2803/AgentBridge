"""CachingTransport (R29 cloud mirror): hot reads are served from a warm
in-memory snapshot, refreshed in the background, and a FAILED refresh keeps
serving the last good snapshot instead of "missing" (stability).

A CountingTransport (a real in-memory transport that tallies calls) proves
the read collapse without any network; a Bulk variant adds ``get_docs`` to
exercise the one-query snapshot path and its failure modes; the factory test
proves the folder stays bare and a supabase root gets wrapped.
"""

from __future__ import annotations

import time

import pytest

from agentbridge.transport import CachingTransport, FolderTransport, make_transport
from agentbridge.transport.base import Transport, Watcher


class CountingTransport(Transport):
    """An in-memory transport that counts the reads the mirror is meant to
    absorb. Only implements what the cache and these tests exercise."""

    scheme = "counting"

    def __init__(self) -> None:
        self.docs: dict[str, object] = {}
        self.logs: dict[tuple[str, str], list[dict]] = {}
        self.reads = {"get_doc": 0, "list_docs": 0, "list_chat_ids": 0}
        self.root = "mem"
        self.cache_key = "counting:mem"

    def reset_reads(self) -> None:
        self.reads = {k: 0 for k in self.reads}

    def get_doc(self, path, default=None):
        self.reads["get_doc"] += 1
        return self.docs.get(path, default)

    def put_doc(self, path, data):
        self.docs[path] = data

    def delete_doc(self, path):
        self.docs.pop(path, None)

    def list_docs(self, prefix):
        self.reads["list_docs"] += 1
        return sorted(p for p in self.docs if p.startswith(prefix) and p.endswith(".json"))

    def list_chat_ids(self):
        self.reads["list_chat_ids"] += 1
        ids = set()
        for (chat, _log) in self.logs:
            ids.add(chat)
        for p in self.docs:
            if p.startswith("chats/"):
                ids.add(p.split("/")[1])
        return sorted(ids)

    def list_logs(self, chat_id):
        return [(log, len(recs)) for (c, log), recs in self.logs.items() if c == chat_id]

    def append_log(self, chat_id, log_name, record):
        self.logs.setdefault((chat_id, log_name), []).append(record)

    def read_log(self, chat_id, log_name, offset=0):
        recs = self.logs.get((chat_id, log_name), [])
        return recs[offset:], len(recs)

    def delete_chat(self, chat_id):
        self.docs = {p: v for p, v in self.docs.items()
                     if not p.startswith(f"chats/{chat_id}/")}
        self.logs = {k: v for k, v in self.logs.items() if k[0] != chat_id}

    def put_blob(self, path, data): ...
    def put_blob_from(self, local_src, path): ...
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


class BulkTransport(CountingTransport):
    """CountingTransport + the one-query snapshot read (the supabase shape).
    ``fail`` simulates the cloud being unreachable; ``on_get_docs`` lets a
    test act MID-refresh (the recent-write race)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads["get_docs"] = 0
        self.fail = False
        self.on_get_docs = None

    def get_docs(self, prefix=""):
        self.reads["get_docs"] += 1
        if self.fail:
            raise ConnectionError("cloud unreachable")
        snapshot = {p: v for p, v in self.docs.items() if p.startswith(prefix)}
        if self.on_get_docs is not None:
            self.on_get_docs()   # a local write lands while the query is out
        return snapshot


@pytest.fixture
def mirror():
    """A manually-refreshed mirror (no background thread — deterministic)."""
    inner = BulkTransport()
    return inner, CachingTransport(inner, refresh_s=30.0, auto_refresh=False)


# ------------------------------------------------------------ read collapse

def test_reads_served_from_the_warm_mirror(mirror):
    inner, tx = mirror
    inner.put_doc("users/aryan.json", {"name": "aryan"})
    inner.put_doc("presence/aryan@box.json", {"user": "aryan"})
    for _ in range(10):
        assert tx.get_doc("users/aryan.json")["name"] == "aryan"
        assert tx.list_docs("users") == ["users/aryan.json"]
        assert tx.list_chat_ids() == []
    # one warm-up snapshot (bulk + chat ids), then zero per-read traffic
    assert inner.reads["get_docs"] == 1
    assert inner.reads["get_doc"] == 0
    assert inner.reads["list_docs"] == 0
    assert inner.reads["list_chat_ids"] == 1


def test_missing_doc_is_default_without_refetch(mirror):
    inner, tx = mirror
    for _ in range(5):
        assert tx.get_doc("users/ghost.json", default={}) == {}
    assert inner.reads["get_doc"] == 0   # the mirror KNOWS it's absent


def test_external_change_lands_on_refresh(mirror):
    inner, tx = mirror
    inner.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1
    inner.docs["users/a.json"] = {"v": 2}       # another process wrote
    assert tx.get_doc("users/a.json")["v"] == 1  # mirror still on the snapshot
    tx.refresh()
    assert tx.get_doc("users/a.json")["v"] == 2


# -------------------------------------------------------- writer visibility

def test_writer_sees_own_writes_immediately(mirror):
    inner, tx = mirror
    tx.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1
    tx.put_doc("users/a.json", {"v": 2})
    assert tx.get_doc("users/a.json")["v"] == 2
    assert tx.list_docs("users") == ["users/a.json"]   # lists too
    tx.delete_doc("users/a.json")
    assert tx.get_doc("users/a.json", default="gone") == "gone"


def test_own_chat_visible_after_first_append(mirror):
    inner, tx = mirror
    assert tx.list_chat_ids() == []
    tx.append_log("c1", "aryan@m.jsonl", {"id": "m1"})
    assert tx.list_chat_ids() == ["c1"]
    tx.put_doc("chats/c2/meta.json", {"id": "c2"})
    assert tx.list_chat_ids() == ["c1", "c2"]   # meta-doc-derived ids count


def test_delete_chat_drops_subtree_and_id(mirror):
    inner, tx = mirror
    tx.put_doc("chats/c1/meta.json", {"id": "c1"})
    tx.append_log("c1", "a@m.jsonl", {"id": "m1"})
    tx.delete_chat("c1")
    assert tx.get_doc("chats/c1/meta.json") is None
    assert tx.list_chat_ids() == []


# ------------------------------------------------- stability under failure

def test_failed_refresh_keeps_serving_the_last_snapshot(mirror):
    inner, tx = mirror
    inner.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1   # warm
    inner.fail = True
    with pytest.raises(ConnectionError):
        tx.refresh()
    # the doc did NOT flicker out — stale beats vanished
    assert tx.get_doc("users/a.json")["v"] == 1
    assert inner.reads["get_doc"] == 0


def test_mirror_status_reports_warmth_and_age(mirror):
    """The GUI Connection panel reads mirror_status(): cold = not warm, no
    age; after a pull = warm with a fresh age; a failed refresh keeps warm
    (still serving the last snapshot) while the age keeps growing."""
    inner, tx = mirror
    assert tx.mirror_status() == {"warm": False, "age_s": None,
                                  "refresh_s": 30.0}
    inner.put_doc("users/a.json", {"v": 1})
    tx.get_doc("users/a.json")   # first read warms the mirror
    st = tx.mirror_status()
    assert st["warm"] is True
    assert st["age_s"] is not None and st["age_s"] < 5
    inner.fail = True
    with pytest.raises(ConnectionError):
        tx.refresh()
    assert tx.mirror_status()["warm"] is True   # stale beats vanished


def test_cold_start_offline_falls_through_then_recovers():
    inner = BulkTransport()
    inner.put_doc("users/a.json", {"v": 1})
    inner.fail = True
    tx = CachingTransport(inner, refresh_s=0.05, auto_refresh=False)
    # warm-up fails: reads fall through to the inner driver (still works)
    assert tx.get_doc("users/a.json")["v"] == 1
    assert inner.reads["get_doc"] == 1
    # after the back-off window the next read warms the mirror
    inner.fail = False
    deadline = time.monotonic() + 2.0
    while not tx._warm and time.monotonic() < deadline:
        tx.get_doc("users/a.json")
        time.sleep(0.01)
    assert tx._warm
    inner.reset_reads()
    assert tx.get_doc("users/a.json")["v"] == 1
    assert inner.reads["get_doc"] == 0   # served from the mirror again


def test_local_write_survives_a_racing_refresh(mirror):
    inner, tx = mirror
    inner.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1   # warm

    # while the snapshot query is in flight, this process writes v=2 — the
    # apply must keep the newer local write, not resurrect the older row
    inner.on_get_docs = lambda: tx.put_doc("users/a.json", {"v": 2})
    tx.refresh()
    inner.on_get_docs = None
    assert tx.get_doc("users/a.json")["v"] == 2

    # same for a delete landing mid-refresh
    inner.on_get_docs = lambda: tx.delete_doc("users/a.json")
    tx.refresh()
    inner.on_get_docs = None
    assert tx.get_doc("users/a.json", default="gone") == "gone"


def test_returned_docs_never_alias_the_mirror(mirror):
    inner, tx = mirror
    tx.put_doc("users/a.json", {"profile": {"about": "hi"}})
    doc = tx.get_doc("users/a.json")
    doc["profile"]["about"] = "mutated in place"
    assert tx.get_doc("users/a.json")["profile"]["about"] == "hi"


# --------------------------------------------------------- background thread

def test_background_refresh_picks_up_external_changes():
    inner = BulkTransport()
    inner.put_doc("users/a.json", {"v": 1})
    tx = CachingTransport(inner, refresh_s=0.05)
    assert tx.get_doc("users/a.json")["v"] == 1   # warm starts the refresher
    inner.docs["users/a.json"] = {"v": 2}          # another process wrote
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if tx.get_doc("users/a.json")["v"] == 2:
            break
        time.sleep(0.01)
    assert tx.get_doc("users/a.json")["v"] == 2
    tx.close()


# ---------------------------------------------------------------- pass-through

def test_logs_and_blobs_not_cached(mirror):
    inner, tx = mirror
    tx.append_log("c1", "a@m.jsonl", {"id": "m1"})
    recs, off = tx.read_log("c1", "a@m.jsonl", 0)
    assert [r["id"] for r in recs] == ["m1"]
    # a fresh append is visible on the next read_log (no stale log cache)
    tx.append_log("c1", "a@m.jsonl", {"id": "m2"})
    recs2, _ = tx.read_log("c1", "a@m.jsonl", off)
    assert [r["id"] for r in recs2] == ["m2"]


def test_delegates_unknown_attributes(mirror):
    inner, tx = mirror
    assert tx.root == "mem"
    assert tx.cache_key == "counting:mem"
    assert tx.scheme == "counting"


def test_per_path_fallback_when_no_bulk_read():
    """A driver without ``get_docs`` still gets a mirror (assembled per path)."""
    inner = CountingTransport()
    inner.put_doc("users/a.json", {"v": 1})
    tx = CachingTransport(inner, auto_refresh=False)
    assert tx.get_doc("users/a.json")["v"] == 1
    inner.reset_reads()
    for _ in range(5):
        assert tx.get_doc("users/a.json")["v"] == 1
        tx.list_docs("users")
    assert sum(inner.reads.values()) == 0


# ------------------------------------------------------------------ factory

def test_folder_root_is_not_wrapped(tmp_path):
    tx = make_transport(tmp_path / "mesh2")
    assert isinstance(tx, FolderTransport)   # local folder stays bare


def test_supabase_root_is_wrapped(tmp_path):
    tx = make_transport("supabase://team", home=tmp_path)
    assert isinstance(tx, CachingTransport)
    assert tx.scheme == "supabase"
    assert tx.root == "team"


# ---------------------------- the hot-path collapse (why this round exists) --

def _seed_directory(tx):
    """A small mesh's metadata: 4 users (one with a MEMBERS-audience profile,
    to exercise shares_chat) + 3 chats they share."""
    from agentbridge.mesh.paths import P

    users = {"aryan": "everyone", "fable": "everyone",
             "sudhir": "members", "kim": "everyone"}
    for name, about_aud in users.items():
        tx.put_doc(P.user(name), {
            "name": name, "kind": "human", "display": name.title(),
            "privacy": {"about": about_aud, "status": about_aud},
        })
    for i in range(3):
        tx.put_doc(P.meta(f"c{i}"), {
            "id": f"c{i}", "kind": "group", "name": f"Room {i}",
            "members": {u: {"role": "member", "joined_ns": 1} for u in users},
        })
    # each user has a presence device doc — presence_of scans them all, per user
    for name in users:
        tx.put_doc(f"presence/{name}@box.json",
                   {"user": name, "online": True, "last_seen_ns": 1})
    return list(users)


def _state_sweep(tx):
    """The core of GET /api/mesh/state: for every user, visible_profile (which
    re-reads the account doc per field and, for MEMBERS audiences, all chat
    metas via shares_chat) + visible_presence (which re-lists and re-reads
    every presence doc), then chats_for."""
    from agentbridge.mesh.directory import Directory
    from agentbridge.mesh.paths import P
    from agentbridge.mesh.presence import PresenceService
    from agentbridge.mesh.privacy import PrivacyService

    d = Directory(tx)
    priv = PrivacyService(tx, d, "aryan")
    pres = PresenceService(tx, priv, "aryan", "box")
    for name in d.names():
        priv.visible_profile(name, viewer="aryan")
        pres.visible_presence(name, viewer="aryan")
    # chats_for: list ids + read each meta
    for cid in tx.list_chat_ids():
        tx.get_doc(P.meta(cid))


def test_state_sweep_needs_zero_transport_reads_once_warm():
    """Same sweep, mirrored vs bare: warm mirror = the whole /api/mesh/state
    metadata sweep touches the transport ZERO times — folder-grade latency on
    a cloud root, the whole point of this round."""
    bare = BulkTransport()
    _seed_directory(bare)
    bare.reset_reads()
    _state_sweep(bare)
    uncached = sum(bare.reads.values())
    assert uncached > 30   # the blowup the mirror kills

    inner = BulkTransport()
    cached = CachingTransport(inner, refresh_s=30.0, auto_refresh=False)
    _seed_directory(cached)          # writes go through the mirror
    cached.refresh()
    inner.reset_reads()
    _state_sweep(cached)
    assert sum(inner.reads.values()) == 0
