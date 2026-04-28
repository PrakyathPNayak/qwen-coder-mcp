# Loop 172 candidates

1. **Token meter — count tool_result body bytes.** Currently the TUI status bar only counts user/assistant tokens; large `fs_read` results invisibly inflate the prompt. Tally them and surface in the footer.
2. **Auto-checkpoint agent state.** Persist transcript + tool history to `.agent/agent_state.json` every N turns so an interrupt doesn't lose multi-step work.
3. **Live vLLM smoke test of `<tool_call>` protocol.** Long-standing empirical question: does Qwen3.6-27B reliably emit the syntax we parse? Add an opt-in integration test that hits a running server.
4. **Streaming tail bytes-budget — align to nearest space.** The 2000-char tail in stream rendering can chop mid-token; round to whitespace.
5. **Per-loop devil's advocate prompt.** Add a `--devil` flag to `run_agent` that injects a critic turn before the final answer.

Pick the highest-impact one next loop. Token meter (1) probably wins — the 64k window we just unlocked makes accurate accounting more important, not less.
