# Loop 207 candidates

1. **End-to-end TUI smoke test against fake-vLLM HTTP fixture** (carried from 206 candidates) — same dry-run-vs-reality gap that bit `serve_qwen.sh`. Highest-leverage candidate; the more interesting infra investment.
2. **`/checkpoints export N <path> --gzip`** — compressed archive variant.
3. **`/tokens --json --top K`** — top-K heaviest messages.
4. **Live vLLM smoke test** — env-dependent.
5. **`/help <term> --regex`** — escape hatch for regex patterns.

**Recommended:** (1) — the `--help=all` validator we shipped in loop 205 proves end-to-end harnesses pay off. The TUI doesn't have one.
