# Loop 217 candidates

1. **Real-model engine-init E2E** (user just authorised this) — un-gate or add a SECOND gated test that uses the actual default model `Lorbus/Qwen3.6-27B-int4-AutoRound`. This is the only way to be sure the loop-216 fix actually boots end-to-end.
2. **Add `QWEN_SERVE_FORCE_OFFLOAD=1` escape hatch** — for false-positive hybrid-name matches.
3. **Probe vLLM /health from agent_loop pre-flight** — same probe as /sysinfo --probe, but in the autonomous loop's startup so it self-detects engine readiness instead of timing out chat.
4. Live vLLM smoke test of `<tool_call>` syntax (env-dependent, carried since 164).

**Recommended:** (1) — actually boot the model, hit it with a chat request, verify the loop-216 fix is real and not just structurally plausible.
