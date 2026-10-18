#!/usr/bin/env python3
"""Check the commit-ordered feeds (P7) through the real driver and PostgREST.

docs/COMMIT_ORDERED_CURSORS.md. This signs in the two Scratch logins with
the production ``SupabaseTransport`` (member auth, row security) and checks
on ``--root``, where both are members of ``chats/room``:

- the driver's probe finds the feed RPCs, so it does not fall back to the
  pre-P7 counters (a parameter-name mismatch would look like "missing");
- a full pull returns a ``FeedCursor`` at the frontier and each row's write
  position (PostgREST renders ``xid8`` as a string);
- a doc the owner writes reaches the reader's delta, its soft delete comes
  as a deletion, and an idle delta changes nothing;
- a pre-P7 counter cursor answers "too old", and the log feed restarts at
  the frontier;
- two appends to one log reach the reader's log feed and read back in
  order;
- a chat only the owner belongs to stays out of the reader's doc and log
  feeds (row security applies through the RPCs).

Each wait for a write to pass the frontier is bounded and timed: the lag is
how long the slowest concurrent transaction on the project held it.

The root is prepared on Scratch beforehand (both logins members of it and
of ``chats/room``). Writes stay in that root. The script prints positions,
lags and booleans; never passwords, tokens, keys or document bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FRONTIER_WAIT_S = 20.0


class GateFailure(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise GateFailure(f"required environment variable {name} is missing")
    return value


def _transport(root: str, url: str, key: str, email: str, password: str):
    from agentbridge.transport.supabase import SupabaseTransport

    tx = SupabaseTransport(root, env={
        "SUPABASE_URL": url, "SUPABASE_PUBLISHABLE_KEY": key,
        "SUPABASE_MEMBER_EMAIL": email, "SUPABASE_MEMBER_PASSWORD": password,
    })
    tx._ensure_rt = lambda: None   # this check is about the feeds, not pokes
    tx._sb()
    if not tx.auth_mode.startswith("member:"):
        raise GateFailure("a Scratch login did not sign in as a member")
    return tx


def _member_name(tx, root: str) -> str:
    client = tx._sb()
    uid = str(client.auth.get_user().user.id)
    rows = client.table("ab_members").select("username,uid") \
        .eq("root", root).execute().data
    names = [row["username"] for row in rows if str(row["uid"]) == uid]
    if len(names) != 1:
        raise GateFailure("the owner is not a member of the root")
    return names[0]


def _until(read, done):
    """Call ``read`` until ``done(result)``; return (result, seconds)."""
    start = time.monotonic()
    while True:
        result = read()
        waited = time.monotonic() - start
        if done(result) or waited > FRONTIER_WAIT_S:
            return result, round(waited, 2)
        time.sleep(0.25)


def _check(root: str, owner, reader) -> dict:
    from agentbridge.transport.base import CursorTooOld, FeedCursor

    out: dict = {"checks": {}, "lag_s": {}}
    checks, lag = out["checks"], out["lag_s"]

    checks["reader_feeds_live"] = reader._feeds_ok() is True
    checks["owner_feeds_live"] = owner._feeds_ok() is True
    docs, cursor = reader.snapshot_docs()
    checks["snapshot_cursor_is_frontier"] = (
        type(cursor) is FeedCursor and cursor.basis.startswith("frontier:"))
    positions = cursor.row_positions if type(cursor) is FeedCursor else None
    checks["snapshot_positions_are_ints"] = (
        isinstance(positions, dict) and bool(positions)
        and all(type(v) is int for v in positions.values()))
    checks["snapshot_sees_room"] = "chats/room/meta.json" in docs
    out["epoch_present"] = (type(cursor) is FeedCursor
                            and cursor.basis != "frontier:")
    out["frontier"] = cursor.position if type(cursor) is FeedCursor else None

    nonce = uuid4().hex[:8]
    path = f"chats/room/p7/{nonce}.json"
    owner.put_doc(path, {"check": nonce})
    (changed, deleted, c2), lag["doc_write"] = _until(
        lambda: reader.get_docs_delta(cursor), lambda r: path in r[0])
    checks["delta_serves_write"] = changed.get(path) == {"check": nonce}
    checks["delta_advances"] = (c2.position > cursor.position
                                and c2.basis == cursor.basis)

    owner.delete_doc(path)
    (changed, deleted, c3), lag["doc_delete"] = _until(
        lambda: reader.get_docs_delta(c2), lambda r: path in r[1])
    checks["delta_serves_tombstone"] = path in deleted and path not in changed
    changed, deleted, c4 = reader.get_docs_delta(c3)
    checks["idle_delta_is_empty"] = (changed == {} and deleted == set()
                                     and c4.position >= c3.position)

    try:
        reader.get_docs_delta(5)
        checks["counter_cursor_too_old"] = False
    except CursorTooOld:
        checks["counter_cursor_too_old"] = True
    try:
        reader.changed_logs(0)
        restart = None
    except CursorTooOld as exc:
        restart = exc.restart
    checks["log_feed_restarts_at_frontier"] = (
        type(restart) is FeedCursor and restart.basis == cursor.basis)

    log = f"p7-{nonce}@check"
    first, second = f"{nonce}-1", f"{nonce}-2"
    owner.append_log("room", log, {"id": first, "from": "p7-check"})
    owner.append_log("room", log, {"id": second, "from": "p7-check"})
    (pairs, lc), lag["log_append"] = _until(
        lambda: reader.changed_logs(restart), lambda r: ("room", log) in r[0])
    checks["log_feed_names_the_log"] = ("room", log) in pairs
    records, _offset = reader.read_log("room", log)
    checks["log_reads_in_order"] = [r.get("id") for r in records] == [first, second]

    solo = f"solo-{nonce}"
    owner_name = _member_name(owner, root)
    owner.create_doc(f"chats/{solo}/meta.json", {
        "name": "P7 owner only", "members": {owner_name: {"role": "admin"}}})
    owner.put_doc(f"chats/{solo}/p7.json", {"check": nonce})
    owner.append_log(solo, log, {"id": f"{nonce}-solo", "from": "p7-check"})
    (owner_changed, _, _), lag["solo_write"] = _until(
        lambda: owner.get_docs_delta(c4),
        lambda r: f"chats/{solo}/p7.json" in r[0])
    checks["owner_sees_own_chat"] = f"chats/{solo}/p7.json" in owner_changed
    owner_pairs, _ = owner.changed_logs(lc)
    checks["owner_log_feed_sees_own_chat"] = (solo, log) in owner_pairs
    reader_changed, reader_deleted, _ = reader.get_docs_delta(c4)
    checks["reader_doc_feed_hides_foreign_chat"] = not any(
        p.startswith(f"chats/{solo}/") for p in set(reader_changed) | reader_deleted)
    reader_pairs, _ = reader.changed_logs(lc)
    checks["reader_log_feed_hides_foreign_chat"] = all(
        chat != solo for chat, _ in reader_pairs)

    out["passed"] = all(checks.values())
    out["nonce"] = nonce
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    url = _required("AGENTBRIDGE_SCRATCH_SUPABASE_URL")
    key = _required("AGENTBRIDGE_SCRATCH_SUPABASE_PUBLISHABLE_KEY")
    owner = _transport(args.root, url, key,
                       _required("AGENTBRIDGE_SCRATCH_OWNER_EMAIL"),
                       _required("AGENTBRIDGE_SCRATCH_OWNER_PASSWORD"))
    reader = _transport(args.root, url, key,
                        _required("AGENTBRIDGE_SCRATCH_READER_EMAIL"),
                        _required("AGENTBRIDGE_SCRATCH_READER_PASSWORD"))
    try:
        result = _check(args.root, owner, reader)
    finally:
        owner.close()
        reader.close()
    result["root"] = args.root
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateFailure as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        raise SystemExit(1)
