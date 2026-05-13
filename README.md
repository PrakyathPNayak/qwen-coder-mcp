# qwen-coder-mcp

A Model Context Protocol (MCP) server backed by **Qwen3.6-27B running
locally on a single RTX 4090** via vLLM (4-bit AutoRound, OpenAI-compatible),
focused on coding workflows, plus a self-improving **agentic loop** that
continuously audits this repository, proposes fixes, plays devil's advocate
against its own proposals, and commits accepted changes.

> **Local-use tool.** This server is meant to run on a developer's own
> workstation (or a remote machine the operator owns) and back an MCP
> client like Claude Desktop, Continue, or the VS Code MCP extension.
> It is **not** designed to be exposed as a public/commercial service.
> The bundled tools intentionally support the same things other local
> agent tools support: executing shell commands (with operator
> confirmation), prompting the user for input mid-run, fetching data
> from the open internet, and editing files inside the configured
> workspace root. See [Local-use posture](#local-use-posture) for the
> full list and the permission model.

## TL;DR — local 4090 setup

```bash
./scripts/serve_qwen.sh        # launches vLLM on :8000 (auto-installs into .venv-serve)
./scripts/wait_ready.sh        # blocks until /v1/models responds
cp .env.example .env           # already pointed at the local server
./scripts/run_loop.sh          # detached agentic loop, logs to .loop/runtime.log
```

See [`docs/LOCAL_SERVE.md`](docs/LOCAL_SERVE.md) for VRAM budget, alternate
quantizations, and the llama.cpp fallback. See
[`docs/AGENT_CHECKPOINTS.md`](docs/AGENT_CHECKPOINTS.md) for the agent's
checkpoint / rotation / `/resume` recovery flow.

## Features

- **MCP server** (stdio transport) exposing coding tools:
  - `chat` — free-form conversation with Qwen3.6-27B
  - `complete_code` — code completion for a snippet
  - `explain_code` — natural-language explanation of code
  - `find_bugs` — static review for bugs / smells / risks
  - `propose_fix` — generate a unified diff fix for an issue
  - `devils_advocate` — critique a proposed fix
  - `refactor` — rewrite code per a goal
  - `write_tests` — generate tests for code
  - `summarize_repo` — high-level repo summary

## TUI

A Textual-based terminal UI is shipped under the `tui` extra:

```
pip install -e '.[tui]'
qwen-coder-tui
```

Anything not starting with `/` is sent to Qwen as a chat message with
multi-turn memory preserved within the session. Press TAB after `/`
for completions; `@path` expands a file inline.

### Slash commands (selected; `/help` for the full list)

**Chat & files**
- `/help [pattern] [--regex]` — full command reference (regex supported)
- `/read <path>` / `/view <path> <start> [end]` — full or line-range read
- `/grep <pattern> [path] [--ext .py] [--count]` — repo-wide grep
- `/ls [path]`, `/find_bugs <path>`, `/explain <path>`
- `/diff <path>` (HEAD diff) or `/diff <a> <b>` (two paths)
- `/apply` — extract a unified diff from the last reply, `git apply --check`,
  then apply
- `/history [n|clear]`, `/quit`

**Web**
- `/search [--max N] <query>` — DuckDuckGo HTML search (no API key)
- `/fetch <url>` — text body, byte-capped, binary refused
- `/perplexity_search [flags] <query>` — Perplexity `/search`
  (PERPLEXITY_API_KEY required). Flags: `--max N`, `--tpp N`,
  `--country CC`, `--mode web|academic|sec`,
  `--recency hour|day|week|month|year`, `--domain a.com[,b.com]`
  (repeatable), `--lang en[,es]` (repeatable),
  `--after YYYY-MM-DD`, `--before YYYY-MM-DD`,
  `--updated-after`, `--updated-before`.
- `/perplexity_ask [flags] <question>` — quick web-grounded Q&A via
  `sonar-pro`. Accepts every chat flag: search filters above plus
  `--context low|medium|high`, `--search-type fast|pro|auto`,
  `--no-search`, `--related`, `--images`, `--max-tokens N`,
  `--temperature F`, `--top-p F`, `--top-k N`,
  `--frequency-penalty F`, `--presence-penalty F`, `--country CC`.
- `/perplexity_research [flags] <topic>` — deep multi-source research
  via `sonar-deep-research` (slow). Adds
  `--effort minimal|low|medium|high` and `--keep-think` (preserve
  `<think>` blocks; default: stripped).
- `/perplexity_reason [flags] <question>` — step-by-step reasoning via
  `sonar-reasoning-pro`. Same flag surface plus `--keep-think`.
- `/perplexity_embed [--dim N] [--encoding base64_int8|base64_binary] <model> <text>`
  — generate a vector embedding via `/v1/embeddings`. Models are
  `pplx-embed-v1-0.6b` and `pplx-embed-v1-4b`.
- `/perplexity_async create <model> [chat flags] <question>` /
  `/perplexity_async get <id>` / `/perplexity_async list` — submit,
  poll, or list async chat-completions jobs (use for long-running
  deep-research queries that exceed the sync timeout).

**Editing**
- `/patch_anchor <path> <<<old>>> <<<new>>>` — replace exactly one
  occurrence of `old_str`; rejects 0 or >1 matches. Complements
  `/apply` (unified diff) for files not in git or when only a unique
  surrounding context is known.
- `/bug [summary]` — write a redacted bug report (last 20 messages +
  sysinfo) to `.agent/bugs/<UTC-timestamp>.md`. Local-only; nothing
  uploaded.

**Agent mode (tool-calling loop)**
- `/agent <task>` / `/agentw <task>` — one-shot agent turn (read-only / write)
- `/agent_on` / `/agent_off` — make plain chat go through the agent loop
- `/agent_write_on` / `/agent_write_off` — include `fs_write`/`apply_patch`
  in the default agent's tool registry
- `/confirm_writes_on` / `/confirm_writes_off` — y/n modal before each
  destructive tool call (default ON)
- `/tools` — list read-only and write tool registries

**Shell with audit log**
- `/run [--yes] <cmd>` — shell out (default-DENY; `--yes` one-shots,
  or `/run_on` for the session)
- `/run_on` / `/run_off` — session-wide auto-approve toggle
- `/runs [N] [--json]` — tail of `.agent/runs.log` audit trail

**Mega-toggles (loop 258)**
- `/allow_all` — agent_on + agent_write_on + confirm_writes_off +
  run_auto_approve. Maximum autonomy; use with care.
- `/safe_mode` — inverse: every confirmation re-enabled

**Autonomous self-improvement loop (loop 258)**
- `/loop start` — spawn `python -m agent.loop` as a detached subprocess;
  pid persisted to `.agent/loop.pid`
- `/loop stop` — SIGTERM the recorded pid; `/loop kill` forces SIGKILL
- `/loop status` — pid, alive?, `runtime.log` size
- `/loop tail [N]` — last N lines of `.loop/runtime.log` (default 30)

**Introspection**
- `/sysinfo [--json] [--probe]`, `/lat [--json] [--top K] [--by-role]`,
  `/tokens [--json] [--top K] [--by-role]`
- `/checkpoints [list|export N <path> [--gzip]]`

### MCP server tools

  - `web_search` — DuckDuckGo HTML web search (no API key)
  - `fetch_url` — fetch a URL's text body (binary refused, byte-capped)
  - `read_file` — read a file (full / line-range / regex-pattern slice with
    `--before/--after` context, loop 256)
  - `list_dir` — list a directory inside the repo root
  - `write_file` — write a file inside the repo root (utf-8)
  - `apply_patch` — apply a unified diff via `git apply` (`check_only` supported)
  - `patch_anchor` — anchor-based string-replace inside the repo root.
    Replaces exactly one occurrence of `old_str` with `new_str`; rejects
    0 or >1 matches. Complements `apply_patch` for files not in git or
    when only a unique surrounding context is known.
  - `run_shell` — run a shell command via `/bin/sh` inside the sandbox
    (deny list, wall-clock cap, output cap, optional `cwd`/`timeout`)
    — added in loop 260 so MCP clients have the same shell access the
    TUI's `/run` already provides.
  - `perplexity_search` — Perplexity `/search` (ranked web results;
    requires `PERPLEXITY_API_KEY`). Full SDK option surface:
    recency / domain / language / date filters, `search_mode`
    (web / academic / sec), `country`.
  - `perplexity_ask` — quick web-grounded Q&A via Perplexity
    `sonar-pro`. Accepts the full chat option surface from the
    `perplexity-py` SDK: search filters, generation knobs
    (`temperature`, `top_p`, `max_tokens`, etc.), `web_search_options`
    (`search_context_size`, `search_type`, `user_location`),
    `disable_search`, `return_related_questions`, `return_images`,
    structured-output `response_format`.
  - `perplexity_research` — deep multi-source research via
    `sonar-deep-research` (slow; SSE-streamed under the hood). Adds
    `reasoning_effort`.
  - `perplexity_reason` — step-by-step reasoning via
    `sonar-reasoning-pro`.
  - `perplexity_embed` — vector embeddings via `/v1/embeddings`
    (`pplx-embed-v1-0.6b` / `pplx-embed-v1-4b`, optional Matryoshka
    `dimensions`, optional `base64_int8` / `base64_binary` encoding).
  - `perplexity_async_create` / `perplexity_async_get` /
    `perplexity_async_list` — submit / poll / list async
    chat-completions jobs (POST/GET `/async/chat/completions`).

  See [`docs/EXTERNAL_INTEGRATION.md`](docs/EXTERNAL_INTEGRATION.md) for
  the full inventory of features studied from upstream projects, what
  was integrated, and what was deliberately skipped.

The filesystem tools are sandboxed to `$QWEN_MCP_FS_ROOT` (default:
server's cwd). Paths that escape via `..` or symlinks are rejected.

- **Backend-agnostic** Qwen client speaking the OpenAI Chat Completions
  protocol — works with vLLM, SGLang, Ollama (OAI shim), DashScope,
  OpenRouter, Together, etc.
- **Auto-continue on length** (loops 254/255) — when the upstream
  finishes with `finish_reason="length"`, both `chat()` and
  `chat_stream()` automatically re-prompt and stitch segments until a
  natural stop, emitting `[truncated: model hit max_tokens]` only when
  the round cap fires. Tunable via `QWEN_AUTO_CONTINUE`,
  `QWEN_AUTO_CONTINUE_MAX_ROUNDS`, `QWEN_AUTO_CONTINUE_PROMPT`.
- **Self-improving agentic loop** (`agent/loop.py`) that:
  1. picks a file from the repo
  2. asks Qwen to find issues
  3. asks Qwen to propose a fix (unified diff)
  4. invokes a separate "devil's advocate" pass to challenge the fix
  5. if the fix survives critique, applies it, commits, and pushes
  6. records every step into `STATE.md` and `.loop/history/`
  7. sleeps briefly and repeats — forever

  Start it from inside the TUI with `/loop start` (loop 258), or
  directly with `python -m agent.loop`.


## Configuration

Copy `.env.example` to `.env` and adjust if needed:

| Variable | Default | Description |
| --- | --- | --- |
| `QWEN_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible endpoint (the bundled vLLM server) |
| `QWEN_API_KEY` | `EMPTY` | API key (use `EMPTY` for the local server) |
| `QWEN_MODEL` | `qwen3.6-27b` | Served model name registered by `serve_qwen.sh` |
| `QWEN_TIMEOUT` | `120` | Request timeout (seconds) |
| `QWEN_MAX_TOKENS` | `16384` | Default max output tokens. Bumped from 8192 (loop 236) so Qwen3-Next has room for its long `<think>...</think>` blocks before truncation. Capped client-side at `QWEN_SERVER_MAX_LEN` minus prompt tokens. With `QWEN_AUTO_CONTINUE=1` (default) hitting the cap no longer ends the answer — the client transparently issues continuation rounds (loop 254). When auto-continue is disabled or the round-cap fires, the marker `[truncated: model hit max_tokens]` is appended so callers can detect the boundary. |
| `QWEN_AUTO_CONTINUE` | `1` | Loop 254: when a chat response comes back with `finish_reason="length"`, append the partial output as an assistant turn and re-call so long answers don't hard-stop at `max_tokens`. Set `0` to fall back to the legacy "append `[truncated: ...]` marker and return" behaviour. |
| `QWEN_AUTO_CONTINUE_MAX_ROUNDS` | `8` | Loop 254: hard ceiling on continuation rounds for a single `chat()` call. Prevents runaway generation. `0` disables auto-continue (equivalent to `QWEN_AUTO_CONTINUE=0`). |
| `QWEN_AUTO_CONTINUE_PROMPT` | `continue exactly where you left off; do not repeat or restart.` | Loop 254: synthetic user nudge sent on each continuation round. Override if a particular model responds better to a different phrasing. |
| `QWEN_RUNS_LOG_MAX_BYTES` | `1048576` | Loop 257: size cap (in bytes) before `.agent/runs.log` is rotated. When the live log exceeds this it's renamed to `runs.log.1` (single-generation, overwriting any prior backup) and a fresh log is started. Set `0` to disable rotation. |
| `QWEN_REPETITION_PENALTY` | `1.05` | Repetition penalty applied to every chat/stream request (loop 238). Qwen3-Next degenerates into n-gram loops at low temperature without one — symptom: the model "repeats itself and doesn't stop" until it hits `max_tokens`. Set `1.0` to disable; `1.10`–`1.20` for aggressive de-looping. |
| `QWEN_AUTO_COMPRESS` | `1` | Loop 240: drop oldest non-protected messages when prompt + completion would overflow `QWEN_SERVER_MAX_LEN`. System messages and the last user message are always preserved. Set `0` to disable (request goes straight through to vLLM, which will 400 on overflow). |
| `QWEN_CONTEXT_RESERVE` | `256` | Loop 240: tokens kept free of prompt + completion as headroom for chat-template overhead (per-message role tags, eot markers). Raise if you see vLLM still 400'ing on edge-case overflows. |
| `QWEN_CHARS_PER_TOKEN` | `3.0` | Loop 240: estimator ratio used for client-side token counting. Code/markdown is ~3 chars/token on Qwen3-Next; English prose is closer to 4. Lower → tighter clamping, more aggressive compression. |
| `QWEN_REAL_TOKENIZER` | unset | Loop 268: HuggingFace model id (e.g. `Qwen/Qwen3-Next-80B-A3B-Instruct`). When set, `_estimate_tokens` lazy-loads `transformers.AutoTokenizer` once (LRU-cached) and uses true token counts for budget gating instead of the char heuristic. Falls back silently to `QWEN_CHARS_PER_TOKEN` math on any tokenizer error. Leave unset to avoid the `transformers` import cost. |
| `QWEN_PER_MESSAGE_TOKENS` | `6` | Loop 241: ChatML wrapper overhead added per message during estimation. Qwen3-Next wraps every message with `<\|im_start\|>role\n...<\|im_end\|>\n` (~4-7 tokens). On 50-turn histories the un-accounted overhead added up to 300+ tokens. Set `0` to disable. |
| `QWEN_COMPRESSION_SUMMARY` | `1` | Loop 243: when context compression drops messages, leave a synthetic `system` message containing `[Earlier in conversation: N message(s) summarized... - role: snippet...]` so the model retains some signal about what was discussed. Set `0` to fall back to silent loop-240 drops. |
| `QWEN_COMPRESSION_SUMMARY_CHARS` | `200` | Loop 243: max characters of each dropped message kept in the summary snippet. Lower → more compact, less context preserved. |
| `QWEN_TASK_MEMORY` | unset | Loop 244: set `1`/`true`/`yes`/`on` to enable the persistent task / todo / facts memory. When enabled, every chat request gets a synthetic system message containing the current task, open todos, and recent decisions — so the model never forgets what it's working on across turns or session restarts. |
| `QWEN_TASK_MEMORY_PATH` | `.agent/context/state.json` | Loop 244: where the JSON state file lives. Atomic writes (tempfile + rename) so an interrupted save can't corrupt it. |
| `QWEN_TASK_MEMORY_MAX_TODOS` | `32` | Loop 244: cap on todo entries. Eviction prefers oldest *done* todos first, then any oldest. |
| `QWEN_TASK_MEMORY_MAX_DECISIONS` | `16` | Loop 244: cap on recent decision entries. FIFO. |
| `QWEN_TASK_MEMORY_MAX_FACTS` | `32` | Loop 244: cap on key→value facts. FIFO by insertion order. |
| `QWEN_DISABLE_THINK_STRIP` | unset | Set `1` to disable stripping of `<think>...</think>` reasoning blocks from assistant content. |
| `LOOP_INTERVAL_SECONDS` | `45` | Sleep between iterations |
| `LOOP_MAX_FILE_BYTES` | `60000` | Skip files larger than this |
| `LOOP_PUSH` | `1` | Set `0` to commit without pushing |
| `PERPLEXITY_API_KEY` | unset | API key for the Perplexity tools (`perplexity_search`, `perplexity_ask`, `perplexity_research`, `perplexity_reason`, `perplexity_embed`, `perplexity_async_*`). Without this the perplexity tools error on first call; the rest of the server still works. |
| `PERPLEXITY_BASE_URL` | `https://api.perplexity.ai` | Override for testing or self-hosted gateways. |
| `PERPLEXITY_TIMEOUT_MS` | `300000` | Request timeout in milliseconds (default 5 min, matches the reference Perplexity MCP server). |
| `PERPLEXITY_PROXY` | unset | HTTP/S proxy URL for Perplexity calls. Falls back to `HTTPS_PROXY` / `HTTP_PROXY` if unset. |

## Running the MCP server

```bash
pip install -e .
qwen-coder-mcp            # speaks MCP over stdio
```

Register it with any MCP-capable client (Claude Desktop, Continue, VS Code's
MCP extension, etc.) by pointing the client at the `qwen-coder-mcp` binary.

### Local-use posture

`qwen-coder-mcp` is intended to be run **on a developer's own machine** (or on
a remote machine the operator owns and trusts), backing a local Qwen model
over stdio. It is **not** intended to be exposed as a public/commercial
service. Following that, the bundled tools intentionally do the kinds of
things VS Code, Continue, and similar local agent tooling let you do:

- **Run shell commands** — `run_shell` (write-mode only) and the
  `git_status` / `git_diff` / `git_log` helpers execute commands in the
  workspace via `/bin/sh`. The agent loop uses the
  `confirm`-callback to gate each destructive call so the operator
  approves it before it fires (the TUI's modal hooks into this).
- **Ask the user for input** — the `ask_user` tool bridges back through
  the TUI (`_AskScreen` modal) so the model can prompt the operator
  mid-run for clarifications, choices, or shell input it doesn't have
  context for. Default-deny on a 2-minute timeout.
- **Fetch data from the internet** — `web_search`, `fetch_url`, and the
  `perplexity_*` tools talk to the open internet. In the MCP server this
  fetch tool is named `fetch_url`; in the TUI agent loop / default tool
  registry, the corresponding tool is named `web_fetch`. `fetch_url` only
  follows `http(s)://` URLs and refuses non-text content types so the
  model can't accidentally dump a binary blob into chat history.
- **Read & edit files in the workspace root** — `fs_read`, `fs_write`,
  `fs_edit`, `apply_patch`, and friends are all sandboxed to the
  `FsConfig.root` directory (no `..` escapes, no absolute paths
  outside the root). Writes go through a tempfile + `os.replace` so
  interrupted saves don't corrupt the destination.

Because this is a local tool and the operator is the threat model author,
the bundled hardening focuses on **correctness and operator-visible
behaviour**, not on defending against a hostile MCP client. If you do
expose this server beyond your own machine, layer your own auth /
allowlist on top — e.g. wrap the binary in an `ssh` session you control,
or set `QWEN_ALLOWED_TOOLS` (your own wrapper) to gate which tools the
remote side can invoke.

### Tool permissions cheat-sheet

| Tool family | Registry | Confirm prompt? | Notes |
| --- | --- | --- | --- |
| `fs_read`, `grep`, `find`, `git_status/diff/log`, `web_search`, `fetch_url`, `http_request` (GET/HEAD) | `DEFAULT_TOOLS` | no | Read-only, available without `/allow_all`. |
| `fs_write`, `fs_edit`, `apply_patch`, `mv`, `cp`, `run_shell`, `http_request` (POST/PUT/DELETE) | `WRITE_TOOLS` / `DESTRUCTIVE_TOOLS` | yes (modal y/n, 30 s default-deny) | Only available after `/allow_all` or `agent_write_default=on`. |
| `ask_user` | always available | n/a | Pops the `_AskScreen` modal; default-deny on a 2-minute timeout. |
| `perplexity_*` | `WRITE_TOOLS` registry (network-touching) | yes | Requires `PERPLEXITY_API_KEY`. |

## Running the agentic loop

```bash
python -m agent.loop      # runs forever, commits + pushes as it goes
```

## Repository layout

```
src/qwen_coder_mcp/   # MCP server + Qwen client
agent/                # Self-improving loop
.loop/                # Runtime state (history, cursor, logs)
STATE.md              # Human-readable rolling state
docs/                 # Design notes
```

## Safety notes

`qwen-coder-mcp` is a **local developer tool**: it deliberately exposes
shell execution, file writes, and outbound HTTP requests so an MCP-capable
editor or the bundled TUI can drive real coding workflows on your machine.
The guard-rails it ships are aimed at operator visibility and correctness,
not at sandboxing a hostile MCP client:

- All write-mode tools (`fs_write`, `apply_patch`, `run_shell`,
  `http_request` for mutating methods, etc.) are gated behind the
  `confirm` callback. The TUI wires that callback to a y/n modal with a
  30-second default-deny timeout (see [`docs/AGENT_CHECKPOINTS.md`](docs/AGENT_CHECKPOINTS.md)
  for the full flow).
- File operations are confined to `FsConfig.root`; relative paths that
  escape via `..` are rejected and absolute paths outside the root are
  refused.
- `run_shell` runs through `/bin/sh` with a deny-list of obviously
  destructive patterns (`rm -rf /`, `mkfs`, `shutdown`, fork-bombs,
  etc.) and a per-call timeout / output cap so a runaway command can't
  wedge the agent loop.
- Secrets (`QWEN_API_KEY`, `PERPLEXITY_API_KEY`) are read from
  environment / `.env` and **kept out of `Settings.__repr__`** so an
  accidental `print(settings)` or unhandled traceback doesn't echo
  them into the TUI scrollback or local log.

The self-improving agentic loop only modifies files inside this repo, runs
unified diffs through `git apply --check` first, runs `python -m compileall`
on touched Python files, and rolls back on any failure. Commits are atomic
per accepted fix.

If you plan to run `qwen-coder-mcp` on a remote machine you trust, prefer
fronting it with `ssh` rather than exposing the stdio transport on a
network socket — that's outside the project's threat model.

## Runtime introspection

While the loop is running, send it `SIGUSR1` to dump a snapshot of every
rate-limited swallow logger to `runtime.log`. This is the fastest way to
diagnose "the loop is iterating but something is silently failing":

```bash
# Find the loop process and signal it (POSIX only; SIGUSR1 has no Windows equivalent)
pkill -USR1 -f "agent.loop"
```

The dump includes the current iteration number, every logger's cumulative
count and last-logged message, and the cached delta-summary state. None of
the dumped data is sensitive (no diff content, no model output), so the dump
is safe to grep for in shared logs.

Tunables relevant to introspection:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `QWEN_AGGREGATE_SUMMARY_EVERY` | `100` | Iterations between cumulative swallow-logger summaries (0 disables). Capped at 100k. |
| `QWEN_TIMING_MAX_BYTES` | `1_000_000` | Size cap for `.loop/timing.log` before rotation. Capped at 100MB. |

## Iteration outcome schema

Every loop iteration ends with an outcome string that maps to a stable
leading category (the part before the first `:`). `_outer_outcome_category`
extracts this for log aggregation; the full set of valid leading categories
is `OUTER_OUTCOME_CATEGORIES` in `agent/loop.py`. The categories are:

| Category | Meaning |
| -------- | ------- |
| `applied` | Diff applied, validated, committed, pushed. |
| `clean` | Reviewer found no actionable issue in the file. |
| `skip` | File was unreadable or too large. |
| `rejected` | Devil's-advocate rejected the proposed fix. |
| `out_of_scope` | Diff touched paths outside the iteration's scope. |
| `validation_failed` | Apply succeeded but the post-apply validator (compileall, json/toml/yaml parse, etc.) found the result invalid. |
| `commit_failed` | Local commit failed after a clean apply. |
| `commit_skipped_empty` | Apply produced no committable changes (empty staged tree). |
| `revert_failed` | After a rejection or validation failure, the revert path (clean + reset) couldn't restore a clean state. |
| `apply_failed` | `git apply` rejected the diff. The outcome is shaped `apply_failed:<sub_category>:<file>:<msg>` where `<sub_category>` is one of the values in `agent.loop.APPLY_ERROR_CATEGORIES`: `not_a_unified_diff`, `oversized_diff`, `unsafe_path`, `binary_patch`, `unsafe_mode`, `malformed_diff`, `dir_conflict`, `apply_check_failed`, `apply_failed`. The `<msg>` tail is truncated to the first 60 characters of the underlying error so the outcome stays single-line. |
| `qwen_error_find_bugs` / `qwen_error_propose_fix` / `qwen_error_devils_advocate` | Backend failure on one of the three Qwen calls. |
| `budget_exceeded` | Per-iteration wall-clock budget (`QWEN_LOOP_ITER_BUDGET_S`) was exceeded between phases. |
| `no_candidate_files` | `_candidate_files()` returned an empty list (no eligible files). |
| `crashed` | `_iteration` raised an unhandled exception; the main loop's crash branch synthesized this record. |
| `exit` | Synthetic shutdown record emitted by `_write_timing_exit` when the autonomous loop terminates (loop 226). The full outcome string is `exit:<reason>` where `<reason>` is one of `sigterm`, `keyboard-interrupt`, `system-exit`, or `unhandled-exception`. The record carries an `iteration_count` field that joins to the `loop exit ... iter=N` line in `runtime.log`. Records `phases: {}` because no phase ran on the shutdown path. |

Each record in `.loop/timing.log` is a JSON line with:

- `ts` (ISO timestamp), `file`, `outcome`, `category` (the leading token),
  `phases` (per-phase wall-clock seconds), `wall_s` (whole-iteration
  wall-clock seconds; emitted only when `iter_monotonic` was provided),
  and `wall_s_delta_phases` (`wall_s - sum(phases)`, floored at 0; flags
  unaccounted-for time outside the named phases).

The named phases inside `phases` are: `discovery` (file selection +
candidate read; emitted on iterations that find a real candidate),
`find_bugs`, `propose_fix`, and `devils_advocate` (the three Qwen
calls), `apply_diff` (running `git apply` on the proposed diff),
`validate` (post-apply syntax/structural checks on changed files),
and `commit_push` (the final `git commit` and optional `git push`),
and `revert` (rolling back uncommitted changes after a rejection,
out-of-scope diff, validation failure, or commit failure).
Early-exit outcomes (`no_candidate_files`, `skip:...`, `crashed`)
emit `phases: {}` -- no phase ran to completion in those records.

Example timing.log line for an applied fix (one JSON object per
line, pretty-printed here for readability):

```json
{
  "ts": "2026-04-28T05:00:00Z",
  "file": "agent/loop.py",
  "outcome": "applied:agent/loop.py",
  "category": "applied",
  "phases": {
    "discovery": 0.012,
    "find_bugs": 4.31,
    "propose_fix": 6.07,
    "devils_advocate": 3.84,
    "apply_diff": 0.041,
    "validate": 0.118,
    "commit_push": 0.692
  },
  "wall_s": 15.21,
  "wall_s_delta_phases": 0.13
}
```

Example for a `validation_failed` iteration (includes the `revert`
phase that doesn't appear on the happy path):

```json
{
  "ts": "2026-04-28T05:01:14Z",
  "file": "agent/loop.py",
  "outcome": "validation_failed:agent/loop.py:py_invalid",
  "category": "validation_failed",
  "phases": {
    "discovery": 0.011,
    "find_bugs": 3.92,
    "propose_fix": 5.41,
    "devils_advocate": 3.16,
    "apply_diff": 0.038,
    "validate": 0.082,
    "revert": 0.064
  },
  "wall_s": 12.79,
  "wall_s_delta_phases": 0.09
}
```

Example for a `no_candidate_files` early-exit (no file selected, so
`phases` is empty -- only the iteration-level wall clock is recorded):

```json
{
  "ts": "2026-04-28T05:02:09Z",
  "file": "",
  "outcome": "no_candidate_files",
  "category": "no_candidate_files",
  "phases": {},
  "wall_s": 0.083,
  "wall_s_delta_phases": 0.083
}
```

### Analysing timing.log

The `agent.timing_analyze` module is a tiny CLI that summarises
records in `.loop/timing.log` -- per-category counts and per-phase
wall-clock stats (count, total, mean, p50, p95). It tolerates
partially-corrupt logs (e.g., a half-written final line from a
rotation race).

```sh
# human-readable text report
python -m agent.timing_analyze

# machine-readable JSON for dashboards
python -m agent.timing_analyze --json

# point at a non-default location
python -m agent.timing_analyze --file /path/to/timing.log

# only the live slot (skip rotated .1 file)
python -m agent.timing_analyze --no-rotated

# show only the 5 slowest phases by p95 wall-clock
python -m agent.timing_analyze --top-n 5

# only records since a specific timestamp (lexicographic ISO-8601 compare)
python -m agent.timing_analyze --since 2026-04-28T00:00:00Z

# closed interval (since + until)
python -m agent.timing_analyze --since 2026-04-28T00:00:00Z --until 2026-04-29T00:00:00Z

# only one outcome category, or only iterations that ran a specific phase
python -m agent.timing_analyze --category applied
python -m agent.timing_analyze --phase devils_advocate

# scope to the current run only -- ignore everything up to and
# including the last exit:<reason> shutdown breadcrumb (loop 231).
# Pairs with the loop-226 synthetic exit records produced when
# the autonomous loop receives SIGTERM, KeyboardInterrupt,
# SystemExit, or an unhandled exception. Composes with every
# other filter -- --since-last-exit runs first, then --since /
# --until / --category / --phase narrow further.
python -m agent.timing_analyze --since-last-exit
python -m agent.timing_analyze --since-last-exit --category applied
```

The text report ends with a `shutdown records` section listing
every `exit:<reason>` breadcrumb with its `iteration_count` and
`pid` fields, which together join to the corresponding
`loop exit reason=R | iter=N | pid=P` line in `runtime.log`
(loops 229/233). The `--json` report exposes this as a
top-level `exit_records` array of dicts with the exact keys
`{ts, reason, iteration_count, pid}` (loops 230/234 contract pin
-- both `iteration_count` and `pid` are `null` for records that
predate the corresponding schema version). The composite
`(pid, iteration_count)` key disambiguates simultaneous loops
running in different repos -- without `pid`, two processes
would collide on `iteration_count` alone.
