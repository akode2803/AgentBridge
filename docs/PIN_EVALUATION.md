# Pin evaluation and publication

`agentbridge.mesh.pins.evaluate_pin` is the pure decision boundary for local
key trust. It receives an account name, a caller-observed pin, the published
signing and agreement keys, and optional signed key history. It returns a frozen
`PinDecision` containing an action and the selected key strings.

The actions describe the current pinning policy:

- `keep`: select the observed published pair without changing trust state. This
  covers an exact existing-pin match and an unpinned keyless account.
- `first_seen`: select a newly observed published pair and propose first-sight
  pinning.
- `rotate`: select the published pair after signed history proves a chain from
  the pinned signing key to that pair.
- `alert`: select the pinned pair because the published pair does not match and
  no valid rotation reaches it.

The evaluator reads only its arguments. It does not read or write the pin file,
read the clock, access a transport or Store, or publish an alert. For the supported
string-key inputs, the result retains only scalar fields, not the pin dictionary
or history. The frozen container does not validate or deeply freeze malformed
caller values; legacy input handling is preserved. Inputs are caller-provided
observations and are not claimed to be one coherent capture. A `PinDecision` is
therefore not authority to perform a later write.

`KeyPinStore.trusted` remains the publication owner. Under its existing local
lock it evaluates the current process pin and immediately performs the existing
pin or alert mutation. First-sight publication still rereads the read-merge-write
result: another store instance may already have supplied the pin. This wrapper
also retains the historical same-sign first-sight behavior and the policy that
process memory uses the merged result even when durable file replacement fails.

The extracted history verifier preserves the established behavior. Dictionary
entries are stably ordered by integer `ns`; non-dictionaries and entries that do
not extend the current signing key are skipped. An invalid signature on a
candidate next link rejects the walk immediately. Malformed ordering values keep
their existing exception behavior.

This extraction does not add cross-process locking, compare-and-swap publication,
durable trust generations, retained lifecycle-head coordination, or a frozen
authority closure. A future captured resolver must collect bounded immutable
account, history, lifecycle, retained-head, historical-actor, owner, and negative
lookup observations. It must validate trust and session coordination around
capture and handout before a result can serve as frozen authority.
