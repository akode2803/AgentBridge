# v1 → v2 migration & cutover runbook

The mechanism (`agentbridge/migrate.py`, R9.5) is tested; this is the
*operational* procedure R14 follows. Nothing here runs against live data until
R14 — and even then the source tree is only ever READ.

## Guarantees the tool gives you

- **Source is read-only.** The migrator opens the v1 `mesh/` tree read-only and
  writes exclusively to a fresh, must-be-empty destination (`mesh2/`). It
  refuses a non-empty destination.
- **`--dry-run` touches nothing** — it reports counts + verification without
  writing, so you can inspect the plan first.
- **Built-in verification**: after writing, it re-folds every chat and checks
  the materialized meta matches, and that v2 line counts equal v1 + 1 (the
  synthesized genesis). A non-clean run exits non-zero.
- **Identities keep working**: message ids and ns are preserved, so read
  cursors and receipts carry over; v1 PBKDF2 logins still verify (upgraded to
  scrypt + identity keys on first v2 login); v1 owner → group admin; starred
  snapshots → id lists; redactions/edits/pins/state overlays all move.
- **Seal-forward**: migrated messages are epoch-0 (readable) envelopes. New
  post-migration messages seal under real chat keys once E2EE is on. History
  is not retro-encrypted (documented in `docs/THREAT_MODEL.md`).

## Procedure (R14, on Aryan's machine)

1. **Freeze writes**: stand every worker down and close every app instance
   pointing at the live folder. Confirm no process is posting.
2. **Snapshot**: copy the live `mesh/` tree to a dated backup
   (`mesh.backup-YYYYMMDD/`) — this is the rollback.
3. **Dry-run**: `python -m agentbridge.migrate --src <sync>/mesh --dest <sync>/mesh2 --dry-run`
   Read the report: user/chat/message counts should match expectations,
   `verification: PASS`, zero warnings. Investigate any warning before step 4.
4. **Migrate for real** (drop `--dry-run`). Re-confirm `verification: PASS`.
5. **Validate on a copy first**: point a v2 app instance at `mesh2` (a spare
   account) and spot-check: a busy chat reads correctly, a deleted message is
   still a tombstone, an edited message shows edited, receipts look sane.
6. **Cut over**: switch the GUI + local worker to `mesh2` (config flip). Bring
   workers back up on the v2 harness. Announce machines (R11) so presence
   re-establishes.
7. **Coordinated remote update**: any other machine (e.g. a hosted agent box)
   re-points at `mesh2` — a short pasteable per-machine step, like the v1
   Phase-2 CoCo cutover doc.

## Rollback (if step 5 or 6 reveals a problem)

- The live `mesh/` tree was never modified — point everything back at it (and,
  if anything did touch it, restore the step-2 backup). `mesh2` can be deleted
  and the migration re-run after a fix. No data is lost because the source is
  pristine.

## Deliberately NOT migrated

Runtime artifacts: `status/`, `outbox/`, `control.json`, worker state dirs —
they regenerate. Legacy handler wiring retires in R26.
