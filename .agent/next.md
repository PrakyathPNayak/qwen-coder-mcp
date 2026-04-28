# Loop 235 candidates

1. timing_analyze --since-last-pid: filter records to those
   matching the most-recent exit record's pid (current-process
   scope). Pairs with --since-last-exit (run scope) and gives
   operators a "what did THIS process do" lens.

2. /checkpoints export N <path> --gzip (carried, low priority).

3. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

4. Real-model E2E for the loop-217 think-strip in streaming
   mode (only non-streaming is gated currently).

5. /sysinfo --probe --json: already exists per loop-220
   investigation -- audit only, no work needed.

Recommended next: (1) - direct continuation of the loop-233/234
pid arc and natural pairing with --since-last-exit.
