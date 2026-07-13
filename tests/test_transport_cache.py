"""CachingTransport (R28 cloud-perf fix): the short-TTL read cache collapses
repeated metadata reads and stays correct through writes + expiry.

A CountingTransport (a real in-memory transport that tallies calls) proves the
read collapse without any network; the factory test proves the folder stays
bare and a supabase root gets wrapped.
"""

from __future__ import annotations

import time

import pytest

from agentbridge.transport import CachingTransport, FolderTransport, make_transport
from agentbridge.transport.base import Transport, Watcher


class CountingTransport(Transport):
    """An in-memory transport that counts the reads the cache is meant to
    absorb. Only implements what the cache and these tests exercise."""

    scheme = "counting"

    def __init__(self) -> None:
        self.docs: dict[str, object] = {}
        self.logs: dict[tuple[str, str], list[dict]] = {}
        self.reads = {"get_doc": 0, "list_docs": 0, "list_chat_ids": 0}
        self.root = "mem"
        self.cache_key = "counting:mem"

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


@pytest.fixture
def counting():
    inner = CountingTransport()
    return inner, CachingTransport(inner, ttl=5.0)


def test_repeated_reads_hit_the_cache(counting):
    inner, tx = counting
    inner.put_doc("users/aryan.json", {"name": "aryan"})
    inner.reads["get_doc"] = 0
    for _ in range(10):
        assert tx.get_doc("users/aryan.json")["name"] == "aryan"
    assert inner.reads["get_doc"] == 1   # nine reads served from cache


def test_missing_doc_is_cached_too(counting):
    inner, tx = counting
    for _ in range(5):
        assert tx.get_doc("users/ghost.json", default={}) == {}
    assert inner.reads["get_doc"] == 1   # the miss is cached, not re-fetched


def test_list_reads_cached(counting):
    inner, tx = counting
    inner.put_doc("users/a.json", {})
    inner.put_doc("users/b.json", {})
    inner.reads["list_docs"] = 0
    for _ in range(4):
        assert tx.list_docs("users/") == ["users/a.json", "users/b.json"]
    assert inner.reads["list_docs"] == 1


def test_write_invalidates_and_is_self_consistent(counting):
    inner, tx = counting
    tx.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1   # writer sees its own write
    assert inner.reads["get_doc"] == 0            # served from the write-through
    tx.put_doc("users/a.json", {"v": 2})
    assert tx.get_doc("users/a.json")["v"] == 2   # updated write visible at once


def test_list_invalidated_on_doc_write(counting):
    inner, tx = counting
    assert tx.list_docs("users/") == []
    tx.put_doc("users/a.json", {})
    assert tx.list_docs("users/") == ["users/a.json"]   # not the stale empty list


def test_delete_reflected_immediately(counting):
    inner, tx = counting
    tx.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1
    tx.delete_doc("users/a.json")
    assert tx.get_doc("users/a.json", default="gone") == "gone"


def test_chat_ids_invalidated_on_append(counting):
    inner, tx = counting
    assert tx.list_chat_ids() == []
    tx.append_log("c1", "aryan@m.jsonl", {"id": "m1"})
    assert tx.list_chat_ids() == ["c1"]   # the new chat shows up


def test_ttl_expiry_refetches(counting):
    inner = CountingTransport()
    tx = CachingTransport(inner, ttl=0.05)
    inner.put_doc("users/a.json", {"v": 1})
    assert tx.get_doc("users/a.json")["v"] == 1
    inner.docs["users/a.json"] = {"v": 2}   # external change (another process)
    assert tx.get_doc("users/a.json")["v"] == 1   # still cached
    time.sleep(0.08)
    assert tx.get_doc("users/a.json")["v"] == 2   # TTL lapsed -> refetched


def test_delegates_unknown_attributes(counting):
    inner, tx = counting
    assert tx.root == "mem"
    assert tx.cache_key == "counting:mem"
    assert tx.scheme == "counting"


def test_logs_and_blobs_not_cached(counting):
    inner, tx = counting
    tx.append_log("c1", "a@m.jsonl", {"id": "m1"})
    recs, off = tx.read_log("c1", "a@m.jsonl", 0)
    assert [r["id"] for r in recs] == ["m1"]
    # a fresh append is visible on the next read_log (no stale log cache)
    tx.append_log("c1", "a@m.jsonl", {"id": "m2"})
    recs2, _ = tx.read_log("c1", "a@m.jsonl", off)
    assert [r["id"] for r in recs2] == ["m2"]


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


def test_state_sweep_collapses_reads():
    """Same sweep, cached vs bare: the cache turns the O(users × reads) doc
    fetches into O(distinct docs), the whole point of this round."""
    bare = CountingTransport()
    _seed_directory(bare)
    bare.reads = {"get_doc": 0, "list_docs": 0, "list_chat_ids": 0}
    _state_sweep(bare)
    uncached = sum(bare.reads.values())

    inner = CountingTransport()
    _seed_directory(inner)
    inner.reads = {"get_doc": 0, "list_docs": 0, "list_chat_ids": 0}
    cached = CachingTransport(inner, ttl=30.0)
    _state_sweep(cached)
    after = sum(inner.reads.values())

    # distinct docs = 4 users + 3 chat metas + 4 presence docs = 11; the cache
    # reads each at most once no matter how many times the sweep asks
    assert inner.reads["get_doc"] <= 11
    assert after < uncached / 3                  # a large, real reduction
