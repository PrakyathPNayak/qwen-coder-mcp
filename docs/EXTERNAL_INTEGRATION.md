# External feature integration roadmap

This document inventories features studied from four upstream projects
during the integration sprint that introduced
`perplexity_tools.py`, `fs_tools.patch_anchor`, and the `/bug` TUI
command. It records what was integrated, what was deliberately skipped
and why, and what remains as candidate follow-up work.

The four upstreams:

| Project | URL | License | Notes |
| --- | --- | --- | --- |
| pReAct | <https://github.com/nikhilr612/pReAct> | MPL-2.0 | DSPy-based ReAct agent harness with rich tool set. |
| Perplexity MCP server | <https://github.com/perplexityai/modelcontextprotocol> | MIT | Reference TypeScript MCP server fronting the Perplexity AI APIs. |
| Perplexity Python SDK | <https://github.com/perplexityai/perplexity-py> | Apache-2.0 | Python SDK over the same REST endpoints. |
| claude-code | <https://github.com/anthropics/claude-code> | Proprietary (Anthropic Commercial Terms) | CLI agent. **No source code can be copied.** Concepts only. |

All upstream code was treated as read-only reference material.
`NOTICE` carries the per-project attribution required by their licenses.

## What was integrated

### Perplexity tools (from `perplexityai/modelcontextprotocol`, `perplexity-py`)

* New module `src/qwen_coder_mcp/perplexity_tools.py` exposes:
  * `perplexity_search` -- POST `/search`, returns ranked results.
  * `perplexity_ask` -- POST `/chat/completions`, model `sonar-pro`.
  * `perplexity_research` -- POST `/chat/completions`, model
    `sonar-deep-research` (SSE-buffered).
  * `perplexity_reason` -- POST `/chat/completions`, model
    `sonar-reasoning-pro`.
* Configuration mirrors the reference server:
  `PERPLEXITY_API_KEY`, `PERPLEXITY_BASE_URL`, `PERPLEXITY_TIMEOUT_MS`,
  `PERPLEXITY_PROXY` (with `HTTPS_PROXY` / `HTTP_PROXY` fallback).
* Wired into the MCP server (`server.py`) and the TUI as
  `/perplexity_search`, `/perplexity_ask`, `/perplexity_research`,
  `/perplexity_reason`.
* The reference TypeScript implementation was studied for HTTP contract
  details (request body shape, SSE handling, citation rendering) but
  the Python implementation is independent.

### Anchor-based string edit (from pReAct's `FileWorkspace.patch`)

* New `fs_tools.patch_anchor(cfg, path, old_str, new_str)` replaces
  exactly one occurrence of `old_str` with `new_str`; rejects 0 or >1
  matches; rejects no-op (`old_str == new_str`); enforces sandbox via
  the existing `_resolve_inside_root` helper; atomic write.
* Exposed as MCP tool `patch_anchor` and TUI command
  `/patch_anchor <path> <<<old>>> <<<new>>>` (the `<<<...>>>` delimiter
  lets multi-line / whitespace-heavy strings pass through one input
  line).
* Complements the existing unified-diff `apply_patch` for cases where
  the file is not in a git tree or only a unique surrounding context is
  known.

### `/bug` slash command (from claude-code's `/bug`, concept only)

* New TUI command `/bug [summary]` writes
  `.agent/bugs/<UTC-timestamp>.md` containing:
  * the operator's optional one-line summary,
  * Python / OS / cwd info,
  * the last 20 chat messages with each body run through a
    secret-redaction pass (`api_key=...`, `Bearer ...`, `sk-...`,
    `ghp_...`, `pplx-...`).
* Independent implementation; no claude-code source consulted beyond
  public docs of the command name.

## What was deliberately skipped

### From pReAct

* **Parallel-trajectory ReAct agent / tool drafting.** pReAct relies on
  DSPy for prompt programs and runs multiple ReAct trajectories in
  parallel. Wholesale port would touch ~2k LOC of `agent_loop.py` and
  add a heavyweight DSPy + LiteLLM dependency. Out of scope for a
  surgical PR; this codebase's `agent_loop.py` already implements
  single-trajectory ReAct with devil's-advocate review.
* **Qdrant-backed long-term memory** (`MemoryStore`, `KnowledgeGraph`).
  Adds a vector-DB dependency that would dwarf the rest of the runtime
  footprint. The existing `task_memory` (todos / facts / decisions /
  pinned files) covers the same ergonomics for our scope.
* **Playwright web tool.** Headless Chromium is a 200+ MB install. The
  existing `web_tools.fetch_url` covers the static-content case, and
  `perplexity_search` now covers the live-web case.
* **SymPy math tool.** Dep-heavy; out of theme for a coding agent.
* **Kokoro TTS / MLflow tracing.** Out of theme.
* **`commit` / `revert` workspace overlays.** Conceptually clean but
  require a stateful in-memory file overlay we don't have. The existing
  `apply_patch` (git-backed) plus the new `patch_anchor` cover the
  immediate need; add overlays only if multi-file dry-run becomes a
  recurring ask.
* **Calendar / `datetime` tool.** Already covered: agents can shell out
  via `run_shell` if they really need wall-clock time.

### From `perplexityai/modelcontextprotocol`

* **HTTP-mode / Docker container deployment.** The reference server
  ships both stdio and HTTP transports; this codebase only exposes
  stdio. Add HTTP if a non-MCP HTTP client ever needs the perplexity
  tools; until then it's deadweight.
* **Zod-based schema validation at runtime.** Replaced with explicit
  `validate_messages` checks plus the MCP `inputSchema` JSON-Schema
  declaration the framework already enforces.
* **Service-origin header propagation (`X-Service`).** Carried only as
  the simpler `X-Source: qwen-coder-mcp`.

### From `perplexityai/perplexity-py`

* **`Async` client variant, `Embeddings`, `Responses` API surfaces.**
  None of the four MCP tools we exposed need them. Add when a tool that
  exercises them lands.

### From `anthropics/claude-code`

* **All proprietary code paths.** Cannot be referenced; only public
  user-facing concepts are admissible.
* **Plugin / marketplace system.** Out of scope; would duplicate the
  MCP tool registry we already have. The TUI's slash commands are our
  plugin surface.
* **Rich `/bug` upload to a tracker.** Not implemented: the `/bug`
  command writes a local file only. An operator who wants to share it
  is expected to skim it (in case the redaction missed a secret) and
  attach it to an issue manually.
* **`CLAUDE.md` / instruction-file convention.** This repo already
  honours `.agent/` for state and prompt overrides; no rename needed.

## Candidate follow-up work

If a future agent picks this up, ranked roughly by value-per-LOC:

1. **`perplexity_chat` long-form mode in the TUI.** Today
   `/perplexity_ask` always builds a single-user-message payload. A
   `--continue` flag could carry forward the last few TUI exchanges as
   the `messages` array.
2. **`patch_anchor` dry-run flag.** Mirror `apply_patch --check_only`
   so the agent can probe applicability without mutating the tree.
3. **Bug-report uploader.** Add an opt-in `--upload` flag to `/bug`
   that POSTs the report to a configured `BUG_REPORT_URL`. Off by
   default; never surfaces secrets that the redactor missed because the
   operator must explicitly opt in per invocation.
4. **Stream perplexity_research deltas to the TUI response panel** so
   the 30+ s wait shows incremental progress instead of one block at
   the end. Requires a small refactor of `_render_perplexity_chat` to
   accept a streaming sink.
5. **Workspace overlay** (pReAct-style `checkout`/`commit`/`revert`)
   only if multi-file dry-run becomes a recurring ask.
