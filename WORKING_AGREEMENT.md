# Working agreement — read this FIRST, every round

This is the standing contract between me (Claude) and Sanskar for AgentBridge
work. Re-read it at the start of every round before touching anything. It is
deliberately terse and imperative — it is a checklist for me, not prose for a
reader.

## The seven rules

1. **Document extensively.** The conversation gets compacted often; details
   vanish. Write down every non-obvious decision, rationale, and state change —
   into memory (persists across compaction) and into the relevant repo doc
   (`HANDOFF.md`, `ARCHITECTURE.md`, memory notes). Assume future-me remembers
   nothing.

2. **Think → critique → correct → THEN build. Never jump to implementation.**
   For every task: (a) reconstruct the full context, (b) design one detailed
   approach, (c) critique my own design as an adversary, (d) revise. This is an
   IM app that lives in WhatsApp/Telegram's shadow — those patterns were refined
   over years. Ask "what is the most intuitive thing for a general end user?"
   before "what is easiest to code?" UI is the current focus; treat UX as a
   first-class constraint, not a finish.
   **Presenting the plan does NOT end the round.** I present the plan inline and
   explicitly ask Sanskar "does this plan work?" — then WAIT for the answer and
   continue in the same round. Never stop/hand off just because a plan is ready.

3. **We are partners; debate is expected.** Sanskar will be wrong sometimes; so
   will I. Push back with reasoning, offer alternatives, disagree constructively.
   Do not rubber-stamp. A good objection now beats a rewrite later.

4. **Verify properly, THEN commit — every round, in the same round.** We commit
   *every* round (always), but only *after* thorough live validation — verify
   first, commit second, both inside the round. Do NOT wait to be asked; do NOT
   defer the commit to a later turn. Live browser verification with
   wait-for-element polling (never fixed sleeps). `python check_frontend.py`
   after every frontend edit. Restart GUI server + worker after
   `server.py`/`mesh.py` edits. Only commit once I've *seen* it work. State
   failures honestly.

5. **Decompose big asks myself.** If Sanskar hands me a large task in one prompt,
   that's a signal the workplan wasn't fully thought through — not a license to
   sprint. Break it down, and spend the freed-up budget on rule 4 (thorough
   review of whatever I built this round).

## Standing operating conventions (from HANDOFF.md — the ground truth)

- **Per-round loop:** re-read this file → reconstruct context → design+critique →
  implement → verify live → bump `gui/__init__.py` `__version__` → commit + push
  → reply in the test room → update memory.
- **Frontend:** 19 native ES modules under `gui/static/js/`, strict one-way
  layering, page views register on the `V` registry and never import each other.
  Run `check_frontend.py` after every edit.
- **Restart discipline:** after `server.py`/`mesh.py` edits, restart BOTH the GUI
  server and the agent worker or they serve stale behaviour.
- **Encoding trap:** never round-trip source through PowerShell
  `Get-Content`/`Set-Content` (UTF-16+BOM mangles em-dashes). Use Python for
  version bumps.
- **Testing:** dedicated test human + QA room on the live mesh (creds in memory).
  Sanskar tests live mid-round — expect concurrent writes; `meta.json` is
  last-writer-wins. Run deterministic asserts in throwaway scratch rooms.
- **Safety rails never dropped:** agent tool blocklist + read-only flags stay in
  all fallback paths; unattended agents never get blanket auto-approve.
  INVARIANT: no agent ever in a room without a responsible human.

## Housekeeping cadence

- **Every 5 rounds:** remind each other to clean up working memory — prune it to
  the task at hand so focus stays sharp. (Round counter lives in memory.)
- **Authoritative backlog:** the memory reminder list, not this file. Read it
  each round; `HANDOFF.md` "Next work queue" is the human-readable summary.
