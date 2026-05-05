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
