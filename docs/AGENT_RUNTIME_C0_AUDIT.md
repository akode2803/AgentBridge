# Agent runtime C0 boundary audit

Status: **C0.1 implemented and verified on macOS; C1.1-C1.3 control slices implemented**
Date: **2026-08-02**
Parent plan: `docs/AGENT_RUNTIME_PLAN.md` / V141

This record freezes the current native CLI boundary before AgentBridge adds a
portable execution backend. It distinguishes controls AgentBridge can enforce
today from controls that are only provider claims or accepted native-process
limitations.

## Result of this slice

C0.1 ships two prerequisites without adding agent capability:

1. Every per-run Streamable HTTP MCP bridge now requires a fresh 256-bit-class
   bearer credential. The credential is generated in memory, appears only in
   that run's inline MCP configuration, is checked on every HTTP request, and
   dies when the bridge closes. Finding the loopback port is no longer enough
   to call another run's tools.
2. Provider subprocesses no longer inherit `os.environ`. They receive a small
   process/login baseline, variables explicitly declared by their preset, and
   AgentBridge-owned run values such as `MCP_TOOL_TIMEOUT`. Ambient mesh,
   database, GitHub, and unrelated provider credentials are absent by default.

These are hardening measures, not a sandbox claim. Native provider CLIs still
run as the desktop OS user, and `HOME`/`USERPROFILE` remains available so their
existing local login stores continue to work. Strong filesystem, process,
network, resource, and secret isolation requires the later execution backend.

## Trust-boundary inventory

| Surface | Producer | Consumer | Before C0.1 | Current state | Remaining action |
|---|---|---|---|---|---|
| Permission ask | Harness broker | Owner GUI | Unsigned transport doc | C1.1: strict signed/E2EE room record | Generic effect receipt remains open |
| Permission answer | Owner GUI | Harness broker | Unsigned transport doc; an at-rest writer who learns the ask id can forge a one-call allow | C1.1: signed/E2EE, digest-bound, current-authority checked and one-use | Generic effect receipt remains open |
| Peer verdict | Owner GUI | Target peer service | Unsigned transport doc can authorize a genuine signed peer request | C1.2: signed/E2EE, request-bound and one-use | Encrypt peer request/response/audit metadata |
| Run stop | Owner GUI | Provider adapter | Unsigned status doc; forgery can kill a run | C1.3: signed/E2EE owner command, current-authority checked, chat-bound, expiring and one-use | Bind to canonical run/session id when that ledger lands |
| Timer cancel | Owner GUI | Harness runner | Unsigned merged status doc; forgery can cancel work | C1.3: append-only signed/E2EE command bound to the exact live timer and one-use | Generic effect receipt remains open |
| Global pause | Human GUI | Harness runner | Unsigned singleton; a writer can pause or resume all agents | C1.3: append-only signed current-human state with deterministic ordering | Signed directory root and retention/compaction remain open |
| Room pause | Room member GUI | Harness runner | Unsigned room doc; a writer can pause or resume room agents | C1.3: signed room-bound state, current-member checked, deterministic ordering | Signed directory root and retention/compaction remain open |
| AppLink message | Local app instance | Recipient machine | Plain, unsigned machine-addressed doc | Unchanged | Authenticate sender/user/machine and encrypt sensitive proposals in C1 |
| Per-run MCP | Provider CLI | Run-bound bridge | Unauthenticated ephemeral loopback HTTP | Fresh bearer required on every request | Move toward private socket/pipe or isolated network; add explicit call nonce if transport stops providing session ordering |
| Provider environment | Harness adapter | Provider CLI | Entire parent environment | Default deny plus preset allowlist | Replace raw long-lived credential variables with provider-held or broker-held connections |

The permission answer is the highest-risk current transport record. The ask id
is published in the neighboring ask document, so unguessability of `new_id()`
does not protect against the stated at-rest adversary. Forging `always` through
the answer document grants only the current broker call; standing-rule
persistence remains GUI-owner-gated. A forged one-call allow can still produce
a real side effect and therefore must be closed before adding more powerful
tools.

## MCP channel contract

- Listen on `127.0.0.1` and an ephemeral port, one server per run.
- Generate the bearer credential with `secrets.token_urlsafe(32)` before bind.
- Put the credential in the MCP server's `Authorization: Bearer ...` header
  configuration, never in the URL.
- Check the header with constant-time comparison on every HTTP request.
- Return HTTP 401 before FastMCP sees an unauthenticated request.
- Keep the existing run-bound closures for room, workspace, policy, mesh and
  timers. Authentication identifies the run channel; it does not replace
  membership or capability checks.
- Tear down the server and credential together. Post-teardown calls fail
  because the endpoint no longer exists.
- Do not log the inline MCP configuration or bearer value.

Known native limitation: the inline MCP JSON is part of the provider CLI's
argv. Another process running as the same OS user may be able to inspect that
argv on some platforms. This is still materially better than a port-only
boundary, but it is not isolation from a malicious same-user process. A private
socket/pipe or per-backend network namespace is the intended stronger design.

## Provider environment contract

`provider_env()` applies the following order:

1. Copy only the cross-platform process baseline that exists on the host:
   path, home/login discovery, locale, temporary directory, certificate and
   proxy settings, plus Windows process-launch variables.
2. Copy only names declared by `Preset.env_allow`.
3. Add AgentBridge-owned run values explicitly. A host value with the same name
   cannot override the injected value.
4. Pass the resulting dictionary to every provider `Popen`, including the
   usage-error minimal-argv retry.

Undeclared examples that are now absent include `SUPABASE_SECRET_KEY`,
`SUPABASE_MEMBER_PASSWORD`, `GITHUB_TOKEN`, credentials for another provider,
and arbitrary application variables. A machine overlay preset can extend
`env_allow` for a custom provider, making that disclosure reviewable data
instead of hidden inheritance.

The shipped credential variables are compatibility allowances for existing CLI
authentication modes, not the final secret architecture. They remain visible
to that provider process. C1 and the execution-backend releases must replace
raw long-lived values with provider-held sessions, opaque broker-held
connections, or narrowly scoped short-lived leases.

## Provider capability and enforcement matrix

Evidence date: 2026-08-21. Only the current macOS host was available. "Preset"
means repository configuration was inspected; it does not mean the provider's
claim was independently proven on this platform.

| Family | macOS evidence | Provider tool loop | AgentBridge hook | Provider sandbox claim | C0 environment | Honest current track |
|---|---|---|---|---|---|---|
| Claude Code | CLI not found on current host; preset previously marked verified | Provider-owned | MCP permission prompt plus AgentBridge blocklist | No AgentBridge-enforced process sandbox | Explicit Anthropic/Bedrock/Vertex names | Broker-integrated native, pending live macOS recheck |
| Codex CLI | `codex-cli 0.147.0` installed; CLI and code-mode host carry valid OpenAI macOS signatures | Provider-owned direct shell or standalone code-mode host | Server-filtered per-run `delegate_agent` only | Generated permission profile denies the host root, writes only the absolute run workspace, and disables command network | Authentication home only; API keys, endpoints, and proxy credentials stripped | Trusted R140 slice; signed executable pair, byte hashes, version, and non-user config layers bound; fresh-only; no security-flag fallback |
| Cortex Code | CLI not found | Provider-owned | No AgentBridge per-tool hook | `--sql-read-only` plus configured tool blocklist | Explicit Snowflake/Cortex names | Provider-restricted native, unverified here |
| Grok CLI | CLI not found | Unknown/text-only preset | None | None | Explicit xAI/Grok names | Text-only native; no tool-control claim |
| Ollama | Client `0.32.1`; daemon unavailable | Text generation | None | None | Explicit Ollama names | Local text-only native |
| DeepSeek via Ollama | Same Ollama client; model not probed | Text generation | None | None | Explicit Ollama names | Local text-only native |

Windows and Linux evidence remains open. No release may generalize the macOS
results into a three-platform claim. Each platform row must record executable
version, actual argv, working directory, environment, permission/tool hook,
fallback, stop behavior, and a real smoke result.

R140 re-audits the tagged 0.147.0 schema, feature registry and release surface.
The policy explicitly disables hooks, skills, apps/plugins, browser/computer
control, image generation, delegation, persistence, automatic approvals,
elicitation, updates and proxy features. It keeps only provider inference,
workspace-scoped files, sandboxed process execution and the optional signed
loopback AgentBridge MCP ceiling. Code-mode models use the package's standalone
host with in-process fallback disabled; nested tools remain subject to the same
permission profile. An absolute workspace permission avoids 0.147.0's reported
relative `:workspace_roots` growth path. Every discovered skill path is also
disabled explicitly because hiding skill instructions alone does not block a
literal skill mention. Before signing and again immediately before launch,
AgentBridge inspects Codex's effective project, cloud, system and managed
configuration layers. Any non-user authority outside the two project settings
that session flags replace is rejected; layer fingerprints and both executable
SHA-256 values are part of the signed provider-policy digest.

The reviewed Family default resolves to `gpt-5.6-luna`, so an exact run never
falls through to a mutable provider default. A restarted first-ever room proved
workspace creation precedes policy inspection and completed with that exact
model; its canonical terminal record carried the provider version, model,
policy digests, capability ceiling and all enabled/controlled/blocked groups.

## Adversarial cases and expected result

| Case | C0.1 expectation |
|---|---|
| Discover bridge port, send no credential | HTTP 401; no MCP initialization |
| Reuse a random or sibling token | HTTP 401 |
| Use the run's credential during the run | MCP protocol and existing tool gates work |
| Call after bridge teardown | Connection fails |
| Put `MCP_TOOL_TIMEOUT` in host environment | Host value ignored; harness value wins |
| Put mesh/database/GitHub secret in host environment | Missing from provider child unless a preset explicitly declares it |
| Provider rejects convenience flags and minimal retry runs | Same environment policy, safety args, blocklist and MCP auth remain |
| Forge ask display doc | May create misleading transport/UI metadata; cannot create an in-memory broker call |
| Forge answer for a live ask id | Ignored unless it opens as the current owner's exact signed/E2EE decision |
| Forge stop/pause/timer/peer verdict | Legacy, malformed, copied and tampered records are inert in production |
| Owner changes or loses authority while a control waits | Owner controls revalidate ownership and policy epochs; pause actors revalidate active identity and room membership |

## Verification completed

- Real Streamable HTTP MCP protocol test with valid, missing and incorrect
  bearer credentials.
- Existing broker/capability-tool protocol suite under authenticated transport.
- Provider environment unit fixtures proving explicit pass-through,
  AgentBridge injection precedence and exclusion of ambient secrets.
- Existing real subprocess stub tests, including minimal-argument fallback and
  workspace/outbox behavior, under the restricted environment.
- macOS executable/version probe recorded above.

## C0 continuation checklist

- [x] Reproduce and document unsigned ask/answer and loopback MCP boundaries.
- [x] Stop full provider environment inheritance and add preset declarations.
- [x] Authenticate the current per-run MCP bridge with an ephemeral credential.
- [~] Complete capability/enforcement evidence: macOS partial; Windows and Linux open.
- [x] Add threat cases for bridge/environment, forged and tampered controls,
  replay, owner/policy change, room-member removal and mixed-version records.
- [~] Design and ship the signed/E2EE runtime data plane before adding tools,
  containers or hidden delegation capability.
