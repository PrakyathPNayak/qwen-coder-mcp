# Loop 208 candidates

1. **End-to-end TUI smoke test** — actually drive the App against a fake-vLLM `httpx.MockTransport`, walk through `/tokens`, `/lat`, `/checkpoints list`, then a chat turn, assert no exceptions. This is the dry-run-vs-reality gap closer at the TUI level. Same pattern that caught the `--swap-space` regression in loop 205.
2. **`/checkpoints export N <path> --gzip`** — compressed archives.
3. **`/help <term> --regex`** — escape hatch for regex patterns.
4. **`/lat --json --top K`** — same shape as loop 207 applied to turn-profile latency.
5. **Live vLLM smoke test** — env-dependent.

**Recommended:** (1) — biggest infra leverage. The `--help=all` validator pattern from loop 205 proves end-to-end harnesses pay off; the TUI doesn't have one yet.
