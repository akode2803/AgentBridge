# AgentBridge working agreement

Revised with Aryan's authorization after the R172 experiment and review. This
replaces the former mandatory seven-step workflow; do not automatically restore
the archived procedure. User instructions govern. Use judgment and show evidence.

## Mission and context

Build product-ready human/agent chat with WhatsApp/Telegram-grade UX across folder
and cloud transports, a local web GUI, and per-agent harnesses. Continue the
modular rewrite. Models are configuration, not hardcoded provider assumptions.
Preserve performance and parallel harness work; memory/retrieval/knowledge graphs
and summarization; reliable delivery and receipts; group privacy and human-owned
agent permissions; tool/plugin interoperability, rich previews, and easy setup.
A planned capability is not a shipped capability.

Resume from HANDOFF.md and its current task evidence. Read relevant source and
design context; consult BACKLOG.md, REWRITE_PLAN.md and ARCHITECTURE.md as needed.
Code wins factual disagreements. Preserve unrelated user edits and historical
evidence. Do not repeatedly reload every ledger or duplicate the same update.

## How we work

- Own the next useful, bounded outcome. Implement approved work directly when its
  scope is clear. Ask when a consequential choice needs the user's judgment or
  an action lacks authorization; routine implementation needs no approval ceremony.
- Trace ownership before changing behavior. Explain consequential design choices
  and seek independent critique when the risk warrants it. An existing reviewed
  design does not need another planning round merely because the session changed.
- Keep behavior in its owning modules and APIs/CLIs thin. Prefer existing tested
  machinery when it fits. Investigate recurring failures at their shared cause.
- Test actual failure modes and compatibility at meaningful checkpoints. Combine
  related slices and avoid repeated successful runs between small edits. Delegate
  test design/execution to Sol or Terra where useful; Astra focuses on architecture
  and consequential review. Broaden coverage for shared code, migrations, failures
  or concrete remaining risk; a full suite is not mandatory for each bounded slice.
  Concurrency and privacy boundaries still need adversarial regression tests.
  Seek independent review for consequential changes before release. Do not rerun
  unchanged successful gates solely because a session or model changed.
- Separate implementation, review, and release claims. Commits and feature-branch
  backups may precede release. Routine app restarts and live checks are not release
  gates. Use them when changed integration or process lifecycle behavior cannot be
  established adequately by deterministic tests, or at a meaningful activation
  checkpoint. Record fresh process identity when a restart is actually needed.
  Distinguish shipped source from the currently running build; no need to keep the
  app live on every intermediate release. Documentation needs no runtime restart.
  Version releases in agentbridge/__init__.py; scope commits and verify pushes.
- Save non-obvious decisions, completed evidence and the next action in a concise
  durable handoff. Update other documents only when their meaning or status changes.
  Closing summaries include the next concrete work and any unfinished gates.

## Technical boundaries

- Visibility equals membership for humans and workers. Canonical reads apply
  edits, redactions/tombstones, hidden and cleared state. Tightening access means
  checking mutation paths too. Every agent has a responsible human; preserve tool
  blocklists, read-only settings and sandbox limits in fallbacks. Fail closed.
- Merge per-user overlays. Use ns for ordering, with deterministic ties; receipts
  use read_ns; unread counts also use read_ns, including newer edits. A process-local clock or
  mirror revision cannot establish cross-process authority or remote freshness.
- Preserve the frontend's one-way module layers; views register on V instead of
  importing other views. Run devtools/check_frontend.py for frontend changes; use the tracked checker,
  whose failure cases are tested, rather than the older ignored local helper. Runtime config
  belongs in core/config.py; legacy/bridge.py remains protected reference material.
- Use disposable resources for deterministic tests. Platform QA 2 is off-limits.
  Do not post to user chats, expose credentials, or delete user data without
  authorization. Scope live verification; account for concurrent writes.
- Preserve future multi-server trust, partition and conflict boundaries. Local
  observations, SQLite generations and successful captures are not permission,
  completeness or cache-admission proofs. Distribution is not implemented yet.

## Product direction (2026-09-30)

Prioritize an instant-messaging experience on Supabase, including Realtime and
scoped incremental updates. Folder transport remains in the repository but is
deprecated as a compatibility constraint; it must not block this work. General
relational portability is secondary. Preserve fresh canonical authority checks:
Realtime notifications and local cached state are not authorization. A lean local
server/relay and private self-hosted Supabase are future capabilities, not current
delivery or latency guarantees. Measure actual-instance behavior before claiming
UI regressions or end-to-end latency are resolved.

## Budget and recovery

Work through useful release-sized objectives and save progress at their boundaries.
Do not end a user turn merely because one small source release is complete.
Continue through related ready steps toward a major architecture/product checkpoint,
or until a real blocker needs Aryan's judgment. Keep required progress updates
brief; reserve substantive summaries for major checkpoints. This reduces repeated
manual continuation while preserving recoverable evidence.
Aryan upgraded the quota on 2026-09-15: the earlier 10–15 minute rounds are no
longer a stopping condition. Continue autonomously while useful authorized work
remains; keep tests and recovery current. Optimize model usage: Astra for hard
reasoning/review, Sol for implementation, Terra/Luna for bounded support. Delegate
when it saves useful work and persist results outside agent IDs. Quota pauses can
still happen; a larger allowance does not justify redundant work or token waste.

Checkpoint bounded steps. Record objective, branch/commit/released version,
owned/protected files, completed/missing checks, evidence and next action. Verify
artifacts after interruptions: incomplete logs are not passes. Resume completed
work rather than restarting it; stale helpers must not overwrite current scope.
Personal-memory updates require explicit authorization. Do not erase history.

Previous agreement and entry point: task-dir WORKING_AGREEMENT.before-r173.md and
AGENTS.before-r173.md under historical-local-evidence/.
Older exact history remains in that directory's doc-archive-2026-09-08/.
