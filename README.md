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
quantizations, and the llama.cpp fallback.

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
| `QWEN_MAX_TOKENS` | `4096` | Default max output tokens |
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
```
