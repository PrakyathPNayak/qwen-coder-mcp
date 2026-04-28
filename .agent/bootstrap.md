# Bootstrap analysis — qwen-coder-mcp

Read on 2026-04-28. Repo is the very repo this agent is improving (meta).

## What it is
Python project (`pyproject.toml`, src layout) implementing two artifacts:
1. **MCP stdio server** (`src/qwen_coder_mcp/server.py`) exposing 9 coder tools
   backed by Qwen3.6-27B via an OpenAI-compatible HTTP endpoint.
2. **Self-improving agent loop** (`agent/loop.py`) that audits this repo, asks
   the model to find bugs, generates a unified diff fix, runs a devil's
   advocate pass, applies/commits/pushes if accepted.

Plus: vLLM serve scripts targeting a local RTX 4090 (int4 AutoRound, ~14 GB).

## Stack
- Python ≥ 3.10, src layout.
- Deps: `mcp>=1.2`, `httpx>=0.27`, `pydantic>=2.6`, `python-dotenv`, `anyio`.
- **No test suite, no CI, no lint config.**

## Honest assessment of weaknesses (priority-ordered)

### Priority 3 — test gaps on existing functionality (catastrophic)
The repo has **zero tests**. Every utility in `agent/loop.py` is critical-path
with subtle parsing logic and no verification. Specifically:
- `_strip_fence` — regex requires the *entire* input to be one fenced block
  (anchored with `$`), so any prose around the fence falls through to the raw
  text.
- `_parse_first_issue` — fragile to numbered lists that skip "2.".
- `_verdict_accepts` — case-insensitive but depends on substring match;
  brittle if model says "VERDICT: ACCEPT" inside a quoted critique.
- `_apply_diff` — only accepts diffs that start with `diff --git` or `--- `;
  rejects valid `Index:`-prefixed diffs the model might emit.
- `_python_syntax_ok` — only validates `.py`. **JSON/YAML/TOML changes can be
  silently corrupted and committed.**

### Priority 4 — logic bugs / robustness
- `_commit_and_push` doesn't abort a failed `git pull --rebase`; the working
  tree can be left half-rebased, breaking every subsequent iteration.
- `_iteration` doesn't validate that the model's diff targets the file we
  asked about — model could rewrite some other file silently.
- `Settings.loop_push` env parsing only recognises `0/false/False/no`; misses
  `NO`, `FALSE`, `off`, etc.
- `scripts/serve_qwen.sh` line 70 passes the model id twice as
  `--served-model-name`; works (registers two aliases) but misleading.

### Priority 6 — interface inconsistencies
- `server.py` imports `ChatMessage` and never uses it.
- `prompts.py` returns prompts that wrap user code in raw triple backticks
  with no escape — code that itself contains ``` ``` ``` will confuse the
  model.

### Priority 8 — fragile assumptions
- `_candidate_files` walks the whole working tree using suffix filtering;
  doesn't honor `.gitignore`. Will eventually try to scan committed
  artifacts that the user expects to be ignored.
- `STATE.md` is appended every iteration with no rotation/cap; will grow
  unboundedly across the lifetime of the loop.
- `.loop/history/` written as `int(time.time())-<outcome>.md`; iterations
  inside the same second will collide.

## Plan of attack
- **Loop 1**: introduce a real test harness (pytest) and tests for the
  highest-risk parser utilities. This unblocks every later refactor.
- **Loop 2**: fix `_strip_fence` to handle prose-around-fence and add tests.
- **Loop 3**: harden `_python_syntax_ok` into format-aware
  `_validate_changed` covering `.py`, `.json`, `.toml`, `.yaml`.
- **Loop 4**: make `_commit_and_push` abort failed rebase and revert.
- **Loop 5+**: scope-validation, bounded `STATE.md`, collision-free history
  filenames, gitignore-aware candidates, etc.
