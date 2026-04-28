# Loop 188 candidates

1. **`/lat n`** — show the n most recent turns (TurnProfile ring buffer).
2. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
3. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
4. **`/checkpoints diff N`** — diff between current chat history and snapshot N.
5. **`format_turn_profile` honours TTY width** — wrap the tools table to 80 chars by default.

(1) is the natural pick — it makes `/lat` a real performance-debugging tool rather than a single-frame snapshot.
