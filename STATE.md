# qwen-coder-mcp — Rolling State

This file is appended to by the agentic loop. Each entry records one iteration.

## Loop 292 — tool-continuation visibility and streamed-think cleanup

**OBSERVE**: The operator reported that TUI generation appeared to stop after
tool calls. A live Qwen3.6-27B probe against the running vLLM server showed the
core `run_agent` loop continued across `fs_read`, but streamed replies leaked
unwrapped reasoning ending in `</think>` and the UI had no explicit status
between a tool result and the next model turn.

**ORIENT**: Two adjacent failure modes can look like "stopped after tool":
1. a streamed model turn with only hidden reasoning becomes an empty visible
   reply and was treated as final;
2. after a tool result, the TUI reset the live widget to a bare ellipsis while
   Qwen spent time thinking, so long hidden reasoning looked like a freeze.

**DECIDE**: sanitize complete streamed turns with the batch think-stripper before
tool parsing/history persistence, retry empty visible turns with an explicit
continuation nudge, and emit/render a `model_start` event after tool feedback.
Also refresh the static system-prompt tool summary so it includes the newer
`http_request`, `json_query`, `env_get`, and `cp` tools; the dynamic catalog was
already accurate.

**DEVIL**: Sanitizing after streaming cannot prevent already-emitted live chunks
from momentarily appearing, but `_start_agent_turn` clears the live buffer on
assistant-turn boundaries and final/history output is now clean. Retrying empty
visible turns could burn steps if the model repeatedly emits only thoughts, but
`max_steps` bounds it and surfacing a final empty answer was strictly worse.

**ACT**: Added targeted tests for streaming dangling-think cleanup, empty-visible
retry, and `model_start` sequencing. Re-ran the live Qwen probe: event sequence
is now tool call -> tool result -> model_start step 2 -> final, with no hidden
reasoning in the final answer.

## Loop 293 — enforce HTTP mutation approval boundary

**OBSERVE**: `TOOL_BLURBS["http_request"]` said POST/PUT/DELETE/PATCH require
write-mode and Copilot approval, but `DEFAULT_TOOLS` exposed the full
implementation. A read-only agent could therefore attempt mutating HTTP methods
without entering the same approval path used by filesystem and shell tools.

**ORIENT**: This was a prompt/registry contract bug and a permission-boundary
bug. It was not enough to update text; the registry needed separate read-only
and write-mode behavior under the same tool name so the dynamic catalog remains
simple for the model.

**DECIDE**: Add a read-only wrapper for `http_request` in `DEFAULT_TOOLS` that
allows GET/HEAD/OPTIONS and rejects mutating methods before network I/O. Add the
full implementation to `WRITE_TOOLS`, let `ALL_TOOLS` override the wrapper, and
make confirmation conditional on the requested HTTP method so safe GET calls in
write mode do not pop unnecessary prompts.

**DEVIL**: Method-level confirmation is more complex than the old static
`DESTRUCTIVE_TOOLS` check, but without it either read-only mode is unsafe or
safe GET calls become noisy in write mode. Keeping the same public tool name
avoids teaching the model two nearly identical HTTP tools.

## Loop 294 — parse Qwen newline tool-call JSON and sanitize plain streams

**OBSERVE**: The real-model benchmark had no scenario errors but exposed visible
reasoning in plain chat heads. A TUI-like live probe produced an even more
actionable failure: Qwen emitted an unwrapped `</think>` followed by a
`<tool_call>` whose `fs_write.content` JSON string contained literal newlines.
The old parser dropped that block as malformed JSON, so plain streaming could
display a raw tool call instead of switching into agent mode.

**ORIENT**: This directly explains a class of "tool calls stop / do nothing"
reports: the model did request a tool, but the parser rejected the common
Qwen-shaped JSON and the TUI finalized the response as normal text.

**DECIDE**: Retry `json.loads(..., strict=False)` for tool-call blocks after the
strict parse fails, which accepts literal control characters inside JSON strings
without broadly inventing a custom parser. Also sanitize `_finalize_stream` and
committed `chat_turn_stream` history with `_strip_think_blocks` so unwrapped
thinking does not affect tool detection or final display.

**DEVIL**: `strict=False` is permissive, but it is limited to blocks already
inside explicit `<tool_call>` tags and still requires syntactically valid JSON
objects otherwise. That is a safer compatibility fix than regex-editing string
contents by hand.

**ACT**: Added parser and TUI streaming tests. Live probe now parses the
newline-containing `fs_write` call from sanitized history.
