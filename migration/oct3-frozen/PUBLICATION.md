# Public October 3 migration handoff

The owner explicitly approved publishing this sanitized frozen source snapshot
and migration notes on a new branch in the public AgentBridge repository.
Main and PR34 remain separate. Local implementation is frozen for cloud transfer.

Base: `a45e91f72fb959a842dd8ed667126de3461dd08f`.
Branch: `codex/handoff-oct3-frozen-185537`.
Original archive SHA-256:
`0ac90740dfaa69cc0676292dd59cab4c9c99509408f78a07f23a8b0a39223e3e`.

The original manifest remains provenance for the private transfer archive;
`PUBLICATION_FILES.json` records the published file hashes and exact differences
from that manifest. Source implementation and test bytes are unchanged.
Public documentation omits personal machine paths, historical task IDs, private
account/dataset/session notes and the proposed peer name. Runtime credentials,
keys, stores, chat contents, private logs and the archive binary are excluded.

Fetch this branch into a separate checkout and pin the confirmed remote SHA.
Read the migration handoff before cloud runtime setup. A code publication does
not authorize credentials, peer joins, app activation or deployment.
