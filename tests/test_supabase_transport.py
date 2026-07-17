"""Supabase transport (R23): the full Transport contract against an
in-memory fake client — docs, incremental logs, blobs, path discipline,
and the factory. The real project is exercised by the live smoke run
(scripts/supabase_smoke.py), never by CI."""

from __future__ import annotations

import time

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.transport import CachingTransport, FolderTransport, make_transport
from agentbridge.transport.supabase import SupabaseTransport


# ------------------------------------------------------------ the fake client

class FakeQuery:
    """Models the parts of PostgREST the driver uses — including, since R76,
    the migrated schema: ``ab_docs.seq`` (trigger-bumped from a global
    counter on insert/update/upsert), ``deleted``, ``update``/``lt``, and
    write kwargs (``returning=``). A db with ``_legacy=True`` raises the
    PostgREST undefined-column error whenever seq/deleted is referenced,
    exactly like a pre-migration project."""

    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters = []
        self._like = None
        self._gt = None
        self._lt = None
        self._order = None
        self._desc = False
        self._limit = None
        self._range = None
        self._cols = ""
        self._op = ("select", None)

    def select(self, cols="*"):
        self._cols = str(cols)
        return self

    def insert(self, row, **_kw):
        self._op = ("insert", dict(row))
        return self

    def upsert(self, row, **_kw):
        self._op = ("upsert", dict(row))
        return self

    def update(self, patch, **_kw):
        self._op = ("update", dict(patch))
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

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def order(self, col="id", desc=False):
        self._order = col
        self._desc = bool(desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def _match(self, row):
        for col, val in self.filters:
            if row.get(col) != val:
                return False
        if self._like and not str(row.get(self._like[0], "")).startswith(self._like[1]):
            return False
        if self._gt and not row.get(self._gt[0], 0) > self._gt[1]:
            return False
        if self._lt and not str(row.get(self._lt[0], "")) < str(self._lt[1]):
            return False
        return True

    def _legacy_guard(self, payload):
        if not self.db.get("_legacy") or self.table != "ab_docs":
            return
        mentioned = set(self._cols.replace(" ", "").split(","))
        mentioned |= {c for c, _ in self.filters}
        mentioned |= set(payload or ())
        if self._order:
            mentioned.add(self._order)
        if {"seq", "deleted"} & mentioned:
            raise RuntimeError(
                'column ab_docs.seq does not exist (42703)')

    def _touch(self, row):
        """The ab_docs_touch trigger: every insert/update bumps seq."""
        if self.table == "ab_docs" and not self.db.get("_legacy"):
            self.db["_docseq"] = self.db.get("_docseq", 0) + 1
            row["seq"] = self.db["_docseq"]
            row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            row.setdefault("deleted", False)

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        op, payload = self._op
        self._legacy_guard(payload if op in ("insert", "upsert", "update")
                           else None)
        if op == "insert":
            payload = dict(payload)
            payload["id"] = self.db["_seq"] = self.db.get("_seq", 0) + 1
            self._touch(payload)
            rows.append(payload)
            return FakeResult([payload])
        if op == "upsert":
            payload = dict(payload)
            key = ("root", "path")
            rows[:] = [r for r in rows
                       if not all(r.get(k) == payload.get(k) for k in key)]
            self._touch(payload)
            rows.append(payload)
            return FakeResult([payload])
        if op == "update":
            hit = []
            for r in rows:
                if self._match(r):
                    r.update(payload)
                    self._touch(r)
                    hit.append(dict(r))
            return FakeResult(hit)
        if op == "delete":
            keep = [r for r in rows if not self._match(r)]
            gone = len(rows) - len(keep)
            rows[:] = keep
            return FakeResult([{"deleted": gone}])
        out = [r for r in rows if self._match(r)]
        if self._order:
            out.sort(key=lambda r: r.get(self._order) or 0,
                     reverse=self._desc)
        if self._limit:
            out = out[: self._limit]
        if self._range:
            lo, hi = self._range
            out = out[lo:hi + 1]
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
    def __init__(self, legacy: bool = False):
        self.db = {"_legacy": legacy}
        self.storage = FakeStorage()

    def migrate(self):
        """The dashboard SQL paste: columns appear, old rows backfill."""
        self.db["_legacy"] = False
        for r in self.db.get("ab_docs", []):
            if "seq" not in r:
                self.db["_docseq"] = self.db.get("_docseq", 0) + 1
                r["seq"] = self.db["_docseq"]
                r.setdefault("deleted", False)

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
                if r["root"] == params["p_root"] \
                        and r["path"].startswith("chats/") \
                        and not r.get("deleted"):
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


def test_get_docs_bulk_read(tx):
    tx.put_doc("users/aryan.json", {"name": "aryan"})
    tx.put_doc("users/fable.json", {"name": "fable"})
    tx.put_doc("chats/c1/meta.json", {"name": "Room"})
    everything = tx.get_docs()
    assert set(everything) == {"users/aryan.json", "users/fable.json",
                               "chats/c1/meta.json"}
    assert everything["users/aryan.json"]["name"] == "aryan"
    assert set(tx.get_docs("users")) == {"users/aryan.json", "users/fable.json"}


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


def test_changed_logs_is_a_global_cursor_feed(tx):
    assert tx.has_change_feed is True
    assert tx.changed_logs(0) == ([], 0)          # empty root: empty feed
    tx.append_log("c1", "ann@box.jsonl", {"id": "m1"})
    tx.append_log("c1", "ann@box.jsonl", {"id": "m2"})
    tx.append_log("c2", "sue@box.jsonl", {"id": "m3"})
    pairs, cursor = tx.changed_logs(0)
    assert pairs == [("c1", "ann@box.jsonl"), ("c2", "sue@box.jsonl")]
    assert cursor == 3
    assert tx.changed_logs(cursor) == ([], cursor)   # idle tick
    tx.append_log("c2", "sue@box.jsonl", {"id": "m4"})
    pairs2, cursor2 = tx.changed_logs(cursor)
    assert pairs2 == [("c2", "sue@box.jsonl")] and cursor2 == 4


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


# ------------------------------------------------- doc delta feed (R76/V84)

class HintLog:
    """Records the coalescing intervals _hint_for asked for (no threads)."""

    def __init__(self):
        self.calls = []

    def request(self, interval):
        self.calls.append(interval)

    def close(self):
        pass


def _fresh(legacy=False):
    t = SupabaseTransport("team", env={"SUPABASE_URL": "https://x.test",
                                       "SUPABASE_SECRET_KEY": "sb_secret_x"},
                          client=FakeClient(legacy=legacy))
    t._ensure_rt = lambda: None
    t._hints = HintLog()
    return t


def test_delta_feed_update_delete_revive(tx):
    tx.put_doc("users/a.json", {"v": 1})
    docs, cursor = tx.snapshot_docs()
    assert docs == {"users/a.json": {"v": 1}} and cursor > 0

    tx.put_doc("users/a.json", {"v": 2})          # update
    tx.put_doc("users/b.json", {"v": 1})          # create
    changed, deleted, c2 = tx.get_docs_delta(cursor)
    assert changed == {"users/a.json": {"v": 2}, "users/b.json": {"v": 1}}
    assert deleted == set() and c2 > cursor

    tx.delete_doc("users/b.json")                 # soft delete
    changed, deleted, c3 = tx.get_docs_delta(c2)
    assert changed == {} and deleted == {"users/b.json"} and c3 > c2
    assert tx.get_doc("users/b.json") is None     # tombstone reads missing
    assert tx.list_docs("users/") == ["users/a.json"]

    tx.put_doc("users/b.json", {"v": 9})          # revive the tombstone
    changed, deleted, c4 = tx.get_docs_delta(c3)
    assert changed == {"users/b.json": {"v": 9}} and deleted == set()
    assert tx.get_doc("users/b.json") == {"v": 9}

    assert tx.get_docs_delta(c4) == ({}, set(), c4)   # idle tick is empty


def test_delta_final_state_wins_within_one_pull(tx):
    tx.put_doc("users/a.json", {"v": 1})
    _, cursor = tx.snapshot_docs()
    tx.put_doc("users/a.json", {"v": 2})
    tx.delete_doc("users/a.json")                 # delete AFTER the update
    changed, deleted, _ = tx.get_docs_delta(cursor)
    assert "users/a.json" not in changed and "users/a.json" in deleted


def test_snapshot_cursor_taken_before_the_pull(tx):
    """A row racing the snapshot gets a seq ABOVE the returned cursor, so
    the next delta re-fetches it (never silently stale — SCALING.md §2)."""
    tx.put_doc("users/a.json", {"v": 1})
    _, cursor = tx.snapshot_docs()
    tx.put_doc("users/a.json", {"v": 2})
    changed, _, _ = tx.get_docs_delta(cursor)
    assert changed["users/a.json"] == {"v": 2}


def test_delete_chat_tombstones_ride_the_feed(tx):
    tx.put_doc("chats/c1/meta.json", {"name": "Room"})
    tx.put_doc("chats/c1/state/u.json", {"read": 1})
    _, cursor = tx.snapshot_docs()
    tx.delete_chat("c1")
    changed, deleted, _ = tx.get_docs_delta(cursor)
    assert deleted == {"chats/c1/meta.json", "chats/c1/state/u.json"}
    assert tx.list_chat_ids() == []               # RPC skips tombstones


def test_purge_deleted_docs_drops_only_old_tombstones(tx):
    tx.put_doc("users/a.json", {"v": 1})
    tx.delete_doc("users/a.json")
    rows = tx._client.db["ab_docs"]
    assert any(r.get("deleted") for r in rows)
    tx.purge_deleted_docs(30.0)                   # too young: kept
    assert any(r.get("deleted") for r in rows)
    for r in rows:                                # age the tombstone
        if r.get("deleted"):
            r["updated"] = "2000-01-01T00:00:00Z"
    tx.purge_deleted_docs(30.0)
    assert not any(r.get("deleted") for r in tx._client.db["ab_docs"])
    assert tx.get_doc("users/a.json") is None


def test_legacy_schema_falls_back_and_upgrades_live():
    t = _fresh(legacy=True)
    t.put_doc("users/a.json", {"v": 1})           # legacy write path works
    assert t.get_doc("users/a.json") == {"v": 1}
    docs, cursor = t.snapshot_docs()              # full pull, no cursor
    assert docs == {"users/a.json": {"v": 1}} and cursor == 0
    with pytest.raises(NotImplementedError):
        t.get_docs_delta(0)
    t.delete_doc("users/a.json")                  # legacy = HARD delete
    assert not [r for r in t._client.db["ab_docs"]
                if r["path"] == "users/a.json"]

    t._client.migrate()                           # the dashboard paste lands
    t._delta_reprobe = 0.0                        # the 60s leash elapses
    t.put_doc("users/b.json", {"v": 2})
    _, cursor = t.snapshot_docs()
    assert cursor > 0                             # delta mode is live
    t.put_doc("users/b.json", {"v": 3})
    changed, _, _ = t.get_docs_delta(cursor)
    assert changed == {"users/b.json": {"v": 3}}


def test_mid_migration_write_self_heals():
    """Probe says delta but the schema is legacy at write time (races the
    paste): put_doc flips modes and lands the write the old way."""
    t = _fresh(legacy=False)
    assert t._delta_ok() is True
    t._client.db["_legacy"] = True                # the schema "reverts"
    t.put_doc("users/a.json", {"v": 1})           # must not raise
    assert t._delta is False
    assert t.get_doc("users/a.json") == {"v": 1}


def test_hint_classes(tx):
    tx._hints = HintLog()
    tx.put_doc("presence/a@box.json", {"online": True})
    tx.put_doc("status/asks/claude.json", {"ask": 1})
    tx.put_doc("status/claude_run.json", {"step": 1})
    tx.put_doc("chats/c1/state/a.json", {"read_ns": 1})
    tx.put_doc("chats/c1/meta.json", {"name": "Room"})
    tx.append_log("c1", "a@box.jsonl", {"id": "m1"})
    assert tx._hints.calls == [None, 1.0, 5.0, 10.0, 1.0, 0.5]
    tx.hint_now()
    assert tx._hints.calls[-1] == 0.0


def test_hint_coalescer_trailing_edge_and_floor():
    from agentbridge.transport.supabase import _HintCoalescer

    sent = []
    h = _HintCoalescer(lambda: sent.append(time.monotonic()))
    try:
        for _ in range(5):                        # a burst of writes
            h.request(0.05)
        deadline = time.monotonic() + 2.0
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sent) == 1                     # coalesced into ONE poke
        h.request(0.05)                           # trailing write after send
        deadline = time.monotonic() + 2.0
        while len(sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sent) == 2                     # …still gets announced
        assert sent[1] - sent[0] >= h.FLOOR_S - 0.02   # rate floor held
    finally:
        h.close()


def test_transfer_stats_count_queries(tx):
    tx.put_doc("users/a.json", {"v": 1})
    tx.get_doc("users/a.json")
    s = tx.transfer_stats()
    assert s["queries"] >= 2 and s["mode"] == "delta"


# ----------------------------------------------------- R84: member auth (RLS)
class _AuthStub:
    def __init__(self, fail=False):
        self.fail = fail
        self.signins = []
        self.refreshes = 0

    def sign_in_with_password(self, creds):
        self.signins.append(creds)
        if self.fail:
            raise RuntimeError("invalid login credentials")

    def refresh_session(self):
        self.refreshes += 1


class _ClientStub:
    def __init__(self, key, fail_signin=False):
        self.key = key
        self.auth = _AuthStub(fail=fail_signin)


def _member_env(**extra):
    return {"SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "pub-key",
            "SUPABASE_SECRET_KEY": "secret-key",
            "SUPABASE_MEMBER_EMAIL": "aryan@mesh2.agentbridge.local",
            "SUPABASE_MEMBER_PASSWORD": "pw", **extra}


def test_member_credentials_are_preferred(monkeypatch):
    """R84: with a member credential present the client is built on the
    PUBLISHABLE key and signed in as the member — the service key stays
    untouched even though it's in the env."""
    import supabase as sb_mod

    made = []
    monkeypatch.setattr(sb_mod, "create_client",
                        lambda url, key: made.append(_ClientStub(key)) or made[-1])
    tx = SupabaseTransport("mesh2", env=_member_env())
    client = tx._sb()
    assert client.key == "pub-key"
    assert client.auth.signins == [{"email": "aryan@mesh2.agentbridge.local",
                                    "password": "pw"}]
    assert tx.auth_mode == "member:aryan"


def test_member_signin_failure_falls_back_to_service(monkeypatch):
    import supabase as sb_mod

    made = []

    def factory(url, key):
        c = _ClientStub(key, fail_signin=(key == "pub-key"))
        made.append(c)
        return c

    monkeypatch.setattr(sb_mod, "create_client", factory)
    tx = SupabaseTransport("mesh2", env=_member_env())
    client = tx._sb()
    assert client.key == "secret-key"          # the fleet never bricks
    assert tx.auth_mode == "member-signin-FAILED:service"


def test_service_key_alone_still_works(monkeypatch):
    import supabase as sb_mod

    monkeypatch.setattr(sb_mod, "create_client",
                        lambda url, key: _ClientStub(key))
    env = {"SUPABASE_URL": "https://x.supabase.co",
           "SUPABASE_SECRET_KEY": "secret-key"}
    tx = SupabaseTransport("mesh2", env=env)
    assert tx._sb().key == "secret-key"
    assert tx.auth_mode == "service"


def test_auth_expiry_heals_in_the_retry_path(monkeypatch):
    """A JWT-expired error triggers a session refresh before the retry —
    the belt for long-lived fleet processes."""
    import supabase as sb_mod

    monkeypatch.setattr(sb_mod, "create_client",
                        lambda url, key: _ClientStub(key))
    tx = SupabaseTransport("mesh2", env=_member_env())
    client = tx._sb()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("PGRST301: JWT expired")
        return "ok"

    from agentbridge.transport import supabase as supabase_mod

    monkeypatch.setattr(supabase_mod, "_RETRY_WAIT", 0.01)
    assert tx._retry(flaky) == "ok"
    assert client.auth.refreshes == 1


def test_rls_denial_gets_one_fresh_member_signin(monkeypatch):
    """A stale/signed-out member session is reported by PostgREST as a
    generic 42501 row-policy failure. One fresh sign-in heals it without
    changing or bypassing the policy."""
    import supabase as sb_mod
    from agentbridge.transport import supabase as supabase_mod

    monkeypatch.setattr(sb_mod, "create_client",
                        lambda url, key: _ClientStub(key))
    monkeypatch.setattr(supabase_mod, "_RETRY_WAIT", 0.01)
    tx = SupabaseTransport("mesh2", env=_member_env())
    client = tx._sb()
    calls = {"n": 0}

    def stale_session():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(
                "{'message': 'new row violates row-level security policy "
                "for table ab_docs', 'code': '42501'}"
            )
        return "ok"

    assert tx._retry(stale_session) == "ok"
    assert calls["n"] == 2
    assert len(client.auth.signins) == 2  # initial login + one fresh login
    assert client.auth.refreshes == 0


def test_persistent_rls_denial_does_not_reauth_loop(monkeypatch):
    """A real policy failure stays a failure. Re-auth is bounded to one
    attempt rather than becoming an auth loop or a service-key fallback."""
    import supabase as sb_mod
    from agentbridge.transport import supabase as supabase_mod

    monkeypatch.setattr(sb_mod, "create_client",
                        lambda url, key: _ClientStub(key))
    monkeypatch.setattr(supabase_mod, "_RETRY_WAIT", 0.01)
    tx = SupabaseTransport("mesh2", env=_member_env())
    client = tx._sb()
    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        raise RuntimeError(
            "{'message': 'new row violates row-level security policy "
            "for table ab_docs', 'code': '42501'}"
        )

    with pytest.raises(RuntimeError, match="42501"):
        tx._retry(forbidden)
    assert calls["n"] == supabase_mod._RETRIES
    assert len(client.auth.signins) == 2  # initial login + one bounded retry
    assert tx.auth_mode == "member:aryan"  # never fell back to service
