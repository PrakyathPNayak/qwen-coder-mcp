# Loop 165 candidates
1. Streaming AGENT mode: model token stream during agent (currently non-streamed inside the loop).
2. apply_patch / write_file tools so the agent can edit the repo (needs confirmation).
3. /agent --max <n> to bound steps per call.
4. Auto-save every N agent turns.
5. Token meter should count tool_result body bytes.
