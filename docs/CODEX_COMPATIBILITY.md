# Codex CLI compatibility

AgentBridge treats the Codex CLI as a signed native authority boundary. It
does not accept every version merely because the command starts.

The package-owned policy lives in
`agentbridge/harness/adapters/presets/codex.json`. Its
`bridge_profile.supported_versions` field lists reviewed `major.minor` series.
Patch releases inside a listed series are accepted, so ordinary patch updates
do not require Python source changes. A new minor or major remains disabled
until its flags, config isolation and native capability inventory are reviewed
and that JSON list is updated.

The current reviewed series are:

- `0.147.x`, retained for existing installations;
- `0.153.x`, including the locally verified `codex-cli 0.153.4` installation.

Admission still requires all of the following:

- exact `codex-cli major.minor.patch` version output;
- membership in the shipped reviewed-series list;
- matching package metadata when the standalone package provides it;
- the signed OpenAI executable and code-mode-host identity checks;
- inspectable non-user configuration layers;
- the existing strict config, sandbox, environment and capability ceiling.

An unsupported canonical version fails with the detected value, the supported series,
and the exact preset path to update after review. This message is allowed to
reach the existing harness failure surface so the owner sees how to repair the
installation instead of receiving a generic adapter failure.

The renderer label and enforcement contract use `codex-reviewed` rather than
embedding a historical CLI number. Per-run authority digests still bind the
exact installed patch version, executable hashes, config layers and launch
arguments, so accepting multiple reviewed patches does not make their signed
run evidence interchangeable.

The relevant provider configuration controls are documented in the
[official Codex configuration reference](https://developers.openai.com/codex/config-reference/).

Malformed CLI output is never echoed in public errors. Deterministic unsupported
versions end preparation once, before any model invocation, and appear in the
existing run history and optional chat error notice. Other preparation errors
retain bounded retries and their existing redaction policy.
