# Working agreement — read this FIRST, every round

This is the standing contract between me (Claude, the senior dev on this) and
Aryan for AgentBridge work. Re-read it at the start of every round before
touching anything. It is deliberately terse and imperative — a checklist for me,
not prose for a reader.

## This session's mission (the backend rewrite)

We are **rewriting the entire backend from scratch** — mesh, agent worker,
connectors — using the current codebase as **reference only**. The goals, in the
language I'll hold myself to:

- **Performance:** harness/prompt engineering, vector memory (qdrant), knowledge
  graphs (mem0 / graphiti), context summarization, LlamaIndex search, real-time
  messaging + local cache, parallelized agent harness.
- **Reliability:** retry mechanisms, full read-receipt implementation.
- **Safety:** context differentiation (DMs vs groups vs upcoming channels), new
  user-privacy + group-permission model, a Codex/Claude-Code-style per-agent
  permission system (the user is asked to approve an agent's actions).
- **Usefulness:** status + agent descriptions, OpenAI Agents SDK / MCP
  compatibility for mesh-cli, VS-Code / Claude-Code-style plugin support for file
  previews and rich context.

**Generalize away from Claude+Cortex.** The old app baked in assumptions from
building around Claude and CoCo. Scrub them. The target is *every LLM on the
planet*: ship **preconfigs** for Codex, Claude, Grok, Ollama, DeepSeek (any that
expose a harness) — but **hardcode nothing**. The code must be general; a model
is data/config, never a branch in the logic.

This rewrite is also prep for the upcoming **setup & packaging** round, where we
ship AgentBridge as a real product. So organize now; don't leave a mess to
untangle later.

## The seven rules

1. **Document extensively.** The conversation gets compacted often; details
   vanish. Write down every non-obvious decision, rationale, and state change —
   into memory (persists across compaction) and into the relevant repo doc
   (`HANDOFF.md`, `ARCHITECTURE.md`, memory notes). Assume future-me remembers
   nothing.

2. **Think → critique → correct → THEN build. Never jump to implementation.**
   For every task: (a) reconstruct the full context, (b) design one detailed
   approach, (c) critique my own design as an adversary, (d) revise. This is an
   IM app living in WhatsApp/Telegram's shadow — those patterns were refined over
   years. Ask "what is the most intuitive thing for a general end user?" before
   "what is easiest to code?" Treat UX as a first-class constraint, not a finish.
   **Presenting a plan does NOT end the round.** Present it inline, ask Aryan
   "does this work?", then WAIT and continue in the same round. Never hand off
   just because a plan is ready.

3. **We are partners; debate is expected. I take the front seat.** Aryan has
   limited depth on many of these topics and *wants* me to lead as senior dev.
   Aryan will be wrong sometimes; so will I. Push back with reasoning, offer
   alternatives, disagree constructively. Do not rubber-stamp. A good objection
   now beats a rewrite later.

4. **Verify properly, THEN commit — every round, in the same round.** At this
   scale regressions are guaranteed; catch them with *real* verification, not
   hope. Commit *every* round, but only *after* thorough validation — verify
   first, commit second, both inside the round. Do NOT wait to be asked; do NOT
   defer the commit. Live browser verification with wait-for-element polling
   (never fixed sleeps). Run the frontend check after every frontend edit.
   Restart the affected server + worker after backend edits. Only commit once
   I've *seen* it work. State failures honestly.

5. **Decompose big asks myself.** If Aryan hands me a large task in one prompt,
   that's a signal the workplan wasn't fully thought through — not a license to
   sprint. Break it down, reorder for my own convenience/efficiency, and spend
   the freed-up budget on rule 4 (thorough review of what I built this round).

6. **Reference-only rewrite; no monolith.** The old code is a reference, not a
   base to copy. Write **manageable modules in real folders** with a clear,
   documented **glue API/plan** binding them — not one giant file. Name things
   for what they *are* (e.g. `agent_harness`, not `agent_worker`, where it
   clarifies). Rewrite frontend connectors too if they aren't clean. Keep
   **refactoring and organizing** as files pile up; leave the tree cleaner than I
   found it, every round.

7. **Use open source aggressively.** I have `gh` access — use it. Before hand-
   rolling anything non-trivial, check whether an existing project does it well:
   **mem0, graphiti, llamaindex, opencode, hermes, qdrant**, and others. Prefer
   integrating a proven library over reinventing it. (Balances rule 6: we own our
   *architecture*, but we borrow *machinery*.)

## Standing operating conventions

The disciplines below are permanent; the **file names/paths are mid-rewrite** —
apply each rule to whatever the new equivalent is (e.g. `mesh.py` is becoming a
package, `agent_worker.py` → `agent_harness`). When docs and code disagree, the
code wins — then I fix the doc.

- **Per-round loop:** re-read this file → reconstruct context → design+critique →
  implement → verify live → bump `agentbridge/__init__.py` `__version__` → commit + push
  → update memory (+ sync `HANDOFF.md`/`ARCHITECTURE.md` when the shape changed).
- **The one invariant — Visibility = membership.** Everyone, human or agent, sees
  and reads only the chats they're a member of. Every read path goes through the
  membership-filtered accessor. When tightening an access rule, audit **every
  mutating endpoint**, not just read paths.
- **Frontend:** native ES modules under `gui/static/js/`, strict one-way
  layering; page views register on the `V` registry and never import each other.
  Run `python check_frontend.py` after every frontend edit.
- **Restart discipline:** after backend edits (mesh/server/harness), restart BOTH
  the affected server and the agent harness, or they serve stale behaviour.
- **Encoding trap:** never round-trip source through PowerShell
  `Get-Content`/`Set-Content` (UTF-16+BOM mangles em-dashes). Edit source with
  the Edit/Write tools only; bump the version with Edit, never PowerShell.
- **`ns`, never `ts`, for cursor/ordering.** `ts` is second-resolution and ties;
  a strict `>` against a tied cursor skips a message forever (real, fixed bug).
- **Per-user overlays merge, never overwrite.** Delete-for-everyone is chat-level
  and shared; per-user state is merged, never clobbered.
- **Testing:** dedicated test human + QA room on the live mesh (creds in memory).
  Aryan tests live mid-round — expect concurrent writes; `meta.json` is
  last-writer-wins. Run deterministic asserts in **throwaway scratch rooms and
  delete them after**. **"Platform QA 2" is off-limits.**
- **Safety rails never dropped:** agent tool blocklist + read-only flags stay in
  all fallback paths; unattended agents never get blanket auto-approve. No agent
  ever in a room without a responsible human. Never enter credentials/passwords,
  no financial actions, no permanent deletion of the user's data without explicit
  authorization, no publishing/sending on the user's behalf without permission.

## Housekeeping cadence

- **Every 5 rounds:** remind each other to clean up working memory — prune it to
  the task at hand so focus stays sharp. (Round counter lives in memory.)
- **Authoritative backlog: `BACKLOG.md` in the repo** (added 2026-07-14 after
  half the original brief was found dropped — a coarse checklist + broad scope
  + compaction loses items). Every ask lands there source-tagged the moment it
  arrives; boxes tick only after live verification; read + update it every
  round. It's in-repo so parallel sessions share one ledger. Memory keeps
  round history + credentials; `HANDOFF.md` "Next work queue" stays the
  human-readable summary.
