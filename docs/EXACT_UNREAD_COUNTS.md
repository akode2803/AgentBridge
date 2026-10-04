# Exact unread counts over admitted local history

`LocalInputRuntime.unread` owns a separate, yielding background worker. A sidebar
or selected-page request only enqueues/coalesces a session-bound job. It never
runs that worker, repairs its inputs, or folds full history on the HTTP path.
The existing canonical `PageOperation` remains the computation and finalization
owner. There is no unread endpoint or reusable visibility/permission cache.

## Work and retained state

Each worker quantum makes one prepare/finalize attempt, selecting at most 64
visible messages and examining at most 256 ordered raw rows. Its cumulative
operation ledger permits at most 16 MiB, 2048 expensive steps, 2048 crypto calls
and 64 epochs, including dependency recaptures. Work/progress/conflicts yield to
the scheduler rather than resetting a ledger inside a quantum. Overlay proof
work is queued for the existing preparation owner.

The raw keyset boundary advances through invisible rows as well as visible ones.
No read-cursor cutoff stops the scan: an edit to an arbitrarily old message can
make that message unread. Canonical `unread_info` supplies message, edit, mention,
reply and forced-unread semantics; visibility, history-on-join, sender tenure,
keys and viewer overlays remain owned by the canonical projection.

At most 128 jobs retain count/first-unread/mention scalars, a raw boundary,
source/Store positions, names and one-way comparison fingerprints. Each retained
candidate is capped at 64 KiB, with at most 64 account, lifecycle and epoch
dependencies; the candidate payload total therefore cannot exceed 8 MiB. No
plaintext, messages, signed documents, secret epoch observations, keys or evaluated
authority verdicts survive a quantum.

Dispatch is at most once per 250 ms. Normal jobs rotate round-robin; a selected
room receives at most two preferred dispatches before an eligible ordinary job.
Repeated requests do not reset progress, queue order or backoff. Failures back
off through 0.5, 1, 2, 4 and 8 seconds. Capacity, dependency/resource limits and
sustained churn remain explicitly incomplete; no fabricated zero is published.
There is no arbitrary history-size cutoff beyond these per-operation and
retained-evidence bounds.

## Accumulated dependency and time closure

Every next quantum and foreground candidate use freshly resolves all earlier
accounts/lifecycle subjects and captures all earlier epochs before evaluating
new work. This includes dependencies consumed by invisible raw windows. Only
the fresh request's observations enter its ordinary final epoch, pin, Store,
source and lifecycle gates. Old head/key fingerprints are compared; copying old
observations into a new final fence is forbidden.

Evidence preserves the earliest lifecycle evaluation time, latest successful
final validation time and minimum future deadline across every quantum. A
rollback below the most recently validated time, or reaching any earlier
scheduled lifecycle transition, invalidates the entire accumulation even when
source and head identities are unchanged. The final clock checks catch time
changes during preparation/finalization as well.
Successful GUI handouts also advance the retained validation-time high-water
inside the GUI session gate. An older concurrently prepared candidate loses
only its exact-count decoration if another handout has replaced it; it cannot
publish through a rollback below that newer time.

Only raw-history exhaustion plus successful finalization of the complete
accumulated closure creates a complete candidate. This is exact over the current
admitted local history, not a claim that every remote message has been ingested.

## Foreground publication and lifecycle

`candidate()` returns comparison evidence only. A sidebar read constructs a
fresh summary `PageOperation` with that evidence, recaptures and validates its
closure, and passes through `GuiApp.finalize_page_read`. Only the successful
fresh session/screen/canonical final cut can attach the exact unread summary.
A stale candidate is invalidated and the ordinary recent summary is recomputed
within the request's existing retry/work limits.

The wire fields remain `unread`, `unread_lower_bound`, `unread_complete`,
`first_unread_ns`, `mention` and `forced_unread`. Without a finalized complete
candidate, the existing recent-window lower bound and nullable unknown fields
remain unchanged. The count worker does not turn unknown zero into forced
unread.

Jobs bind to the Mesh owners and GUI app/session/viewer. Session advance clears
all jobs and fences old callbacks; a stopped or replaced in-flight job cannot
publish progress. The worker never acquires GUI locks. Shutdown joins both the
background thread and an explicit in-flight quantum before the Store can close.
