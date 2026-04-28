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
