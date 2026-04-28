# Loop 186 candidates

1. **`/agent --resume`** — start an agentic turn with the latest checkpoint pre-loaded.
2. **`/lat n`** — show the n most recent turns (TurnProfile ring buffer).
3. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **`/checkpoints diff N`** — diff between current chat history and snapshot N.

(1) is the natural pick — it threads the checkpoint helpers into the agent-launch path, completing the recovery story.
