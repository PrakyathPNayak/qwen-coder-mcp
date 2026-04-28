# Loop 167 candidates

1. **Blocking interactive y/n modal** for destructive tool calls. The
   confirm hook is now in place; TUI just needs a `threading.Event`
   round-trip: worker pushes the request, UI pops a Confirm widget,
   resolves the event, worker continues. Bind y/n keys + a 30s
   default-deny timeout.
2. **`run_shell` tool** behind write-mode + confirm + allow-list.
3. **`/agent --max <n>`** -- override hardcoded 6-step cap from CLI.
4. **Token meter accounting** for tool_result body bytes in the status
   footer.
5. **Auto-checkpoint every N agent turns** to `.agent/agent_state.json`.

Empirical question still open: live vLLM smoke test of the
`<tool_call>` protocol with Qwen3.6-27B.
