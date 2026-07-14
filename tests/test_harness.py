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
    AgentRunner, HarnessSettings, Reply, SILENCE, clean_reply,
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
    feed = runner.mesh.tx.get_doc("status/helper_run.json")
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
    feed = runner.mesh.tx.get_doc("status/helper_run.json")
    assert "total" in feed["activity"] and "model" in feed["activity"]
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
    feed = runner.mesh.tx.get_doc("status/helper_run.json")
    assert feed["state"] == "error"


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
    activities = [w["activity"] for w in writes]
    assert activities[:4] == ["Starting up…", "step 0", "step 1", "step 2"]
    assert len(writes) == 4                     # step 3/4 throttled as before


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
    assert s.concurrency == 8                          # clamped to the ceiling
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
    feed = runner.mesh.tx.get_doc("status/helper_run.json")
    assert feed["state"] == "done" and "restricted" in feed["activity"]


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
