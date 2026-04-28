# Loop 228 candidates

1. Real-model E2E: drive an actual chat completion (not just
   readiness) against the booted vLLM, gated by
   QWEN_SERVE_E2E_REAL_MODEL=1, asserting (a) no engine-dead, (b)
   parseable response, (c) optionally a tool_call round trip.
   This is the regression pin loop 227 needs but doesn't yet have
   -- the loop-217 readiness gate caught BOOT but not GENERATION.

2. /sysinfo --probe --json: machine-readable form of the loop-219
   /health probe so analytics can ingest it.

3. timing.log analytics: a small reader that groups by category
   over the last N records (uses the loop-226 'exit' record).

4. /checkpoints export N <path> --gzip (carried, low priority).

5. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

Recommended next: (1) - it's the missing pin that would have
prevented loop 227 from being a user-reported regression instead
of a CI-caught one.
