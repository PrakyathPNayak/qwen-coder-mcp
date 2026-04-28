# Loop 210 candidates

1. **End-to-end TUI smoke test** (carried 3x) — drive App against `httpx.MockTransport`, walk slash commands and a chat turn, assert no exceptions.
2. **`/checkpoints export N <path> --gzip`** — compressed archive variant.
3. **`/sysinfo --json --probe`** — actively check vLLM `/health` endpoint.
4. **Audit `qwen_client.py` for httpx 0.28+ deprecations** — same drift class as the vLLM regression.
5. **`/tokens --json --top K --by-role`** — bucket top-K per role.

**Recommended:** (4) — quick audit, mirrors loop 205's drift-prevention pattern. The TUI E2E (1) is the bigger win but warrants a fresh session.
