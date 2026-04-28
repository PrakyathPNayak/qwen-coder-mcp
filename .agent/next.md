# Loop 190 candidates

1. **`/lat reset`** — clear the turn-profile ring buffer mid-session.
2. **`/checkpoints diff N`** — diff between current chat history and snapshot N.
3. **`format_turn_profile` honours TTY width** — wrap the tools table when the terminal is narrow.
4. **Atomic write for `save_agent_state`** — verify and harden if missing.
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(1) is small-scope but useful; (2) is meatier and gives users a way to see what they'd lose by resuming; (3) is pure cosmetics. Lean (1) first to keep the cadence, then (2).
