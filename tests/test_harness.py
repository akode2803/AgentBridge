"""The agent harness core (R15): queue durability, the answered-guard,
sender batching + parallel groups, catch-up policy, edit re-triggers, timers,
stand-down, identity checks, and adoption. Everything runs over a real
folder-transport scratch root with E2EE on — the same stack the live mesh
uses — with a scripted Responder standing in for R16's adapters.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.harness import (
    AgentRunner, HarnessSettings, MESSAGE_BREAK, Reply, SILENCE, clean_reply,
    split_reply,
)
from agentbridge.harness.triggers import Candidate
from agentbridge.mesh.service import Mesh


class Scripted:
    """Deterministic Responder: records deliveries, returns what fn says."""

    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn or (lambda d: Reply(
            body=f"answering @{d.triggers[-1].sender}" if d.triggers
            else "timer follow-up"))

    def respond(self, delivery, on_step=None):
        self.calls.append(delivery)
        return self.fn(delivery)


@pytest.fixture
def hrig(tmp_path):
    """A human owner + an agent on ONE machine sharing one home (exactly the
    production layout: the GUI and the harness share ~/.agentbridge)."""
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    owner = Mesh(root, "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    rig = SimpleNamespace(root=root, home=home, owner=owner, runners=[])

    def make_runner(responder=None, agent="helper", machine="devbox"):
        r = AgentRunner(root, agent, home=home, machine=machine,
                        responder=responder, poll_s=0.2)
        rig.runners.append(r)
        return r

    rig.make_runner = make_runner
    yield rig
    for r in rig.runners:
        r.close()
    owner.close()


def ripple(rig, runner, chat_id):
    """Flush + sync both sides (the test-time stand-in for the run loops)."""
    rig.owner.outbox.flush_once()
    if runner is not None:
        runner.mesh.outbox.flush_once()
        runner.mesh.sync.sync_once([chat_id])
    rig.owner.sync.sync_once([chat_id])


def turn(rig, runner, chat_id):
    """One harness turn: sync, scan+dispatch, wait, publish the reply."""
    runner.mesh.sync.sync_once([chat_id])
    runner.tick()
    runner.drain()
    ripple(rig, runner, chat_id)


def agent_msgs(mesh, chat_id, agent="helper"):
    return [m for m in mesh.messages_for(chat_id) if m.from_ == agent]


def latest_run(tx, agent="helper"):
    runs = (tx.get_doc(f"status/{agent}_runs.json") or {}).get("runs") or []
    return runs[-1] if runs else None


def active_runs(tx, agent="helper"):
    return (tx.get_doc(f"status/{agent}_live.json") or {}).get("runs") or []


# ---------------------------------------------------------------- the basics

def test_tagged_reply_end_to_end(hrig):
    snap = hrig.owner.create_chat("Ops", members=["helper"])
    trigger = hrig.owner.post(snap.id, "hey @helper, status please")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    turn(hrig, runner, snap.id)

    replies = agent_msgs(hrig.owner, snap.id)
    assert len(replies) == 1
    assert replies[0].body == "answering @aryan"
    assert (replies[0].reply_to or {}).get("id") == trigger.id
    # enriched delivery: sender context reached the responder
    d = responder.calls[0]
    assert d.triggers[0].sender == "aryan"
    assert d.triggers[0].sender_kind == "human"
    assert any(r["name"] == "helper" and r["you"] for r in d.roster)
    # the run feed closed cleanly
    feed = latest_run(runner.mesh.tx)
    assert feed and feed["state"] == "done" and feed["chat_id"] == snap.id


def test_reply_timings_are_profiled(hrig):
    """R30: a finished run leaves a stage-timing profile — a JSONL record in
    the local home, a summary on the run feed, and a ⏱ line in the reply's
    Message-info task doc."""
    import json

    snap = hrig.owner.create_chat("Perf", members=["helper"])
    hrig.owner.post(snap.id, "hey @helper, how long do you take?")
    runner = hrig.make_runner(Scripted())
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    log = hrig.home / "harness" / "perf" / "helper.jsonl"
    assert log.is_file()
    rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["outcome"] == "posted" and rec["chat_id"] == snap.id
    for key in ("total_s", "pickup_s", "context_s", "model_s", "post_s"):
        assert key in rec and rec[key] >= 0
    # the run feed carries the human summary…
    feed = latest_run(runner.mesh.tx)
    assert "total" in feed["note"] and "model" in feed["note"]
    # …and so does the reply's Message-info task doc
    reply = agent_msgs(hrig.owner, snap.id)[0]
    tasks = runner.mesh.tx.get_doc(f"chats/{snap.id}/tasks/{reply.id}.json")
    assert any(t["text"].startswith("⏱") for t in tasks["tasks"])


def test_untagged_message_stays_silent(hrig):
    snap = hrig.owner.create_chat("Quiet", members=["helper"])
    hrig.owner.post(snap.id, "just thinking out loud")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert responder.calls == []
    assert agent_msgs(hrig.owner, snap.id) == []


def test_dm_replies_without_tagging(hrig):
    """Talking to an agent one-on-one IS addressing it: a DM defaults to
    the 'all' rule (v1 semantics; the GUI advertises exactly this)."""
    dm = hrig.owner.create_dm("helper")
    hrig.owner.post(dm.id, "hi, no tag needed here")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, dm.id)
    turn(hrig, runner, dm.id)
    assert len(agent_msgs(hrig.owner, dm.id)) == 1
    assert responder.calls[0].rule == "all"

    # an explicit per-chat rule still wins over the DM default
    hrig.owner.accounts.set_agent_harness(
        "helper", {"rules": {dm.id: "tagged"}})
    hrig.owner.post(dm.id, "this untagged one stays unanswered")
    turn(hrig, runner, dm.id)
    assert len(agent_msgs(hrig.owner, dm.id)) == 1


def test_reply_quote_flag_follows_the_chat_moving_on(hrig):
    """R31: answering the NEWEST message keeps the attribution (reply_to.id —
    the answered-guard's transcript leg needs it) but flags quote=False so
    clients show a plain standalone message; once the chat has moved on past
    the trigger, the visible quote stays."""
    snap = hrig.owner.create_chat("Thread", members=["helper"])
    q1 = hrig.owner.post(snap.id, "@helper newest-message question")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    first = agent_msgs(hrig.owner, snap.id)[0]
    assert first.reply_to.get("id") == q1.id
    assert first.reply_to.get("quote") is False        # displays standalone

    q2 = hrig.owner.post(snap.id, "@helper older question")
    hrig.owner.post(snap.id, "an untagged aside lands after it")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    second = agent_msgs(hrig.owner, snap.id)[-1]
    assert second.reply_to.get("id") == q2.id
    assert second.reply_to.get("quote", True) is True  # quote stays visible


def test_scan_is_idempotent(hrig):
    snap = hrig.owner.create_chat("Once", members=["helper"])
    hrig.owner.post(snap.id, "@helper ping")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    for _ in range(3):
        turn(hrig, runner, snap.id)
    assert len(responder.calls) == 1
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


def test_sender_burst_gets_one_reply(hrig):
    snap = hrig.owner.create_chat("Burst", members=["helper"])
    hrig.owner.post(snap.id, "@helper first thing")
    hrig.owner.post(snap.id, "also this")
    last = hrig.owner.post(snap.id, "@helper and finally this")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    replies = agent_msgs(hrig.owner, snap.id)
    assert len(replies) == 1                       # batched, not three replies
    assert (replies[0].reply_to or {}).get("id") == last.id
    assert len(responder.calls[0].triggers) == 2   # both tagged messages in it


def test_two_senders_answered_in_parallel_groups(hrig):
    fable = Mesh(hrig.root, "fable", "devbox", encrypt=True, home=hrig.home)
    fable.accounts.create_human("fable", "fablepass")
    try:
        snap = hrig.owner.create_chat("Busy", members=["helper", "fable"])
        hrig.owner.outbox.flush_once()
        fable.sync.sync_once([snap.id])
        q1 = hrig.owner.post(snap.id, "@helper question from aryan")
        q2 = fable.post(snap.id, "@helper question from fable")
        fable.outbox.flush_once()
        responder = Scripted()
        runner = hrig.make_runner(responder)
        ripple(hrig, runner, snap.id)
        turn(hrig, runner, snap.id)

        replies = agent_msgs(hrig.owner, snap.id)
        assert len(replies) == 2
        answered = {(r.reply_to or {}).get("id") for r in replies}
        assert answered == {q1.id, q2.id}
    finally:
        fable.close()


# ------------------------------------------------------- the answered-guard

def test_no_duplicate_after_local_state_loss(hrig):
    """The bug R15 exists to kill: even losing every local cursor AND the
    ledger must not produce a second reply — the transcript leg holds."""
    snap = hrig.owner.create_chat("Guard", members=["helper"])
    hrig.owner.post(snap.id, "@helper once only please")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1

    # simulate total local-state loss (the v1 failure mode, amplified)
    runner.mesh.store.cache_doc(f"harness/answered/{snap.id}", {})
    runner.queue.set_scan_cursor(snap.id, 0, 0)
    turn(hrig, runner, snap.id)

    assert len(responder.calls) == 1               # never re-ran
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


def test_queue_survives_restart(hrig):
    """Durable queue: items enqueued by one process are answered by the
    next — nothing enqueued is ever dropped by a crash."""
    snap = hrig.owner.create_chat("Durable", members=["helper"])
    hrig.owner.post(snap.id, "@helper hold this thought")
    scanner = hrig.make_runner(responder=None)     # scans, cannot dispatch
    ripple(hrig, scanner, snap.id)
    scanner.mesh.sync.sync_once([snap.id])
    scanner.tick()
    assert scanner.queue.snapshot()                # pending on disk
    scanner.close()

    responder = Scripted()
    runner2 = hrig.make_runner(responder)
    turn(hrig, runner2, snap.id)                   # no rescan needed
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


def test_no_reply_sentinel_stays_quiet(hrig):
    snap = hrig.owner.create_chat("Silence", members=["helper"])
    trig = hrig.owner.post(snap.id, "@helper fyi only, no answer needed")
    responder = Scripted(lambda d: Reply(body=SILENCE))
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert len(responder.calls) == 1
    assert agent_msgs(hrig.owner, snap.id) == []
    assert runner.queue.answered(snap.id, trig.id, 0)
    turn(hrig, runner, snap.id)                    # and it never re-fires
    assert len(responder.calls) == 1


def test_rule_all_own_tail_damping(hrig):
    hrig.owner.accounts.set_agent_harness("helper", {"default_rule": "all"})
    snap = hrig.owner.create_chat("Chatty", members=["helper"])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    hrig.owner.post(snap.id, "morning everyone")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1   # rule-all replied

    # a rule-all trigger that sits BEHIND my own newest message is a closed
    # exchange (the v1 loop-damper, scoped): rewind the cursor to re-see it
    runner.queue.set_scan_cursor(snap.id, 0, 0)
    runner.mesh.store.cache_doc(f"harness/answered/{snap.id}", {})
    turn(hrig, runner, snap.id)
    assert len(responder.calls) == 1                   # damped, no rerun


# ----------------------------------------------------------- rate + catchup

def test_rate_cap_defers_the_second_group(hrig):
    hrig.owner.accounts.set_agent_harness("helper", {"max_replies_per_hour": 1})
    fable = Mesh(hrig.root, "fable", "devbox", encrypt=True, home=hrig.home)
    fable.accounts.create_human("fable", "fablepass")
    try:
        snap = hrig.owner.create_chat("Capped", members=["helper", "fable"])
        hrig.owner.outbox.flush_once()
        fable.sync.sync_once([snap.id])
        hrig.owner.post(snap.id, "@helper one")
        fable.post(snap.id, "@helper two")
        fable.outbox.flush_once()
        responder = Scripted()
        runner = hrig.make_runner(responder)
        ripple(hrig, runner, snap.id)
        turn(hrig, runner, snap.id)
        turn(hrig, runner, snap.id)

        assert len(agent_msgs(hrig.owner, snap.id)) == 1  # cap held
        pending = runner.queue.snapshot()
        assert len(pending) == 1                          # deferred, not lost
        assert pending[0]["status"] == "pending"
    finally:
        fable.close()


def test_catchup_policy_units():
    old = Candidate(message=None, edit_ns=0,
                    trigger_ns=time.time_ns() - int(3 * 3600 * 1e9),
                    reason="tagged")
    fresh = Candidate(message=None, edit_ns=0, trigger_ns=time.time_ns(),
                      reason="tagged")
    r = AgentRunner.__new__(AgentRunner)              # policy is pure — no rig
    r._started_ns = time.time_ns()
    assert r._catchup_skip(old, HarnessSettings(catchup_window_h=1.0)) \
        == "catch-up:window"
    assert r._catchup_skip(old, HarnessSettings(catchup="all")) is None
    assert r._catchup_skip(old, HarnessSettings(catchup="none")) \
        == "catch-up:none"
    assert r._catchup_skip(fresh, HarnessSettings(catchup="none")) is None
    assert r._catchup_skip(fresh, HarnessSettings()) is None


# -------------------------------------------------------------------- edits

def test_human_edit_retriggers_once(hrig):
    snap = hrig.owner.create_chat("Edits", members=["helper"])
    msg = hrig.owner.post(snap.id, "note to self about the report")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert responder.calls == []                       # not a trigger yet

    hrig.owner.edit(snap.id, msg.id, "@helper can you pull the report?")
    turn(hrig, runner, snap.id)
    replies = agent_msgs(hrig.owner, snap.id)
    assert len(replies) == 1                           # the edit fired once
    assert responder.calls[0].triggers[0].reason == "edit"
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1   # and never replays


# ------------------------------------------------------------------- timers

def test_reply_can_schedule_a_timer_that_fires(hrig):
    snap = hrig.owner.create_chat("Later", members=["helper"])
    hrig.owner.post(snap.id, "@helper remind us about the deploy")

    def fn(d):
        if d.kind == "timer":
            return Reply(body=f"scheduled follow-up: {d.note}")
        return Reply(body="will do", timers=[{"in_s": 0.05, "note": "deploy check"}])

    responder = Scripted(fn)
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1

    # the pending timer is owner-visible before it fires
    doc = runner.mesh.tx.get_doc("status/helper_harness.json")
    assert doc and doc["timers"] and doc["timers"][0]["note"] == "deploy check"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(agent_msgs(hrig.owner, snap.id)) < 2:
        turn(hrig, runner, snap.id)
    replies = agent_msgs(hrig.owner, snap.id)
    assert len(replies) == 2
    assert replies[-1].body == "scheduled follow-up: deploy check"
    assert runner.timers.snapshot() == []              # one-shot, consumed


# --------------------------------------------------------------- stand-down

def test_global_pause_holds_then_resume_answers(hrig):
    snap = hrig.owner.create_chat("Paused", members=["helper"])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    hrig.owner.tx.put_doc("control.json", {"paused": True})
    hrig.owner.post(snap.id, "@helper are you there?")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert responder.calls == []                       # standing down

    hrig.owner.tx.put_doc("control.json", {"paused": False})
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1   # backlog answered


def test_owner_stand_down_switch_holds(hrig):
    snap = hrig.owner.create_chat("Down", members=["helper"])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    hrig.owner.accounts.set_machine_agents_active(False)
    hrig.owner.post(snap.id, "@helper hello?")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert responder.calls == []

    hrig.owner.accounts.set_machine_agents_active(True)
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


# ----------------------------------------------------------- error handling

def test_responder_failure_posts_notice_once(hrig):
    snap = hrig.owner.create_chat("Broken", members=["helper"])
    hrig.owner.post(snap.id, "@helper do the thing")

    def boom(d):
        raise RuntimeError("adapter fell over")

    responder = Scripted(boom)
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    replies = agent_msgs(hrig.owner, snap.id)
    assert len(replies) == 1                           # one notice, no loop
    assert "could not produce a reply" in replies[0].body
    assert len(responder.calls) == 1
    feed = latest_run(runner.mesh.tx)
    assert feed["state"] == "error" and "adapter fell over" in feed["note"]
    assert "RuntimeError: adapter fell over" in replies[0].body


# ------------------------------------------------- files, identity, adoption

def test_reply_files_are_sealed_and_readable_by_members(hrig, tmp_path):
    out = tmp_path / "result.csv"
    out.write_bytes(b"a,b\n1,2\n")
    snap = hrig.owner.create_chat("Files", members=["helper"])
    hrig.owner.post(snap.id, "@helper send the export")
    responder = Scripted(lambda d: Reply(body="here you go",
                                         files=[str(out)]))
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    reply = agent_msgs(hrig.owner, snap.id)[0]
    assert reply.files and reply.files[0]["name"] == "result.csv"
    blob_id = reply.files[0]["id"]
    raw = hrig.owner.tx.get_blob(f"chats/{snap.id}/files/{blob_id}")
    assert raw.startswith(b"AB2E")                     # sealed at rest
    assert hrig.owner.sealer.open_blob(snap.id, blob_id, raw) == b"a,b\n1,2\n"


def test_oversized_attachment_is_named_but_reply_still_posts(hrig, tmp_path):
    small = tmp_path / "summary.txt"
    large = tmp_path / "archive.zip"
    small.write_text("ready", encoding="utf-8")
    large.write_bytes(b"x" * 2048)
    snap = hrig.owner.create_chat("File cap", members=["helper"])
    hrig.owner.post(snap.id, "@helper send both files")
    runner = hrig.make_runner(Scripted(lambda d: Reply(
        body="Delivery ready", files=[str(small), str(large)])))
    runner.mesh.tx.max_upload_bytes = 1024
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    reply = agent_msgs(hrig.owner, snap.id)[0]
    assert reply.body.startswith("Delivery ready")
    assert "Not attached" in reply.body and "archive.zip" in reply.body
    assert [f["name"] for f in reply.files] == ["summary.txt"]
    assert latest_run(runner.mesh.tx)["state"] == "done"


def test_attachment_upload_failure_rolls_back_earlier_blobs(hrig, tmp_path,
                                                            monkeypatch):
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    snap = hrig.owner.create_chat("Rollback", members=["helper"])
    runner = hrig.make_runner(Scripted())
    tx = runner.mesh.tx
    original_put = tx.put_blob
    original_delete = tx.delete_blob
    uploaded = []
    deleted = []

    def flaky_put(path, data):
        if uploaded:
            raise OSError("storage unavailable")
        original_put(path, data)
        uploaded.append(path)

    def tracked_delete(path):
        deleted.append(path)
        original_delete(path)

    monkeypatch.setattr(tx, "put_blob", flaky_put)
    monkeypatch.setattr(tx, "delete_blob", tracked_delete)
    with pytest.raises(OSError, match="storage unavailable"):
        runner._attach(snap.id, [str(first), str(second)])

    assert deleted == uploaded
    assert tx.get_blob(uploaded[0]) is None


def test_multi_message_burst_end_to_end(hrig):
    """V78 (R79): a reply split by MESSAGE_BREAK posts as separate messages,
    in order; the FIRST carries the trigger's reply_to (the answered-guard's
    transcript leg), the rest are standalone; the feed says how many. The
    burst runs even under a cap of 1 — one turn spends ONE rate slot."""
    snap = hrig.owner.create_chat("Burst", members=["helper"])
    trigger = hrig.owner.post(snap.id, "@helper walk me through it")
    responder = Scripted(lambda d: Reply(
        body=f"Short answer: yes.\n{MESSAGE_BREAK}\nThe longer why."))
    runner = hrig.make_runner(responder)
    hrig.owner.accounts.set_agent_harness("helper", {"max_replies_per_hour": 1})
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    replies = agent_msgs(hrig.owner, snap.id)
    assert [m.body for m in replies] == ["Short answer: yes.",
                                         "The longer why."]
    assert (replies[0].reply_to or {}).get("id") == trigger.id
    assert not replies[1].reply_to
    assert replies[0].ns < replies[1].ns
    feed = latest_run(runner.mesh.tx)
    assert feed["state"] == "done" and "(2 messages)" in feed["note"]
    # the answered-guard holds: a re-scan re-fires nothing
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 2


def test_multi_message_files_ride_the_last_part(hrig, tmp_path):
    out = tmp_path / "notes.txt"
    out.write_bytes(b"hello")
    snap = hrig.owner.create_chat("BurstFiles", members=["helper"])
    hrig.owner.post(snap.id, "@helper the file please")
    responder = Scripted(lambda d: Reply(
        body=f"Making it now.\n{MESSAGE_BREAK}\nHere it is.",
        files=[str(out)]))
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)

    first, last = agent_msgs(hrig.owner, snap.id)
    assert not first.files
    assert last.files and last.files[0]["name"] == "notes.txt"


def test_identity_checks_and_adoption(hrig):
    # wrong machine: refuse
    foreign = hrig.make_runner(machine="laptop")
    assert any("hosted on" in p for p in foreign.verify_identity())
    # right machine, created here: clean
    ok = hrig.make_runner()
    assert ok.verify_identity() == []

    # a migrated-shaped agent: keyless, machine="migrated", owned by aryan
    hrig.owner.tx.put_doc("users/legacybot.json", {
        "name": "legacybot", "kind": "agent", "display": "Legacybot",
        "created": "2026-01-01T00:00:00Z", "active": True,
        "agent": {"owner": "aryan", "machine": "migrated", "harness": {}},
    })
    stranded = hrig.make_runner(agent="legacybot")
    assert stranded.verify_identity()                  # not runnable yet

    hrig.owner.accounts.adopt_agent("legacybot")       # the owner-side fix
    adopted = hrig.make_runner(agent="legacybot")
    assert adopted.verify_identity() == []
    acc = hrig.owner.directory.get("legacybot")
    assert acc.agent.machine == "devbox" and acc.keys.sign_pub


def test_adopt_refuses_keyed_agent_from_elsewhere(tmp_path):
    root = tmp_path / "mesh2"
    root.mkdir()
    other_home = tmp_path / "other-home"
    other = Mesh(root, "aryan", "otherbox", encrypt=True, home=other_home)
    other.accounts.create_human("aryan", "hunter2x")
    other.accounts.create_agent("roamer")              # keys live on otherbox
    other.close()

    here = Mesh(root, "aryan", "devbox", encrypt=True,
                home=tmp_path / "home")
    try:
        with pytest.raises(ValidationError):
            here.accounts.adopt_agent("roamer")
    finally:
        here.close()


# ---------------------------------------------------------------- pure bits

def test_clean_reply_sentinel_and_narration():
    assert clean_reply(SILENCE) == ("", True)
    assert clean_reply(f"Let me check the files.\n\n{SILENCE}") == ("", True)
    assert clean_reply(f"`{SILENCE}`") == ("", True)      # decorated sentinel
    body, quiet = clean_reply(f"{SILENCE} actually, here is the answer")
    assert not quiet and body.startswith("actually")
    body, quiet = clean_reply("Looking at the request first.\n\nHere it is.")
    assert (body, quiet) == ("Here it is.", False)
    # the OLD bare word never silences anyone anymore — it's just a word
    assert clean_reply("NO_REPLY") == ("NO_REPLY", False)


def test_split_reply_contract():
    # no marker: one message, untouched
    assert split_reply("just one message") == ["just one message"]
    # the marker alone on its own line splits, in order
    assert split_reply(f"one\n{MESSAGE_BREAK}\ntwo\n{MESSAGE_BREAK}\nthree") \
        == ["one", "two", "three"]
    # tolerant like the sentinel: case + stray decoration still count
    assert split_reply(f"a\n `{MESSAGE_BREAK.lower()}` \nb") == ["a", "b"]
    # DISCUSSING the marker inline never splits (the NO_REPLY lesson)
    body = f"the marker {MESSAGE_BREAK} splits messages"
    assert split_reply(body) == [body]
    # empty pieces drop: leading/trailing/doubled markers
    assert split_reply(f"{MESSAGE_BREAK}\nx\n{MESSAGE_BREAK}\n{MESSAGE_BREAK}") == ["x"]
    # overflow merges into the LAST message — nothing is ever lost
    five = f"\n{MESSAGE_BREAK}\n".join(["m1", "m2", "m3", "m4", "m5"])
    parts = split_reply(five)
    assert len(parts) == 4
    assert parts[:3] == ["m1", "m2", "m3"] and "m4" in parts[3] and "m5" in parts[3]


def test_feed_first_steps_bypass_the_throttle():
    """The pane opens on the init write; the first steps arrive inside the
    throttle window and MUST still land in the doc (live @claude feedback:
    the feed used to jump straight to mid-run)."""
    from agentbridge.harness.feed import RunFeed

    writes: list[dict] = []
    tx = SimpleNamespace(put_doc=lambda path, doc: writes.append(dict(doc)))
    feed = RunFeed(tx, "helper", "c1")          # forced init write
    for i in range(5):
        feed.step(f"step {i}")                  # all within the throttle
    activities = [w["runs"][0]["activity"] for w in writes]
    assert activities[:4] == ["Starting up…", "step 0", "step 1", "step 2"]
    assert len(writes) == 4                     # step 3/4 throttled as before
    feed.finish("done", "finished")


def test_parallel_run_feeds_finish_independently():
    """V91: one agent's concurrent runs share a bounded document, but each
    stable run id owns its entry and finishing one leaves the other visible."""
    from agentbridge.harness.feed import RunFeed

    docs: dict[str, dict] = {}
    tx = SimpleNamespace(
        get_doc=lambda path, default=None: docs.get(path, default),
        put_doc=lambda path, doc: docs.__setitem__(path, dict(doc)),
    )
    first = RunFeed(tx, "parallel", "c1")
    second = RunFeed(tx, "parallel", "c2")
    live = docs["status/parallel_live.json"]["runs"]
    assert {r["run_id"] for r in live} == {first.run_id, second.run_id}

    first.finish("done", "first complete")
    live = docs["status/parallel_live.json"]["runs"]
    assert [r["run_id"] for r in live] == [second.run_id]
    second.finish("done", "second complete")
    assert docs["status/parallel_live.json"]["runs"] == []
    assert [r["note"] for r in docs["status/parallel_runs.json"]["runs"]] \
        == ["first complete", "second complete"]


def test_run_feed_heartbeats_while_model_is_quiet(monkeypatch):
    """A long blocking model call stays fresh even when it emits no steps."""
    from agentbridge.harness import feed as feed_mod

    writes = []
    tx = SimpleNamespace(
        get_doc=lambda path, default=None: default,
        put_doc=lambda path, doc: writes.append((path, dict(doc))),
    )
    monkeypatch.setattr(feed_mod, "_HEARTBEAT_S", 0.01)
    feed = feed_mod.RunFeed(tx, "heartbeat", "c1")
    deadline = time.monotonic() + 0.5
    while len(writes) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(writes) >= 2
    feed.finish("done", "complete")
    feed._heartbeat.join(timeout=0.5)
    assert not feed._heartbeat.is_alive()


def test_reap_orphan_run():
    """V129: a run doc left "running" by a killed process is finished as
    interrupted (live screenshot: a working bubble haunted the chat for 10
    minutes while the relaunched harness read as online — V109's process
    truth checks the RUNNER, not the RUN). An active run's doc and a V71
    waiting doc are spared; the run history records the interruption."""
    from agentbridge.harness.feed import reap_orphan_run

    docs: dict[str, dict] = {}
    tx = SimpleNamespace(
        get_doc=lambda path, default=None: docs.get(path, default),
        put_doc=lambda path, doc: docs.__setitem__(path, dict(doc)),
    )
    assert reap_orphan_run(tx, "helper") is False       # nothing to reap
    docs["status/helper_run.json"] = {
        "state": "running", "agent": "helper", "chat_id": "c1",
        "started": "2026-07-16T10:00:00Z", "updated": "2026-07-16T10:00:05Z",
        "turns": 3, "activity": "Reading the conversation",
    }
    assert reap_orphan_run(tx, "helper") is True        # orphan: reaped
    assert docs["status/helper_run.json"]["state"] == "interrupted"
    hist = docs["status/helper_runs.json"]["runs"]
    assert hist[-1]["state"] == "interrupted" and hist[-1]["chat_id"] == "c1"
    assert reap_orphan_run(tx, "helper") is False       # idempotent
    # an ACTIVE run in this process is never reaped
    docs["status/helper_run.json"] = {
        "state": "running", "chat_id": "c2", "updated": "x"}
    assert reap_orphan_run(tx, "helper", {"c2"}) is False
    assert docs["status/helper_run.json"]["state"] == "running"
    # a V71 waiting doc is spared (the durable queue owns its lifecycle)
    docs["status/helper_run.json"] = {
        "state": "running", "chat_id": "c3", "waiting": True, "updated": "x"}
    assert reap_orphan_run(tx, "helper") is False
    # V107: the orphan's last activity rides the history entry as "doing"
    docs["status/helper_run.json"] = {
        "state": "running", "chat_id": "c4", "updated": "x",
        "activity": "Running a command"}
    assert reap_orphan_run(tx, "helper") is True
    hist = docs["status/helper_runs.json"]["runs"]
    assert hist[-1]["doing"] == "Running a command"


# ------------------------------------------- self-awareness (V87/V107, R-A)

def test_history_records_what_a_stopped_run_was_doing():
    """V107: the stop note REPLACES the activity line — the history keeps
    what the run was doing so the agent's next-run context can say it. A
    claim-time stop (nothing ran) and a normal finish record no 'doing'."""
    from agentbridge.harness.feed import RunFeed

    docs: dict[str, dict] = {}
    tx = SimpleNamespace(
        get_doc=lambda path, default=None: docs.get(path, default),
        put_doc=lambda path, doc: docs.__setitem__(path, dict(doc)),
    )
    feed = RunFeed(tx, "helper", "c1")
    feed.step("Reading the conversation")
    feed.step("Searching for invoices")
    feed.finish("stopped", "Stopped by your member")
    runs = docs["status/helper_runs.json"]["runs"]
    assert runs[-1]["state"] == "stopped"
    assert runs[-1]["doing"] == "Searching for invoices"

    feed = RunFeed(tx, "helper", "c1")     # claim-time stop: zero steps
    feed.finish("stopped", "Stopped by your member")
    runs = docs["status/helper_runs.json"]["runs"]
    assert "doing" not in runs[-1]

    feed = RunFeed(tx, "helper", "c1")     # a posted reply needs no 'doing'
    feed.step("Writing the reply")
    feed.finish("done", "Reply posted · 3s")
    runs = docs["status/helper_runs.json"]["runs"]
    assert runs[-1]["state"] == "done" and "doing" not in runs[-1]


def test_stop_surfaces_into_the_next_runs_context(hrig):
    """V107 end-to-end: a run stopped by the owner leaves a history entry,
    and the NEXT run's delivery + rendered context carry the stop — the
    agent finally KNOWS it was stopped instead of blindly re-attempting."""
    from agentbridge.harness.prompt import PromptManager
    from agentbridge.harness.responder import RunStopped

    snap = hrig.owner.create_chat("Ops", members=["helper"])
    hrig.owner.post(snap.id, "hey @helper, do the big thing")

    def stopped(d):
        raise RunStopped()

    class StoppedMidStep(Scripted):
        def respond(self, delivery, on_step=None):
            if on_step:
                on_step("Scanning the files")   # the run got somewhere first
            return super().respond(delivery)

    runner = hrig.make_runner(StoppedMidStep(stopped))
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    assert agent_msgs(hrig.owner, snap.id) == []          # nothing posted
    hist = runner.mesh.tx.get_doc("status/helper_runs.json")["runs"]
    assert hist[-1]["state"] == "stopped"
    assert hist[-1]["doing"] == "Scanning the files"

    # the next trigger's delivery knows, and the context says it up top
    responder = Scripted()
    runner.responder = responder
    hrig.owner.post(snap.id, "@helper still there?")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    d = responder.calls[-1]
    assert d.recent_runs and d.recent_runs[-1]["state"] == "stopped"
    ctx = PromptManager(hrig.home).for_agent(None).context_text(d)
    assert "STOPPED by your responsible member" in ctx
    assert "(while: Scanning the files)" in ctx
    # the "still there?" run POSTED, so the next delivery's newest entry is
    # done — the attention line retires the moment a run completes normally
    hrig.owner.post(snap.id, "@helper one more")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    d = responder.calls[-1]
    assert d.recent_runs[-1]["state"] == "done"
    assert "STOPPED by your responsible member" not in \
        PromptManager(hrig.home).for_agent(None).context_text(d)


def test_reaction_predicate_targets_only_my_messages():
    """V92: a reaction breadcrumb triggers ONLY the reacted message's author
    (never the reactor, never bystanders), bypassing the reply rule like a
    reply does."""
    from agentbridge.core.models import MsgKind
    from agentbridge.harness.triggers import should_reply

    def crumb(frm, to):
        return SimpleNamespace(
            from_=frm, kind=MsgKind.INFO, deleted=False, tags=[],
            reply_to=None, event={"type": "reaction", "msg_id": "m-1",
                                  "emoji": "👍", "to": to})

    kinds = {}
    assert should_reply("tagged", crumb("aryan", "helper"),
                        "helper", kinds) == "reaction"
    assert should_reply("tagged", crumb("aryan", "aryan"),
                        "helper", kinds) is None      # not my message
    assert should_reply("all", crumb("helper", "helper"),
                        "helper", kinds) is None      # my own reaction
    other = SimpleNamespace(from_="aryan", kind=MsgKind.INFO, deleted=False,
                            tags=[], reply_to=None, event={"type": "renamed"})
    assert should_reply("all", other, "helper", kinds) is None


def test_reaction_nudges_the_agent_and_silence_is_normal(hrig):
    """V92 end-to-end: a member reacting to the agent's message raises ONE
    run (reason 'reaction'), whose context names the emoji + the reacted
    message; a silent outcome reads 'Noticed the reaction' in the feed; the
    ledger never re-fires it. A substantive follow-up posts STANDALONE —
    never quoting the breadcrumb info event."""
    from agentbridge.harness.prompt import PromptManager

    snap = hrig.owner.create_chat("Rx", members=["helper"])
    hrig.owner.post(snap.id, "hey @helper, what's 2+2?")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    reply = agent_msgs(hrig.owner, snap.id)[-1]

    hrig.owner.react(snap.id, reply.id, "👍")
    ripple(hrig, runner, snap.id)
    responder.fn = lambda d: Reply(body=SILENCE)      # the normal outcome
    turn(hrig, runner, snap.id)
    d = responder.calls[-1]
    assert [t.reason for t in d.triggers] == ["reaction"]
    assert d.triggers[0].sender == "aryan"
    ctx = PromptManager(hrig.home).for_agent(None).context_text(d)
    assert 'reacted 👍 to your message "answering @aryan"' in ctx
    pack = PromptManager(hrig.home).for_agent(None)
    prompt = pack.prompt(d, None, context_file="ctx.md", outbox="out")
    assert "FYI-grade nudge" in prompt                # task_reaction block
    feed = latest_run(runner.mesh.tx)
    assert feed["state"] == "done"
    assert feed["note"] == "Noticed the reaction — no reply needed"
    assert [m.id for m in agent_msgs(hrig.owner, snap.id)] == [reply.id]

    calls = len(responder.calls)
    turn(hrig, runner, snap.id)                       # ledger: never re-fires
    assert len(responder.calls) == calls

    # a reaction the agent DOES answer posts standalone (no quote of the
    # breadcrumb — an info event renders empty)
    hrig.owner.react(snap.id, reply.id, "🎉")
    ripple(hrig, runner, snap.id)
    responder.fn = lambda d: Reply(body="glad that helped")
    turn(hrig, runner, snap.id)
    follow = agent_msgs(hrig.owner, snap.id)[-1]
    assert follow.body == "glad that helped"
    assert not follow.reply_to


def test_delivery_lists_this_chats_timers_only(hrig):
    """V87: the run's context lists THIS chat's pending wake-ups (with the
    ids cancel_timer takes); other chats contribute only a count."""
    from agentbridge.harness.prompt import PromptManager

    snap = hrig.owner.create_chat("Here", members=["helper"])
    other = hrig.owner.create_chat("There", members=["helper"])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    at_ns = time.time_ns() + int(3600 * 1e9)
    tid = runner.timers.set(snap.id, at_ns, "follow up on the invoices")
    runner.timers.set(other.id, at_ns, "somewhere else entirely")

    hrig.owner.post(snap.id, "hey @helper")
    ripple(hrig, runner, snap.id)
    turn(hrig, runner, snap.id)
    d = responder.calls[-1]
    assert [t["id"] for t in d.timers] == [tid]
    assert d.other_timers == 1
    ctx = PromptManager(hrig.home).for_agent(None).context_text(d)
    assert f"(id {tid})" in ctx and "follow up on the invoices" in ctx
    assert "somewhere else entirely" not in ctx           # never leaks over
    assert "1 wake-up(s) scheduled in other chats" in ctx


def test_repeat_parsing_and_occurrence_math():
    """V88 part 2: recurrence specs and the schedule-anchored occurrence
    math (catch-up past a long offline stretch; month-length clamp)."""
    import datetime as dt
    import time as _t

    from agentbridge.harness.timers import (next_occurrence, parse_repeat,
                                            repeat_label)

    assert parse_repeat("daily") == {"kind": "daily"}
    assert parse_repeat("weekly:mon,wed") == {"kind": "weekly", "days": [0, 2]}
    assert parse_repeat("weekly:0,2") == {"kind": "weekly", "days": [0, 2]}
    assert parse_repeat("monthly:15") == {"kind": "monthly", "day": 15}
    assert parse_repeat("weekly:") is None
    assert parse_repeat("bogus") is None and parse_repeat("") is None
    assert repeat_label({"kind": "daily"}) == "repeats daily"
    assert "Mon, Wed" in repeat_label({"kind": "weekly", "days": [0, 2]})
    # catch-up: a 3-days-stale anchor advances past NOW in one call
    now = _t.time_ns()
    nxt = next_occurrence(now - int(3 * 86400e9), {"kind": "daily"},
                          now_ns=now)
    assert nxt and now < nxt <= now + int(86400e9)
    # monthly clamp: a day-31 anchor lands on Feb's last day, no crash
    jan31 = int(dt.datetime(2026, 1, 31, 9, 0).timestamp() * 1e9)
    nxt = next_occurrence(jan31, {"kind": "monthly", "day": 31},
                          now_ns=jan31 + 1)
    d = dt.datetime.fromtimestamp(nxt / 1e9)
    assert (d.month, d.day) == (2, 28) and d.hour == 9


def test_recurring_timer_rearms_on_fire_ends_on_dismiss(hrig):
    """V88 part 2: the FIRE-side pop re-arms a repeating wake-up at its next
    occurrence under the SAME id (the owner's chip stays one thing); the
    dismiss/cancel pop ends the series."""
    import time as _t

    runner = hrig.make_runner(Scripted())
    snap = hrig.owner.create_chat("Recur", members=["helper"])
    tid = runner.timers.set(snap.id, _t.time_ns() - int(60e9),
                            "daily standup ping", repeat="daily")
    t = runner.timers.pop(tid, reschedule=True)      # the fire-path pop
    assert t and t["repeat"] == {"kind": "daily"}
    left = runner.timers.snapshot()
    assert len(left) == 1 and left[0]["id"] == tid   # same id, re-armed
    assert left[0]["at_ns"] > _t.time_ns()           # strictly in the future
    runner.timers.pop(tid)                           # dismissal: series ends
    assert runner.timers.snapshot() == []


def test_owner_timer_dismiss_notifies_the_agent(hrig):
    """V88: the owner's dismissal (GUI ✕ → cancel doc) pops the timer AND
    lands a "dismissed" entry in the run history — the same tail R99 feeds
    into every delivery, so the agent's next run knows its wake-up was
    dismissed instead of silently losing it."""
    runner = hrig.make_runner(Scripted())
    snap = hrig.owner.create_chat("Sched", members=["helper"])
    ripple(hrig, runner, snap.id)
    tid = runner.timers.set(snap.id, 2**62, "check the export at 3pm")
    assert tid and len(runner.timers.snapshot()) == 1
    # the GUI endpoint's doc, planted by the owner
    hrig.owner.tx.put_doc("status/helper_timer_cancel.json",
                          {"ids": [tid], "by": "aryan", "ns": 1})
    hrig.owner.outbox.flush_once()
    runner._consume_timer_cancels()
    assert runner.timers.snapshot() == []            # popped
    assert runner.mesh.tx.get_doc(
        "status/helper_timer_cancel.json") is None   # consumed once
    hist = runner.mesh.tx.get_doc("status/helper_runs.json")
    last = (hist or {}).get("runs", [])[-1]
    assert last["state"] == "dismissed" and last["chat_id"] == snap.id
    assert "@aryan" in last["note"] and "check the export" in last["note"]
    # and the R99 delivery plumbing carries it into the next run's context
    recent = runner.conversation._recent_runs(snap.id)
    assert any(r.get("state") == "dismissed" for r in recent)


def test_settings_parse_and_clamp():
    s = HarnessSettings.from_account(None)
    assert (s.default_rule, s.concurrency, s.catchup) == ("tagged", 2, "recent")
    assert s.rule_for("any") == "tagged"
    assert s.rule_for("any", dm=True) == "all"         # a DM answers everyone
    acc = SimpleNamespace(agent=SimpleNamespace(harness={
        "default_rule": "ALL", "concurrency": 99, "catchup": "bogus",
        "rules": {"c1": "humans", "c2": "bogus"},
        "models": {"c1": "m-fast", "c2": ""},
    }))
    s = HarnessSettings.from_account(acc)
    assert s.default_rule == "all"
    assert s.concurrency == 4                          # clamped to the ceiling
    assert s.catchup == "recent"                       # unknown fails closed
    assert s.rule_for("c1") == "humans"
    assert s.rule_for("c1", dm=True) == "humans"       # explicit beats the DM default
    assert s.rule_for("c2") == "all"                   # bad per-chat -> default
    assert s.models == {"c1": "m-fast"}                # blank picks are dropped


def test_model_precedence_most_specific_wins():
    acc = SimpleNamespace(agent=SimpleNamespace(harness={
        "model": "global", "models": {"c1": "chat-pick"},
        "routing": {"humans": {"model": "route-pick"}},
    }))
    s = HarnessSettings.from_account(acc)
    assert s.model_for("humans", "c1") == "chat-pick"  # the chat's own model
    assert s.model_for("humans", "c2") == "global"     # then the override-all
    acc.agent.harness.pop("model")
    s = HarnessSettings.from_account(acc)
    assert s.model_for("humans", "c2") == "route-pick"  # then the audience


# ------------------------------------------------- R55: the V35/V36 bug bash

def test_cannot_post_group_resolves_without_model_run(hrig):
    """V35 (live loop): a group's send_messages flipped to admins-only while
    the agent stayed a plain member — every mention then ran the model and
    died at post, retrying forever. Claim-time can_send now resolves the
    trigger through the ledger without burning a run."""
    snap = hrig.owner.create_chat("Locked", members=["helper"])
    hrig.owner.membership.set_permissions(snap.id, {"send_messages": "admins"})
    trigger = hrig.owner.post(snap.id, "hey @helper, anyone home?")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    for _ in range(3):
        turn(hrig, runner, snap.id)

    assert responder.calls == []                      # no model run burnt
    assert agent_msgs(hrig.owner, snap.id) == []
    assert runner.queue.answered(snap.id, trigger.id)  # never re-fires
    assert runner.queue._pending() == {}
    feed = latest_run(runner.mesh.tx)
    assert feed["state"] == "done" and "restricted" in feed["note"]


def test_post_failure_is_terminal_not_a_loop(hrig):
    """V35 defense-in-depth: a post that fails mid-run (permissions flipped
    between claim and deliver) resolves via _run_failed — ledger written,
    exactly one model run, no silent 20s retry loop."""
    snap = hrig.owner.create_chat("FlipMidRun", members=["helper"])
    hrig.owner.membership.set_permissions(snap.id, {"send_messages": "admins"})
    trigger = hrig.owner.post(snap.id, "hey @helper, race me")
    responder = Scripted()
    runner = hrig.make_runner(responder)
    runner._can_post = lambda chat_id: True   # simulate the mid-run flip
    ripple(hrig, runner, snap.id)

    for _ in range(3):
        turn(hrig, runner, snap.id)

    assert len(responder.calls) == 1                  # ran once, not forever
    assert agent_msgs(hrig.owner, snap.id) == []      # nothing ever posted
    assert runner.queue.answered(snap.id, trigger.id)
    assert runner.queue._pending() == {}


def test_repeated_pre_model_failure_gives_up(hrig):
    """V35: an exception before the model (context build) retries on a
    bounded budget, refunds its rate slot each lap, then resolves as an
    error instead of looping forever."""
    snap = hrig.owner.create_chat("Poisoned", members=["helper"])
    trigger = hrig.owner.post(snap.id, "hey @helper, choke on this")
    responder = Scripted()
    runner = hrig.make_runner(responder)

    def boom(*a, **k):
        raise RuntimeError("poisoned context")

    runner.conversation.build = boom
    ripple(hrig, runner, snap.id)

    for _ in range(4):                       # 3 failures = the full budget
        turn(hrig, runner, snap.id)
        time.sleep(0.85)                     # let the retry backoff expire

    assert responder.calls == []
    assert runner.queue._pending() == {}
    led = runner.queue._ledger(snap.id)
    assert led.get(f"{trigger.id}@0") == "error:gave-up"
    rate = runner.queue.store.cached_doc("harness/rate", default={}) or {}
    assert not rate.get(snap.id)             # every slot was refunded


def test_attachment_sync_barrier_defers_until_blob_lands(hrig):
    """V36: the message line can sync ahead of its attachment blob. The run
    defers (slot-free) until the blob is fetchable, then answers — the CLI
    never sees a transcript advertising a file that isn't on disk."""
    snap = hrig.owner.create_chat("Files", members=["helper"])
    rec = {"id": "fx1.bin", "name": "report.bin", "bytes": 5}
    hrig.owner.post(snap.id, "hey @helper, read the file", files=[rec])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    turn(hrig, runner, snap.id)
    assert responder.calls == []                      # deferred, not run
    assert runner.queue._pending()                    # still queued
    # V71: the wait is VISIBLE — a running feed with a "waiting" activity so
    # the requester sees the agent is waiting on the file, not frozen
    feed = active_runs(runner.mesh.tx)[0]
    assert feed.get("state") == "running" and feed.get("waiting")
    assert "syncing" in feed.get("activity", "") and "report.bin" in feed["activity"]

    sealed = hrig.owner.sealer.seal_blob(snap.id, "fx1.bin", b"hello")
    hrig.owner.tx.put_blob(f"chats/{snap.id}/files/fx1.bin", sealed)
    time.sleep(0.65)                                  # the defer backoff
    turn(hrig, runner, snap.id)

    assert len(responder.calls) == 1
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


def test_attachment_barrier_grace_expires(hrig, monkeypatch):
    """V36: a blob that never syncs must not wedge the chat — past the grace
    window the run proceeds with the bare filename (v1 semantics)."""
    import agentbridge.harness.runner as runner_mod

    snap = hrig.owner.create_chat("LostBlob", members=["helper"])
    rec = {"id": "fx2.bin", "name": "gone.bin", "bytes": 5}
    hrig.owner.post(snap.id, "hey @helper, the file is lost", files=[rec])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    monkeypatch.setattr(runner_mod, "BLOB_GRACE_S", 0.0)
    turn(hrig, runner, snap.id)

    assert len(responder.calls) == 1                  # ran despite the blob
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


def test_claim_time_stop_doc_consumed(hrig):
    """V35 ('won't even stop'): a Stop pressed while nothing was running
    used to evaporate — the in-run poller was its only consumer. A fresh
    stop doc now resolves the next claimed group as stopped-by-owner."""
    snap = hrig.owner.create_chat("StopMe", members=["helper"])
    trigger = hrig.owner.post(snap.id, "hey @helper, don't answer")
    hrig.owner.tx.put_doc("status/helper_stop.json", {
        "ns": time.time_ns(), "by": "aryan", "chat_id": ""})
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    turn(hrig, runner, snap.id)

    assert responder.calls == []
    assert agent_msgs(hrig.owner, snap.id) == []
    assert runner.queue.answered(snap.id, trigger.id)
    assert runner.mesh.tx.get_doc("status/helper_stop.json") is None
    runs = runner.mesh.tx.get_doc("status/helper_runs.json")
    assert runs["runs"][-1]["state"] == "stopped"


def test_deleted_agent_runner_stands_down(hrig):
    """R56 (V49): a soft-deleted agent gets no supervisor (hosted_agents
    skips it) and a RUNNING runner exits cleanly on its next tick instead
    of idling forever."""
    from agentbridge.harness.runner import hosted_agents

    assert hosted_agents(hrig.root, "devbox") == ["helper"]
    hrig.owner.delete_agent("helper")
    assert hosted_agents(hrig.root, "devbox") == []

    runner = hrig.make_runner(Scripted())
    runner.mesh.sync.sync_once()
    with pytest.raises(SystemExit) as e:
        runner.tick()
    assert e.value.code == 0


def test_chat_stand_down_holds_and_resumes(hrig):
    """V62: any member's per-chat control doc holds THIS chat's triggers +
    timers (cursor keeps its place); other chats keep answering; lifting the
    pause answers the held backlog."""
    snap = hrig.owner.create_chat("Held", members=["helper"])
    other = hrig.owner.create_chat("Live", members=["helper"])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    ripple(hrig, runner, other.id)

    # the pause doc lands (what /api/mesh/chat_pause writes)
    hrig.owner.tx.put_doc(f"chats/{snap.id}/control.json",
                          {"paused": True, "by": "aryan"})
    hrig.owner.post(snap.id, "hey @helper, held ask")
    hrig.owner.post(other.id, "hey @helper, live ask")
    ripple(hrig, runner, snap.id)
    ripple(hrig, runner, other.id)

    turn(hrig, runner, snap.id)
    turn(hrig, runner, other.id)
    assert agent_msgs(hrig.owner, snap.id) == []      # held chat: silence
    assert len(agent_msgs(hrig.owner, other.id)) == 1  # other chat unaffected

    # resume: the cursor never moved, so the backlog answers now
    hrig.owner.tx.put_doc(f"chats/{snap.id}/control.json",
                          {"paused": False, "by": "aryan"})
    runner._chat_pause.clear()   # tests skip the 20s TTL wait
    turn(hrig, runner, snap.id)
    assert len(agent_msgs(hrig.owner, snap.id)) == 1


def test_chat_stand_down_gates_claimed_groups(hrig):
    """V62 claim-time leg: a group already queued BEFORE the pause waits
    slot-free instead of running."""
    snap = hrig.owner.create_chat("Race", members=["helper"])
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)
    hrig.owner.post(snap.id, "hey @helper, quick one")
    ripple(hrig, runner, snap.id)

    runner.mesh.sync.sync_once([snap.id])
    runner.scan_all()             # trigger is IN the queue now
    hrig.owner.tx.put_doc(f"chats/{snap.id}/control.json",
                          {"paused": True, "by": "aryan"})
    hrig.owner.outbox.flush_once()
    runner._chat_pause.clear()
    runner.dispatch_fill()
    runner.drain()
    assert responder.calls == [] and agent_msgs(hrig.owner, snap.id) == []
    # the item is still pending — released, not resolved
    assert runner.queue.snapshot()


def test_deferred_leave_posts_goodbye_first(hrig):
    """V53: an owner-approved leave_chat rides the Reply — the goodbye
    posts while the agent is still a member, THEN it leaves."""
    snap = hrig.owner.create_chat("Farewell", members=["helper"])
    hrig.owner.post(snap.id, "hey @helper, you can go")
    responder = Scripted(lambda d: Reply(body="thanks — signing off",
                                         leave_chat=True))
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    turn(hrig, runner, snap.id)

    replies = [m for m in agent_msgs(hrig.owner, snap.id)
               if m.kind.value == "message"]
    assert len(replies) == 1 and "signing off" in replies[0].body
    assert "helper" not in hrig.owner.snapshot(snap.id).members
    # the departure pill is in the log (fold-visible to the owner)
    assert any((m.event or {}).get("type") == "member_left"
               for m in hrig.owner.messages_for(snap.id))


def test_parse_at_local_shapes():
    """V55: the absolute-time shapes an agent (or human) reaches for."""
    import datetime as dt
    from agentbridge.harness.timers import parse_at

    # a fixed base: 2026-07-15 10:00 local
    base = dt.datetime(2026, 7, 15, 10, 0).timestamp()
    # HH:MM later today
    ns = parse_at("15:30", now_s=base)
    assert dt.datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M") \
        == "2026-07-15 15:30"
    # HH:MM already past -> tomorrow
    ns = parse_at("09:00", now_s=base)
    assert dt.datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M") \
        == "2026-07-16 09:00"
    # explicit local date-time
    ns = parse_at("2026-07-20 08:15", now_s=base)
    assert dt.datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M") \
        == "2026-07-20 08:15"
    # junk -> None, never a crash
    assert parse_at("noonish", now_s=base) is None
    assert parse_at("", now_s=base) is None
    assert parse_at("25:99", now_s=base) is None


def test_timer_brief_survives_to_the_wakeup_run(hrig):
    """V55: a long multi-line brief rides Reply.timers (at_ns) into the
    store uncut and reaches the wake-up run's delivery note."""
    snap = hrig.owner.create_chat("Reminders", members=["helper"])
    brief = "\n".join(["Remind @aryan about the quarterly export.",
                       "1. check the dashboard refreshed",
                       "2. if it failed, say WHO to ping",
                       "Done looks like: a one-line status in this chat."]
                      + [f"context line {i}" for i in range(20)])
    hrig.owner.post(snap.id, "hey @helper, remind me in a bit")
    fire_at = time.time_ns() + int(0.05 * 1e9)
    responder = Scripted(lambda d: Reply(
        body="will do" if d.kind == "message" else f"reminder: {d.note[:40]}",
        timers=[{"at_ns": fire_at, "note": brief}] if d.kind == "message"
        else []))
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    turn(hrig, runner, snap.id)              # the ask -> schedules the timer
    stored = runner.timers.snapshot()
    assert len(stored) == 1 and stored[0]["note"] == brief  # uncut (2000 cap)

    time.sleep(0.1)                          # the timer comes due
    turn(hrig, runner, snap.id)              # fires -> wake-up run
    deliveries = [d for d in responder.calls if d.kind == "timer"]
    assert len(deliveries) == 1 and deliveries[0].note == brief
    replies = [m for m in agent_msgs(hrig.owner, snap.id)
               if m.kind.value == "message"]
    assert any(m.body.startswith("reminder:") for m in replies)
    assert runner.timers.snapshot() == []    # consumed, not re-firing


# ----------------------------------------- R66: the undecryptable-scan barrier

def test_undecryptable_message_holds_the_scan_until_keys_arrive(hrig):
    """The V72 lost-@all bug: a message sealed with a key epoch this device
    hasn't synced yet reads as EMPTY (tags invisible) — the scan must hold
    its cursor and retry until the key doc lands, then answer normally,
    instead of consuming the trigger silently forever."""
    snap = hrig.owner.create_chat("Fresh", members=["helper"])
    trigger = hrig.owner.post(snap.id, "hey @helper, first message")
    # hide the chat's key docs — this device "hasn't synced them yet"
    key_docs = {p: hrig.owner.tx.get_doc(p)
                for p in hrig.owner.tx.list_docs(f"chats/{snap.id}/keys")}
    assert key_docs
    for p in key_docs:
        hrig.owner.tx.delete_doc(p)
    responder = Scripted()
    runner = hrig.make_runner(responder)
    ripple(hrig, runner, snap.id)

    # the sealed record arrived but cannot open here — flagged, not blank
    view = {m.id: m for m in runner.mesh.messages_for(snap.id)}
    assert view[trigger.id].undecrypted and view[trigger.id].body == ""
    assert runner.scan_all() == 0
    last_ns, _ = runner.queue.scan_cursor(snap.id)
    assert last_ns < trigger.ns                    # held, not consumed
    assert not runner.queue.answered(snap.id, trigger.id, 0)

    # the key doc lands (the mirror refresh, in production) -> answered
    for p, doc in key_docs.items():
        hrig.owner.tx.put_doc(p, doc)
    turn(hrig, runner, snap.id)
    replies = agent_msgs(hrig.owner, snap.id)
    assert len(replies) == 1
    assert (replies[0].reply_to or {}).get("id") == trigger.id


def test_undecryptable_past_deadline_is_skipped_honestly(hrig):
    """A truly dead envelope can never wedge its chat: past the hold
    deadline the scan records the skip in the ledger and moves on."""
    snap = hrig.owner.create_chat("Stuck", members=["helper"])
    trigger = hrig.owner.post(snap.id, "hey @helper, doomed")
    for p in hrig.owner.tx.list_docs(f"chats/{snap.id}/keys"):
        hrig.owner.tx.delete_doc(p)
    runner = hrig.make_runner(Scripted())
    ripple(hrig, runner, snap.id)

    runner.UNSEAL_HOLD_NS = 0                      # everything is instantly old
    assert runner.scan_all() == 0
    last_ns, _ = runner.queue.scan_cursor(snap.id)
    assert last_ns >= trigger.ns                   # moved on — never wedged
    assert runner.queue._ledger(snap.id).get(
        f"{trigger.id}@0") == "skipped:undecryptable"
