# Loop 168 candidates

1. **`run_shell` tool** behind write-mode + confirm + a small allow-list
   (pytest, ruff, git status, ls, cat, grep -- no rm/mv/curl). The
   confirm gate now exists, so the missing piece is just the tool fn
   plus extending DESTRUCTIVE_TOOLS.
2. **`/agent --max <n>`** -- override hardcoded 6-step cap from CLI.
3. **Token meter accounting** for tool_result body bytes in the status
   footer.
4. **Auto-checkpoint every N agent turns** to `.agent/agent_state.json`.
5. **Streaming tail bytes-budget** -- the 2000-char tail in
   `_on_stream_chunk` truncates mid-word; align to nearest space.

Empirical question still open: does live Qwen3.6-27B reliably emit
`<tool_call>`? Need a manual smoke test against the vLLM server.
