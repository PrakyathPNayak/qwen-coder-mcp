# Loop 219 candidates

1. **`QWEN_SERVE_FORCE_OFFLOAD=1` escape hatch** for false-positive
   hybrid-name matches in `serve_qwen.sh`. Currently a misnamed
   dense model (e.g., contains "mamba") cannot get offloading even
   when the operator explicitly requests it.
2. **agent_loop pre-flight `/health` probe** — same probe as
   `/sysinfo --probe`, but in the autonomous loop's startup so it
   self-detects engine readiness instead of timing out the first
   chat call.
3. **`/checkpoints export N <path> --gzip`** — long-deferred.
4. **Audit `agent_loop` for any spot consuming `qwen_client` output
   without `_extract_text`** — ensure the strip applies everywhere
   chain-of-thought could leak into a parser.
5. **TUI prefix-buffering policy** for unwrapped `</think>` case in
   streaming (deferred from loop 218; needs a reasoned default for
   prefix size and a way for the TUI to surface "thinking..." in
   the buffer window without leaking content).

**Recommended:** (4) — the loop-217 strip is in `_extract_text` so
any callsite that bypasses it (calling `httpx` directly, or a
private branch in agent_loop) still leaks. Worth a 30-minute audit
pass with a grep + reasoning.
