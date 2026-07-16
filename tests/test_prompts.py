"""The prompt manager (R17): pack layering, assembly, the silence rail,
context rendering, and the feed wording map."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentbridge.core.models import Message
from agentbridge.harness import (
    Delivery, MESSAGE_BREAK, PromptManager, SILENCE, TriggerContext,
)
from agentbridge.harness.adapters.cli import extract_step
from agentbridge.harness.prompt import render_message


def acc(display="Helper", about="", prompts=None):
    harness = {"prompts": prompts} if prompts else {}
    return SimpleNamespace(display=display, about=about,
                           agent=SimpleNamespace(harness=harness))


def delivery(**kw):
    d = dict(agent="helper", chat_id="c1", chat_name="Ops",
             chat_kind="group", kind="message", rule="tagged",
             roster=[{"name": "aryan", "desc": "member", "you": False},
                     {"name": "helper", "desc": "you", "you": True}],
             pins=[], transcript=[], triggers=[], note="")
    d.update(kw)
    return Delivery(**d)


def msg(**kw):
    base = dict(id="m1", from_="aryan", ts="2026-07-13 10:00",
                body="hello there")
    base.update(kw)
    return Message(**base)


# ------------------------------------------------------------- pack layering

def test_pack_loads_and_overlays(tmp_path):
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "default.json").write_text(json.dumps({
        "persona": "You are the house bot {display}.",
        "activity": {"grep": "Digging for {detail}"},
    }), encoding="utf-8")
    pm = PromptManager(home)
    pack = pm.for_agent(acc())
    # the overlay rewrote one key; shipped keys survive around it
    assert pack.text("persona", display="Helper") == "You are the house bot Helper."
    assert "OPTIONAL" in pack.text("silence", sentinel=SILENCE)
    # activity merged ONE LEVEL deep — the overlay's key joins the shipped map
    assert pack.step_line("tool", "Grep", "needle") == "Digging for needle"
    assert pack.step_line("tool", "Read", "a.txt") == "Reading a.txt"


def test_agent_overrides_win(tmp_path):
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc(prompts={"etiquette": "Be brief."}))
    assert pack.text("etiquette") == "Be brief."
    assert "OPTIONAL" in pack.text("silence", sentinel=SILENCE)


def test_broken_template_degrades_to_raw(tmp_path):
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc(prompts={"persona": "Bad {brace and {display}"}))
    out = pack.text("persona", display="x", agent="a", chat_name="c")
    assert out == "Bad {brace and {display}"   # never raises


# ---------------------------------------------------------------- the prompt

def test_prompt_assembly_blocks(tmp_path):
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc(about="Runs the deploys."))
    p = pack.prompt(delivery(), acc(about="Runs the deploys."),
                    context_file="C:/w/context.md", outbox="C:/w/outbox")
    assert "You are Helper (@helper)" in p
    assert "Runs the deploys." in p                  # persona_about included
    assert "@aryan (member)" in p                    # roster with behaviours
    assert "C:/w/context.md" in p and "C:/w/outbox" in p
    assert SILENCE in p                              # the real sentinel rides
    assert "NO_REPLY and nothing else" not in p      # the old bare word is gone
    assert "threaded reply" in p                     # reply-vs-tag etiquette
    assert MESSAGE_BREAK in p                        # V78: the real break marker
    assert "ONE message" in p                        # …with the restraint rail


def test_bridge_guidance_encourages_asking_not_refusing(tmp_path):
    """R68 (V80/V82): with the ask gate live, the agent is told to ATTEMPT
    or ASK for gated actions rather than refuse for its member, to relay
    that it asked when the requester isn't the owner, and to report the
    outcome."""
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc())
    p = pack.prompt(delivery(), acc(), context_file="c", outbox="o",
                    bridge=True)
    low = p.lower()
    assert "rather than refusing" in low                 # V82: don't preempt
    assert "asked your responsible member to approve" in low  # V81 relay
    assert "allowed to do or could not do" in low        # V80 outcome report
    # V83: don't infer the sandbox is open from a successful outside op
    assert "never conclude from a success" in low


def test_prompt_timer_task(tmp_path):
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc())
    p = pack.prompt(delivery(kind="timer", note="check the export"),
                    acc(), context_file="ctx.md", outbox="out")
    assert "wake-up" in p and "check the export" in p
    assert "ctx.md" in p                             # timers still get context


def test_silence_rail_survives_a_gutted_pack(tmp_path):
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc(prompts={"silence": ""}))
    p = pack.prompt(delivery(), acc(), context_file="c", outbox="o")
    assert SILENCE in p                              # fallback injected it


# ------------------------------------------------------------- context text

def test_context_text_renders_transcript(tmp_path):
    pm = PromptManager(tmp_path / "nohome")
    pack = pm.for_agent(acc())
    d = delivery(
        transcript=[
            msg(),
            msg(id="m2", from_="helper", body="hi",
                reply_to={"id": "m1", "from": "aryan", "body": "hello there"}),
        ],
        triggers=[TriggerContext(message=msg(), reason="tagged",
                                 sender="aryan")],
        pins=[{"id": "m1", "by": "aryan", "body": "pinned note"}],
    )
    text = pack.context_text(d, staged={"a.csv": "inbox/a.csv"})
    assert "Chat: Ops (group)" in text
    assert "@helper (you)" in text
    assert "Trigger (tagged): @aryan" in text
    # the pin carries its message id so the agent can unpin it (R33)
    assert "[PINNED by @aryan] (id m1) pinned note" in text
    assert '@helper (you): [replying to @aryan: "hello there"] hi' in text
    assert "(id m1)" in text and "(id m2)" in text   # tool-actable ids (R19)
    assert "- a.csv -> read it at inbox/a.csv" in text


def test_render_message_variants():
    deleted = msg(deleted=True)
    assert "a message was deleted" in render_message(deleted, "helper")
    filed = msg(files=[{"name": "r.pdf"}], edited={"at": "x"})
    line = render_message(filed, "helper")
    assert "[files: r.pdf]" in line and "(edited)" in line


def test_context_time_grounding(tmp_path):
    """V117: every context carries the current time; the staleness caution
    appears only when the newest message is genuinely old."""
    import time as _t

    pack = PromptManager(tmp_path / "nohome").for_agent(acc())
    now_ns = _t.time_ns()

    fresh = pack.context_text(delivery(transcript=[msg(ns=now_ns)]))
    assert "Current time: " in fresh
    assert "you may have been offline" not in fresh

    old = pack.context_text(delivery(
        transcript=[msg(ns=now_ns - int(5 * 3.6e12))]))
    assert "last moved about 5 hours ago" in old
    assert "you may have been offline" in old

    older = pack.context_text(delivery(
        transcript=[msg(ns=now_ns - int(72 * 3.6e12))]))
    assert "last moved about 3 days ago" in older

    barely = pack.context_text(delivery(
        transcript=[msg(ns=now_ns - int(1.2 * 3.6e12))]))
    assert "last moved about an hour ago" in barely

    # empty transcript (fresh chat / pure wakeup): time yes, caution no
    empty = pack.context_text(delivery())
    assert "Current time: " in empty
    assert "you may have been offline" not in empty


# ------------------------------------------------------------- feed wording

def test_step_lines_are_clean_wording(tmp_path):
    pack = PromptManager(tmp_path / "nohome").for_agent(acc())
    assert pack.step_line("init") == "Getting ready"
    assert pack.step_line("result") == "Writing the reply"
    assert pack.step_line("tool", "Read", "C:/deep/path/notes.md") \
        == "Reading notes.md"                        # paths shrink to basename
    assert pack.step_line("tool", "Bash", "rm -rf x") == "Running a command"
    # unmapped tools humanize instead of leaking raw ids (R36)
    assert pack.step_line("tool", "Frobnicate", "") == "Using frobnicate"
    assert pack.step_line("tool", "mcp__github__search_issues", "") \
        == "Using search issues (github)"
    # the run's own plumbing file reads as the conversation, not a filename
    assert pack.step_line("tool", "Read", "C:/ws/context.md") \
        == "Reading the conversation"
    assert pack.step_line("text", "", "Let me look") == "Let me look"
    # the sentinel never leaks into the owner-visible feed
    assert pack.step_line("text", "", SILENCE) is None
    assert pack.step_line("text", "", f"okay: {SILENCE.lower()}") is None


def test_extract_step_facts():
    ev = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Grep", "input": {"query": "needle"}}]}}
    assert extract_step(ev, "claude-stream") == ("tool", "Grep", "needle")
    # a LONG path survives extraction intact — step_line basenames it later
    # (live bug: a 90-char cap here made "Reading f164" out of a deep path)
    deep = "C:/very/" + "deep/" * 30 + "context.md"
    ev = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": deep}}]}}
    kind, name, detail = extract_step(ev, "claude-stream")
    assert detail.endswith("context.md")
    assert extract_step({"type": "system", "subtype": "init"},
                        "claude-stream") == ("init", "", "")
    assert extract_step({"type": "result"}, "claude-stream") == ("result", "", "")
    done = {"type": "item.completed",
            "item": {"type": "command_execution", "command": "ls"}}
    assert extract_step(done, "codex-jsonl") == ("tool", "command_execution", "ls")
    final = {"type": "item.completed",
             "item": {"type": "agent_message", "text": "done"}}
    assert extract_step(final, "codex-jsonl") == ("result", "", "")
