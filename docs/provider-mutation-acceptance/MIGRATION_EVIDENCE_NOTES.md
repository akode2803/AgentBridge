# Supabase migration evidence notes

These files preserve the exact SQL and observations from the disposable
AgentBridge Scratch upgrade. They are also a starting point for a future
project-to-project migration feature.

## Evidence layout

- Keep every applied migration immutable under its actual provider ledger
  version. Record byte length and SHA-256.
- Keep scratch-only prerequisite cleanup separate from the product clean-install
  schema. A target project may have data that makes an otherwise valid DDL
  change unsafe.
- Store post-migration shape queries separately. Verify columns, types,
  nullability, function identity arguments, overload counts, policies and
  grants; successful DDL execution alone is insufficient.
- Store behavioral acceptance separately from structural verification. Expected
  failures should run as independent transactions so they do not invalidate the
  surrounding schedule.
- Preserve superseded migrations and explain why they were replaced. Do not
  rewrite historical evidence to resemble the final design.

## Storage-specific lessons

- Move object bytes only through the Storage API. SQL metadata deletion does not
  remove bytes and Supabase protects the metadata tables against direct delete.
- Bind portable evidence to supported API fields. `storage.objects.id` is
  observable through current Storage responses/listing; the internal
  `storage.objects.version` field is not a client contract.
- Treat delete/re-upload at one logical path as a new object identity. Verify
  the target row ID after transfer before publishing a manifest.
- Separate SQL/RLS acceptance from real Storage HTTP acceptance. Metadata-only
  fixtures prove database behavior, not object transfer, hashing or HTTP retry
  semantics.

## Future source-to-target workflow

1. Inventory source and target project/PostgreSQL/Storage/Auth versions and the
   complete migration ledgers.
2. Capture an immutable source cut plus table, function, policy, grant and bucket
   manifests. Do not treat the cut as user authority.
3. Classify each migration as clean-install schema, compatible upgrade, or
   explicit data transform with a fail-closed precondition.
4. Apply schema changes transactionally on a disposable target and verify the
   exact post-migration shape.
5. Transfer Storage bytes through supported APIs, preserve or map supported
   object identities explicitly, and verify byte length/digest at both ends.
6. Transfer Auth identities through supported administrative interfaces; never
   copy password material or session tokens from database internals.
7. Run authenticated RLS/RPC/Storage acceptance with disposable principals,
   then compare bounded source/target canonical results.
8. Record rollback boundaries. A failed or ambiguous data transfer remains
   pending and must not advertise readiness.

The `20261013080407-*` files demonstrate this separation: cleanup prerequisite,
transactional DDL, structural verification, behavioral acceptance and
machine-readable results. The `20261013081948-http-*` files then preserve the
fresh Auth-root setup, exact post-run SQL verification and sanitized real
Storage HTTP result. The matching `http-storage-log-*` files retain the bounded
read-only service-log query and sanitized request outcomes used to resolve an
independent-review ambiguity in the first class-only failure evidence. The
executable HTTP schedule lives in
`devtools/validate_blob_storage_http.py`; credentials and API tokens are kept
outside the repository and are never written into the evidence.
