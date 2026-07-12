# Backend rewrite — technical decisions (R1 spike results)

Companion to `REWRITE_PLAN.md` §2 (the strategic decision log). This file holds
the *technical* pins, verdicts, and fallbacks — recorded 2026-07-12 from the R1
research spike (web research + `spikes/r1/` smoke scripts run on the dev box:
Windows 11 Enterprise, corporate-managed).

## Toolchain

- **uv** manages the project; venv at `.venv/`, lock in `uv.lock` (committed).
- **CPython 3.12** pinned (`.python-version`). Why not newer:
  `llama-index-embeddings-fastembed` requires Python **<3.13**, and ML wheels
  (onnxruntime etc.) lag new CPython releases. `requires-python = ">=3.11"`.
- Core deps stay minimal (`cryptography` only); everything heavy is an
  **optional extra** (`memory`, `retrieval`, `mcp`, `cloud`) — the packaging
  round depends on this split.

## Library verdicts (pins in pyproject.toml)

| Library | Verdict | Notes |
|---|---|---|
| `cryptography` ≥49 | ADOPT | Ed25519/X25519/ChaCha20Poly1305/scrypt all verified in `smoke_crypto.py`, which prototypes the FULL D4/D5 flow: password wrap+recovery, per-member chat-key wrap, signed envelopes, rotation-on-removal. |
| `qdrant-client` ≥1.18 | ADOPT | Embedded local mode (`path=...`) verified incl. persistence. **Constraints:** local mode targets <20k points/collection and is **single-process** (portalocker) → each harness process owns its own qdrant paths; never share a path across processes. Same API points at a real server later. |
| `fastembed` ≥0.8 | ADOPT as default, with fallback | Torch-free ONNX embeddings. Default model `BAAI/bge-small-en-v1.5` (384-dim, ~67 MB). **BUT see the onnxruntime incident below.** |
| `model2vec` ≥0.5 | ADOPT as fallback | Pure numpy static embeddings; `minishlab/potion-base-8M` (256-dim, ~30 MB) verified working on the dev box (`smoke_embeddings.py`). Lower quality than bge-small but immune to native-DLL policy blocks. |
| `mem0ai` ≥2.0 | ADOPT with caveats | v2 rearchitecture (2026). Qdrant default store; native embedders incl. fastembed/ollama/lmstudio (torch-free); LLM extraction providers incl. ollama, deepseek, anthropic, xai. `add(infer=False)` skips LLM. **v2 is ADD-only extraction** (no auto memory reconciliation — we own cleanup). **Ships posthog telemetry → MUST disable in product.** E2E validation deferred to R20 (needs ollama or working onnxruntime on the dev box). |
| `graphiti-core` | **DEFER** | Kuzu (embedded backend) archived Oct 2025 (Apple acquired Kùzu Inc.); graphiti deprecated its support. FalkorDB-Lite has **no Windows wheels**. No embedded+Windows+maintained combo exists today. Watch: graphiti issue #1132 (LadybugDB fork adoption), `real-ladybug` maturation. |
| `mcp` ~=1.28 | ADOPT, migration budgeted | Official SDK; FastMCP server + in-memory client session verified (`smoke_mcp.py`). Server→client notifications supported (session-scoped). **SDK v2 lands ~2026-07-27 and renames FastMCP→MCPServer** — R12 should check which is current and budget the small migration. (The separate `fastmcp` PyPI package is a third-party framework — we use the official SDK.) |
| `supabase` ≥2.31 | ADOPT with caveats | Import + surface verified. **Realtime is async-only in Python** and less battle-tested than JS (subscription timeout issues reported) → R23 wraps it in a reconnect/backoff supervisor with a polling fallback. Live test waits on the account (plan D2). |
| `llama-index-core` ~=0.14 | ADOPT with eyes open | Retrieval-without-LLM verified via a **custom BaseEmbedding** over our probe chain (`smoke_llamaindex.py`) — our embedder interface plugs in regardless of backend. ~25 runtime deps (no torch/pandas). R21 re-evaluates whether its loaders/chunkers earn the weight vs. plain qdrant+embedder. |

## The onnxruntime incident (dev box) → embedding probe chain

`import onnxruntime` fails on this machine with *"DLL initialization routine
failed"* — on 1.27.0 AND 1.22.1, in the repo venv AND a fresh AppData venv,
sandboxed and not. MSVC runtimes present; other native wheels (cryptography,
numpy, pydantic-core) load fine. Conclusion: machine-level block (corporate
security policy most likely), **not** a code or version problem — end-user
machines will mostly be fine.

**Design consequence (feeds R20):** embeddings live behind our own interface
with a **runtime probe chain** — `fastembed` (best quality) → `model2vec`
(pure numpy, always loads) → `ollama` (if installed) → API embedder (if key
configured). Probe once, cache the working backend per machine. This also
serves the mission rule: nothing hardcoded, every backend is config.

**Graph memory (feeds R20):** default = **mem0 v2 built-in entity linking**
(entities stored in a parallel collection inside the local qdrant store —
embedded, zero servers). Graphiti-grade knowledge graphs become an *optional*
feature that activates only when the user supplies a Neo4j/FalkorDB server.

## Deferred verifications (owned by later rounds)

- mem0 end-to-end add/search (R20 — needs ollama or a machine with working
  onnxruntime; introspection in `smoke_mem0.py` confirmed the provider surface).
- Supabase live realtime channel test (R23 — needs the account, plan D2).
- MCP SDK v2 migration check (R12).
- Set `HF_HUB_DISABLE_SYMLINKS_WARNING=1` when we ship model downloads
  (Windows non-dev-mode caveat).

## Smoke scripts

`spikes/r1/*.py`, each standalone via `uv run python spikes/r1/<name>.py`,
exit 0 on pass. All 7 pass on the dev box as of 2026-07-12 (embeddings via the
model2vec fallback, by design).
