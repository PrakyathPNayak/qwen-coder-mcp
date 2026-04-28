# Architecture

## MCP server (`src/qwen_coder_mcp/`)
- `qwen_client.py`: thin OpenAI Chat Completions client with retries.
- `prompts.py`: shared system + user prompt templates.
- `server.py`: MCP stdio server registering nine coding tools.
- `config.py`: env-driven settings (Qwen endpoint + loop knobs).

## Agentic loop (`agent/loop.py`)
A finite-state machine repeated forever:

```
pick file -> find_bugs -> parse top issue
        |-> NO_ISSUES -> rotate cursor, sleep
        '-> propose_fix (unified diff)
              -> devils_advocate critique
                    |-> REJECT -> log, rotate, sleep
                    '-> ACCEPT -> git apply --check
                                  -> git apply
                                  -> python -m compileall (touched .py)
                                  -> commit -> pull --rebase -> push
                                  -> log applied, sleep
```

All errors are caught at the iteration boundary; the outer loop never exits.

## State surfaces
- `STATE.md` — append-only human log.
- `.loop/history/<ts>-<outcome>.md` — full per-iteration record (issue, diff, critique).
- `.loop/cursor.json` — rotating file index.
- `.loop/runtime.log` — process log.

## Safety rails
- Excludes `.git`, `.loop`, virtualenvs, hidden dirs.
- Skips files larger than `LOOP_MAX_FILE_BYTES`.
- Validates diffs with `git apply --check` before applying.
- Compiles touched `.py` files; reverts on syntax error.
- `git pull --rebase --autostash` before push to avoid lost commits.
