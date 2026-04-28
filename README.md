# qwen-coder-mcp

A Model Context Protocol (MCP) server backed by **Qwen3.6-27B running
locally on a single RTX 4090** via vLLM (4-bit AutoRound, OpenAI-compatible),
focused on coding workflows, plus a self-improving **agentic loop** that
continuously audits this repository, proposes fixes, plays devil's advocate
against its own proposals, and commits accepted changes.

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

Slash commands: `/help`, `/search <q>`, `/fetch <url>`, `/read <path>`,
`/ls [path]`, `/find_bugs <path>`, `/explain <path>`, `/apply`,
`/history [n]`, `/diff <a> <b>`, `/quit`. Anything not starting with
`/` is sent to Qwen as a chat message with multi-turn memory preserved
within the session.

`/apply` extracts the first unified diff from the assistant's last
reply (looking for a ```diff or ```patch fence first, then a bare
`diff --git` header) and runs `git apply --check` before actually
applying. `/history [n]` shows the last n turns (default 10).

(continued tools list below)
  - `web_search` — DuckDuckGo HTML web search (no API key)
  - `fetch_url` — fetch a URL's text body (binary content refused, byte-capped)
  - `read_file` — read a file from the configured repo root
  - `list_dir` — list a directory inside the repo root
  - `write_file` — write a file inside the repo root (utf-8)
  - `apply_patch` — apply a unified diff via `git apply` (supports `check_only`)

The filesystem tools are sandboxed to the directory pointed to by
`$QWEN_MCP_FS_ROOT` (default: server's cwd). Paths that escape via
`..` or symlinks are rejected.
- **Backend-agnostic** Qwen client speaking the OpenAI Chat Completions
  protocol — works with vLLM, SGLang, Ollama (OAI shim), DashScope,
  OpenRouter, Together, etc.
- **Self-improving agentic loop** (`agent/loop.py`) that:
  1. picks a file from the repo
  2. asks Qwen to find issues
  3. asks Qwen to propose a fix (unified diff)
  4. invokes a separate "devil's advocate" pass to challenge the fix
  5. if the fix survives critique, applies it, commits, and pushes
  6. records every step into `STATE.md` and `.loop/history/`
  7. sleeps briefly and repeats — forever

## Configuration

Copy `.env.example` to `.env` and adjust if needed:

| Variable | Default | Description |
| --- | --- | --- |
| `QWEN_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible endpoint (the bundled vLLM server) |
| `QWEN_API_KEY` | `EMPTY` | API key (use `EMPTY` for the local server) |
| `QWEN_MODEL` | `qwen3.6-27b` | Served model name registered by `serve_qwen.sh` |
| `QWEN_TIMEOUT` | `120` | Request timeout (seconds) |
| `QWEN_MAX_TOKENS` | `16384` | Default max output tokens. Bumped from 8192 (loop 236) so Qwen3-Next has room for its long `<think>...</think>` blocks before truncation. Capped client-side at `QWEN_SERVER_MAX_LEN` minus prompt tokens. With `QWEN_AUTO_CONTINUE=1` (default) hitting the cap no longer ends the answer — the client transparently issues continuation rounds (loop 254). |
| `QWEN_AUTO_CONTINUE` | `1` | Loop 254: when a chat response comes back with `finish_reason="length"`, append the partial output as an assistant turn and re-call so long answers don't hard-stop at `max_tokens`. Set `0` to fall back to the legacy "append `[truncated: ...]` marker and return" behaviour. |
| `QWEN_AUTO_CONTINUE_MAX_ROUNDS` | `8` | Loop 254: hard ceiling on continuation rounds for a single `chat()` call. Prevents runaway generation. `0` disables auto-continue (equivalent to `QWEN_AUTO_CONTINUE=0`). |
| `QWEN_AUTO_CONTINUE_PROMPT` | `continue exactly where you left off; do not repeat or restart.` | Loop 254: synthetic user nudge sent on each continuation round. Override if a particular model responds better to a different phrasing. |
| `QWEN_REPETITION_PENALTY` | `1.05` | Repetition penalty applied to every chat/stream request (loop 238). Qwen3-Next degenerates into n-gram loops at low temperature without one — symptom: the model "repeats itself and doesn't stop" until it hits `max_tokens`. Set `1.0` to disable; `1.10`–`1.20` for aggressive de-looping. |
| `QWEN_AUTO_COMPRESS` | `1` | Loop 240: drop oldest non-protected messages when prompt + completion would overflow `QWEN_SERVER_MAX_LEN`. System messages and the last user message are always preserved. Set `0` to disable (request goes straight through to vLLM, which will 400 on overflow). |
| `QWEN_CONTEXT_RESERVE` | `256` | Loop 240: tokens kept free of prompt + completion as headroom for chat-template overhead (per-message role tags, eot markers). Raise if you see vLLM still 400'ing on edge-case overflows. |
| `QWEN_CHARS_PER_TOKEN` | `3.0` | Loop 240: estimator ratio used for client-side token counting. Code/markdown is ~3 chars/token on Qwen3-Next; English prose is closer to 4. Lower → tighter clamping, more aggressive compression. |
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

## Running the MCP server

```bash
pip install -e .
qwen-coder-mcp            # speaks MCP over stdio
```

Register it with any MCP-capable client (Claude Desktop, Continue, etc.) by
pointing the client at the `qwen-coder-mcp` binary.

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

The loop only modifies files inside this repo, applies unified diffs through
`git apply --check` first, runs `python -m compileall` on touched Python files,
and rolls back on any failure. Commits are atomic per accepted fix.

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
