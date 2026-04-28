# Loop 166 candidates

1. **Confirmation prompt for write tools.** `apply_patch` and `fs_write`
   currently fire silently. Add a TUI confirmation modal (or at minimum a
   one-key Y/N prompt) before any write tool runs in interactive mode.
2. **`/agent --max <n>`** to override the hardcoded 6-step cap.
3. **Token meter accounting**: include tool_result body bytes in the
   status footer's input-token estimate so users see why their context is
   filling up.
4. **`run_shell` tool** (sandboxed via existing `shell_tools`) so the
   agent can run tests, lint, etc. Highest risk — must be write-mode-gated
   AND have an allow-list.
5. **Auto-checkpoint every N agent turns** (write `.agent/agent_state.json`)
   so a crash mid-loop doesn't lose context.

Empirical question still open: does Qwen3.6-27B reliably emit
`<tool_call>` blocks given TOOL_PROTOCOL_DOC? Need a manual smoke test
against the live vLLM server.
