# Loop 169 candidates

1. **`/agent --max <n>`** -- override the hardcoded 6-step cap; long
   debugging sessions can need 20+ steps.
2. **Token meter accounting** for tool_result body bytes in the status
   footer so users see why their context fills up.
3. **Auto-checkpoint every N agent turns** to `.agent/agent_state.json`
   so a crash mid-loop doesn't lose context.
4. **Streaming tail bytes-budget** -- the 2000-char tail in
   `_on_stream_chunk` truncates mid-word; align to nearest space.
5. **Per-loop devil's advocate prompt** -- when the agent claims a fix
   is done, force one extra turn with a system reminder to argue
   against itself before the final answer lands.

Empirical question still open: live Qwen3.6-27B `<tool_call>` reliability.
