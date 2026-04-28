# Loop 181 candidates

1. **`/lat` slash command** — pretty-print last-turn timing (TTFT + per-tool list + summary) using events emitted by loops 175-178.
2. **Auto-load latest checkpoint on TUI boot** — if `agent_state.json` is missing/corrupt, fall back to newest rotation in `checkpoints/`.
3. **`/agent --resume`** — start a turn with the last checkpoint pre-loaded.
4. **Configurable `keep` for rotation** — currently hardcoded to 5 in TUI; expose via env or config setting.
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(2) is the natural follow-up — the rotated fallback exists in helper form but isn't wired into the boot path, so a corrupt primary still leaves users dead in the water until they run `/checkpoints load`.
