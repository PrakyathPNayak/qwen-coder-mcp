# Loop 170 candidates

1. **Token meter accounting** for tool_result body bytes in the status
   footer so users see why their context fills up.
2. **Auto-checkpoint every N agent turns** to `.agent/agent_state.json`.
3. **Streaming tail bytes-budget** -- the 2000-char tail in
   `_on_stream_chunk` truncates mid-word; align to nearest space.
4. **Per-loop devil's advocate prompt** -- when the agent claims a fix
   is done, force one extra turn with a system reminder to argue
   against itself before the final answer lands.
5. **`/tools` slash command** -- print the active tool registry so users
   can see what the agent currently has access to.

Empirical question still open: live Qwen3.6-27B `<tool_call>` reliability.
