"""Supabase transport (R23): the full Transport contract against an
in-memory fake client — docs, incremental logs, blobs, path discipline,
and the factory. The real project is exercised by the live smoke run
(scripts/supabase_smoke.py), never by CI."""

from __future__ import annotations


import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.transport import CachingTransport, FolderTransport, make_transport
from agentbridge.transport.supabase import SupabaseTransport


# ------------------------------------------------------------ the fake client

class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters = []
        self._like = None
        self._gt = None
        self._order = None
        self._limit = None
        self._op = ("select", None)

    def select(self, *_):
        return self

    def insert(self, row):
        self._op = ("insert", dict(row))
        return self

    def upsert(self, row):
        self._op = ("upsert", dict(row))
        return self

    def delete(self):
        self._op = ("delete", None)
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def like(self, col, pat):
        self._like = (col, pat.rstrip("%"))
        return self

    def gt(self, col, val):
        self._gt = (col, val)
        return self

    def order(self, *_):
        self._order = True
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for col, val in self.filters:
            if row.get(col) != val:
                return False
        if self._like and not str(row.get(self._like[0], "")).startswith(self._like[1]):
            return False
        if self._gt and not row.get(self._gt[0], 0) > self._gt[1]:
            return False
        return True

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        op, payload = self._op
        if op == "insert":
            payload = dict(payload)
            payload["id"] = self.db["_seq"] = self.db.get("_seq", 0) + 1
            rows.append(payload)
            return FakeResult([payload])
        if op == "upsert":
            key = ("root", "path")
            rows[:] = [r for r in rows
                       if not all(r.get(k) == payload.get(k) for k in key)]
            rows.append(dict(payload))
            return FakeResult([payload])
        if op == "delete":
            keep = [r for r in rows if not self._match(r)]
            gone = len(rows) - len(keep)
            rows[:] = keep
            return FakeResult([{"deleted": gone}])
        out = [r for r in rows if self._match(r)]
        if self._order:
            out.sort(key=lambda r: r.get("id", 0))
        if self._limit:
            out = out[: self._limit]
        return FakeResult([dict(r) for r in out])


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeBucketApi:
    def __init__(self, objects):
        self.objects = objects

    def upload(self, key, data, file_options=None):
        self.objects[key] = bytes(data)

    def download(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def list(self, parent):
        out = []
        for key, data in self.objects.items():
            p, _, name = key.rpartition("/")
            if p == parent:
                out.append({"name": name, "metadata": {"size": len(data)}})
        return out

    def remove(self, keys):
        for k in keys:
            self.objects.pop(k, None)


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.buckets = []

    def list_buckets(self):
        return [type("B", (), {"name": n}) for n in self.buckets]

    def create_bucket(self, name):
        self.buckets.append(name)

    def from_(self, _bucket):
        return FakeBucketApi(self.objects)


class FakeClient:
    def __init__(self):
        self.db = {}
        self.storage = FakeStorage()

    def table(self, name):
        return FakeQuery(self.db, name)

    def rpc(self, fn, params):
        if fn == "ab_list_logs":
            heads = {}
            for r in self.db.get("ab_logs", []):
                if r["root"] == params["p_root"] and r["chat_id"] == params["p_chat"]:
                    heads[r["log_name"]] = max(heads.get(r["log_name"], 0), r["id"])
            rows = [{"log_name": k, "head": v} for k, v in heads.items()]
        elif fn == "ab_chat_ids":
            ids = {r["chat_id"] for r in self.db.get("ab_logs", [])
                   if r["root"] == params["p_root"]}
            for r in self.db.get("ab_docs", []):
                if r["root"] == params["p_root"] and r["path"].startswith("chats/"):
                    ids.add(r["path"].split("/")[1])
            rows = [{"chat_id": c} for c in ids]
        else:  # pragma: no cover
            rows = []
        return FakeExec(rows)


class FakeExec:
    def __init__(self, rows):
        self._rows = rows

    def execute(self):
        return FakeResult(self._rows)


@pytest.fixture
def tx():
    t = SupabaseTransport("team", env={"SUPABASE_URL": "https://x.test",
                                       "SUPABASE_SECRET_KEY": "sb_secret_x"},
                          client=FakeClient())
    t._ensure_rt = lambda: None       # no realtime thread in unit tests
    return t


# ------------------------------------------------------------------- docs

def test_doc_roundtrip_and_prefix_listing(tx):
    assert tx.get_doc("users/aryan.json", default="nope") == "nope"
    tx.put_doc("users/aryan.json", {"name": "aryan"})
    tx.put_doc("users/fable.json", {"name": "fable"})
    tx.put_doc("chats/c1/meta.json", {"name": "Room"})
    assert tx.get_doc("users/aryan.json")["name"] == "aryan"
    assert tx.list_docs("users/") == ["users/aryan.json", "users/fable.json"]
    tx.put_doc("users/aryan.json", {"name": "Aryan K"})   # upsert replaces
    assert tx.get_doc("users/aryan.json")["name"] == "Aryan K"
    tx.delete_doc("users/aryan.json")
    assert tx.get_doc("users/aryan.json") is None


def test_path_discipline(tx):
    with pytest.raises(ValidationError):
        tx.put_doc("../escape.json", {})
    with pytest.raises(ValidationError):
        tx.get_blob("a/../../b")


# ------------------------------------------------------------------- logs

def test_log_append_read_incremental(tx):
    tx.append_log("c1", "aryan@box.jsonl", {"id": "m1", "body": "hello"})
    tx.append_log("c1", "aryan@box.jsonl", {"id": "m2", "body": "again"})
    tx.append_log("c1", "fable@box.jsonl", {"id": "m3"})
    recs, off = tx.read_log("c1", "aryan@box.jsonl", 0)
    assert [r["id"] for r in recs] == ["m1", "m2"] and off > 0
    recs2, off2 = tx.read_log("c1", "aryan@box.jsonl", off)
    assert recs2 == [] and off2 == off                 # incremental: no re-read
    tx.append_log("c1", "aryan@box.jsonl", {"id": "m4"})
    recs3, off3 = tx.read_log("c1", "aryan@box.jsonl", off)
    assert [r["id"] for r in recs3] == ["m4"] and off3 > off

    heads = dict(tx.list_logs("c1"))
    assert set(heads) == {"aryan@box.jsonl", "fable@box.jsonl"}
    assert heads["aryan@box.jsonl"] == off3            # head == newest offset
    assert tx.list_chat_ids() == ["c1"]


def test_corrupt_log_row_is_skipped_not_stuck(tx):
    tx.append_log("c1", "log.jsonl", {"id": "m1"})
    tx._client.db["ab_logs"].append({          # a hand-corrupted row
        "id": 999, "root": "team", "chat_id": "c1",
        "log_name": "log.jsonl", "line": "{not json"})
    recs, off = tx.read_log("c1", "log.jsonl", 0)
    assert [r["id"] for r in recs] == ["m1"]
    assert off == 999                          # advanced past the bad row


def test_delete_chat_wipes_rows_and_docs(tx):
    tx.append_log("c1", "log.jsonl", {"id": "m1"})
    tx.put_doc("chats/c1/meta.json", {"name": "Room"})
    tx.put_doc("chats/other/meta.json", {"name": "Keep"})
    tx.delete_chat("c1")
    assert tx.list_logs("c1") == []
    assert tx.get_doc("chats/c1/meta.json") is None
    assert tx.get_doc("chats/other/meta.json")["name"] == "Keep"


# ------------------------------------------------------------------- blobs

def test_blob_roundtrip_and_size(tx):
    tx.put_blob("chats/c1/files/f1.bin", b"12345")
    assert tx.get_blob("chats/c1/files/f1.bin") == b"12345"
    assert tx.blob_size("chats/c1/files/f1.bin") == 5
    assert tx.get_blob("chats/c1/files/missing.bin") is None
    with pytest.raises(ValidationError):
        tx.put_blob("big.bin", b"x" * (tx.max_upload_bytes + 1))


# ----------------------------------------------------------------- factory

def test_factory_picks_the_driver(tmp_path):
    # a cloud root is wrapped in the R28 read cache; the SupabaseTransport is
    # underneath, and .root/.scheme still resolve through the wrapper
    t = make_transport("supabase://team-a", home=tmp_path)
    assert isinstance(t, CachingTransport)
    assert isinstance(t.inner, SupabaseTransport)
    assert t.root == "team-a" and t.scheme == "supabase"
    f = make_transport(tmp_path / "mesh2")
    assert isinstance(f, FolderTransport)   # a local folder stays bare


def test_cache_key_unique_per_project():
    a = SupabaseTransport("mesh", env={"SUPABASE_URL": "https://a.test",
                                       "SUPABASE_SECRET_KEY": "k"})
    b = SupabaseTransport("mesh", env={"SUPABASE_URL": "https://b.test",
                                       "SUPABASE_SECRET_KEY": "k"})
    assert a.cache_key != b.cache_key


def test_missing_credentials_fail_loud(tmp_path):
    t = SupabaseTransport("mesh", env={}, home=tmp_path)
    with pytest.raises(ValidationError):
        t.get_doc            # noqa: B018 — attribute is fine...
        t.put_doc("x.json", {})
