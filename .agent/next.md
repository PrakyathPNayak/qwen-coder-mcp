# Loop 211 candidates

1. **End-to-end TUI smoke test** (carried 4x) — drive App against `httpx.MockTransport`, walk slash commands and a chat turn, assert no exceptions. **Now top priority** — JSON-export work has effectively saturated; the next big leverage is closing the dry-run-vs-reality gap at the TUI level.
2. **`/checkpoints export N <path> --gzip`** — compressed archive variant.
3. **`/sysinfo --json --probe`** — actively check vLLM `/health`.
4. **`/lat --json --top K --by-role`** — extend the by-role pattern to latency tool-call buckets.
5. **Live vLLM smoke test** — env-dependent, defer.

**Recommended:** (1) — finally tackle the TUI E2E. The JSON-export pattern arc (lat/tokens/sysinfo + top/by-role) is now mature; further tweaks have diminishing returns.
