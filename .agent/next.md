# Loop 187 candidates

1. **`/lat n`** — show the n most recent turns (TurnProfile ring buffer).
2. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
3. **Document `/agent --resume`** in AGENT_CHECKPOINTS.md.
4. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).
5. **`/checkpoints diff N`** — diff between current chat history and snapshot N.

(3) is a small but obvious pairing with this loop — the new flag isn't in the checkpoint doc yet.
