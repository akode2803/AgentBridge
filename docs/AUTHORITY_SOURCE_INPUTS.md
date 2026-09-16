# Bounded account, chat metadata and lifecycle inputs

These inactive APIs prepare raw inputs for the future authority coordinator.
They do not authorize chat reads or activate paging.

`capture_authority_documents` selects a complete bounded users/lifecycle namespace
and one exact chat metadata document at a provider-observed mirror position.
Selection copies only references under the mirror mutex; JSON traversal, detached
construction and canonical serialization happen outside it. A final position
check rejects changed cuts. Row/path, aggregate wire-byte, node and depth limits
apply before serialization. Integer magnitude is checked before decimal conversion.
The JSON prewalk accounts for UTF-8, escaped strings and container framing; a
second exact wire-size check guards the serializer contract. Unsupported inputs
are unavailable, never silently omitted or converted into absence.

At every eligible ingress, the cache constructs a bounded exact-JSON graph
without calling provider hooks. This follows legacy deepcopy because a custom
`__deepcopy__` can return a still-aliased exact dictionary. The second copy severs
that alias. If it cannot safely detach a value, existing canonical behavior is
preserved but that path becomes ineligible for authority-source publication.
Eligibility is updated with mirror mutations, including recent-local-write wins,
delta deletes, revocations and read-through insertions. No second serialized
mirror catalog is maintained.

`publish_authority_source` publishes those documents in the existing SQLite
observation namespace. The receipt binds chat, live mirror identity/revision and
SQLite source generation. The source contains accounts, lifecycle documents and
chat metadata together; the future coordinator must not combine it with a
separately captured lifecycle copy. Full publication retires removed documents.
Foreground exact reads use existing covering size indexes before payload loads;
subject lifecycle reads use the bounded R210 range reader on that same source.

`capture_lookup_policy` is payload-free. For each demanded account, it observes
present, known-negative, offline-absent or online-readthrough-required behavior.
`matches_lookup_policy` recomputes the selected statuses at the exact mirror
position. Health and negative-cache changes are therefore checked even when they
do not advance mirror revision. Meta absence follows the canonical non-readthrough
rule. A returned JSON null document remains present, distinct from a missing row.

Online unknown accounts raise `AuthorityReadThroughRequired` with bounded paths.
The owner must schedule existing canonical `get_doc` outside the foreground
attempt and all pin/SQLite locks, then restart completely. A found account changes
the mirror and requires republication; a provider-confirmed miss enters the
negative cache. The helper never schedules provider work itself, invents absence,
or infers remote completeness. Known negatives/offline absence preserve current
canonical cache behavior, not a guarantee about remote changes while disconnected.

Cold, bootstrap-only, unsupported, changed and oversized sources fail explicitly.
No foreground call rebuilds a source or falls back to a complete fold. Publication
failure may leave a pending generation, and old receipts never override live
mirror/source/policy checks. The coordinator still owns operation-wide budgets,
current pins, lifecycle effects, membership/terminal checks and the common final
page/session fence. These inputs are not continuing authority.
