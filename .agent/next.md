# Loop 209 candidates

1. **End-to-end TUI smoke test** (carried twice) — drive the App against `httpx.MockTransport`, walk slash commands and a chat turn, assert no exceptions. Highest-leverage candidate; covers the dry-run-vs-reality gap at the TUI level.
2. **`/checkpoints export N <path> --gzip`** — compressed archive variant.
3. **`/help <term> --regex`** — regex escape hatch.
4. **`/sysinfo --json --probe`** — actively check vLLM `/health` endpoint (separate from passive sysinfo).
5. **Audit `qwen_client.py` for httpx 0.28+ deprecations** — same drift class as the vLLM regression.

**Recommended:** (1) — second-time carried; the JSON-export trio is now complete (lat/sysinfo/tokens all have --json with top-K where meaningful), so the next big leverage is closing the TUI dry-run gap.
