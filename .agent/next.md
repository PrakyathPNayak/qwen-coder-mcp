# Loop 182 candidates

1. **`/lat` slash command** — last-turn timing breakdown (TTFT + per-tool list + summary) using events from loops 175-178.
2. **`/agent --resume`** — start an agentic turn with the latest checkpoint pre-loaded.
3. **Configurable `keep` for rotation** — env-var or config setting; currently hardcoded to 5.
4. **Atomic write of audit log** — same `.tmp + os.replace` treatment we gave checkpoints.
5. **Live vLLM smoke test** of `<tool_call>` protocol (opt-in, `pytest -m live`).

(1) is the natural pick — observability events have been emitted since loop 175 but there's no command to inspect a single turn's profile.
