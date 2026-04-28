# Loop 213 candidates

1. **End-to-end TUI smoke test against httpx.MockTransport** — carried 6x now; JSON arc done; serve-launcher arc has matured; the TUI is the next dry-run-vs-reality gap. Walk slash commands and a chat turn, assert no exceptions.
2. `/checkpoints export N <path> --gzip`
3. `/sysinfo --json --probe` — active vLLM `/health` probe.
4. Add similar pairing-invariant audit to `agent/run_agent.py` argv (mirror of loop 212 lessons).
5. Live vLLM smoke test of `<tool_call>` syntax (env-dependent, carried since 164).

**Recommended:** (1) — six-time deferral is conspicuous. The TUI is real surface area we have not stressed.
