# Loop 189 candidates

1. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
2. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
3. **`/checkpoints diff N`** — diff between current chat history and snapshot N.
4. **`format_turn_profile` honours TTY width** — wrap the tools table.
5. **`/lat reset`** — clear the turn-profile ring buffer.

(1) is the natural integrity-hardening pick — every other persistence point in the agent layer now has atomic writes.
