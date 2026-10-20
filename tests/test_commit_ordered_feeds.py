"""P7: commit-ordered feeds (docs/COMMIT_ORDERED_CURSORS.md).

The fake cloud models the frontier: a transaction left in flight with
``begin_transaction`` holds it, and ``finish_transaction`` commits rows that
carry its (smaller) id after later transactions already committed. These
tests drive the real Supabase driver, the mirror and the sync engine.
"""
from __future__ import annotations

import time

import pytest

from agentbridge.mesh.sync import CURSOR_DOC, SyncEngine
from agentbridge.store.db import Store
from agentbridge.transport import supabase as supabase_mod
from agentbridge.transport.base import CursorTooOld, FeedCursor


def _record(chat_id, sender, n):
    return {"id": f"{chat_id}-m{n}", "ns": n, "from": sender}


# --------------------------------------------------------------- transport

def test_doc_feed_never_passes_a_row_still_in_flight(tmp_path, clouds):
    reader = clouds.bare(tmp_path / "r")
    writer = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    _, cursor = reader.snapshot_docs()

    held = client.begin_transaction()          # A takes the smaller id
    writer.put_doc("users/b.json", {"v": "b"})  # B commits first
    changed, deleted, mid = reader.get_docs_delta(cursor)
    assert changed == {} and deleted == set()  # B waits behind A's frontier
    assert mid.position == held

    client.finish_transaction(held, lambda: writer.put_doc("users/a.json", {"v": "a"}))
    changed, _, after = reader.get_docs_delta(mid)
    assert changed == {"users/a.json": {"v": "a"}, "users/b.json": {"v": "b"}}
    assert after.position > held
    assert reader.get_docs_delta(after) == ({}, set(), after)


def test_log_feed_never_passes_a_row_still_in_flight(tmp_path, clouds):
    reader = clouds.bare(tmp_path / "r")
    writer = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    with pytest.raises(CursorTooOld) as old:
        reader.changed_logs(0)
    cursor = old.value.restart
    assert type(cursor) is FeedCursor

    held = client.begin_transaction()
    writer.append_log("c2", "bob@m", _record("c2", "bob", 1))
    pairs, mid = reader.changed_logs(cursor)
    assert pairs == [] and mid.position == held

    client.finish_transaction(
        held, lambda: writer.append_log("c1", "ann@m", _record("c1", "ann", 1)))
    pairs, after = reader.changed_logs(mid)
    assert sorted(pairs) == [("c1", "ann@m"), ("c2", "bob@m")]
    assert reader.changed_logs(after) == ([], after)


def test_feeds_page_on_has_more_and_keep_the_pair_in_memory(tmp_path, clouds, monkeypatch):
    monkeypatch.setattr(supabase_mod, "_FEED_PAGE", 2)
    tx = clouds.bare(tmp_path / "r")
    _, cursor = tx.snapshot_docs()
    for i in range(5):
        tx.put_doc(f"users/{i}.json", {"v": i})
        tx.append_log(f"c{i}", "ann@m", _record(f"c{i}", "ann", 1))
    changed, _, after = tx.get_docs_delta(cursor)
    assert sorted(changed) == [f"users/{i}.json" for i in range(5)]
    assert after.position > cursor.position and after.basis == cursor.basis
    pairs, _ = tx.changed_logs(cursor)
    assert sorted(pairs) == [(f"c{i}", "ann@m") for i in range(5)]


def test_a_counter_cursor_is_too_old_for_the_commit_ordered_feeds(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    tx.put_doc("users/a.json", {"v": 1})
    with pytest.raises(CursorTooOld):
        tx.get_docs_delta(5)                   # a pre-P7 seq cursor
    with pytest.raises(CursorTooOld) as old:
        tx.changed_logs(5)                     # a pre-P7 row-id cursor
    frontier = clouds.client(tmp_path / "r")._rpc_locked(
        "ab_feeds_frontier", {"p_root": tx.root})
    assert old.value.restart == FeedCursor(
        frontier["frontier"], "frontier:" + frontier["epoch"])


def test_a_purge_below_the_cursor_answers_too_old(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    tx.put_doc("status/a.json", {"v": 1})
    _, before = tx.snapshot_docs()
    tx.delete_doc("status/a.json")
    for row in clouds.client(tmp_path / "r").db["ab_docs"]:
        row["updated"] = "2000-01-01T00:00:00Z"
    tx.purge_deleted_docs(30.0)                # the janitor's hard delete
    with pytest.raises(CursorTooOld):
        tx.get_docs_delta(before)              # it may have missed the delete
    _, after = tx.snapshot_docs()
    assert tx.get_docs_delta(after) == ({}, set(), after)


def test_an_empty_purge_raises_no_floor(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    tx.put_doc("status/a.json", {"v": 1})
    _, cursor = tx.snapshot_docs()
    tx.purge_deleted_docs(30.0)                # nothing old enough
    assert clouds.client(tmp_path / "r").db.get("_doc_floors", {}) == {}
    assert tx.get_docs_delta(cursor) == ({}, set(), cursor)


def test_a_new_epoch_or_a_rewound_server_answers_too_old(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    _, cursor = tx.snapshot_docs()
    with pytest.raises(CursorTooOld):          # above the server's horizon
        tx.get_docs_delta(FeedCursor(cursor.position + 1_000, cursor.basis))
    clouds.client(tmp_path / "r").db["_epoch"] = "restored-epoch"
    with pytest.raises(CursorTooOld):
        tx.get_docs_delta(cursor)
    with pytest.raises(CursorTooOld) as old:
        tx.changed_logs(cursor)
    assert old.value.restart.basis == "frontier:restored-epoch"


def test_full_pull_pages_by_key_so_a_mid_pull_delete_skips_nothing(
        tmp_path, clouds, monkeypatch):
    monkeypatch.setattr(supabase_mod, "_DOC_PAGE", 2)
    tx = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    for name in "abcd":
        tx.put_doc(f"d/{name}.json", {"v": name})
    assert tx._delta_ok() and tx._feeds_ok()   # probes done before the hook
    table = client.table
    pages = []

    def hooked(name):
        query = table(name)
        execute = query.execute

        def run():
            out = execute()
            pages.append(out.data)
            if len(pages) == 1:                # a hard delete lands mid-pull
                client.db["ab_docs"][:] = [
                    r for r in client.db["ab_docs"] if r["path"] != "d/a.json"]
            return out
        query.execute = run
        return query

    client.table = hooked
    docs, cursor = tx.snapshot_docs()
    assert sorted(docs) == ["d/a.json", "d/b.json", "d/c.json", "d/d.json"]
    assert set(cursor.row_positions) == set(docs)


def test_without_the_feed_rpcs_the_driver_keeps_counters(tmp_path, clouds):
    client = clouds.client(tmp_path / "r")
    client.db["_feeds"] = False
    tx = clouds.bare(tmp_path / "r")
    tx.put_doc("users/a.json", {"v": 1})
    _, cursor = tx.snapshot_docs()
    assert type(cursor) is int and cursor > 0
    with pytest.raises(CursorTooOld):          # schema rolled back under us
        tx.get_docs_delta(FeedCursor(10, "frontier:x"))
    client.db["_feeds"] = True                 # the paste lands
    tx._feeds_reprobe = 0.0
    with pytest.raises(CursorTooOld):          # the counter is now too old
        tx.get_docs_delta(cursor)
    _, cursor = tx.snapshot_docs()
    assert type(cursor) is FeedCursor


def test_a_network_fault_before_the_first_probe_raises(tmp_path, clouds, monkeypatch):
    monkeypatch.setattr(supabase_mod, "_RETRY_WAIT", 0)
    tx = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    assert tx._delta_ok()
    rpc = client.rpc

    def offline(fn, params=None):
        if fn == "ab_feeds_frontier":
            raise ConnectionError("network down")
        return rpc(fn, params)

    client.rpc = offline
    with pytest.raises(ConnectionError):
        tx.snapshot_docs()                     # no guess at "legacy"
    assert tx._feeds is None
    client.rpc = rpc
    _, cursor = tx.snapshot_docs()
    assert type(cursor) is FeedCursor


# ------------------------------------------------------------------ mirror

def _forget_chat(mirror, chat_id):
    """Put the mirror in the state of a chat that became visible after its
    docs were written: listed, but its older docs never read."""
    prefix = f"chats/{chat_id}/"
    for path in [p for p in mirror._docs if p.startswith(prefix)]:
        del mirror._docs[path]
    mirror._caught_up.discard(chat_id)


def test_mirror_catches_up_a_chat_that_appears(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/c9/meta.json", {"name": "Late"})
    writer.put_doc("chats/c9/pins/p.json", {"pin": 1})
    mirror = clouds.cached(tmp_path / "r")
    _forget_chat(mirror, "c9")
    reads = []
    get_docs = mirror.inner.get_docs
    mirror.inner.get_docs = lambda prefix="": reads.append(prefix) or get_docs(prefix)

    assert mirror._refresh_tick() is True      # docs below the cursor arrive
    assert mirror.get_doc("chats/c9/pins/p.json") == {"pin": 1}
    assert reads == ["chats/c9/"]
    assert "c9" in mirror._caught_up
    mirror._refresh_tick()
    assert reads == ["chats/c9/"]              # once per newly visible chat


def test_mirror_catch_up_runs_even_after_this_process_appended(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/c9/meta.json", {"name": "Late"})
    writer.put_doc("chats/c9/pins/p.json", {"pin": 1})
    mirror = clouds.cached(tmp_path / "r")
    _forget_chat(mirror, "c9")
    mirror.append_log("c9", "ann@m", _record("c9", "ann", 1))
    assert "c9" in mirror.list_chat_ids()      # listed, still not caught up
    mirror._refresh_tick()
    assert mirror.get_doc("chats/c9/pins/p.json") == {"pin": 1}


def test_mirror_catch_up_replaces_what_it_held_for_the_chat(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/c9/meta.json", {"name": "Late"})
    mirror = clouds.cached(tmp_path / "r")
    _forget_chat(mirror, "c9")
    mirror._docs["chats/c9/gone.json"] = {"stale": True}
    mirror._refresh_tick()
    assert mirror.get_doc("chats/c9/gone.json") is None
    assert mirror.get_doc("chats/c9/meta.json") == {"name": "Late"}


def test_mirror_pulls_everything_once_after_too_old(tmp_path, clouds):
    mirror = clouds.cached(tmp_path / "r")
    pulls = []
    snapshot = mirror.inner.snapshot_docs
    mirror.inner.snapshot_docs = lambda: pulls.append(1) or snapshot()
    clouds.client(tmp_path / "r").db["_epoch"] = "restored-epoch"
    mirror._last_full = time.monotonic() - 3600   # past the metered floor
    mirror._refresh_tick()
    assert pulls == [1] and mirror._cursor_basis == "frontier:restored-epoch"
    mirror._refresh_tick()
    assert pulls == [1]                        # back on the delta


def test_an_old_snapshot_cursor_costs_one_full_pull(tmp_path, clouds):
    snap = tmp_path / "mirror.json"
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("users/a.json", {"v": 1})
    first = clouds.cached(tmp_path / "r", snapshot_path=snap)
    assert first._cursor_basis.startswith("frontier:")
    import json
    doc = json.loads(snap.read_text())
    doc.pop("cursor_basis")                    # as written before P7
    doc.pop("caught_up")
    doc["cursor"] = 3
    snap.write_text(json.dumps(doc))

    second = clouds.cached(tmp_path / "r", snapshot_path=snap, warm=False)
    assert second._cursor_basis == "" and second._caught_up == set(second._chat_ids)
    pulls = []
    snapshot = second.inner.snapshot_docs
    second.inner.snapshot_docs = lambda: pulls.append(1) or snapshot()
    second._last_full = time.monotonic() - 3600
    second._refresh_tick()
    assert pulls == [1] and second._cursor_basis.startswith("frontier:")
    second._refresh_tick()
    assert pulls == [1]


def test_drift_counts_only_changes_the_delta_should_have_delivered(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("users/a.json", {"v": 1})
    writer.put_doc("chats/c1/meta.json", {"name": "Room"})
    mirror = clouds.cached(tmp_path / "r")
    reconcile = mirror.profile.reconcile_s
    assert reconcile == 24 * 3600.0            # P7: a once-a-day safety net

    mirror._docs["users/a.json"] = {"v": "missed"}     # below the cursor
    mirror._docs["users/ghost.json"] = {"v": "gone"}   # no row on the server
    writer.put_doc("users/news.json", {"v": 1})        # newer than the cursor
    mirror._last_full = time.monotonic() - reconcile - 1
    mirror._refresh_tick()
    drift = mirror.mirror_status()["drift"]
    assert drift["last"] == 2 and drift["total"] == 2
    since = drift["zero_since"]
    assert since is not None

    mirror._last_full = time.monotonic() - reconcile - 1
    mirror._refresh_tick()
    drift = mirror.mirror_status()["drift"]
    assert drift["last"] == 0 and drift["total"] == 2
    assert drift["zero_since"] == since        # the streak continues


def test_drift_skips_own_writes_and_chats_not_caught_up(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/c9/meta.json", {"name": "Late"})
    mirror = clouds.cached(tmp_path / "r")
    _forget_chat(mirror, "c9")                 # never read: not drift
    mirror.put_doc("users/me.json", {"v": 1})  # this process's own write
    mirror._docs["users/me.json"] = {"v": "local view"}
    mirror._last_full = time.monotonic() - mirror.profile.reconcile_s - 1
    mirror._refresh_tick()
    assert mirror.mirror_status()["drift"]["last"] == 0


def test_a_full_pull_after_too_old_counts_no_drift(tmp_path, clouds):
    mirror = clouds.cached(tmp_path / "r")
    mirror._docs["users/ghost.json"] = {"v": "gone"}
    clouds.client(tmp_path / "r").db["_epoch"] = "restored-epoch"
    mirror._last_full = time.monotonic() - 3600
    mirror._refresh_tick()                     # forced, not scheduled
    assert mirror.mirror_status()["drift"]["last"] is None


# -------------------------------------------------------------- sync engine

def test_sync_restarts_an_old_log_cursor_at_the_frontier(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    tx.append_log("c1", "ann@m", _record("c1", "ann", 1))
    store = Store(tmp_path / "cache.sqlite")
    try:
        store.cache_doc(CURSOR_DOC, {"cursor": 1})     # a pre-P7 row id
        eng = SyncEngine(tx, store)
        assert eng.sync_once() == 1
        doc = store.cached_doc(CURSOR_DOC)
        assert doc["basis"].startswith("frontier:") and doc["cursor"] > 1
        tx.append_log("c1", "ann@m", _record("c1", "ann", 2))
        assert eng.sync_once() == 1            # the feed names the log
        assert store.message_count("c1") == 2
    finally:
        store.close()


def test_sync_never_skips_a_late_commit_in_one_log(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    writer = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    store = Store(tmp_path / "cache.sqlite")
    try:
        eng = SyncEngine(tx, store)
        writer.append_log("c1", "ann@m", _record("c1", "ann", 1))
        assert eng.sync_once() == 1
        held = client.begin_transaction()
        writer.append_log("c2", "bob@m", _record("c2", "bob", 1))
        eng.sync_once()
        client.finish_transaction(
            held, lambda: writer.append_log("c1", "ann@m", _record("c1", "ann", 2)))
        eng.sync_once()
        assert store.message_count("c1") == 2 and store.message_count("c2") == 1
    finally:
        store.close()


def test_driver_parameters_match_the_schema_signatures(tmp_path, clouds):
    """A wrong parameter name reads as "function missing" (PGRST202) and
    would silently keep the pre-P7 counters, so pin both ends."""
    import re
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[1] / "docs"
              / "supabase_schema.sql").read_text(encoding="utf-8")
    declared = {}
    for fn in ("ab_feeds_frontier", "ab_docs_feed", "ab_logs_feed"):
        match = re.search(
            rf"create or replace function public\.{fn}\((.*?)\)\s*returns jsonb",
            schema, re.S)
        assert match, fn
        declared[fn] = {part.split()[0] for part in match.group(1).split(",")}
    tx = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    sent = {}
    rpc = client.rpc

    def record(fn, params=None):
        sent.setdefault(fn, set(params or {}))
        return rpc(fn, params)

    client.rpc = record
    _, cursor = tx.snapshot_docs()
    tx.get_docs_delta(cursor)
    tx.changed_logs(cursor)
    assert {fn: sent[fn] for fn in declared} == declared


# ------------------------------------------- review findings (2026-10-20)

def test_hard_deleting_a_live_row_makes_every_older_cursor_too_old(tmp_path, clouds):
    """Only an operator can do this (members may purge tombstones only),
    and no reader could learn of it from the feed."""
    tx = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    tx.put_doc("status/a.json", {"v": 1})
    _, before = tx.snapshot_docs()
    client.table("ab_docs").delete().eq("root", tx.root) \
        .eq("path", "status/a.json").execute()
    with pytest.raises(CursorTooOld):
        tx.get_docs_delta(before)
    _, after = tx.snapshot_docs()
    assert tx.get_docs_delta(after) == ({}, set(), after)


def test_an_undecided_schema_probe_never_guesses_legacy(tmp_path, clouds, monkeypatch):
    monkeypatch.setattr(supabase_mod, "_RETRY_WAIT", 0)
    client = clouds.client(tmp_path / "r")
    tx = clouds.bare(tmp_path / "r")
    tx.put_doc("users/a.json", {"v": 1})
    tx._delta = None                           # a fresh process
    offline = {"on": True}
    table = client.table

    def flaky(name):
        query = table(name)
        select = query.select

        def probe(cols="*"):
            if cols == "seq" and offline["on"]:
                raise ConnectionError("network down")
            return select(cols)
        query.select = probe
        return query

    client.table = flaky
    with pytest.raises(ConnectionError):
        tx.delete_doc("users/a.json")          # never a hard delete
    with pytest.raises(ConnectionError):
        tx.snapshot_docs()                     # never tombstones as live docs
    with pytest.raises(ConnectionError):
        tx.changed_logs(FeedCursor(1, "frontier:x"))
    assert tx._delta is None and tx._feeds is None
    assert [r["path"] for r in client.db["ab_docs"]] == ["users/a.json"]
    offline["on"] = False
    tx.delete_doc("users/a.json")              # now a soft delete
    assert client.db["ab_docs"][0]["deleted"] is True


def test_a_prefix_read_keeps_its_slash(tmp_path, clouds):
    tx = clouds.bare(tmp_path / "r")
    tx.put_doc("chats/c9/meta.json", {"name": "nine"})
    tx.put_doc("chats/c90/meta.json", {"name": "ninety"})
    assert list(tx.get_docs("chats/c9/")) == ["chats/c9/meta.json"]
    assert tx.list_docs("chats/c9/") == ["chats/c9/meta.json"]


def test_a_keyset_read_that_does_not_advance_raises(tmp_path, clouds, monkeypatch):
    monkeypatch.setattr(supabase_mod, "_DOC_PAGE", 2)
    tx = clouds.bare(tmp_path / "r")
    client = clouds.client(tmp_path / "r")
    for name in "abc":
        tx.put_doc(f"d/{name}.json", {"v": name})
    table = client.table

    def stuck(name):
        query = table(name)
        query.gt = lambda col, val: query    # the server ignores `path > last`
        return query

    client.table = stuck
    with pytest.raises(supabase_mod.TransportError, match="did not advance"):
        tx.get_docs("d/")


def test_without_the_feeds_the_full_pull_keeps_its_six_hours(tmp_path, clouds):
    client = clouds.client(tmp_path / "r")
    client.db["_feeds"] = False
    legacy = clouds.cached(tmp_path / "r")
    assert legacy._cursor_basis == ""
    pulls = []
    snapshot = legacy.inner.snapshot_docs
    legacy.inner.snapshot_docs = lambda: pulls.append(1) or snapshot()
    legacy._last_full = time.monotonic() - 7 * 3600
    legacy._refresh_tick()
    assert pulls == [1]

    client.db["_feeds"] = True
    current = clouds.cached(tmp_path / "r2")
    assert current._cursor_basis.startswith("frontier:")
    snapshot2 = current.inner.snapshot_docs
    current.inner.snapshot_docs = lambda: pulls.append(2) or snapshot2()
    current._last_full = time.monotonic() - 7 * 3600
    current._refresh_tick()
    assert pulls == [1]                        # still on the delta


def test_a_membership_change_reads_the_chat_again(tmp_path, clouds):
    """Removed and re-added between two ticks: the delta skipped what the
    chat wrote meanwhile; the re-add's new joined_ns triggers a re-read."""
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/c9/meta.json", {"members": {"ann": {"joined_ns": 1}}})
    writer.put_doc("chats/c9/redactions.json", {"m1": True})
    mirror = clouds.cached(tmp_path / "r")
    del mirror._docs["chats/c9/redactions.json"]   # written while hidden
    assert "c9" in mirror._caught_up
    writer.put_doc("chats/c9/meta.json", {"members": {"ann": {"joined_ns": 2}}})
    mirror._refresh_tick()
    assert mirror.get_doc("chats/c9/redactions.json") == {"m1": True}
    reads = []
    get_docs = mirror.inner.get_docs
    mirror.inner.get_docs = lambda prefix="": reads.append(prefix) or get_docs(prefix)
    writer.put_doc("chats/c9/meta.json", {"members": {"ann": {"joined_ns": 2}},
                                          "name": "renamed"})
    mirror._refresh_tick()
    assert reads == []                         # a rename is not a re-read


def test_one_failing_chat_does_not_stall_the_delta(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/ok/meta.json", {"name": "ok"})
    writer.put_doc("chats/bad/meta.json", {"name": "bad"})
    mirror = clouds.cached(tmp_path / "r")
    _forget_chat(mirror, "ok")
    _forget_chat(mirror, "bad")
    get_docs = mirror.inner.get_docs

    def failing(prefix=""):
        if prefix == "chats/bad/":
            raise ConnectionError("this chat's read keeps failing")
        return get_docs(prefix)

    mirror.inner.get_docs = failing
    writer.put_doc("users/news.json", {"v": 1})
    mirror._refresh_tick()
    assert mirror.get_doc("users/news.json") == {"v": 1}
    assert mirror.get_doc("chats/ok/meta.json") == {"name": "ok"}
    assert "ok" in mirror._caught_up and "bad" not in mirror._caught_up
    mirror.inner.get_docs = get_docs
    mirror._refresh_tick()                     # retried on the next tick
    assert "bad" in mirror._caught_up


def test_a_guard_skipped_value_is_read_again_next_tick(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    mirror = clouds.cached(tmp_path / "r")
    mirror.put_doc("users/p.json", {"v": "mine"})
    writer.put_doc("users/p.json", {"v": "theirs, newer"})
    # our write landed while the tick's delta was in flight
    mirror._doc_writes["users/p.json"] = time.monotonic() + 3600
    mirror._refresh_tick()
    assert mirror.get_doc("users/p.json") == {"v": "mine"}
    assert "users/p.json" in mirror._reread
    mirror._doc_writes["users/p.json"] = time.monotonic() - 3600
    mirror._refresh_tick()                     # the delta has passed it
    assert mirror.get_doc("users/p.json") == {"v": "theirs, newer"}
    assert mirror._reread == set()


def test_a_chat_revoked_during_the_pull_is_not_drift(tmp_path, clouds):
    writer = clouds.bare(tmp_path / "r")
    writer.put_doc("chats/c1/meta.json", {"name": "Room"})
    writer.put_doc("chats/c1/pins/p.json", {"pin": 1})
    mirror = clouds.cached(tmp_path / "r")
    snapshot = mirror.inner.snapshot_docs

    def revoked_mid_pull():
        docs, cursor = snapshot()
        hidden = [p for p in docs if p.startswith("chats/c1/")]
        for path in hidden:
            del docs[path]
            del cursor.row_positions[path]
        return docs, cursor

    mirror.inner.snapshot_docs = revoked_mid_pull
    mirror._last_full = time.monotonic() - mirror.profile.reconcile_s - 1
    mirror._refresh_tick()
    assert mirror.mirror_status()["drift"]["last"] == 0
