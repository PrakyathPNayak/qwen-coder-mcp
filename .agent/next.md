# Loop 212 candidates

1. **Generalise the lights-on E2E pattern** — extract `_looks_like_engine_init_failure` and the launcher-poll-teardown sequence into a fixture other heavy E2E tests can reuse (TUI E2E will need exactly this).
2. **Audit OTHER vLLM init-time incompatibility pairs** — does `--enforce-eager` + `--enable-chunked-prefill` clash? `fp8 KV` + opt-125m? Document the matrix; add pure-argv pairing invariants where they apply.
3. **End-to-end TUI smoke test** — drive the App against `httpx.MockTransport`, walk slash commands and a chat turn (carried 5x).
4. `/checkpoints export N <path> --gzip`
5. `/sysinfo --json --probe` — active vLLM `/health` probe.

**Recommended:** (2) — same class of bug; preempt the next user-reported regression by enumerating the pairings vLLM enforces at init time. The lights-on E2E only catches them after they happen; pairing invariants catch them at PR review.
