# October 3 cloud migration handoff

Start at [migration/oct3-frozen/HANDOFF.md](migration/oct3-frozen/HANDOFF.md).
The local source snapshot froze on October 3, 2026 at 18:55:37 UTC.
Cloud becomes the single writer after materialization.

R243–R248 and October 3 canonical page, unread/read-ack, Realtime, preparation
queue and terminal-priority changes are included, along with opt-in bounded
delivery diagnostics. Performance acceptance remains open: earlier cold/warm
canonical DOM samples and native acknowledgments do not establish an overall
speedup or independently owned peer acceptance. Canonical authority, encryption,
session, source and durability gates remain required.

Private machine paths, process/session/security state and local checkpoint
references are omitted from this public handoff. They do not describe cloud
runtime readiness. No runtime credentials, user stores or private logs are
transferred. Independent peer setup and diagnostic activation remain pending.
