# Typed provider blob-manifest acceptance

Status: inactive P1b blob-manifest SQL with API-visible object-ID binding
accepted on a disposable hosted database and through real Auth/Storage HTTP
requests. This is implementation and Supabase PostgreSQL 17/Storage acceptance,
not production installation, application activation, runtime acceptance or
release evidence.

## Exact target and source

- Scratch project: `riagjoluhcwjvmfchbzl` (`AgentBridge Scratch`,
  `ap-south-1`).
- PostgreSQL: 17.11.
- The first scratch migration,
  `20261013074701_agentbridge_p1b_typed_blob_manifest_rpc`, bound the internal
  `storage.objects.version` field. Client-wire review showed that field is not a
  supported client-visible identity contract. Its exact statement and results
  remain as superseded historical evidence in
  [20261013074701.sql](provider-mutation-acceptance/20261013074701.sql) and
  [20261013074701-results.json](provider-mutation-acceptance/20261013074701-results.json).
- Corrective scratch migration:
  `20261013080407_agentbridge_p1b_blob_manifest_object_id_correction`.
- Exact applied correction:
  [20261013080407.sql](provider-mutation-acceptance/20261013080407.sql), 10,864
  bytes, SHA-256
  `590a94a00adf588be4e344e7207656187db8127683cd01c86f5d1135e285aa2a`.
- Exact cleanup, verification and acceptance SQL are retained beside the
  migration in `docs/provider-mutation-acceptance/`; corrected machine-readable
  observations are in
  [20261013080407-results.json](provider-mutation-acceptance/20261013080407-results.json).
- The real HTTP setup, post-run verification and sanitized client result are
  retained as `20261013081948-http-{setup.sql,verify.sql,results.json}` in the
  same directory. Passwords, tokens and the publishable key are excluded.

No production project, production root, application process or harness was
changed.

## Implemented boundary

The client uploads sealed bytes through the Storage API before calling
`ab_mutation_blob_manifest`. PostgreSQL cannot read those bytes. The RPC instead
binds one exact current principal, room, staged object path, API-visible Storage
object ID, bounded metadata size, sender-declared sealed hash and manifest digest
to one same-transaction publication receipt. Existing attachment decryption and
signed plaintext-hash checks remain the end-to-end content proof.

`ab_blob_manifests` is private provider publication evidence. It has no direct
API grants or RLS read policy and does not grant chat visibility. The staged
Storage predicate checks current authority on every read. Before publication,
only the exact uploader who remains a room member can read the object. After
publication, a current/history-authorized room reader sees only the exact
published Storage object ID and size. Any replacement identity fails closed.

The migration replaces all four existing permissive `ab-mesh` Storage policies
rather than adding an OR-combined policy. Their legacy avatar/chat behavior is
retained outside `blob-stage`; the staged lane has exact four-segment grammar,
insert-only client behavior and owner-only deletion. A private metadata parser
rejects malformed/zero/oversized sizes without casting them. The public
security-definer predicate exposes only a boolean because authenticated Storage
policy evaluation must be able to invoke it; the underlying manifest table
remains inaccessible.

## Hosted PostgreSQL observations

Real `authenticated` role switches and `auth.uid()` claims exercised the SQL
boundary after a disposable database-v1 seal:

- a current room member inserted its own staged metadata row; before publication
  the owner saw it while another room member and a root outsider did not;
- the typed RPC wrote one manifest and one terminal receipt, and exact replay
  returned the same receipt;
- changed request evidence collided with the existing receipt, while a fresh
  operation for the same blob failed; wrong owner, identity and size also failed;
- after publication the other room member saw the exact row and the outsider did
  not;
- authenticated `UPDATE` on the staged path affected zero rows;
- a simulated provider replacement identity became invisible to owner and room
  reader rather than falling back to unpublished-owner access;
- after typed room metadata removed the uploader, exact receipt replay still
  succeeded and a fresh publication failed `42501`;
- rejected operations left no receipts; the RPC has one authenticated execute
  grant and none for `PUBLIC`, `anon` or `service_role`.

The corrected migration leaves exactly one UUID-signature read predicate and
one UUID-signature manifest RPC, with no legacy text overload or
`object_version` column. A second hosted schedule proved that wrong object IDs
are rejected without receipts, the receipt result contains `object_id`, and an
administratively simulated row-identity replacement hides the object from both
owner and room reader while exact receipt replay remains stable.

The performance advisor is clean. Security advisor notices for the manifest
table without policies and the authenticated security-definer RPC/predicate are
intentional for this private-table/execute-only design. Other notices belong to
the deliberately minimal earlier scratch scaffold.

The first scratch schedule inserted one private bucket and object metadata
fixtures through the administrative SQL connector. The separate HTTP schedule
below covers the Storage service and bytes that SQL cannot prove.

## Real Auth and Storage HTTP observations

Two generated `example.com` users were auto-confirmed in AgentBridge Scratch.
The validation client authenticated both through `sign_in_with_password`; their
API UUIDs matched the dashboard rows. Under a fresh sealed disposable root, it
then exercised only the public publishable key and each user's own session:

- the owner created a fresh 4,096-byte staged object with upsert disabled;
- an exact-path list exposed one stable API-visible object UUID, and an owner
  download matched the durable payload bytes and local SHA-256;
- the second current room member could not download before publication;
- repeating the same create-only upload failed, after which exact-path recovery
  returned the original object UUID;
- `ab_mutation_blob_manifest` published the exact UUID/size and exact replay
  returned the same receipt;
- the second room member downloaded identical bytes after publication;
- attempted upsert and in-place move both failed, and the exact object UUID
  remained unchanged;
- owner deletion through the Storage API removed exactly one object. A final SQL
  check observed zero remaining object rows, one immutable manifest and one
  terminal blob-manifest receipt.

The first sanitized result records the object UUID, digest and denial exception
classes, but contains no email, password, JWT, refresh token or API key.
Independent review found that class-only evidence could misclassify a transient
or unrelated Storage failure. A retained read-only query over the exact
five-second request window confirms the concrete service results: reader
`404 NoSuchKey`, create-only repeat `409 KeyAlreadyExists`, upsert
`403 AccessDenied`, move `404 NoSuchKey` and delete HTTP 200. The client now
requires the matching public status/code pair and pure regressions reject 429,
5xx, unrelated 400 and generic Storage exceptions. Its tightened classifier was
not rerun live after the disposable passwords were destroyed; the bounded
service log reconciles this run, and future runs emit structured status/code
evidence directly. The two disposable Auth accounts still exist; deleting them
is an external account operation outside this acceptance run.

## Local and review evidence

Static tests cover bounded/private manifest evidence, receipt-gate admission,
the full legacy-policy carve-out, exact staged grammar, fail-closed published
matching, request binding, lock order, Storage owner/object-ID/size checks and
narrow grants. The executable PostgreSQL gate adds authenticated staged-policy
checks, success/replay/collision/duplicate/mismatch/removal schedules and an
exact blob-key advisory-lock holder/waiter schedule.

The lead checkout passes 263 focused mutation, local-node fence, Supabase
transport and fake-cloud tests. Ruff, Python compilation and whitespace checks
pass. Independent Astra review ran 44 focused tests and found no P1/P2 in the
clean-install implementation. It found one scratch-upgrade replay risk: a draft
receipt could survive after manifest cleanup. The applied correction now
requires both draft manifests and blob-manifest receipts to be absent before
changing signatures. It did not contact the provider.

Astra's HTTP-evidence review then found the class-only denial ambiguity. After
the exact status/code classifier, ten regressions and bounded provider-log
reconciliation were added, follow-up review found no remaining actionable P1/P2
and independently passed all 30 HTTP-classifier/schema tests. The tightened
client's missing live rerun remains stated above rather than inferred from those
local checks.

## Remaining acceptance

- PostgreSQL 16 CI must execute the new fixture and exact lock schedule after
  publication.
- The management connector cannot prove simultaneous hosted sessions, so exact
  contention remains the disposable PostgreSQL gate.
- No readiness marker exists and the Python mutation adapter still uses legacy
  attachment paths.
